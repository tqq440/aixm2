"""独立 Node 的登记、不可变修订、启动实例与事件。"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, MetaData, String, UniqueConstraint, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class HostNodeBase(DeclarativeBase):
    metadata = MetaData(naming_convention={"pk": "pk_%(table_name)s", "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s", "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"})

    def to_dict(self):
        return {column.key: getattr(self, column.key) for column in inspect(self).mapper.column_attrs}


class HostNodeIdentity(HostNodeBase):
    __tablename__ = "host_node_identities"
    identity_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    node_id: Mapped[str] = mapped_column(String(64), unique=True)
    epoch_id: Mapped[str | None] = mapped_column(String(36))
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime)


class HostNodeService(HostNodeBase):
    __tablename__ = "host_node_services"
    service_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    config_revision: Mapped[str] = mapped_column(String(64))
    config_json: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    current_instance_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    runtime_revision: Mapped[str | None] = mapped_column(String(64))
    control_version: Mapped[int] = mapped_column(Integer, default=0)
    control_json: Mapped[dict] = mapped_column(JSON, default=dict)


class HostNodeServiceRevision(HostNodeBase):
    __tablename__ = "host_node_service_revisions"
    __table_args__ = (UniqueConstraint("service_id", "revision"),)
    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("host_node_services.service_id"))
    revision: Mapped[str] = mapped_column(String(64))
    config_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class HostNodeRuntimeRevision(HostNodeBase):
    __tablename__ = "host_node_runtime_revisions"
    __table_args__ = (UniqueConstraint("service_id", "revision"),)
    runtime_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("host_node_services.service_id"))
    revision: Mapped[str] = mapped_column(String(64))
    config_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class HostNodeOperation(HostNodeBase):
    __tablename__ = "host_node_operations"
    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("host_node_services.service_id"), index=True)
    action: Mapped[str] = mapped_column(String(16))
    request_hash: Mapped[str] = mapped_column(String(64))
    runtime_revision: Mapped[str] = mapped_column(String(64))
    config_revision: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))
    phase: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class HostNodeResourceSnapshot(HostNodeBase):
    __tablename__ = "host_node_resource_snapshots"
    snapshot_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    node_epoch_id: Mapped[str] = mapped_column(String(36))
    payload: Mapped[dict] = mapped_column(JSON)
    sampled_at: Mapped[datetime] = mapped_column(DateTime)


class HostNodeInstance(HostNodeBase):
    __tablename__ = "host_node_instances"
    __table_args__ = (UniqueConstraint("service_id", "generation"),)
    instance_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("host_node_services.service_id"))
    config_revision: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    report_hash: Mapped[str | None] = mapped_column(String(64))
    reported_state: Mapped[str] = mapped_column(String(24), default="unknown")
    model_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    accepting_requests: Mapped[bool] = mapped_column(Boolean, default=False)
    health: Mapped[str] = mapped_column(String(16), default="unknown")
    queued_tasks: Mapped[int | None] = mapped_column(Integer)
    active_tasks: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    reported_at: Mapped[datetime | None] = mapped_column(DateTime)
    received_at: Mapped[datetime | None] = mapped_column(DateTime)
    node_epoch_id: Mapped[str | None] = mapped_column(String(36))
    control_report_json: Mapped[dict | None] = mapped_column(JSON)
    exit_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    may_have_accepted: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class HostNodeEvent(HostNodeBase):
    __tablename__ = "host_node_events"
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_id: Mapped[str | None] = mapped_column(ForeignKey("host_node_services.service_id"), index=True)
    instance_id: Mapped[str | None] = mapped_column(String(36))
    config_revision: Mapped[str | None] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(16))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)
