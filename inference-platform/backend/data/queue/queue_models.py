"""SQLite 是队列通知的可恢复来源；Redis 状态不改变执行许可。"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.database.base import Base


class InferenceQueueState(Base):
    __tablename__ = "inference_queue_states"
    __table_args__ = (CheckConstraint("availability IN ('unknown','available','unavailable')", name="availability_allowed"),)
    state_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    broker_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    availability: Mapped[str] = mapped_column(String(16), nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime())
    last_error_code: Mapped[str | None] = mapped_column(String(64))


class InferenceQueueOutbox(Base):
    __tablename__ = "inference_queue_outboxes"
    __table_args__ = (
        CheckConstraint("state IN ('pending','published','received','settled','online')", name="state_allowed"),
        CheckConstraint("publish_attempts >= 0", name="attempts_nonnegative"),
        Index("ix_inference_queue_outboxes_due", "state", "next_attempt_at"),
    )
    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    invocation_id: Mapped[str] = mapped_column(ForeignKey("inference_invocations.invocation_id"), unique=True, nullable=False)
    redis_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(64))
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    publisher_token: Mapped[str | None] = mapped_column(String(32))
    publisher_expires_at: Mapped[datetime | None] = mapped_column(DateTime())
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime())
    received_at: Mapped[datetime | None] = mapped_column(DateTime())
    settled_at: Mapped[datetime | None] = mapped_column(DateTime())
    last_error_code: Mapped[str | None] = mapped_column(String(64))
