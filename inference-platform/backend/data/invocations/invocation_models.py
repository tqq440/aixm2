"""调用档案引用原任务/在线请求；结果仍保存在原业务记录中。"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.database.base import Base


class InferenceApplicationBudget(Base):
    __tablename__ = "inference_application_budgets"
    __table_args__ = (CheckConstraint("concurrency_limit BETWEEN 1 AND 64", name="limit_range"),
                      CheckConstraint("version >= 1", name="positive_version"))
    application_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    concurrency_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    allowed_services: Mapped[list] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class InferenceInvocation(Base):
    __tablename__ = "inference_invocations"
    __table_args__ = (
        CheckConstraint("(task_id IS NOT NULL AND online_request_id IS NULL) OR (task_id IS NULL AND online_request_id IS NOT NULL)", name="one_owner"),
        CheckConstraint("state IN ('waiting','reserved','sent','finished')", name="state_allowed"),
        CheckConstraint("permit_state IN ('waiting','held','uncertain','released')", name="permit_allowed"),
        CheckConstraint("kind IN ('asr','vision','llm')", name="kind_allowed"),
        CheckConstraint("attempt >= 0", name="attempt_nonnegative"),
        UniqueConstraint("task_id"), UniqueConstraint("online_request_id"),
        Index("ix_inference_invocations_waiting", "state", "queue_key", "accepted_at"),
        Index("ix_inference_invocations_expiry", "state", "lease_expires_at", "deadline_at"),
    )
    invocation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.task_id"), nullable=True)
    online_request_id: Mapped[str | None] = mapped_column(ForeignKey("online_requests.online_request_id"), nullable=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("inference_application_budgets.application_id"), nullable=False)
    service_id: Mapped[str] = mapped_column(ForeignKey("inference_services.service_id"), nullable=False)
    service_revision: Mapped[str] = mapped_column(ForeignKey("inference_service_revisions.revision"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    queue_key: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    permit_state: Mapped[str] = mapped_column(String(16), nullable=False)
    selected_target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    selected_target_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    queue_deadline_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)


class InferenceReservationItem(Base):
    __tablename__ = "inference_reservation_items"
    __table_args__ = (Index("ix_inference_reservation_items_resource", "resource_key"),
                      CheckConstraint("limit_snapshot > 0", name="positive_limit"))
    invocation_id: Mapped[str] = mapped_column(ForeignKey("inference_invocations.invocation_id"), primary_key=True)
    resource_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    limit_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)


class InferenceTaskEvent(Base):
    __tablename__ = "inference_task_events"
    __table_args__ = (
        CheckConstraint("(task_id IS NOT NULL AND online_request_id IS NULL) OR (task_id IS NULL AND online_request_id IS NOT NULL)", name="one_owner"),
        Index("ix_inference_task_events_task_sequence", "task_id", "sequence"),
        Index("ix_inference_task_events_online_sequence", "online_request_id", "sequence"),
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.task_id"), nullable=True)
    online_request_id: Mapped[str | None] = mapped_column(ForeignKey("online_requests.online_request_id"), nullable=True)
    invocation_id: Mapped[str | None] = mapped_column(ForeignKey("inference_invocations.invocation_id"), nullable=True)
    application_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class InferenceSchedulerCursor(Base):
    __tablename__ = "inference_scheduler_cursors"
    cursor_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_queue_key: Mapped[str | None] = mapped_column(String(128), nullable=True)


class InferenceCpuProfile(Base):
    __tablename__ = "inference_cpu_profiles"
    service_revision: Mapped[str] = mapped_column(ForeignKey("inference_service_revisions.revision"), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    profile: Mapped[dict] = mapped_column(JSON, nullable=False)
    cpu_budget_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
