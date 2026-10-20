"""M05 每次真实执行的持久化历史；同租约失败提交可安全重放。"""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.database.base import Base


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt", name="uq_execution_attempts_task_attempt"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
    )

    lease_token: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dispatch_version: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    failure_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
