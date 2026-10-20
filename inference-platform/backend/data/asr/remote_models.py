"""外部调用事实；容量由目标许可约束，不替代离线任务生命周期。"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.database.base import Base


class RemoteAsrCall(Base):
    __tablename__ = "remote_asr_calls"
    __table_args__ = (
        CheckConstraint("state IN ('queued', 'held', 'released', 'uncertain')", name="state_allowed"),
        Index("ix_remote_asr_calls_profile_state", "profile_id", "state"),
    )

    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("tasks.task_id"), primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(64))
    lease_token: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime())
    resolution_note: Mapped[str | None] = mapped_column(String(512))

