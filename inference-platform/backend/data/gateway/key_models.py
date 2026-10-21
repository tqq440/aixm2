"""凭据仅保存随机标识和 SHA256，不保存可用密钥。"""

from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, String, Index, JSON
from sqlalchemy.orm import Mapped, mapped_column
from backend.data.database.base import Base


class GatewayKey(Base):
    __tablename__ = "gateway_api_keys"
    __table_args__ = (Index("ix_gateway_api_keys_application", "application_id", "created_at"),)
    key_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("inference_application_budgets.application_id"))
    label: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GatewayNodeObservation(Base):
    __tablename__="gateway_node_observations"
    node_key: Mapped[str]=mapped_column(String(64),primary_key=True)
    snapshot: Mapped[dict]=mapped_column(JSON)
    checked_at: Mapped[datetime]=mapped_column(DateTime)
