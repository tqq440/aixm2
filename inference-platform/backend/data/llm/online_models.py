"""在线请求只保存生命周期元数据，不保存聊天正文或上游凭据。"""

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, JSON, String, false, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.database.base import Base


class OnlineRequestRecord(Base):
    __tablename__ = "online_requests"
    __table_args__ = (
        CheckConstraint("length(online_request_id) = 32", name="online_request_id_length"),
        CheckConstraint("state IN ('accepted', 'waiting_instance', 'streaming', 'completed', 'failed', 'cancelled')", name="state_values"),
        CheckConstraint("permit_state IN ('waiting', 'held', 'released', 'uncertain')", name="permit_state_values"),
        CheckConstraint("output_characters >= 0", name="output_characters_nonnegative"),
        Index("ix_online_requests_caller_accepted", "caller_id", "accepted_at", "online_request_id"),
        Index("ix_online_requests_instance_permit", "instance_id", "permit_state"),
        Index("ix_online_requests_state_lease", "state", "lease_expires_at"),
    )

    online_request_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    caller_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    instance_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="accepted", server_default="accepted")
    permit_state: Mapped[str] = mapped_column(String(16), nullable=False, default="waiting", server_default="waiting")
    accepted_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())
    deadline_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    first_token_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    output_characters: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    finish_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    cancellation_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
