"""调用方持有事务并裁决状态；本层只读写受管记录及身份条件更新。"""

from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend.data.agent.agent_models import AgentControl, ManagedInstanceSession


class AgentRepository:
    _CONTROL_FIELDS = {
        "agent_session_id", "agent_pid", "agent_started_at", "heartbeat_at",
        "desired_state", "state", "command_revision", "worker_session_id",
        "child_pid", "model_ready", "active_task_id", "last_error", "updated_at",
        "shutdown_confirmed_at",
    }
    _REPORT_FIELDS = {
        "heartbeat_at", "state", "child_pid", "model_ready", "active_task_id",
        "last_error", "updated_at", "shutdown_confirmed_at",
    }
    _SESSION_FIELDS = {"child_pid", "state", "ended_at", "exit_confirmed_at", "last_error"}

    @staticmethod
    def get_control(session: Session, instance_id: str) -> AgentControl | None:
        return session.get(AgentControl, instance_id)

    @staticmethod
    def get_local_control(session: Session) -> AgentControl | None:
        # 本阶段只有一个本机 ASR 实例；意外多行不能静默选择任一控制者。
        return session.execute(select(AgentControl)).scalar_one_or_none()

    @staticmethod
    def add_control(session: Session, row: AgentControl) -> None:
        session.add(row)
        session.flush()

    @classmethod
    def update_control(cls, row: AgentControl, **values: Any) -> None:
        if values.keys() - cls._CONTROL_FIELDS:
            raise ValueError("不能修改受管实例身份或未定义字段。")
        for key, value in values.items():
            setattr(row, key, value)

    @classmethod
    def report_control(
        cls, session: Session, *, instance_id: str, agent_session_id: str,
        expected_worker_session_id: str | None, values: dict[str, Any],
        expected_revision: int | None = None,
    ) -> bool:
        """报告只更新观测字段；旧 Agent/Worker 会话不能改写当前会话。"""
        if values.keys() - cls._REPORT_FIELDS:
            raise ValueError("运行报告不能覆盖命令意图、修订号或会话身份。")
        conditions = [
            AgentControl.instance_id == instance_id,
            AgentControl.agent_session_id == agent_session_id,
            AgentControl.worker_session_id == expected_worker_session_id,
        ]
        if expected_revision is not None:
            conditions.append(AgentControl.command_revision == expected_revision)
        result = session.execute(update(AgentControl).where(*conditions).values(**values))
        return result.rowcount == 1

    @staticmethod
    def get_managed_session(session: Session, worker_session_id: str) -> ManagedInstanceSession | None:
        return session.get(ManagedInstanceSession, worker_session_id)

    @staticmethod
    def add_managed_session(session: Session, row: ManagedInstanceSession) -> None:
        session.add(row)
        session.flush()

    @classmethod
    def update_managed_session(cls, row: ManagedInstanceSession, **values: Any) -> None:
        if values.keys() - cls._SESSION_FIELDS:
            raise ValueError("不能修改受管启动会话的身份、所属实例或启动时间。")
        for key, value in values.items():
            setattr(row, key, value)

    @staticmethod
    def list_managed_sessions(session: Session, instance_id: str, *, page: int, page_size: int):
        query = select(ManagedInstanceSession).where(ManagedInstanceSession.instance_id == instance_id)
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = session.scalars(query.order_by(ManagedInstanceSession.started_at.desc(),
                                             ManagedInstanceSession.worker_session_id.desc())
                               .offset((page - 1) * page_size).limit(page_size)).all()
        return rows, total

    @staticmethod
    def latest_managed_session(session: Session, instance_id: str) -> ManagedInstanceSession | None:
        return session.scalar(select(ManagedInstanceSession)
                              .where(ManagedInstanceSession.instance_id == instance_id)
                              .order_by(ManagedInstanceSession.started_at.desc(),
                                        ManagedInstanceSession.worker_session_id.desc()).limit(1))
