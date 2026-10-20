"""Agent 状态解释及管理器退出凭据检查，不通过 PID 猜测进程已停止。"""

from backend.common.exceptions.app_error import AppError
from backend.data.agent.agent_repository import AgentRepository
from backend.data.database.database import Database, database_now
from backend.data.nodes.node_repository import NodeRepository
from backend.schema.agent.agent_schema import ManagementStatus, ManagedSessionPage, ManagedSessionView
from backend.schema.models.model_schema import RegistryListQuery
from backend.schema.nodes.node_schema import InstancePath
from backend.utils.time.time_utils import as_utc


def fresh(row, now, settings):
    age = (as_utc(now) - as_utc(row.heartbeat_at)).total_seconds()
    return row.shutdown_confirmed_at is None and 0 <= age <= settings.runtime_stale_seconds


def session_confirmed(session, row):
    if row.worker_session_id is None:
        return True
    launch = AgentRepository.get_managed_session(session, row.worker_session_id)
    return (launch is not None and launch.instance_id == row.instance_id
            and launch.exit_confirmed_at is not None)


def agent_is_ready(settings, agent_pid, *, started_after=None):
    database = Database(settings)
    try:
        with database.transaction() as session:
            row = AgentRepository.get_local_control(session)
            return bool(row and row.agent_pid == agent_pid
                        and (started_after is None or as_utc(row.agent_started_at) >= as_utc(started_after))
                        and fresh(row, database_now(session), settings) and row.state != 'stop_unconfirmed')
    finally:
        database.dispose()


def agent_shutdown_confirmed(settings, agent_pid, *, started_after=None):
    database = Database(settings)
    try:
        with database.transaction() as session:
            row = AgentRepository.get_local_control(session)
            # Worker 启动前必须提交本轮控制行；没有控制行就不可能创建模型进程。
            if row is None:
                return True
            if row.shutdown_confirmed_at is None or not session_confirmed(session, row):
                return False
            if started_after is not None and as_utc(row.agent_started_at) < as_utc(started_after):
                return True  # 上一轮已确认，本轮尚未获得启动资格。
            return row.agent_pid == agent_pid
    finally:
        database.dispose()


class AgentStatusService:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings

    def _instance(self, session, instance_id):
        instance_id = InstancePath(instance_id=instance_id).instance_id
        instance = NodeRepository.get_instance(session, instance_id)
        if instance is None:
            raise AppError('INSTANCE_NOT_FOUND', '未找到该 ASR 实例。', 404)
        return instance

    def status(self, instance_id):
        with self.database.transaction() as session:
            instance = self._instance(session, instance_id)
            row = AgentRepository.get_control(session, instance_id)
            now = database_now(session)
            if row is None:
                return ManagementStatus(instance_id=instance_id, node_id=instance.node_id,
                                        state='unknown', evaluated_at=now)
            online = fresh(row, now, self.settings)
            launch = AgentRepository.get_managed_session(session, row.worker_session_id) if row.worker_session_id else None
            confirmed = session_confirmed(session, row)
            state = row.state
            if not online and not confirmed:
                state = 'stop_unconfirmed' if row.desired_state == 'stopped' else 'unknown'
            elif not online and state not in {'stopped', 'failed'}:
                state = 'unknown'
            ready = online and row.model_ready and state in {'ready', 'busy', 'stopping'}
            return ManagementStatus(
                instance_id=instance_id, node_id=instance.node_id, state=state, reported_state=row.state,
                desired_state=row.desired_state, agent_available=online, agent_session_id=row.agent_session_id,
                agent_pid=row.agent_pid, worker_session_id=row.worker_session_id, child_pid=row.child_pid,
                command_revision=row.command_revision, model_ready=ready,
                accepting_tasks=ready and state in {'ready', 'busy'} and row.desired_state == 'running', active_task_id=row.active_task_id,
                last_error=row.last_error, heartbeat_at=row.heartbeat_at,
                exit_confirmed_at=launch.exit_confirmed_at if launch else None,
                shutdown_confirmed_at=row.shutdown_confirmed_at, evaluated_at=now,
            )

    def sessions(self, instance_id, query):
        query = RegistryListQuery.model_validate(query)
        with self.database.transaction() as session:
            self._instance(session, instance_id)
            rows, total = AgentRepository.list_managed_sessions(session, instance_id, **query.model_dump())
            return ManagedSessionPage(items=[ManagedSessionView.model_validate(row.to_dict()) for row in rows],
                                      total=total, **query.model_dump())
