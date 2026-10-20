"""ASR-2B 受管实例记录；旧 Worker 观测历史保持独立。"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.database.base import Base


class AgentControl(Base):
    __tablename__ = "agent_controls"
    __table_args__ = (
        CheckConstraint("length(agent_session_id) = 32", name="agent_session_id_length"),
        CheckConstraint("agent_pid > 0", name="agent_pid_positive"),
        CheckConstraint("desired_state IN ('running', 'stopped')", name="desired_state_allowed"),
        CheckConstraint("state IN ('stopped', 'starting', 'ready', 'busy', 'stopping', 'stop_unconfirmed', 'failed')", name="state_allowed"),
        CheckConstraint("command_revision >= 0", name="command_revision_nonnegative"),
        CheckConstraint("worker_session_id IS NULL OR length(worker_session_id) = 32", name="worker_session_id_length"),
        CheckConstraint("child_pid IS NULL OR child_pid > 0", name="child_pid_positive"),
    )

    instance_id: Mapped[str] = mapped_column(String(32), ForeignKey("service_instances.instance_id"), primary_key=True)
    agent_session_id: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_pid: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_started_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    desired_state: Mapped[str] = mapped_column(String(16), nullable=False, default="stopped", server_default="stopped")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="stopped", server_default="stopped")
    command_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    worker_session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    child_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    active_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(256), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    shutdown_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)


class ManagedInstanceSession(Base):
    __tablename__ = "managed_instance_sessions"
    __table_args__ = (
        CheckConstraint("length(worker_session_id) = 32", name="worker_session_id_length"),
        CheckConstraint("length(agent_session_id) = 32", name="agent_session_id_length"),
        CheckConstraint("agent_pid > 0", name="agent_pid_positive"),
        CheckConstraint("child_pid IS NULL OR child_pid > 0", name="child_pid_positive"),
        CheckConstraint("state IN ('stopped', 'starting', 'ready', 'busy', 'stopping', 'stop_unconfirmed', 'failed')", name="state_allowed"),
        Index("ix_managed_instance_sessions_started", "instance_id", "started_at", "worker_session_id"),
    )

    worker_session_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(32), ForeignKey("service_instances.instance_id"), nullable=False)
    agent_session_id: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_pid: Mapped[int] = mapped_column(Integer, nullable=False)
    child_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    exit_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(256), nullable=True)
