"""新任务路由绑定与原事务内设备裁决；日常执行默认仍走旧固定通道。"""

from backend.common.exceptions.app_error import AppError
from backend.config.asr.catalog_config import catalog_settings
from backend.config.asr.routing_config import require_routing_isolation
from backend.data.asr.catalog_repository import AsrCatalogRepository
from backend.data.asr.remote_repository import RemoteAsrRepository
from backend.data.asr.routing_repository import AsrRoutingRepository
from backend.data.asr.target_repository import AsrTargetRepository
from backend.data.database.database import database_now
from backend.schema.asr.catalog_schema import CatalogConnection
from backend.schema.asr.routing_schema import RoutingRegistration, RoutingWriteResult
from backend.service.asr.device_policy_service import DevicePolicyService
from backend.service.asr.remote_catalog import config_fingerprint, remote_asr_model_spec
from backend.service.asr.routing_inventory import RoutingInventory
from backend.utils.json.json_utils import canonical_json_sha256
from backend.utils.time.time_utils import as_utc


class AsrRoutingService:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings
        self.repository = AsrRoutingRepository()
        self.catalogs, self.targets, self.calls = AsrCatalogRepository(), AsrTargetRepository(), RemoteAsrRepository()

    def register(self, command):
        command = RoutingRegistration.model_validate(command)
        snapshot = command.model_dump(mode="json", by_alias=True)
        key = canonical_json_sha256(snapshot)
        with self.database.transaction(immediate=True) as session:
            catalogs = self.repository.get_many_catalogs(session, [c.catalog_revision_id for c in command.candidates])
            if len(catalogs) != len(command.candidates):
                raise AppError("ASR_ROUTING_CATALOG_MISSING", "部分目录修订不存在。", 422)
            target_ids = [c.target_id for c in catalogs.values()]
            if len(set(target_ids)) != len(target_ids):
                raise AppError("ASR_ROUTING_TARGET_DUPLICATE", "同一路由不能包含同一目标的多个目录修订。", 422)
            for candidate in command.candidates:
                catalog = catalogs[candidate.catalog_revision_id]
                metadata = catalog.metadata_snapshot
                if (self.catalogs.latest_catalog(session, catalog.target_id).catalog_revision_id != catalog.catalog_revision_id
                        or metadata["evidenceKind"] != command.evidence_kind):
                    raise AppError("ASR_ROUTING_CATALOG_CHANGED", "目录已变化或资料类别不一致，请重新核对。", 409)
                if (metadata.get("compatibilityKey") != command.compatibility_key
                        or not metadata.get("compatibilityEvidence")):
                    raise AppError("ASR_ROUTING_COMPATIBILITY_REQUIRED", "每个候选必须保存同模型兼容标识与依据。", 422)
                profile = candidate.cpu_profile
                if profile is not None and (metadata["deviceKind"] != "cpu"
                        or profile.target_revision != catalog.catalog_revision_id or profile.evidence_kind != command.evidence_kind):
                    raise AppError("ASR_ROUTING_CPU_PROFILE_MISMATCH", "CPU测量必须对应候选目录修订及资料类别。", 422)
            if command.gpu_configuration == "none" and any(c.metadata_snapshot["deviceKind"] == "gpu" for c in catalogs.values()):
                raise AppError("ASR_ROUTING_GPU_CONFLICT", "无GPU声明不能包含GPU候选。", 422)
            existing = self.repository.get_revision(session, key)
            if existing is None:
                self.repository.add_revision(session, routing_revision_id=key, model_id=command.model_id,
                    model_version=command.model_version, evidence_kind=command.evidence_kind,
                    snapshot=snapshot, created_at=database_now(session))
            return RoutingWriteResult(routing_revision_id=key, created=existing is None)

    def _load(self, session, revision_id=None):
        revision_id = revision_id or self.settings.remote_asr_routing_revision
        row = self.repository.get_revision(session, revision_id) if revision_id else None
        if row is None:
            raise AppError("ASR_ROUTING_NOT_FOUND", "路由修订不存在，请先登记，不回退到其他服务。", 409)
        policy = RoutingRegistration.model_validate(row.snapshot)
        catalogs = self.repository.get_many_catalogs(session, [c.catalog_revision_id for c in policy.candidates])
        if len(catalogs) != len(policy.candidates):
            raise AppError("ASR_ROUTING_CATALOG_MISSING", "路由目录记录不完整。", 409)
        origins = [self.targets.get_revision(session, c.revision_id).config_snapshot["baseUrl"] for c in catalogs.values()]
        require_routing_isolation(self.settings, origins)
        if policy.evidence_kind != "isolated" or any(c.metadata_snapshot["evidenceKind"] != "isolated" for c in catalogs.values()):
            raise AppError("ASR_ROUTING_REAL_TARGET_PENDING", "本阶段只运行隔离路由；真实资料仍待后续核实启用。", 409)
        return row, policy, catalogs

    def candidate_settings(self, session, catalog):
        snapshot = self.targets.get_revision(session, catalog.revision_id).config_snapshot
        info = snapshot.get("versionInfo") or {}
        version = None if info.get("version_basis", info.get("versionBasis")) == "integration_revision" else snapshot["modelVersion"]
        connection = CatalogConnection(base_url=snapshot["baseUrl"], model_id=snapshot["modelId"],
            model_version=version, ca_file=snapshot.get("caFile"), insecure_tls_origin=snapshot.get("insecureTlsOrigin"))
        try:
            configured = catalog_settings(self.settings, connection)
        except ValueError:
            raise AppError("ASR_ROUTING_CONNECTION_INVALID", "候选连接或本机证书已变化。", 409) from None
        if (configured.remote_asr_insecure_tls_origin is not None
                and configured.remote_asr_insecure_tls_origin != self.settings.remote_asr_insecure_tls_origin):
            raise AppError("ASR_ROUTING_TLS_NOT_AUTHORIZED", "路由不能扩大现有TLS例外。", 409)
        return configured

    def worker_plans(self):
        with self.database.session_factory() as session:
            _, _, catalogs = self._load(session)
            self.validate_start(session)
            plans = []
            for catalog in catalogs.values():
                target = self.targets.get_target(session, catalog.target_id)
                group = self.targets.get_group(session, target.capacity_group_id)
                if not target.enabled or self.catalogs.latest_catalog(session, target.target_id).catalog_revision_id != catalog.catalog_revision_id:
                    raise AppError("ASR_ROUTING_TARGET_DISABLED", "候选目标未启用或目录已更新，不启动调用槽。", 409)
                configured = self.candidate_settings(session, catalog)
                for index in range(min(target.target_limit, group.capacity_limit)):
                    plans.append(dict(settings=configured, target_id=target.target_id, slot_index=index))
            if not 1 <= len(plans) <= 8:
                raise AppError("ASR_ROUTING_SLOT_LIMIT", "隔离路由总执行槽必须在1至8之间。", 409)
            return plans

    def validate_start(self, session):
        self._load(session)
        if self.repository.has_legacy_waiting(session):
            raise AppError("ASR_ROUTING_LEGACY_PENDING", "仍有原固定通道的待执行任务；请先沿原通道收尾，再启动隔离路由。", 409)
        if self.repository.has_other_route_waiting(session, self.settings.remote_asr_routing_revision):
            raise AppError("ASR_ROUTING_PREVIOUS_ROUTE_PENDING", "仍有其他路由修订的待执行任务；请先按原路由收尾。", 409)

    def validate_submission(self, session, task=None):
        row, _, catalogs = self._load(session)
        if task is not None and (task.model_id, task.model_version) != (row.model_id, row.model_version):
            raise AppError("ASR_ROUTING_MODEL_MISMATCH", "提交模型与路由逻辑模型不一致。", 409)
        runtime = self.calls.runtime(session)
        now = database_now(session)
        if (runtime is None or runtime.state not in {"ready", "running"} or not runtime.model_ready
                or not 0 <= (as_utc(now)-as_utc(runtime.heartbeat_at)).total_seconds() <= self.settings.runtime_stale_seconds):
            raise AppError("REMOTE_ASR_UNAVAILABLE", "调用池未就绪或正在停止。", 503)
        if not any(self.targets.get_target(session, c.target_id).enabled for c in catalogs.values()):
            raise AppError("ASR_ROUTING_TARGET_DISABLED", "没有已启用的隔离候选目标。", 409)
        allowed = {c.revision_id for c in catalogs.values()}
        if not any(worker.revision_id in allowed and worker.model_ready
                and worker.state in {"ready", "idle", "running", "busy", "committing"}
                and 0 <= (as_utc(now)-as_utc(worker.heartbeat_at)).total_seconds() <= self.settings.runtime_stale_seconds
                for worker in self.targets.active_workers(session)):
            raise AppError("REMOTE_ASR_UNAVAILABLE", "本路由没有新鲜且匹配候选修订的调用槽。", 503)

    def record_submission(self, session, task):
        self.repository.add_task_route(session, task_id=task.task_id,
            routing_revision_id=self.settings.remote_asr_routing_revision, created_at=database_now(session))

    def matches_task(self, session, task, target_id, *, require_selected=False):
        route = self.repository.get_task_route(session, task.task_id)
        if route is None:
            return False
        row, _, catalogs = self._load(session, route.routing_revision_id)
        if (task.model_id, task.model_version) != (row.model_id, row.model_version):
            return False
        candidates = [c for c in catalogs.values() if c.target_id == target_id]
        if len(candidates) != 1:
            return False
        catalog = candidates[0]
        revision = self.targets.get_revision(session, catalog.revision_id)
        if revision.config_fingerprint != config_fingerprint(self.settings):
            return False
        if require_selected:
            permit = self.targets.get_permit(session, task.task_id)
            call = self.calls.get(session, task.task_id)
            return bool(route.selected_catalog_revision_id == catalog.catalog_revision_id and permit
                and permit.revision_id == catalog.revision_id and permit.worker_session_id == task.lease_worker_id
                and permit.lease_token == task.lease_token and call
                and call.config_fingerprint == route.routing_revision_id)
        return route.selected_catalog_revision_id in (None, catalog.catalog_revision_id)

    def choose(self, session, task, worker_id, now, *, freeze=False):
        route = self.repository.get_task_route(session, task.task_id)
        if route is None or route.selected_catalog_revision_id is not None:
            return False
        row, policy, catalogs = self._load(session, route.routing_revision_id)
        call = self.calls.get(session, task.task_id)
        worker = self.targets.get_worker(session, worker_id)
        if (call is None or call.state != "queued" or call.config_fingerprint != route.routing_revision_id
                or worker is None or (task.model_id, task.model_version) != (row.model_id, row.model_version)):
            return False
        request, observation_ids = RoutingInventory(self.settings).build(session, task, policy, catalogs, now,
            has_permit=self.targets.get_permit(session, task.task_id) is not None)
        # 任务自身冻结的路由修订，不采用进程重启后的新默认修订。
        request = request.model_copy(update={"routing_revision": route.routing_revision_id})
        decision = DevicePolicyService.evaluate(request)
        selected = catalogs.get(decision.target_revision)
        if (selected is None or worker.revision_id != selected.revision_id
                or not self.matches_task(session, task, selected.target_id)):
            return False
        if freeze:
            self.repository.select_target(session, task.task_id, selected.catalog_revision_id,
                {"reasonCode": decision.reason_code, "targetId": decision.target_id,
                 "deviceKind": decision.device_kind,
                 "catalogRevisionId": selected.catalog_revision_id, "connectionRevisionId": selected.revision_id,
                 "routingRevisionId": route.routing_revision_id, "evidenceKind": policy.evidence_kind,
                 "observationIds": observation_ids, "estimatedCpuSeconds": decision.estimated_cpu_seconds}, now)
        return True

    def reserve(self, tx, target_id):
        if not self.matches_task(tx.session, tx.task, target_id, require_selected=False):
            raise AppError("ASR_ROUTING_SELECTION_CONFLICT", "执行目标与不可变路由不匹配。", 409)
        route = self.repository.get_task_route(tx.session, tx.task.task_id)
        catalog = self.catalogs.get_catalog(tx.session, route.selected_catalog_revision_id) if route else None
        worker = self.targets.get_worker(tx.session, tx.task.lease_worker_id)
        target = self.targets.get_target(tx.session, target_id)
        call = self.calls.get(tx.session, tx.task.task_id)
        if (catalog is None or worker is None or worker.revision_id != catalog.revision_id or not target.enabled
                or tx.task.status != "running" or call is None or call.state != "queued"
                or call.config_fingerprint != route.routing_revision_id
                or self.targets.get_permit(tx.session, tx.task.task_id) is not None):
            raise AppError("ASR_ROUTING_SELECTION_CONFLICT", "路由领取资格已变化。", 409)
        self.targets.add_permit(tx.session, task_id=tx.task.task_id, revision_id=catalog.revision_id,
            capacity_group_id=target.capacity_group_id, worker_session_id=worker.session_id,
            lease_token=tx.task.lease_token, state="held", created_at=tx.now)
        tx.session.flush()
