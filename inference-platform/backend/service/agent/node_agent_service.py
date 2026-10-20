"""M11 固定本机实例生命周期；复用 M12 Worker，不改变任务状态机。"""

import os
from uuid import uuid4

from filelock import FileLock, Timeout

from backend.common.exceptions.app_error import AppError
from backend.data.agent.agent_models import AgentControl, ManagedInstanceSession
from backend.data.agent.agent_repository import AgentRepository
from backend.data.database.database import database_now
from backend.service.agent.agent_status_service import session_confirmed
from backend.service.nodes.node_registry_service import NodeRegistryService
from backend.service.worker.worker_service import WorkerService


class NodeAgentService:
    def __init__(self, database, settings, logger, *, worker_factory=WorkerService, registry=None):
        self.database, self.settings, self.logger = database, settings, logger
        self.worker_factory = worker_factory
        self.registry = registry or NodeRegistryService(database, settings)
        self.session_id = uuid4().hex
        self.instance_id = None
        self.worker = None
        self.shutting_down = False
        self.shutdown_confirmed = False
        self._failure = None
        self._pending_closed = None
        self._closing = False
        self._lock = FileLock(str(settings.database_path) + '.agent.lock', timeout=0)
        self._owns_lock = False

    def start(self):
        try:
            self._lock.acquire()
            self._owns_lock = True
        except Timeout as exc:
            raise AppError('AGENT_ALREADY_RUNNING', '此数据库已有节点 Agent 运行。', 409) from exc
        try:
            with FileLock(str(self.settings.database_path) + '.worker.lock', timeout=0):
                registration = self.registry.register_local()
                self.instance_id = registration.instance.instance_id
                with self.database.transaction(immediate=True) as session:
                    now = database_now(session)
                    row = AgentRepository.get_control(session, self.instance_id)
                    unknown = row is not None and not session_confirmed(session, row)
                    values = dict(agent_session_id=self.session_id, agent_pid=os.getpid(), agent_started_at=now,
                        heartbeat_at=now, updated_at=now, desired_state='stopped',
                        state='stop_unconfirmed' if unknown else 'stopped', model_ready=False, active_task_id=None,
                        shutdown_confirmed_at=None, last_error='PREVIOUS_SESSION_UNCONFIRMED' if unknown else None)
                    if row is None:
                        AgentRepository.add_control(session, AgentControl(instance_id=self.instance_id,
                            command_revision=0, **values))
                    else:
                        AgentRepository.update_control(row, command_revision=row.command_revision + 1, **values)
        except BaseException:
            self._lock.release()
            self._owns_lock = False
            raise
        self.logger.info('Agent 已启动 session_id=%s；ASR 默认等待显式启动', self.session_id)

    def _owned(self, session):
        row = AgentRepository.get_control(session, self.instance_id)
        if row is None or row.agent_session_id != self.session_id:
            raise AppError('AGENT_OWNERSHIP_LOST', 'Agent 管理会话已变化，不能覆盖其他会话。', 409)
        return row

    def request_shutdown(self):
        self.shutting_down = True
        if self.worker:
            self.worker.request_drain()

    def _intent(self):
        with self.database.transaction(immediate=True) as session:
            row = self._owned(session)
            now = database_now(session)
            AgentRepository.update_control(row, heartbeat_at=now, updated_at=now)
            if (self.shutting_down or (self.worker and self.worker.draining)) and row.desired_state != 'stopped':
                AgentRepository.update_control(row, desired_state='stopped', command_revision=row.command_revision + 1,
                    state='stopping' if self.worker else row.state)
            return row.desired_state, row.state

    def _launch(self):
        worker_id = uuid4().hex
        # 先记实际启动尝试，再交给 Worker 的同事务 spawn guard；停止可在两步间获胜。
        with self.database.transaction(immediate=True) as session:
            row = self._owned(session)
            if row.desired_state != 'running' or not session_confirmed(session, row):
                return
            now = database_now(session)
            AgentRepository.add_managed_session(session, ManagedInstanceSession(
                worker_session_id=worker_id, instance_id=self.instance_id, agent_session_id=self.session_id,
                agent_pid=os.getpid(), state='starting', started_at=now))
            AgentRepository.update_control(row, worker_session_id=worker_id, child_pid=None, active_task_id=None,
                state='starting', model_ready=False, last_error=None, updated_at=now)
        self._failure = None
        self._closing = False
        try:
            self.worker = self.worker_factory(self.database, self.settings, self.logger,
                agent_session_id=self.session_id, worker_session_id=worker_id)
            self.worker.start()
        except Exception:
            self.logger.exception('ASR 启动失败，正在确认本次启动尝试退出')
            self._failure = 'ASR_START_FAILED'
            if self.worker:
                self.worker.request_drain()
            else:
                # 构造阶段未建立 Worker，更不可能调用 start/spawn。
                self._pending_closed = (worker_id, True)
                self._save_pending_exit()

    def _save_pending_exit(self):
        if self._pending_closed is None:
            return
        worker_id, failed = self._pending_closed
        self._finish(worker_id, failed=failed)
        self._pending_closed = None

    def _finish(self, worker_id, *, failed=False):
        with self.database.transaction(immediate=True) as session:
            row = self._owned(session)
            if row.worker_session_id != worker_id:
                raise AppError('AGENT_WORKER_SESSION_CHANGED', '启动会话已变化，退出报告被拒绝。', 409)
            now = database_now(session)
            launch = AgentRepository.get_managed_session(session, worker_id)
            state = 'failed' if failed else 'stopped'
            # 提交响应丢失时允许重放，已落盘的退出时间不得被后续重试改写。
            if launch.exit_confirmed_at is None:
                AgentRepository.update_managed_session(launch, state=state, ended_at=now,
                    exit_confirmed_at=now, last_error=self._failure)
                AgentRepository.update_control(row, state=state, desired_state='stopped', model_ready=False,
                    active_task_id=None, updated_at=now, heartbeat_at=now, last_error=self._failure)
        self.worker = None
        self._closing = False

    def _report_worker(self, *, unconfirmed=False):
        worker = self.worker
        with self.database.transaction(immediate=True) as session:
            row = self._owned(session)
            if row.worker_session_id != worker.session_id:
                raise AppError('AGENT_WORKER_SESSION_CHANGED', 'Worker 报告不属于当前启动会话。', 409)
            now = database_now(session)
            stopping = row.desired_state == 'stopped' or worker.draining
            state = ('stop_unconfirmed' if unconfirmed else 'stopping' if stopping else
                     'busy' if worker.lease or worker.pending else 'ready' if worker.model_ready else 'starting')
            values = dict(state=state, child_pid=worker.runner.pid or row.child_pid,
                model_ready=worker.model_ready, active_task_id=worker.lease.task_id if worker.lease else None,
                last_error='ASR_STOP_UNCONFIRMED' if unconfirmed else self._failure or worker.last_error)
            AgentRepository.update_control(row, **values, heartbeat_at=now, updated_at=now)
            launch = AgentRepository.get_managed_session(session, worker.session_id)
            AgentRepository.update_managed_session(launch, state=state, child_pid=values['child_pid'],
                last_error=values['last_error'])

    def tick(self):
        persistence_error = None
        try:
            desired, state = self._intent()
        except Exception as exc:
            # 控制库故障不能阻止 Worker 执行其本地保守租约期限。
            persistence_error = exc
            desired, state = 'stopped', 'stop_unconfirmed'
            if self.worker:
                self.worker.request_drain()
        if self._pending_closed is not None:
            try:
                self._save_pending_exit()
            except Exception as exc:
                persistence_error = persistence_error or exc
        if (self.worker is None and self._pending_closed is None and persistence_error is None
                and desired == 'running' and state != 'stop_unconfirmed'):
            self._launch()
        if self.worker is not None and self._pending_closed is None:
            worker = self.worker
            if desired == 'stopped' or self.shutting_down or self._failure:
                worker.request_drain()
            if not self._failure and not self._closing:
                worker.tick()
            if not self._closing and (worker.can_exit or worker.state == 'error'):
                self._failure = self._failure or worker.last_error or 'ASR_WORKER_FAILED'
                worker.request_drain()
            try:
                self._report_worker()
            except Exception as exc:
                persistence_error = persistence_error or exc
                worker.request_drain()
            if worker.drained or self._failure:
                self._closing = True
                if worker.close():
                    # 保留受管句柄已经确认退出的事实；失败时不得再次 tick 已关闭 Worker。
                    self._pending_closed = (worker.session_id, bool(self._failure))
                    try:
                        self._save_pending_exit()
                    except Exception as exc:
                        persistence_error = persistence_error or exc
                else:
                    try:
                        self._report_worker(unconfirmed=True)
                    except Exception as exc:
                        persistence_error = persistence_error or exc
        if self.shutting_down and self.worker is None and self._pending_closed is None:
            with self.database.transaction(immediate=True) as session:
                row = self._owned(session)
                if not session_confirmed(session, row):
                    if persistence_error is not None:
                        raise persistence_error
                    return
                now = database_now(session)
                AgentRepository.update_control(row, desired_state='stopped', shutdown_confirmed_at=now,
                    heartbeat_at=now, updated_at=now)
            self.shutdown_confirmed = True
            self._lock.release()
            self._owns_lock = False
        if persistence_error is not None:
            raise persistence_error
