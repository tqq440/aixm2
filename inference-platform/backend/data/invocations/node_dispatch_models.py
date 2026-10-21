"""中心 Node 派发绑定；迟到结果单独保存，不改写原超时历史。"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.database.base import Base


class InferenceNodeDispatch(Base):
    __tablename__ = "inference_node_dispatches"
    invocation_id: Mapped[str] = mapped_column(ForeignKey("inference_invocations.invocation_id"), primary_key=True)
    command_json: Mapped[dict] = mapped_column(JSON)
    request_hash: Mapped[str] = mapped_column(String(64))
    terminal_hash: Mapped[str | None] = mapped_column(String(64))
    remote_state: Mapped[str | None] = mapped_column(String(16))
    late_result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    monitoring_json: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
