"""外部调用目标、固定会话和持久许可；不保存第二套任务状态。"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.database.base import Base


class AsrCapacityGroup(Base):
    __tablename__ = "asr_capacity_groups"
    __table_args__ = (
        CheckConstraint("length(group_id) BETWEEN 1 AND 64", name="group_id_length"),
        CheckConstraint("capacity_limit > 0", name="capacity_limit_positive"),
    )

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    capacity_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    independence_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())


class AsrComputeTarget(Base):
    __tablename__ = "asr_compute_targets"
    __table_args__ = (
        CheckConstraint("length(target_id) BETWEEN 1 AND 64", name="target_id_length"),
        CheckConstraint("target_limit > 0", name="target_limit_positive"),
        Index("ix_asr_compute_targets_group", "capacity_group_id", "target_id"),
    )

    target_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    capacity_group_id: Mapped[str] = mapped_column(String(64), ForeignKey("asr_capacity_groups.group_id"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    target_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())


class AsrTargetRevision(Base):
    __tablename__ = "asr_target_revisions"
    __table_args__ = (
        CheckConstraint("length(revision_id) BETWEEN 1 AND 64", name="revision_id_length"),
        CheckConstraint("length(config_fingerprint) = 64", name="config_fingerprint_length"),
        CheckConstraint("length(endpoint_identity) = 64", name="endpoint_identity_length"),
        CheckConstraint("device_kind IN ('unknown', 'cpu', 'gpu')", name="device_kind_allowed"),
        UniqueConstraint("target_id", "config_fingerprint"),
        Index("ix_asr_target_revisions_endpoint", "endpoint_identity", "target_id"),
    )

    revision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(64), ForeignKey("asr_compute_targets.target_id"), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    config_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    device_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    device_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())


class AsrTargetWorkerSession(Base):
    __tablename__ = "asr_target_worker_sessions"
    __table_args__ = (
        CheckConstraint("length(session_id) = 32", name="session_id_length"),
        CheckConstraint("length(worker_key) BETWEEN 1 AND 128", name="worker_key_length"),
        CheckConstraint("slot_index >= 0", name="slot_index_nonnegative"),
        CheckConstraint("active_slot_key IS NULL OR length(active_slot_key) BETWEEN 1 AND 128", name="active_slot_key_length"),
        CheckConstraint("pid > 0", name="pid_positive"),
        CheckConstraint("length(state) BETWEEN 1 AND 32", name="state_length"),
        UniqueConstraint("active_slot_key"),
        Index("ix_asr_target_worker_sessions_revision", "revision_id", "started_at", "session_id"),
    )

    session_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(64), ForeignKey("asr_target_revisions.revision_id"), nullable=False)
    worker_key: Mapped[str] = mapped_column(String(128), nullable=False)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    active_slot_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    model_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())
    started_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    active_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    child_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(256), nullable=True)


class AsrCallPermit(Base):
    __tablename__ = "asr_call_permits"
    __table_args__ = (
        CheckConstraint("length(task_id) = 32", name="task_id_length"),
        CheckConstraint("length(lease_token) = 32", name="lease_token_length"),
        CheckConstraint("state IN ('held', 'uncertain', 'released')", name="state_allowed"),
        Index("ix_asr_call_permits_group_state", "capacity_group_id", "state"),
        Index("ix_asr_call_permits_revision_state", "revision_id", "state"),
        Index("ix_asr_call_permits_worker_state", "worker_session_id", "state"),
    )

    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("tasks.task_id"), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(64), ForeignKey("asr_target_revisions.revision_id"), nullable=False)
    capacity_group_id: Mapped[str] = mapped_column(String(64), ForeignKey("asr_capacity_groups.group_id"), nullable=False)
    worker_session_id: Mapped[str] = mapped_column(String(32), ForeignKey("asr_target_worker_sessions.session_id"), nullable=False)
    lease_token: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)


class AsrCapacityChange(Base):
    """本机维护改变限额的不可覆盖审计；不代表硬件容量观测。"""

    __tablename__ = "asr_capacity_changes"
    __table_args__ = (
        CheckConstraint("length(change_id) = 32", name="change_id_length"),
        CheckConstraint("previous_target_limit BETWEEN 1 AND 8", name="previous_target_limit_range"),
        CheckConstraint("target_limit BETWEEN 1 AND 8", name="target_limit_range"),
        CheckConstraint("previous_group_limit BETWEEN 1 AND 8", name="previous_group_limit_range"),
        CheckConstraint("group_limit BETWEEN 1 AND 8", name="group_limit_range"),
        CheckConstraint("length(trim(evidence)) BETWEEN 1 AND 2048", name="evidence_length"),
        Index("ix_asr_capacity_changes_target_time", "target_id", "changed_at"),
    )

    change_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(64), ForeignKey("asr_compute_targets.target_id"), nullable=False)
    capacity_group_id: Mapped[str] = mapped_column(String(64), ForeignKey("asr_capacity_groups.group_id"), nullable=False)
    previous_target_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    target_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_group_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    group_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, server_default=func.current_timestamp())
