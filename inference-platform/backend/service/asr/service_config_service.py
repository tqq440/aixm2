"""手工接口配置、健康测试及准确模型登记；样本测试复用正常任务链路。"""

from contextlib import contextmanager
from time import perf_counter

from filelock import Timeout
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from backend.common.exceptions.app_error import AppError
from backend.config.asr.service_config import (
    current_service_config, read_service_document, service_revision, settings_for_service,
)
from backend.data.asr.service_config_repository import existing_service_spec, incompatible_asr_work, service_model_specs
from backend.data.asr.service_config_store import ServiceConfigStore
from backend.schema.asr.service_config_schema import ServiceConfigDocument
from backend.schema.models.model_schema import ModelRegistration
from backend.service.asr.remote_catalog import REMOTE_ADAPTER_KEY, config_fingerprint, remote_asr_configured, remote_asr_model_spec
from backend.service.asr.remote_v2_adapter import probe_remote_asr
from backend.service.models.model_registry_service import ModelRegistryService
from backend.utils.json.json_utils import json_values_equal


class AsrServiceConfiguration:
    def __init__(self, database, settings, environment_settings=None):
        self.database, self.settings = database, settings
        self.environment_settings = environment_settings or settings
        self.store = ServiceConfigStore(settings.database_path)

    @contextmanager
    def _storage(self, write=False):
        try:
            if write:
                with self.store.locked():
                    yield
            else:
                yield
        except Timeout:
            raise AppError("ASR_CONFIG_BUSY", "配置正在被修改，请刷新后重试。", 409) from None
        except (OSError, ValueError, ValidationError):
            raise AppError("ASR_CONFIG_STORAGE_INVALID", "ASR 配置文件无法读取或校验失败；未回退或覆盖原配置。", 503) from None

    def _configured(self, config):
        try:
            return settings_for_service(self.settings, config)
        except (ValueError, OSError):
            raise AppError("ASR_CONFIG_INVALID", "请核对服务地址、接口路径、协议、模型及证书配置。", 422) from None

    def _state(self, document):
        selected = document.revisions.get(document.selected_revision)
        spec = remote_asr_model_spec(self.settings) if remote_asr_configured(self.settings) else None
        legacy = current_service_config(self.environment_settings)
        return {
            "items": [dict(document.revisions[revision].model_dump(mode="json", by_alias=True), revision=revision)
                      for revision in document.items.values()],
            "selectedServiceId": selected.service_id if selected else None,
            "selectedRevision": document.selected_revision,
            "runningServiceId": self.settings.asr_service_id,
            "runningRevision": self.settings.asr_service_revision,
            "restartRequired": document.selected_revision != self.settings.asr_service_revision,
            "runningModel": {"modelId": spec.model_id, "modelVersion": spec.model_version} if spec else None,
            "legacyConfiguration": legacy.model_dump(mode="json", by_alias=True) if legacy else None,
        }

    def state(self):
        with self._storage():
            return self._state(read_service_document(self.settings))

    def save(self, service_id, command):
        if service_id != command.config.service_id:
            raise AppError("ASR_CONFIG_ID_MISMATCH", "服务编号与保存地址不一致。", 422)
        self._configured(command.config)
        revision = service_revision(command.config)
        with self._storage(write=True):
            document = read_service_document(self.settings)
            current = document.items.get(service_id)
            if current == revision:
                return self._state(document)  # 同一次保存超时后的幂等确认。
            if current != command.expected_revision:
                raise AppError("ASR_CONFIG_CHANGED", "配置已被其他页面修改，请刷新后再保存。", 409)
            document.items[service_id] = revision
            document.revisions[revision] = command.config
            self._write(document)
            return self._state(document)

    def delete(self, service_id, command):
        with self._storage(write=True):
            document = read_service_document(self.settings)
            current = document.items.get(service_id)
            if current is None:
                raise AppError("ASR_CONFIG_NOT_FOUND", "未找到此服务配置。", 404)
            if current != command.expected_revision:
                raise AppError("ASR_CONFIG_CHANGED", "配置已变化，请刷新后再删除。", 409)
            selected = document.revisions.get(document.selected_revision)
            if service_id in {self.settings.asr_service_id, selected.service_id if selected else None}:
                raise AppError("ASR_CONFIG_IN_USE", "当前运行或已选用的服务不能删除；请先切换并重启。", 409)
            del document.items[service_id]
            # 历史修订保留，正在启动的进程仍可读取其固定快照。
            self._write(document)
            return self._state(document)

    def select(self, command):
        with self._storage(write=True):
            document = read_service_document(self.settings)
            if command.service_id is None:
                if command.expected_revision != document.selected_revision:
                    raise AppError("ASR_CONFIG_CHANGED", "已选配置发生变化，请刷新。", 409)
                candidate, revision = self.environment_settings, None
            else:
                revision = document.items.get(command.service_id)
                if revision is None:
                    raise AppError("ASR_CONFIG_NOT_FOUND", "请先保存服务配置。", 404)
                if revision != command.expected_revision:
                    raise AppError("ASR_CONFIG_CHANGED", "配置已变化，请刷新后重新选用。", 409)
                candidate = self._configured(document.revisions[revision])
            fingerprint = config_fingerprint(candidate) if remote_asr_configured(candidate) else None
            try:
                # 与新任务受理串行；写入选择后原API停止受理旧接口的新任务，直到重启。
                with self.database.transaction(immediate=True) as session:
                    self._assert_pending(session, fingerprint)
                    if remote_asr_configured(candidate):
                        spec = remote_asr_model_spec(candidate)
                        for previous in service_model_specs(session, spec.model_id):
                            previous = ModelRegistration.model_validate(previous)
                            if previous.task_type != "asr" or previous.adapter_key != REMOTE_ADAPTER_KEY:
                                raise AppError("ASR_MODEL_ID_RESERVED",
                                    "此平台模型标识已用于现有模型，请为接口服务填写新的平台模型标识。接口 model 参数可保持原名。", 409)
                        existing = existing_service_spec(session, spec.model_id, spec.model_version)
                        if existing is not None and not json_values_equal(
                            ModelRegistration.model_validate(existing).model_dump(), spec.model_dump()
                        ):
                            raise AppError("MODEL_VERSION_IMMUTABLE",
                                "该模型编号和版本已用于其他规范，请换一个平台模型编号或填写新版本。", 409)
                    document.selected_revision = revision
                    self._write(document)
            except SQLAlchemyError:
                raise AppError("ASR_CONFIG_DATABASE_UNAVAILABLE", "无法核对现有 ASR 任务，请先检查数据库迁移。", 503) from None
            return self._state(document)

    def _write(self, document):
        validated = ServiceConfigDocument.model_validate(document.model_dump())
        self.store.write(validated.model_dump(mode="json", by_alias=True))

    def _check_pending(self, candidate):
        fingerprint = config_fingerprint(candidate) if remote_asr_configured(candidate) else None
        try:
            with self.database.transaction(immediate=True) as session:
                self._assert_pending(session, fingerprint)
        except SQLAlchemyError:
            raise AppError("ASR_CONFIG_DATABASE_UNAVAILABLE", "无法核对现有 ASR 任务，请先检查数据库迁移。", 503) from None

    @staticmethod
    def _assert_pending(session, fingerprint):
        if incompatible_asr_work(session, fingerprint):
            raise AppError("ASR_CONFIG_PENDING_TASKS",
                "仍有其他接口配置的未完成任务或结果未知调用；请先按原配置收尾，再切换服务。", 409)

    def test(self, config):
        configured = self._configured(config)
        started = perf_counter()
        health = probe_remote_asr(configured)
        return {
            "success": health.ready, "phase": "health",
            "message": ("健康检查通过；尚未验证音频转写，请选用并重启后上传短音频测试。"
                        if health.ready else health.error_message or "健康检查未通过。"),
            "durationMs": round((perf_counter() - started) * 1000, 1),
            "configRevision": service_revision(config),
            "errorCode": health.error_code, "apiVersion": health.api_version,
        }

    def register_running(self):
        """用户已选用的快照在启动时登记；不能把新接口套给旧排队请求。"""
        if not self.settings.asr_service_id:
            # 从手工接口恢复.env后，恢复其准确默认版本；不能遗留新接口的默认模型。
            with self._storage():
                if not remote_asr_configured(self.settings) or not read_service_document(self.settings).revisions:
                    return
        self._check_pending(self.settings)
        registry = ModelRegistryService(self.database, self.settings)
        spec = remote_asr_model_spec(self.settings)
        registry.register(spec)
        registry.enable(spec.model_id, spec.model_version)
        registry.set_default(spec.model_id, spec.model_version)
