"""默认调用目标的只读状态；不采样GPU、不访问远端、不改变登记或许可。"""

from sqlalchemy import text

from backend.data.asr.remote_repository import RemoteAsrRepository
from backend.data.asr.target_repository import AsrTargetRepository
from backend.data.asr.target_status_repository import TargetStatusRepository
from backend.data.database.database import database_now
from backend.schema.asr.target_status_schema import TargetActiveCall, TargetPoolStatus, TargetSlotStatus
from backend.service.asr.adapter_catalog import config_fingerprint, remote_asr_configured
from backend.utils.time.time_utils import as_utc


class TargetStatusService:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings

    def _fresh(self, row, now):
        return (row.active_slot_key is not None and row.state not in {"stopped", "lost", "error"}
                and 0 <= (as_utc(now) - as_utc(row.heartbeat_at)).total_seconds()
                <= self.settings.runtime_stale_seconds)

    def status(self):
        repo, views = AsrTargetRepository(), TargetStatusRepository()
        with self.database.session_factory() as session:
            # 显式只读快照，避免多条SELECT跨越一次许可提交而显示相互矛盾的计数。
            session.execute(text("BEGIN"))
            runtime = RemoteAsrRepository.runtime(session)
            now = database_now(session)
            value = TargetPoolStatus(observed_at=as_utc(now))
            if runtime is not None:
                age = (as_utc(now) - as_utc(runtime.heartbeat_at)).total_seconds()
                value.pool_state = (runtime.state if runtime.state == "stopped"
                    or 0 <= age <= self.settings.runtime_stale_seconds else "stale")
            fingerprint = config_fingerprint(self.settings) if remote_asr_configured(self.settings) else None
            value.queued, value.incompatible_queued = views.queued(session, fingerprint)
            value.legacy_unknown_count = views.legacy_unknown_count(session)
            target = repo.get_target(session, value.target_id)
            if target is None:
                return value
            group = repo.get_group(session, target.capacity_group_id)
            revision = repo.revision_by_config(session, target.target_id, fingerprint) if fingerprint else None
            value.registered, value.enabled = True, target.enabled
            value.capacity_group = target.capacity_group_id
            value.target_limit, value.group_limit = target.target_limit, group.capacity_limit
            if revision is not None:
                value.revision_id, value.device_kind = revision.revision_id, revision.device_kind
            counts, shared = views.counts(session, target.target_id, target.capacity_group_id)
            value.held, value.uncertain = counts["held"], counts["uncertain"]
            value.group_held, value.group_uncertain = shared["held"], shared["uncertain"]
            value.remaining_capacity = (max(0, min(target.target_limit - sum(counts.values()),
                group.capacity_limit - sum(shared.values()))) if target.enabled and revision is not None
                and not value.legacy_unknown_count else 0)
            slot_count = min(8, target.target_limit, group.capacity_limit)
            latest = {row.slot_index: row for row in views.latest_slots(session, target.target_id, slot_count)}
            for index in range(slot_count):
                row = latest.get(index)
                if row is None:
                    value.slots.append(TargetSlotStatus(slot_index=index, state="not_started", fresh=False, model_ready=False))
                    continue
                fresh = self._fresh(row, now)
                current_revision = revision is not None and row.revision_id == revision.revision_id
                state = row.state
                if row.active_slot_key is not None and not fresh and state not in {"error", "lost"}:
                    state = "stale"
                elif row.active_slot_key is not None and not current_revision:
                    state = "configuration_changed"
                value.slots.append(TargetSlotStatus(slot_index=index, session_id=row.session_id,
                    state=state, fresh=fresh and current_revision, model_ready=row.model_ready and fresh and current_revision,
                    current_task_id=row.active_task_id, last_error=row.last_error,
                    heartbeat_at=as_utc(row.heartbeat_at)))
            value.active_calls_total = sum(shared.values())
            for permit, target_id, worker in views.active_calls(session, group.group_id):
                value.active_calls.append(TargetActiveCall(task_id=permit.task_id, state=permit.state,
                    target_id=target_id, slot_index=worker.slot_index, owner_fresh=self._fresh(worker, now)))
            value.active_calls_truncated = value.active_calls_total > len(value.active_calls)
            return value

