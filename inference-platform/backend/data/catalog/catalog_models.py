"""独立的目录资料；本阶段不绑定旧任务、许可或执行目标。"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.database.base import Base


class InferenceService(Base):
    __tablename__ = "inference_services"
    __table_args__ = (CheckConstraint("version >= 1", name="positive_version"),)
    service_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    draft_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    active_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class InferenceServiceRevision(Base):
    __tablename__ = "inference_service_revisions"
    __table_args__ = (Index("ix_inference_service_revisions_service_created", "service_id", "created_at"),)
    revision: Mapped[str] = mapped_column(String(64), primary_key=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("inference_services.service_id"), nullable=False)
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class InferenceResourceGroup(Base):
    __tablename__ = "inference_resource_groups"
    __table_args__ = (CheckConstraint("version >= 1", name="positive_version"),)
    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class InferenceTargetCheck(Base):
    __tablename__ = "inference_target_checks"
    __table_args__ = (UniqueConstraint("revision", "target_id"),)
    check_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision: Mapped[str] = mapped_column(ForeignKey("inference_service_revisions.revision"), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class InferenceCatalogEvent(Base):
    __tablename__ = "inference_catalog_events"
    __table_args__ = (Index("ix_inference_catalog_events_service_time", "service_id", "created_at"),)
    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    service_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
