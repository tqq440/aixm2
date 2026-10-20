"""由本机 Worker 和投递器报告的运行快照。"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.database.base import Base


class RuntimeComponent(Base):
    __tablename__ = "runtime_components"
    __table_args__ = (
        CheckConstraint("component_id IN ('worker', 'vision_worker', 'dispatcher', 'remote_asr_worker')", name="component_id_allowed"),
        CheckConstraint("pid > 0", name="pid_positive"),
    )

    component_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(32), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    child_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    model_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    active_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(256), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
