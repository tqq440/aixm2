"""受控ASR目录与追加式观测；本阶段不授予路由或调用权限。"""

from datetime import timedelta
from hashlib import sha256

from backend.common.exceptions.app_error import AppError
from backend.config.asr.catalog_config import catalog_settings
from backend.data.asr.catalog_repository import AsrCatalogRepository
from backend.data.asr.target_repository import AsrTargetRepository
from backend.data.database.database import database_now
from backend.schema.asr.catalog_schema import (
    CatalogPage, CatalogQuery, CatalogRegistration, CatalogTargetView, CatalogWriteResult,
    ObservationView, ObservationWriteResult, ResourceObservationInput,
)
from backend.service.asr.remote_catalog import config_fingerprint, remote_asr_model_spec
from backend.utils.json.json_utils import canonical_json_sha256
from backend.utils.time.time_utils import as_utc


class AsrCatalogService:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings
        self.repository = AsrCatalogRepository()
        self.targets = AsrTargetRepository()

    def register(self, command: CatalogRegistration) -> CatalogWriteResult:
        command = CatalogRegistration.model_validate(command)
        if command.target_id == "remote-default":
            raise AppError("ASR_CATALOG_LEGACY_RESERVED", "原目标保留既有登记方式，不能由新目录覆盖。", 409)
        try:
            configured = catalog_settings(self.settings, command.connection)
        except ValueError:
            # CLI会记录异常链；连接输入可能含路径或误填凭据，不能写入日志。
            raise AppError("ASR_CATALOG_INVALID_CONNECTION", "目标连接配置无效，请检查HTTPS根地址及本机证书。", 422) from None
        if (configured.remote_asr_insecure_tls_origin is not None
                and configured.remote_asr_insecure_tls_origin != self.settings.remote_asr_insecure_tls_origin):
            raise AppError("ASR_CATALOG_TLS_NOT_AUTHORIZED", "新目录不能扩大当前配置明确允许的TLS例外。", 422)
        spec, fingerprint = remote_asr_model_spec(configured), config_fingerprint(configured)
        origin = configured.remote_asr_base_url
        endpoint = sha256(origin.encode("utf-8")).hexdigest()
        metadata = command.model_dump(mode="json", by_alias=True, exclude={"connection"})
        with self.database.transaction(immediate=True) as session:
            repo, now = self.targets, database_now(session)
            groups = repo.endpoint_groups(session, endpoint)
            if groups and groups != {command.capacity_group_id}:
                raise AppError("ASR_CATALOG_SHARED_CAPACITY", "同一服务入口必须沿用同一容量组。", 409)
            target = repo.get_target(session, command.target_id)
            if target is not None and target.enabled:
                raise AppError("ASR_CATALOG_TARGET_ACTIVE", "不能通过待接入目录修改已启用目标。", 409)
            if target is not None and target.capacity_group_id != command.capacity_group_id:
                raise AppError("ASR_CATALOG_GROUP_IMMUTABLE", "已有目标不能更换共享容量归属。", 409)
            group = repo.get_group(session, command.capacity_group_id)
            if group is None:
                if command.capacity_group_id != "remote-asr-shared" and not command.independence_evidence:
                    raise AppError("ASR_CATALOG_GROUP_EVIDENCE_REQUIRED", "新独立容量组必须附核对依据。", 422)
                repo.add_group(session, group_id=command.capacity_group_id, capacity_limit=1,
                    independence_evidence=command.independence_evidence, created_at=now)
                session.flush()
            if target is None:
                repo.add_target(session, target_id=command.target_id, capacity_group_id=command.capacity_group_id,
                    enabled=False, target_limit=1, created_at=now)
                session.flush()
            revision = repo.revision_by_config(session, command.target_id, fingerprint)
            if revision is None:
                revision = repo.add_revision(session,
                    revision_id=sha256(f"{command.target_id}:{fingerprint}".encode()).hexdigest(),
                    target_id=command.target_id, config_fingerprint=fingerprint, endpoint_identity=endpoint,
                    config_snapshot={"protocol": "remote-v2-sync", "baseUrl": origin,
                        "modelId": spec.model_id, "modelVersion": spec.model_version,
                        "versionInfo": spec.version_info.model_dump() if spec.version_info else None,
                        "caFile": str(configured.remote_asr_ca_file) if configured.remote_asr_ca_file else None,
                        "insecureTlsOrigin": configured.remote_asr_insecure_tls_origin},
                    device_kind="unknown", device_evidence=None, created_at=now)
                session.flush()
            key = canonical_json_sha256({"revisionId": revision.revision_id, "metadata": metadata})
            catalog = self.repository.get_catalog(session, key)
            created = catalog is None
            if created:
                self.repository.add_catalog(session, catalog_revision_id=key, target_id=command.target_id,
                    revision_id=revision.revision_id, metadata_snapshot=metadata, created_at=now)
                session.flush()
            return CatalogWriteResult(target_id=command.target_id, catalog_revision_id=key, created=created)

    def observe(self, command: ResourceObservationInput) -> ObservationWriteResult:
        command = ResourceObservationInput.model_validate(command)
        payload = command.model_dump(mode="json", by_alias=True)
        # 同一采样的重复提交幂等；时区表示先规范为UTC。
        payload["observedAt"] = as_utc(command.observed_at).isoformat()
        key = canonical_json_sha256(payload)
        with self.database.transaction(immediate=True) as session:
            now = database_now(session)
            catalog = self.repository.get_catalog(session, command.catalog_revision_id)
            if catalog is None:
                raise AppError("ASR_CATALOG_NOT_FOUND", "目标目录修订不存在。", 404)
            if self.repository.latest_catalog(session, catalog.target_id).catalog_revision_id != command.catalog_revision_id:
                raise AppError("ASR_OBSERVATION_OLD_REVISION", "配置已更新，请核对后向当前修订记录新观测。", 409)
            if as_utc(command.observed_at) > as_utc(now):
                raise AppError("ASR_OBSERVATION_FUTURE", "采样时间不能晚于当前数据库时间。", 422)
            if command.evidence_kind != catalog.metadata_snapshot["evidenceKind"]:
                raise AppError("ASR_OBSERVATION_EVIDENCE_MISMATCH", "隔离记录与实际提供的资料不能混用。", 422)
            existing = self.repository.get_observation(session, key)
            if existing is None:
                self.repository.add_observation(session, observation_id=key,
                    catalog_revision_id=command.catalog_revision_id, scope=command.scope,
                    source=command.source, evidence_kind=command.evidence_kind,
                    observed_at=as_utc(command.observed_at).replace(tzinfo=None),
                    ttl_seconds=command.ttl_seconds, payload=payload, recorded_at=now)
                session.flush()
            return ObservationWriteResult(observation_id=key, created=existing is None)

    def list_targets(self, query: CatalogQuery | None = None) -> CatalogPage:
        query = CatalogQuery.model_validate(query or {})
        with self.database.session_factory() as session:
            now = database_now(session)
            total, rows = self.repository.page_targets(session, query.offset, query.limit)
            ids = [catalog.catalog_revision_id for _, _, catalog, _ in rows if catalog is not None]
            observations = self.repository.latest_observations(session, ids)
            items = [self._target_view(row, observations, now) for row in rows]
            return CatalogPage(observed_at=as_utc(now), total=total,
                offset=query.offset, limit=query.limit, items=items)

    def _target_view(self, row, observations, now):
        target, group, catalog, revision = row
        metadata = catalog.metadata_snapshot if catalog is not None else {}
        snapshot = revision.config_snapshot if revision is not None else {}
        views = []
        if catalog is not None:
            for scope in ("target", "capacity_group"):
                record = observations.get((catalog.catalog_revision_id, scope))
                if record is not None:
                    views.append(self._observation_view(record, now))
        return CatalogTargetView(target_id=target.target_id, capacity_group_id=group.group_id,
            target_limit=target.target_limit, group_limit=group.capacity_limit,
            legacy_enabled=target.enabled, mode="catalog_only" if catalog is not None else "legacy",
            connection_revision_id=revision.revision_id if revision is not None else None,
            catalog_revision_id=catalog.catalog_revision_id if catalog is not None else None,
            device_kind=metadata.get("deviceKind", "unknown"),
            model_id=snapshot.get("modelId"), model_version=snapshot.get("modelVersion"),
            has_device_evidence=bool(metadata.get("deviceEvidence")),
            has_compatibility_evidence=bool(metadata.get("compatibilityEvidence")), observations=views)

    @staticmethod
    def _observation_view(record, now):
        sampled, current = as_utc(record.observed_at), as_utc(now)
        expiry = sampled + timedelta(seconds=record.ttl_seconds)
        freshness = "unknown" if current < max(sampled, as_utc(record.recorded_at)) else "fresh" if current < expiry else "stale"
        payload = record.payload
        return ObservationView(observation_id=record.observation_id, scope=record.scope,
            source=record.source, evidence_kind=record.evidence_kind, observed_at=sampled,
            recorded_at=as_utc(record.recorded_at), expires_at=expiry, freshness=freshness,
            reported_health=payload["healthState"],
            reported_available_concurrency=payload.get("availableConcurrency"),
            gpu_utilization_percent=payload.get("gpuUtilizationPercent"),
            gpu_memory_free_mi_b=payload.get("gpuMemoryFreeMiB"),
            cpu_available_cores=payload.get("cpuAvailableCores"))
