"""M04 模型身份、不可覆盖版本和独立准入状态。"""

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.data.database.base import Base


class ModelRecord(Base):
    __tablename__ = "models"

    model_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 不形成循环外键；Service 只允许指向本模型已存在且可准入的版本。
    default_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime(), server_default=func.current_timestamp())


class ModelVersionRecord(Base):
    __tablename__ = "model_versions"

    model_id: Mapped[str] = mapped_column(String(64), ForeignKey("models.model_id"), primary_key=True)
    model_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(), server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime(), server_default=func.current_timestamp())
