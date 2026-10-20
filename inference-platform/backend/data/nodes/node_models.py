"""M09 本机登记与会话观测；登记记录不代表模型已经运行。"""

from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
    Index, Integer, String, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.database.base import Base


class ComputeNode(Base):
    __tablename__ = "compute_nodes"
    __table_args__ = (
        CheckConstraint("length(node_id) = 32", name="node_id_length"),
        CheckConstraint("length(local_identity) = 64", name="local_identity_length"),
        CheckConstraint("cpu_count IS NULL OR cpu_count > 0", name="cpu_count_positive"),
        UniqueConstraint("local_identity"),
        Index("ix_compute_nodes_registered", "registered_at", "node_id"),
    )

    node_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    local_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    os_name: Mapped[str] = mapped_column(String(64), nullable=False)
    os_version: Mapped[str] = mapped_column(String(255), nullable=False)
    architecture: Mapped[str] = mapped_column(String(64), nullable=False)
    cpu_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(), server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime(), server_default=func.current_timestamp())


class ServiceInstance(Base):
    __tablename__ = "service_instances"
    __table_args__ = (
        CheckConstraint("length(instance_id) = 32", name="instance_id_length"),
        CheckConstraint("instance_key = 'asr-default'", name="instance_key_allowed"),
        CheckConstraint("execution_device = 'cpu'", name="execution_device_allowed"),
        ForeignKeyConstraint(
            ["model_id", "model_version"],
            ["model_versions.model_id", "model_versions.model_version"],
        ),
        UniqueConstraint("node_id", "instance_key"),
        Index("ix_service_instances_registered", "registered_at", "instance_id"),
    )

    instance_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(32), ForeignKey("compute_nodes.node_id"), nullable=False)
    instance_key: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter_key: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_device: Mapped[str] = mapped_column(String(16), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(), server_default=func.current_timestamp())


class InstanceSession(Base):
    __tablename__ = "instance_sessions"
    __table_args__ = (
        CheckConstraint("length(session_id) = 32", name="session_id_length"),
        CheckConstraint("length(worker_session_id) = 32", name="worker_session_id_length"),
        CheckConstraint("source = 'legacy_worker_observation'", name="source_allowed"),
        CheckConstraint("worker_pid > 0", name="worker_pid_positive"),
        CheckConstraint("child_pid IS NULL OR child_pid > 0", name="child_pid_positive"),
        UniqueConstraint("instance_id", "worker_session_id"),
        Index("ix_instance_sessions_instance_started", "instance_id", "worker_started_at", "session_id"),
    )

    session_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(32), ForeignKey("service_instances.instance_id"), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    worker_session_id: Mapped[str] = mapped_column(String(32), nullable=False)
    worker_pid: Mapped[int] = mapped_column(Integer, nullable=False)
    child_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reported_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reported_model_ready: Mapped[bool] = mapped_column(Boolean, nullable=False)
    active_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    worker_started_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(256), nullable=True)
