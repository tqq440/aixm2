"""把持久化目录、资源报告、固定槽及许可账本转换为原设备策略输入。"""

from datetime import timedelta

from backend.data.asr.catalog_repository import AsrCatalogRepository
from backend.data.asr.target_repository import AsrTargetRepository
from backend.schema.asr.device_policy_schema import DevicePolicyRequest
from backend.utils.time.time_utils import as_utc


class RoutingInventory:
    def __init__(self, settings):
        self.settings = settings
        self.catalogs = AsrCatalogRepository()
        self.targets = AsrTargetRepository()

    @staticmethod
    def fresh(record, now):
        return bool(record is not None and max(as_utc(record.observed_at), as_utc(record.recorded_at))
            <= now < as_utc(record.observed_at) + timedelta(seconds=record.ttl_seconds))

    def build(self, session, task, policy, catalogs, now, *, has_permit=False):
        now = as_utc(now)
        observations = self.catalogs.latest_observations(session, list(catalogs))
        workers = self.targets.active_workers(session)
        groups, targets, observation_ids, group_sources = {}, [], [], {}
        untracked = self.targets.untracked_legacy_active(session)
        for entry in policy.candidates:
            catalog = catalogs[entry.catalog_revision_id]
            target = self.targets.get_target(session, catalog.target_id)
            group = self.targets.get_group(session, target.capacity_group_id)
            metadata = catalog.metadata_snapshot
            current = self.catalogs.latest_catalog(session, target.target_id).catalog_revision_id == catalog.catalog_revision_id
            trusted = current and target.enabled and metadata["evidenceKind"] == policy.evidence_kind
            local = self.targets.active_permits_with_target(session, group.group_id)
            own = [permit for permit, target_id in local if target_id == target.target_id]
            held = sum(permit.state == "held" for permit in own)
            uncertain = sum(permit.state == "uncertain" for permit in own)
            group_held = sum(permit.state == "held" for permit, _ in local)
            group_uncertain = sum(permit.state == "uncertain" for permit, _ in local)
            report = observations.get((catalog.catalog_revision_id, "target"))
            shared = observations.get((catalog.catalog_revision_id, "capacity_group"))
            same_source = bool(report and shared and report.source == shared.source)
            valid = trusted and same_source and not untracked
            report_valid = bool(valid and self.fresh(report, now) and report.evidence_kind == policy.evidence_kind)
            group_valid = bool(valid and self.fresh(shared, now) and shared.evidence_kind == policy.evidence_kind
                and shared.payload["healthState"] == "ready")
            source = shared.source if shared is not None else None
            if group.group_id in group_sources and group_sources[group.group_id] != source:
                group_valid = False
            group_sources[group.group_id] = source
            for record in (report, shared):
                if record is not None:
                    observation_ids.append(record.observation_id)
            slot_reports = [worker for worker in workers if worker.revision_id == catalog.revision_id
                and worker.model_ready and worker.state in {"ready", "idle", "running", "busy", "committing"}
                and 0 <= (now - as_utc(worker.heartbeat_at)).total_seconds() <= self.settings.runtime_stale_seconds
                and worker.slot_index < min(target.target_limit, group.capacity_limit)]
            slot_time = max((as_utc(worker.heartbeat_at) for worker in slot_reports), default=None)
            available = report.payload.get("availableConcurrency") if report_valid else None
            shared_available = shared.payload.get("availableConcurrency") if group_valid else None
            group_entry = dict(capacityGroupId=group.group_id, limit=group.capacity_limit,
                held=group_held, uncertain=group_uncertain, capacityKnown=group_valid,
                observedAt=as_utc(shared.observed_at) if group_valid else None,
                ttlSeconds=float(shared.ttl_seconds) if group_valid else 1.0,
                capacityEvidence="recorded-report" if group_valid else None,
                evidenceKind=policy.evidence_kind, resourceRequired=True,
                resourceExhaustionConfirmed=shared_available == 0 or group_held + group_uncertain >= group.capacity_limit,
                resourceAvailable=max(0, shared_available - group_held - group_uncertain)
                    if shared_available is not None else None)
            previous = groups.get(group.group_id)
            if previous is not None:
                # 同组的各目标均须有匹配且有效的报告，取保守下界，不叠加容量。
                if not previous["capacityKnown"] or not group_entry["capacityKnown"]:
                    group_entry.update(capacityKnown=False, observedAt=None, resourceAvailable=None)
                elif previous["resourceAvailable"] is None or group_entry["resourceAvailable"] is None:
                    group_entry["resourceAvailable"] = None
                else:
                    group_entry["resourceAvailable"] = min(previous["resourceAvailable"], group_entry["resourceAvailable"])
                group_entry["resourceExhaustionConfirmed"] = (
                    previous["resourceExhaustionConfirmed"] and group_entry["resourceExhaustionConfirmed"])
            groups[group.group_id] = group_entry
            targets.append(dict(targetId=target.target_id, targetRevision=catalog.catalog_revision_id,
                compatibilityKey=metadata.get("compatibilityKey") or "unconfirmed",
                deviceKind=metadata["deviceKind"], deviceEvidence=metadata.get("deviceEvidence") if trusted else None,
                compatibilityEvidence=metadata.get("compatibilityEvidence") if trusted else None,
                evidenceKind=policy.evidence_kind, capacityGroupId=group.group_id, limit=target.target_limit,
                held=held, uncertain=uncertain, capacityKnown=report_valid,
                capacityObservedAt=as_utc(report.observed_at) if report_valid else None,
                capacityTtlSeconds=float(report.ttl_seconds) if report_valid else 1.0,
                capacityEvidence="recorded-report" if report_valid else None,
                healthState=report.payload["healthState"] if report_valid else "unknown",
                healthObservedAt=as_utc(report.observed_at) if report_valid else None,
                healthTtlSeconds=float(report.ttl_seconds) if report_valid else 1.0,
                slotReady=bool(slot_reports) and trusted, slotObservedAt=slot_time,
                slotTtlSeconds=float(self.settings.runtime_stale_seconds),
                cpuSupported=entry.cpu_supported, cpuSupportEvidence=entry.cpu_support_evidence,
                cpuProfile=entry.cpu_profile.model_dump(mode="json", by_alias=True) if entry.cpu_profile else None,
                cpuProfileUsable=bool(entry.cpu_profile and 0 <= (now-as_utc(entry.cpu_profile.measured_at)).total_seconds()
                    < entry.cpu_profile.valid_for_seconds),
                resourceExhaustionConfirmed=available == 0 or held + uncertain >= target.target_limit,
                resourceRequired=True, resourceAvailable=max(0, available-held-uncertain) if available is not None else None))
        request = DevicePolicyRequest.model_validate(dict(evaluatedAt=now, executionEnabled=False,
            routingRevision=self.settings.remote_asr_routing_revision, compatibilityKey=policy.compatibility_key,
            gpuConfiguration=policy.gpu_configuration,
            task=dict(status=task.status, attempt=task.attempt, hasPermit=has_permit,
                upstreamExecutionStarted=task.attempt > 0, audioSeconds=task.audio_duration_seconds,
                queueDeadlineAt=as_utc(task.queue_deadline_at), totalDeadlineAt=as_utc(task.total_deadline_at),
                attemptBudgetSeconds=float(self.settings.attempt_timeout_seconds),
                remoteBudgetSeconds=float(self.settings.remote_asr_total_timeout_seconds),
                cpuBudgetSeconds=policy.cpu_budget_seconds), groups=list(groups.values()), targets=targets))
        return request, sorted(set(observation_ids))
