"""中心查询名额账本；与推理容量独立，无 TTL 自动释放。"""

from datetime import datetime
from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from backend.data.database.base import Base


class MonitoringPermit(Base):
    __tablename__ = "monitoring_permits"
    __table_args__ = (Index("ix_monitoring_permits_state_service_key", "state", "service_key"),
                     Index("ix_monitoring_permits_state_endpoint_key", "state", "endpoint_key"))
    poll_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    command_hash: Mapped[str] = mapped_column(String(64))
    node_id: Mapped[str] = mapped_column(String(64))
    invocation_id: Mapped[str] = mapped_column(String(32))
    service_key: Mapped[str] = mapped_column(String(64))
    endpoint_key: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
