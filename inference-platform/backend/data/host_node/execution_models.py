"""Node 执行恢复档案，不是待执行队列。结果仅在受保护的结果接口返回。"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.host_node.node_models import HostNodeBase


class HostNodeExecution(HostNodeBase):
    __tablename__ = "host_node_executions"
    __table_args__ = (CheckConstraint("state IN ('sending','succeeded','failed','cancelled','unknown')", name="execution_state"),
                      Index("ix_host_node_executions_service_state", "service_id", "state"))
    invocation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("host_node_services.service_id"))
    instance_id: Mapped[str] = mapped_column(ForeignKey("host_node_instances.instance_id"))
    request_hash: Mapped[str] = mapped_column(String(64))
    command_json: Mapped[dict] = mapped_column(JSON)
    node_epoch_id: Mapped[str] = mapped_column(String(36))
    state: Mapped[str] = mapped_column(String(16))
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_hash: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    monitoring_json: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")


def has_unresolved_executions(session, service_id):
    from sqlalchemy import select
    return session.scalar(select(HostNodeExecution.invocation_id).where(
        HostNodeExecution.service_id == service_id, HostNodeExecution.state.in_(("sending", "unknown"))).limit(1)) is not None
