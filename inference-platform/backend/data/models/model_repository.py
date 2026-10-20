"""目录查询及写入只操作调用方给定会话，规则与事务边界由 Service 决定。"""

from datetime import datetime

from sqlalchemy import and_, func, select, tuple_
from sqlalchemy.orm import Session

from backend.data.models.model_models import ModelRecord, ModelVersionRecord


class ModelRepository:
    def get_model(self, session: Session, model_id: str) -> ModelRecord | None:
        return session.get(ModelRecord, model_id)

    def get_version(self, session: Session, model_id: str, version: str) -> ModelVersionRecord | None:
        return session.get(ModelVersionRecord, (model_id, version))

    def get_versions(self, session: Session, identities: list[tuple[str, str]]):
        return session.scalars(select(ModelVersionRecord).where(
            tuple_(ModelVersionRecord.model_id, ModelVersionRecord.model_version).in_(identities)
        )).all()

    def add_model(self, session: Session, model: ModelRecord) -> None:
        session.add(model)
        session.flush()

    def add_version(self, session: Session, version: ModelVersionRecord) -> None:
        session.add(version)
        session.flush()

    def set_enabled(self, row: ModelVersionRecord, enabled: bool, now: datetime) -> None:
        row.enabled = enabled
        row.updated_at = now

    def set_default(self, model: ModelRecord, version: str, now: datetime) -> None:
        model.default_version = version
        model.updated_at = now

    def list_versions(self, session: Session, *, page: int, page_size: int):
        total = session.scalar(select(func.count()).select_from(ModelVersionRecord)) or 0
        rows = session.execute(
            select(ModelVersionRecord, ModelRecord.default_version)
            .join(ModelRecord, ModelRecord.model_id == ModelVersionRecord.model_id)
            .order_by(ModelVersionRecord.model_id, ModelVersionRecord.model_version)
            .offset((page - 1) * page_size).limit(page_size)
        ).all()
        return rows, total

    def list_enabled_defaults(self, session: Session, *, page: int, page_size: int):
        return session.scalars(
            select(ModelVersionRecord).join(ModelRecord, and_(
                ModelRecord.model_id == ModelVersionRecord.model_id,
                ModelRecord.default_version == ModelVersionRecord.model_version,
            )).where(ModelVersionRecord.enabled.is_(True))
            .order_by(ModelVersionRecord.model_id)
            .offset((page - 1) * page_size).limit(page_size)
        ).all()
