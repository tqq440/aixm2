"""ASR 不可变路由快照与任务选择记录；不替代原任务或容量许可。"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.database.base import Base


class AsrRoutingRevision(Base):
    __tablename__ = "asr_routing_revisions"
    __table_args__ = (
        CheckConstraint("length(routing_revision_id) BETWEEN 1 AND 64", name="routing_revision_id_length"),
        CheckConstraint("length(model_id) BETWEEN 1 AND 64", name="model_id_length"),
        CheckConstraint("length(model_version) BETWEEN 1 AND 64", name="model_version_length"),
        CheckConstraint("evidence_kind IN ('provided', 'isolated')", name="evidence_kind_allowed"),
        Index("ix_asr_routing_revisions_model_created", "model_id", "model_version", "created_at"),
    )

    routing_revision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON(none_as_null=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())


class AsrTaskRoute(Base):
    __tablename__ = "asr_task_routes"
    __table_args__ = (
        CheckConstraint("length(task_id) = 32", name="task_id_length"),
        CheckConstraint(
            "(selected_catalog_revision_id IS NULL AND selected_at IS NULL) OR "
            "(selected_catalog_revision_id IS NOT NULL AND selected_at IS NOT NULL AND decision_snapshot IS NOT NULL)",
            name="selection_fields_consistent",
        ),
        Index("ix_asr_task_routes_revision_task", "routing_revision_id", "task_id"),
    )

    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("tasks.task_id"), primary_key=True)
    routing_revision_id: Mapped[str] = mapped_column(String(64), ForeignKey("asr_routing_revisions.routing_revision_id"), nullable=False)
    selected_catalog_revision_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("asr_catalog_revisions.catalog_revision_id"), nullable=True)
    decision_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())
    selected_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
