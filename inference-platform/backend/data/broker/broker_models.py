"""M07 持久化通知；不保存任务的权威执行状态。"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.database.base import Base


class NotificationRecord(Base):
    __tablename__ = "notification_records"
    __table_args__ = (
        CheckConstraint("length(notification_id) = 32", name="notification_id_length"),
        CheckConstraint("status IN ('ready', 'inflight', 'acked')", name="valid_status"),
        CheckConstraint("deliveries >= 0", name="deliveries_nonnegative"),
        CheckConstraint("dispatch_version >= 1", name="dispatch_version_positive"),
        CheckConstraint("execution_plan_version >= 1", name="execution_plan_version_positive"),
        Index("ix_notification_records_queue_visible", "queue_key", "status", "visible_at"),
    )

    notification_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("dispatch_events.event_id"), nullable=False, unique=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id"), nullable=False)
    queue_key: Mapped[str] = mapped_column(String(128), nullable=False)
    dispatch_version: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready", server_default="ready")
    delivery_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    visible_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    deliveries: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
