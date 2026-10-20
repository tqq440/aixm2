"""ASR 目录修订与追加式资源观测；不修改执行目标或并发许可。"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.database.base import Base


class AsrCatalogRevision(Base):
    __tablename__ = "asr_catalog_revisions"
    __table_args__ = (
        CheckConstraint("length(catalog_revision_id) BETWEEN 1 AND 64", name="catalog_revision_id_length"),
        UniqueConstraint("catalog_revision_id"),
        Index("ix_asr_catalog_revisions_target_sequence", "target_id", "sequence"),
        {"sqlite_autoincrement": True},
    )

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    catalog_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), ForeignKey("asr_compute_targets.target_id"), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(64), ForeignKey("asr_target_revisions.revision_id"), nullable=False)
    metadata_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())


class AsrResourceObservation(Base):
    __tablename__ = "asr_resource_observations"
    __table_args__ = (
        CheckConstraint("length(observation_id) BETWEEN 1 AND 64", name="observation_id_length"),
        CheckConstraint("scope IN ('target', 'capacity_group')", name="scope_allowed"),
        CheckConstraint("length(trim(source)) BETWEEN 1 AND 64", name="source_length"),
        CheckConstraint("evidence_kind IN ('provided', 'isolated')", name="evidence_kind_allowed"),
        CheckConstraint("ttl_seconds BETWEEN 1 AND 3600", name="ttl_seconds_range"),
        UniqueConstraint("observation_id"),
        Index("ix_asr_resource_observations_catalog_scope_time", "catalog_revision_id", "scope", "observed_at", "sequence"),
        {"sqlite_autoincrement": True},
    )

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    observation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_revision_id: Mapped[str] = mapped_column(String(64), ForeignKey("asr_catalog_revisions.catalog_revision_id"), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())
