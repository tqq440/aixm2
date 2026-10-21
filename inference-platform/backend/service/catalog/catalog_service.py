"""阶段一目录管理：不可变修订、显式导入、能力缺口；不修改执行链路。"""

from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from pydantic import ValidationError

from backend.common.exceptions.app_error import AppError
from backend.config.asr.service_config import read_service_document
from backend.data.catalog.catalog_models import InferenceResourceGroup, InferenceService, InferenceServiceRevision
from backend.data.catalog.catalog_repository import CatalogRepository
from backend.data.database.database import database_now
from backend.schema.catalog.service_schema import ServiceDefinition, ServiceTarget
from backend.service.catalog.connection_probe import probe_target
from backend.utils.json.json_utils import canonical_json_sha256


class ServiceCatalog:
    def __init__(self, database, settings, *, probe=None):
        self.database, self.settings = database, settings
        self.repository = CatalogRepository()
        self.probe = probe or probe_target

    @contextmanager
    def _transaction(self, write=False):
        try:
            with self.database.transaction(immediate=write) as session:
                yield session
        except SQLAlchemyError:
            raise AppError("CATALOG_STORAGE_UNAVAILABLE", "服务目录尚未迁移或暂不可用；请先备份并完成 0015 迁移。", 503) from None

    @staticmethod
    def _match(row, expected):
        if (row.version if row else None) != expected:
            raise AppError("CATALOG_VERSION_CONFLICT", "目录已被其他操作修改，请刷新后重试。", 409)

    def _require(self, session, service_id):
        row = self.repository.get(session, service_id)
        if row is None:
            raise AppError("CATALOG_NOT_FOUND", "未找到此服务。", 404)
        return row

    @staticmethod
    def _definition(revision):
        if revision is None:
            raise AppError("CATALOG_REVISION_INVALID", "目录修订缺失，请核对备份。", 503)
        try:
            config = ServiceDefinition.model_validate(revision.config)
        except ValidationError:
            raise AppError("CATALOG_REVISION_INVALID", "目录修订内容无法校验，请核对备份。", 503) from None
        if canonical_json_sha256(revision.config) != revision.revision:
            raise AppError("CATALOG_REVISION_INVALID", "目录修订校验失败，请核对备份；未自动覆盖配置。", 503)
        return config

    def _issues(self,config):
        connected=self.settings.common_execution_enabled
        issues = [{"code": "EXECUTION_RUNTIME_VERIFICATION_REQUIRED" if connected else "EXECUTION_NOT_CONNECTED", "targetId": None,
                   "message": "公共执行已配置；实际可接单仍须核对Node门禁、服务协议和容量，目录不保证实时就绪。" if connected else "公共执行当前关闭；目录不会切换原日常任务。", "blocksActivation": False}]
        for target in config.targets:
            if target.node:
                issues.append({"code": "NODE_RUNTIME_VERIFICATION_REQUIRED", "targetId": target.target_id,
                    "message": "已提供 Node HTTP/文件/SSE 和监控；须核对固定 Node 修订及实际接入能力。目录声明不代表真实服务已验证。",
                    "blocksActivation": False})
            if target.monitoring.protocol == "none":
                issues.append({"code": "STATUS_QUERY_UNSUPPORTED", "targetId": target.target_id,
                               "message": "服务未提供任务状态查询；同步最终响应仍可用于确认完成。", "blocksActivation": False})
            if not target.monitoring.reports_progress:
                issues.append({"code": "PROGRESS_UNSUPPORTED", "targetId": target.target_id,
                               "message": "服务未提供进度；不推算百分比。", "blocksActivation": False})
            if not target.monitoring.reports_device:
                issues.append({"code": "ACTUAL_DEVICE_UNSUPPORTED", "targetId": target.target_id,
                               "message": "实际执行设备未提供，配置中的设备仅为目标资料。", "blocksActivation": False})
            if config.invocation_mode == "async" and target.enabled and (
                    target.monitoring.protocol == "none" or not (target.monitoring.result_path or target.monitoring.result_in_status)):
                issues.append({"code": "COMPLETION_UNAVAILABLE", "targetId": target.target_id,
                               "message": "异步受理缺少完成状态或结果获取接口，接入尚未闭环。", "blocksActivation": True})
        enabled = [target for target in config.targets if target.enabled]
        if not enabled:
            issues.append({"code": "NO_ENABLED_TARGET", "targetId": None, "message": "至少需要一个启用目标。", "blocksActivation": True})
        if config.policy == "fixed" and len(enabled) > 1:
            issues.append({"code": "FIXED_TARGET_AMBIGUOUS", "targetId": None, "message": "固定策略只允许一个启用目标。", "blocksActivation": True})
        if config.policy == "gpu_preferred":
            for target in enabled:
                if (target.device_kind == "unknown" or not target.device_evidence or not target.compatibility_evidence
                        or not target.resource_group_ids):
                    issues.append({"code": "DEVICE_POLICY_UNVERIFIED", "targetId": target.target_id,
                                   "message": "自动策略需要设备、兼容依据和共享容量组；资料不足时仅可保存草稿。", "blocksActivation": True})
        return issues

    def _view(self, row, revisions, checks):
        draft = self._definition(revisions[row.draft_revision])
        active = self._definition(revisions[row.active_revision]) if row.active_revision else None
        return {"serviceId": row.service_id, "version": row.version, "draftRevision": row.draft_revision,
                "activeRevision": row.active_revision, "enabled": row.enabled,
                "config": draft.model_dump(mode="json", by_alias=True),
                "activeConfig": active.model_dump(mode="json", by_alias=True) if active else None,
                "source": revisions[row.draft_revision].source, "issues": self._issues(draft),
                "executionReady": False, "createdAt": row.created_at, "updatedAt": row.updated_at,
                "checks": [{"revision": check.revision, "targetId": check.target_id, "success": check.success,
                            "errorCode": check.error_code, "durationMs": check.duration_ms, "checkedAt": check.checked_at}
                           for check in checks if check.revision in {row.draft_revision, row.active_revision}]}

    def list(self, query):
        with self._transaction() as session:
            rows, revisions, checks, total = self.repository.list_services(session, query.page, query.page_size)
            return {"items": [self._view(row, revisions, checks) for row in rows], "total": total,
                    "page": query.page, "pageSize": query.page_size, "module": "catalog-only", "executionConnected": False}

    def get(self, service_id):
        with self._transaction() as session:
            row = self._require(session, service_id)
            revisions = {key: self.repository.revision(session, key) for key in {row.draft_revision, row.active_revision} if key}
            checks = [check for key in revisions for check in self.repository.checks(session, key)]
            return self._view(row, revisions, checks)

    def _save(self, session, config, expected, source="manual"):
        row = self.repository.get(session, config.service_id)
        self._match(row, expected)
        missing = {group for target in config.targets for group in target.resource_group_ids} - self.repository.group_ids(session)
        if missing:
            raise AppError("CATALOG_GROUP_NOT_FOUND", "引用的共享资源组尚未登记。", 422)
        now = database_now(session)
        snapshot = config.model_dump(mode="json", by_alias=True)
        revision = canonical_json_sha256(snapshot)
        if row is None:
            row = InferenceService(service_id=config.service_id, draft_revision=revision, active_revision=None,
                                   enabled=False, version=1, created_at=now, updated_at=now)
            session.add(row)
            session.flush()
        elif row.draft_revision == revision:
            return row
        else:
            row.draft_revision, row.version, row.updated_at = revision, row.version + 1, now
        if self.repository.revision(session, revision) is None:
            session.add(InferenceServiceRevision(revision=revision, service_id=config.service_id,
                                                 config=snapshot, source=source, created_at=now))
        self.repository.event(session, config.service_id, "configuration_saved", revision, now)
        return row

    def save(self, service_id, command):
        if command.config.service_id != service_id:
            raise AppError("CATALOG_ID_MISMATCH", "服务编号与保存路径不一致。", 422)
        with self._transaction(True) as session:
            self._save(session, command.config, command.expected_version)
        return self.get(service_id)

    def activate(self, service_id, command):
        with self._transaction(True) as session:
            row = self._require(session, service_id)
            self._match(row, command.expected_version)
            if command.revision != row.draft_revision:
                raise AppError("CATALOG_REVISION_CONFLICT", "需要选择当前已保存修订。", 409)
            config = self._definition(self.repository.revision(session, command.revision))
            now = database_now(session)
            if command.enabled:
                if any(issue["blocksActivation"] for issue in self._issues(config)):
                    raise AppError("CATALOG_INTEGRATION_INCOMPLETE", "请处理目录中的阻断问题后再启用。", 409)
                checks = {check.target_id: check for check in self.repository.checks(session, command.revision)}
                for target in config.targets:
                    if target.enabled and (target.target_id not in checks or not checks[target.target_id].success
                                           or checks[target.target_id].checked_at < now - timedelta(minutes=5)):
                        raise AppError("CATALOG_HEALTH_REQUIRED", "启用前请对当前修订的全部启用目标执行连接测试（五分钟内）。", 409)
                groups = list(session.scalars(select(InferenceResourceGroup).where(InferenceResourceGroup.group_id.in_(
                    {group for target in config.targets if target.enabled for group in target.resource_group_ids}))))
                if (any(target.concurrency_limit > 1 and not target.capacity_evidence for target in config.targets if target.enabled)
                        or any(group.config["concurrencyLimit"] > 1 and not group.config.get("capacityEvidence") for group in groups)):
                    raise AppError("CATALOG_CAPACITY_EVIDENCE_REQUIRED", "扩大目录额度需填写服务方获准的容量依据；未核实额度可先保存草稿。", 409)
                row.active_revision = command.revision
            row.enabled, row.version, row.updated_at = command.enabled, row.version + 1, now
            self.repository.event(session, service_id, "catalog_enabled" if command.enabled else "catalog_disabled", command.revision, now)
        return self.get(service_id)

    def check(self, service_id, target_id, command):
        with self._transaction() as session:
            row = self._require(session, service_id)
            self._match(row, command.expected_version)
            if command.revision not in {row.draft_revision, row.active_revision}:
                raise AppError("CATALOG_REVISION_CONFLICT", "配置已变化，请刷新。", 409)
            config = self._definition(self.repository.revision(session, command.revision))
            target = next((target for target in config.targets if target.target_id == target_id), None)
            if target is None:
                raise AppError("CATALOG_TARGET_NOT_FOUND", "未找到此调用目标。", 404)
        result = self.probe(config, target)  # 网络调用在数据库事务外。
        with self._transaction(True) as session:
            row = self._require(session, service_id)
            self._match(row, command.expected_version)
            now = database_now(session)
            self.repository.upsert_check(session, command.revision, target_id, result, now)
            self.repository.event(session, service_id, "health_checked", command.revision, now)
        return dict(result, targetId=target_id, revision=command.revision)

    def groups(self, query):
        with self._transaction() as session:
            rows, total = self.repository.list_groups(session, query.page, query.page_size)
            return {"items": [dict(row.config, version=row.version, updatedAt=row.updated_at) for row in rows],
                    "total": total, "page": query.page, "pageSize": query.page_size}

    def save_group(self, group_id, command):
        if group_id != command.config.group_id:
            raise AppError("CATALOG_ID_MISMATCH", "资源组编号与路径不一致。", 422)
        with self._transaction(True) as session:
            row = session.get(InferenceResourceGroup, group_id)
            self._match(row, command.expected_version)
            now = database_now(session)
            if row is None:
                row = InferenceResourceGroup(group_id=group_id, version=1)
                session.add(row)
            else:
                row.version += 1
            row.config, row.updated_at = command.config.model_dump(mode="json", by_alias=True), now
            self.repository.event(session, "resource-group:" + group_id, "resource_group_saved", None, now)
            return dict(row.config, version=row.version, updatedAt=now)

    def import_asr(self):
        try:
            document = read_service_document(self.settings)
        except (ValueError, OSError):
            raise AppError("CATALOG_ASR_IMPORT_INVALID", "原 ASR 配置无法读取或校验失败；未改写原文件。", 503) from None
        imported, skipped = [], []
        with self._transaction(True) as session:
            for service_id, revision in document.items.items():
                old = document.revisions[revision]
                target = ServiceTarget(target_id=service_id + "-endpoint" if len(service_id) <= 55 else service_id,
                    name=old.name, base_url=old.base_url, invoke_path=old.transcribe_path, health_path=old.health_path,
                    upstream_model=old.upstream_model, api_key_env=old.api_key_env, ca_file=old.ca_file,
                    insecure_tls_origin=old.insecure_tls_origin, connect_timeout_seconds=old.connect_timeout_seconds,
                    total_timeout_seconds=old.total_timeout_seconds)
                config = ServiceDefinition(service_id=service_id, name=old.name, kind="asr", model_id=old.model_id,
                                           model_version=old.model_version, protocol=old.protocol, targets=[target])
                if self.repository.get(session, service_id):
                    skipped.append(service_id)
                else:
                    self._save(session, config, None, "asr-v2.13")
                    imported.append(service_id)
        return {"imported": imported, "skipped": skipped, "message": "只复制目录资料，未修改原 ASR 配置、选用状态或执行链路。"}
