"""仅保存实际进程报告；是否失联由读取方结合数据库时间判断。"""

from datetime import datetime

from sqlalchemy import select

from backend.data.database.database import Database, database_now
from backend.data.runtime.runtime_models import RuntimeComponent
from backend.data.agent.agent_repository import AgentRepository
from backend.data.nodes.node_repository import NodeRepository
from backend.data.asr.target_models import AsrComputeTarget, AsrCapacityGroup


class RuntimeRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def report(
        self,
        *,
        component_id: str,
        session_id: str,
        pid: int,
        state: str,
        model_ready: bool = False,
        active_task_id: str | None = None,
        child_pid: int | None = None,
        last_error: str | None = None,
    ) -> None:
        with self.database.transaction(immediate=True) as session:
            now = database_now(session)
            record = session.get(RuntimeComponent, component_id)
            if record is None:
                record = RuntimeComponent(component_id=component_id, started_at=now)
                session.add(record)
            elif record.session_id != session_id:
                record.started_at = now
            if (component_id == "remote_asr_worker" and record.session_id == session_id
                    and record.state == "stopping" and state != "stopped"):
                state = "stopping"
            record.session_id = session_id
            record.pid = pid
            record.child_pid = child_pid
            record.state = state
            record.model_ready = model_ready
            record.active_task_id = active_task_id
            record.heartbeat_at = now
            record.last_error = last_error[:256] if last_error else None

    def list_components(self) -> list[RuntimeComponent]:
        # 主键只允许 ASR Worker、Vision Worker、投递器三项，结果有固定上界。
        with self.database.session_factory() as session:
            return list(session.scalars(select(RuntimeComponent).order_by(RuntimeComponent.component_id)).all())

    def now(self) -> datetime:
        with self.database.session_factory() as session:
            return database_now(session)

    def external_call_limit(self) -> int:
        """配置槽数而非实际就绪数；未登记时仍沿用初始单槽配置。"""
        with self.database.session_factory() as session:
            target = session.get(AsrComputeTarget, "remote-default")
            if target is None:
                return 1
            group = session.get(AsrCapacityGroup, target.capacity_group_id)
            return min(8, target.target_limit, group.capacity_limit) if target.enabled else 0

    def asr_management(self):
        """固定单实例的管理记录；没有控制记录时另区分已登记和旧隔离库。"""
        with self.database.session_factory() as session:
            control = AgentRepository.get_local_control(session)
            if control is not None:
                launch = (AgentRepository.get_managed_session(session, control.worker_session_id)
                          if control.worker_session_id else None)
                return control, launch, True
            _, count = NodeRepository.list_instances(session, page=1, page_size=1)
            return None, None, count > 0
