"""M05 任务档案、幂等记录和待投递事件；事件不等于 M07 通知。"""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.database.base import Base


class TaskRecord(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("length(task_id) = 32", name="task_id_length"),
        CheckConstraint("length(input_ref) = 32", name="input_ref_length"),
        CheckConstraint("length(input_sha256) = 64", name="input_sha256_length"),
        CheckConstraint("input_size_bytes > 0", name="input_size_positive"),
        CheckConstraint("audio_duration_seconds > 0", name="audio_duration_positive"),
        CheckConstraint("task_type IN ('asr', 'vision')", name="task_type_allowed"),
        CheckConstraint(
            "(task_type = 'asr' AND input_media_type = 'audio' "
            "AND audio_duration_seconds IS NOT NULL AND image_width IS NULL AND image_height IS NULL) "
            "OR (task_type = 'vision' AND input_media_type = 'image' "
            "AND audio_duration_seconds IS NULL AND image_width IS NOT NULL AND image_width > 0 "
            "AND image_height IS NOT NULL AND image_height > 0)",
            name="input_metadata_matches_task_type",
        ),
        CheckConstraint("attempt >= 0", name="attempt_nonnegative"),
        CheckConstraint("execution_plan_version >= 1", name="execution_plan_version_positive"),
        CheckConstraint("dispatch_version >= 1", name="dispatch_version_positive"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        Index("ix_tasks_caller_created", "caller_id", "created_at", "task_id"),
        Index("ix_tasks_status_lease", "status", "lease_expires_at"),
        Index("ix_tasks_status_deadlines", "status", "queue_deadline_at", "total_deadline_at"),
    )

    task_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    caller_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False, default="asr", server_default="asr")
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_ref: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    input_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    input_media_type: Mapped[str] = mapped_column(String(16), nullable=False, default="audio", server_default="audio")
    audio_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    image_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created", server_default="created")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")
    # 创建时快照；迁移回填旧档案，不随重新投递或服务重启刷新。
    queue_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    total_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    attempt_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    execution_plan_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    dispatch_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    queue_key: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # SQLite 没有原生时区列：所有写入者保存 UTC，输出 Schema 补上 UTC 标识。
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("caller_id", "idempotency_key", name="uq_idempotency_records_caller_key"),
        CheckConstraint("length(id) = 32", name="id_length"),
        CheckConstraint("length(request_fingerprint) = 64", name="request_fingerprint_length"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    caller_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id"), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())


class DispatchEvent(Base):
    __tablename__ = "dispatch_events"
    __table_args__ = (
        UniqueConstraint("task_id", "dispatch_version", name="uq_dispatch_events_task_dispatch_version"),
        CheckConstraint("length(event_id) = 32", name="event_id_length"),
        CheckConstraint("execution_plan_version >= 1", name="execution_plan_version_positive"),
        CheckConstraint("dispatch_version >= 1", name="dispatch_version_positive"),
        Index("ix_dispatch_events_status_available", "status", "available_at"),
    )

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, default="task.created", server_default="task.created")
    execution_plan_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    dispatch_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    queue_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    available_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    notification_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(256), nullable=True)
