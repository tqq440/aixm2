"""模型版本不可覆盖；可用性只约束新任务，不改变已受理任务。"""

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.common.exceptions.app_error import AppError
from backend.config.vision.model_catalog import REGISTRY_SPEC
from backend.config.llm.model_catalog import configured_spec
from backend.config.settings.app_settings import AppSettings
from backend.data.database.database import Database, database_now
from backend.data.models.model_models import ModelRecord, ModelVersionRecord
from backend.data.models.model_repository import ModelRepository
from backend.schema.models.model_schema import (
    ModelIdentity, ModelRegistration, RegistrationReceipt, RegistryListQuery,
    RegistryPage, RegistryVersionView,
)
from backend.utils.json.json_utils import json_values_equal
from backend.service.asr.adapter_catalog import asr_execution_supported


class ModelRegistryService:
    def __init__(self, database: Database, settings: AppSettings):
        self.database = database
        self.settings = settings
        self.repository = ModelRepository()

    @contextmanager
    def _transaction(self, *, write: bool = False):
        try:
            with self.database.transaction(immediate=write) as session:
                yield session
        except SQLAlchemyError as exc:
            raise AppError("MODEL_REGISTRY_UNAVAILABLE", "模型目录暂不可用，请检查数据库及迁移。", 503) from exc

    def execution_supported(self, spec: ModelRegistration) -> bool:
        # ASR 支持由代码插件目录裁决；注册任意规格不会创造新的执行能力。
        if spec.task_type == "asr":
            return asr_execution_supported(spec, self.settings)
        online = configured_spec(self.settings)
        return (json_values_equal(spec.model_dump(), ModelRegistration.model_validate(REGISTRY_SPEC).model_dump())
                or (online is not None and json_values_equal(spec.model_dump(), online.model_dump())))

    def _spec(self, row: ModelVersionRecord) -> ModelRegistration:
        return ModelRegistration.model_validate(row.spec_json)

    def _view(self, row: ModelVersionRecord, default_version: str | None) -> RegistryVersionView:
        spec = self._spec(row)
        return RegistryVersionView(spec=spec, enabled=row.enabled, is_default=row.model_version == default_version,
                                   execution_supported=self.execution_supported(spec))

    def _get(self, session: Session, model_id: str, version: str | None):
        model = self.repository.get_model(session, model_id)
        if model is None:
            raise AppError("MODEL_NOT_FOUND", "未找到该模型。", 404)
        selected = version if version is not None else model.default_version
        if selected is None:
            raise AppError("MODEL_DEFAULT_UNSET", "该模型尚未设置默认版本。", 422)
        row = self.repository.get_version(session, model_id, selected)
        if row is None:
            raise AppError("MODEL_VERSION_NOT_FOUND", "未找到该模型版本。", 404)
        return model, row

    def _validate_available(self, row: ModelVersionRecord) -> ModelRegistration:
        spec = self._spec(row)
        if not row.enabled:
            raise AppError("MODEL_DISABLED", "该模型版本已禁用，不能受理新任务。", 422)
        if not self.execution_supported(spec):
            raise AppError("MODEL_EXECUTION_UNSUPPORTED", "当前 Worker 尚不支持该模型版本的执行能力。", 422)
        return spec

    def resolve(self, model_id: str, model_version: str | None = None, *, require_enabled: bool = True) -> ModelRegistration:
        with self._transaction() as session:
            _, row = self._get(session, model_id, model_version)
            return self._validate_available(row) if require_enabled else self._spec(row)

    def validate_admission(self, session: Session, model_id: str, version: str) -> ModelRegistration:
        """调用方须在新任务创建的 BEGIN IMMEDIATE 事务内调用，不重新解析默认版本。"""
        _, row = self._get(session, model_id, version)
        return self._validate_available(row)

    def list_available(self) -> list[ModelRegistration]:
        with self._transaction() as session:
            # 仓储分批读取，避免无界单次查询和 N+1。
            result, page = [], 1
            while True:
                rows = self.repository.list_enabled_defaults(session, page=page, page_size=100)
                result.extend(spec for row in rows if self.execution_supported(spec := self._spec(row)))
                if len(rows) < 100:
                    return result
                page += 1

    def list_versions(self, *, page: int = 1, page_size: int = 20) -> RegistryPage:
        query = RegistryListQuery(page=page, page_size=page_size)
        with self._transaction() as session:
            rows, total = self.repository.list_versions(session, page=query.page, page_size=query.page_size)
            return RegistryPage(items=[self._view(row, default) for row, default in rows], total=total,
                                page=query.page, page_size=query.page_size)

    def version_infos(self, identities: list[tuple[str, str]]) -> dict:
        """从不可变规范投影历史版本来源；不依赖当前配置、默认版本或启用状态。"""
        unique = list(dict.fromkeys(identities))
        if not unique:
            return {}
        result = {}
        with self._transaction() as session:
            for offset in range(0, len(unique), 100):
                for row in self.repository.get_versions(session, unique[offset:offset + 100]):
                    result[(row.model_id, row.model_version)] = self._spec(row).version_info
        return result

    def register(self, spec: ModelRegistration) -> RegistrationReceipt:
        spec = ModelRegistration.model_validate(spec)
        with self._transaction(write=True) as session:
            model = self.repository.get_model(session, spec.model_id)
            if model is None:
                now = database_now(session)
                model = ModelRecord(model_id=spec.model_id, created_at=now, updated_at=now)
                self.repository.add_model(session, model)
            row = self.repository.get_version(session, spec.model_id, spec.model_version)
            if row is not None:
                if not json_values_equal(self._spec(row).model_dump(), spec.model_dump()):
                    raise AppError("MODEL_VERSION_IMMUTABLE", "同一模型版本的规范不可覆盖，请使用新版本号。", 409)
                return RegistrationReceipt(item=self._view(row, model.default_version), created=False)
            now = database_now(session)
            row = ModelVersionRecord(model_id=spec.model_id, model_version=spec.model_version,
                                     spec_json=spec.model_dump(mode="json", by_alias=True), enabled=False,
                                     created_at=now, updated_at=now)
            self.repository.add_version(session, row)
            return RegistrationReceipt(item=self._view(row, model.default_version), created=True)

    def register_file(self, path: Path) -> RegistrationReceipt:
        try:
            with path.open("rb") as stream:
                content = stream.read(1024 * 1024 + 1)
        except (OSError, UnicodeError) as exc:
            raise AppError("MODEL_SPEC_FILE_INVALID", "无法读取 UTF-8 模型规范文件，请检查路径与权限。", 422) from exc
        if len(content) > 1024 * 1024:
            raise AppError("MODEL_SPEC_FILE_TOO_LARGE", "模型规范文件不得超过 1 MiB。", 422)
        try:
            decoded = content.decode("utf-8-sig")
        except UnicodeError as exc:
            raise AppError("MODEL_SPEC_FILE_INVALID", "模型规范文件必须使用 UTF-8 编码。", 422) from exc
        return self.register(ModelRegistration.model_validate_json(decoded))

    def enable(self, model_id: str, model_version: str) -> RegistryVersionView:
        return self._set_enabled(model_id, model_version, True)

    def disable(self, model_id: str, model_version: str) -> RegistryVersionView:
        return self._set_enabled(model_id, model_version, False)

    def _set_enabled(self, model_id: str, model_version: str, enabled: bool) -> RegistryVersionView:
        identity = ModelIdentity(model_id=model_id, model_version=model_version)
        with self._transaction(write=True) as session:
            model, row = self._get(session, identity.model_id, identity.model_version)
            if enabled and not self.execution_supported(self._spec(row)):
                raise AppError("MODEL_EXECUTION_UNSUPPORTED", "当前 Worker 尚不支持该模型版本，登记不会下载或加载模型。", 422)
            self.repository.set_enabled(row, enabled, database_now(session))
            # 禁用默认版本保留指针，列表为空；重新启用时恢复原默认，不偷偷挑选其他版本。
            return self._view(row, model.default_version)

    def set_default(self, model_id: str, model_version: str) -> RegistryVersionView:
        identity = ModelIdentity(model_id=model_id, model_version=model_version)
        with self._transaction(write=True) as session:
            model, row = self._get(session, identity.model_id, identity.model_version)
            self._validate_available(row)
            self.repository.set_default(model, row.model_version, database_now(session))
            return self._view(row, model.default_version)
