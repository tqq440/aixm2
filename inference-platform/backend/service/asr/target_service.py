"""外部调用目标准入：与原执行租约同事务占用容量，不管理远端进程。"""

from hashlib import sha256
from uuid import uuid4

from filelock import FileLock, Timeout

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import remote_asr_origin
from backend.data.asr.remote_repository import RemoteAsrRepository
from backend.data.asr.target_repository import AsrTargetRepository
from backend.data.database.database import database_now
from backend.schema.asr.target_schema import TargetBinding, TargetCapacityCommand, TargetRegistration
from backend.service.asr.remote_catalog import config_fingerprint, remote_asr_model_spec
from backend.utils.time.time_utils import as_utc


class TargetAdmissionService:
    """默认单槽；本机维护只能在停止后按明确依据配置1至8个调用槽。"""

    def __init__(self, database, settings, target_id="remote-default"):
        self.database, self.settings = database, settings
        self.target_id = TargetBinding(target_id=target_id).target_id
        self.repository = AsrTargetRepository()
        self.calls = RemoteAsrRepository()

    def register_current(self, *, group_id="remote-asr-shared", independence_evidence=None):
        command = TargetRegistration(group_id=group_id, independence_evidence=independence_evidence)
        with self.database.transaction(immediate=True) as session:
            return self._register(session, command).to_dict()

    def pool_limit(self):
        with self.database.session_factory() as session:
            target = self.repository.get_target(session, self.target_id)
            if target is None or not target.enabled:
                raise AppError("ASR_TARGET_SLOT_DISABLED", "目标尚未登记或未启用。", 409)
            group = self.repository.get_group(session, target.capacity_group_id)
            if not 1 <= target.target_limit <= 8 or not 1 <= group.capacity_limit <= 8:
                raise AppError("ASR_CAPACITY_INVALID", "本机调用池限额必须在1至8之间。", 409)
            return min(target.target_limit, group.capacity_limit)

    def configure_capacity(self, command):
        """服务端复核停池、共享组和未知占位；维护命令本身不启动进程。"""
        command = TargetCapacityCommand.model_validate(command)
        path = self.settings.database_path.resolve()
        lock = FileLock(path.with_name(path.name + ".remote-asr-worker.lock"), timeout=0)
        try:
            lock.acquire()
        except Timeout as exc:
            raise AppError("ASR_POOL_MUST_BE_STOPPED", "请先停止本机外部调用池，再修改容量。", 409) from exc
        try:
            with self.database.transaction(immediate=True) as session:
                repo, now = self.repository, database_now(session)
                target = repo.get_target(session, self.target_id)
                if target is None:
                    raise AppError("ASR_TARGET_NOT_FOUND", "请先登记计算目标。", 404)
                group = repo.get_group(session, target.capacity_group_id)
                _, occupied = repo.active_counts(session, target.target_id, group.group_id)
                runtime = self.calls.runtime(session)
                fresh_runtime = (runtime is not None and runtime.state != "stopped"
                    and 0 <= (as_utc(now) - as_utc(runtime.heartbeat_at)).total_seconds() <= self.settings.runtime_stale_seconds)
                fresh_workers = any(worker.active_slot_key is not None and worker.state != "stopped"
                    and 0 <= (as_utc(now) - as_utc(worker.heartbeat_at)).total_seconds() <= self.settings.runtime_stale_seconds
                    for worker in repo.active_workers(session))
                if occupied or repo.untracked_legacy_active(session):
                    raise AppError("ASR_CAPACITY_OCCUPIED", "共享组仍有在途或未确认请求，不能修改容量。", 409)
                if fresh_runtime or fresh_workers:
                    raise AppError("ASR_POOL_MUST_BE_STOPPED", "调用池或调用槽尚有新鲜活动报告，不能修改容量。", 409)
                previous_target, previous_group = target.target_limit, group.capacity_limit
                if not 1 <= previous_target <= 8 or not 1 <= previous_group <= 8:
                    raise AppError("ASR_CAPACITY_INVALID", "原容量超出受控范围，须先核对历史配置。", 409)
                changed = (previous_target, previous_group) != (command.target_limit, command.group_limit)
                audit = None
                if changed:
                    target.target_limit, group.capacity_limit = command.target_limit, command.group_limit
                    audit = repo.add_capacity_change(session, change_id=uuid4().hex,
                        target_id=target.target_id, capacity_group_id=group.group_id,
                        previous_target_limit=previous_target, target_limit=command.target_limit,
                        previous_group_limit=previous_group, group_limit=command.group_limit,
                        evidence=command.evidence, changed_at=now)
                    session.flush()
                return {"changed": changed, "target_id": target.target_id,
                    "capacity_group_id": group.group_id, "target_limit": target.target_limit,
                    "group_limit": group.capacity_limit, "change_id": audit.change_id if audit else None}
        finally:
            lock.release()

    def _register(self, session, command=None):
        if command is None:
            target = self.repository.get_target(session, self.target_id)
            command = TargetRegistration(group_id=target.capacity_group_id) if target is not None else TargetRegistration()
        spec = remote_asr_model_spec(self.settings)
        fingerprint = config_fingerprint(self.settings)
        origin = remote_asr_origin(self.settings.remote_asr_base_url)
        endpoint = sha256(origin.encode("utf-8")).hexdigest()
        repo, now = self.repository, database_now(session)
        groups = repo.endpoint_groups(session, endpoint)
        if groups and groups != {command.group_id}:
            raise AppError("ASR_TARGET_SHARED_CAPACITY", "同一服务的别名与版本必须共用容量组。", 409)
        target = repo.get_target(session, self.target_id)
        if target is not None and target.capacity_group_id != command.group_id:
            raise AppError("ASR_TARGET_GROUP_IMMUTABLE", "已有目标的容量归属不能通过重新登记改变。", 409)
        group = repo.get_group(session, command.group_id)
        if group is None:
            evidence = command.independence_evidence
            if command.group_id != "remote-asr-shared" and not (evidence and evidence.strip()):
                raise AppError("ASR_CAPACITY_EVIDENCE_REQUIRED", "独立容量组须有人工核实的独立容量依据。", 422)
            repo.add_group(session, group_id=command.group_id, capacity_limit=1,
                           independence_evidence=evidence, created_at=now)
            session.flush()
        if target is None:
            repo.add_target(session, target_id=self.target_id, capacity_group_id=command.group_id,
                            enabled=True, target_limit=1, created_at=now)
            session.flush()
        revision = repo.revision_by_config(session, self.target_id, fingerprint)
        if revision is None:
            revision = repo.add_revision(session,
                revision_id=sha256(f"{self.target_id}:{fingerprint}".encode()).hexdigest(),
                target_id=self.target_id, config_fingerprint=fingerprint, endpoint_identity=endpoint,
                config_snapshot={"protocol": "remote-v2-sync", "baseUrl": origin,
                    "modelId": spec.model_id, "modelVersion": spec.model_version,
                    "versionInfo": spec.version_info.model_dump() if spec.version_info else None,
                    "caFile": str(self.settings.remote_asr_ca_file) if self.settings.remote_asr_ca_file else None,
                    "insecureTlsOrigin": self.settings.remote_asr_insecure_tls_origin},
                device_kind="unknown", device_evidence=None, created_at=now)
            session.flush()
        return revision

    def bind_worker(self, session_id, pid, slot_index=0, *, slot_owned=False):
        with self.database.transaction(immediate=True) as session:
            return self.bind_in_transaction(session, session_id, pid, slot_index, slot_owned=slot_owned).to_dict()

    def bind_in_transaction(self, session, session_id, pid, slot_index=0, *, slot_owned=False):
        from backend.service.asr.target_binding import TargetSlotLock

        binding = TargetBinding(target_id=self.target_id, slot_index=slot_index)
        if len(session_id) != 32 or any(c not in "0123456789abcdef" for c in session_id):
            raise ValueError("Worker会话必须为32位十六进制标识")
        repo, now = self.repository, database_now(session)
        runtime = self.calls.runtime(session)
        if runtime is not None and runtime.state in {"stopping", "draining"}:
            raise AppError("ASR_POOL_DRAINING", "调用池已停止接单，不能再绑定新的调用槽。", 409)
        revision = repo.revision_by_config(session, self.target_id, config_fingerprint(self.settings))
        if revision is None:
            revision = self._register(session)
        target = repo.get_target(session, self.target_id)
        if not target.enabled or binding.slot_index >= target.target_limit:
            raise AppError("ASR_TARGET_SLOT_DISABLED", "目标未启用或执行槽超出登记限额。", 409)
        key = TargetSlotLock(self.settings.database_path, self.target_id, binding.slot_index).worker_key
        existing = repo.get_worker(session, session_id)
        if existing is not None:
            if existing.revision_id != revision.revision_id or existing.active_slot_key != key:
                raise AppError("ASR_TARGET_SESSION_CONFLICT", "既有会话不能更换目标修订或重新使用。", 409)
            return existing
        previous = repo.active_worker(session, key)
        if previous is not None:
            # slot_owned仅由已取得稳定槽文件锁的运行入口传入；心跳过期本身不授予接管权。
            if not slot_owned:
                raise AppError("ASR_TARGET_SLOT_IN_USE", "目标执行槽已有会话，须先取得独占槽锁。", 409)
            repo.set_fields(previous, state="lost", model_ready=False, active_slot_key=None, ended_at=now)
            session.flush()
        worker = repo.add_worker(session, session_id=session_id, revision_id=revision.revision_id,
            worker_key=key, slot_index=binding.slot_index, active_slot_key=key, pid=pid,
            state="starting", model_ready=False, started_at=now, heartbeat_at=now)
        session.flush()
        return worker

    def observe_worker(self, session, runtime):
        """兼容既有固定运行行；不能用旧会话报告恢复已失效的绑定。"""
        if runtime is None:
            return
        worker = self.repository.get_worker(session, runtime.session_id)
        if worker is None or worker.active_slot_key is None:
            return
        self._observe(worker, runtime.state, runtime.model_ready, runtime.heartbeat_at)

    def _observe(self, worker, state, model_ready, now, **report):
        if as_utc(now) < as_utc(worker.heartbeat_at):
            return
        if worker.state in {"stopping", "draining"} and state not in {"stopped", "error"}:
            state = "stopping"
        values = dict(state=state, model_ready=model_ready, heartbeat_at=now, **report)
        if state == "stopped":
            values.update(active_slot_key=None, model_ready=False, ended_at=now, active_task_id=None, child_pid=None)
        self.repository.set_fields(worker, **values)

    def report_worker(self, session_id, state, model_ready, now=None, *,
                      active_task_id=None, child_pid=None, last_error=None):
        with self.database.transaction(immediate=True) as session:
            worker = self.repository.get_worker(session, session_id)
            if worker is not None and worker.state == "stopped" and state == "stopped":
                return True
            if worker is None or worker.active_slot_key is None:
                return False
            self._observe(worker, state, model_ready, now or database_now(session),
                active_task_id=active_task_id, child_pid=child_pid, last_error=last_error[:256] if last_error else None)
            return True

    def _eligible(self, session, session_id, now):
        repo = self.repository
        runtime = self.calls.runtime(session)
        if runtime is not None and runtime.state in {"stopping", "draining"}:
            return None
        worker = repo.get_worker(session, session_id)
        if (worker is None or worker.active_slot_key is None or not worker.model_ready
                or worker.state not in {"ready", "idle", "running", "busy", "committing"}
                or not 0 <= (as_utc(now) - as_utc(worker.heartbeat_at)).total_seconds() <= self.settings.runtime_stale_seconds):
            return None
        revision = repo.get_revision(session, worker.revision_id)
        target = repo.get_target(session, revision.target_id)
        if (revision.target_id != self.target_id or not target.enabled
                or revision.config_fingerprint != config_fingerprint(self.settings)
                or worker.slot_index >= target.target_limit):
            return None
        group = repo.get_group(session, target.capacity_group_id)
        target_count, group_count = repo.active_counts(session, target.target_id, group.group_id)
        if (target_count >= target.target_limit or group_count >= group.capacity_limit
                or repo.untracked_legacy_active(session)
                or repo.worker_active_permits(session, session_id, held_only=True)):
            return None
        return worker, revision, target

    def can_receive(self, session, session_id, now):
        return self._eligible(session, session_id, now) is not None

    def can_claim(self, tx, session_id):
        return self.can_claim_task(tx.session, tx.task, session_id, tx.now)

    def can_claim_task(self, session, task, session_id, now):
        eligible = self._eligible(session, session_id, now)
        if eligible is None or task is None or task.status != "queued":
            return False
        call = self.calls.get(session, task.task_id)
        revision = eligible[1]
        snapshot = revision.config_snapshot
        return bool(call is not None and call.state == "queued"
            and call.config_fingerprint == revision.config_fingerprint
            and task.model_id == snapshot["modelId"] and task.model_version == snapshot["modelVersion"]
            and self.repository.get_permit(session, task.task_id) is None)

    def reserve(self, tx):
        """仅在原tx.start之后调用；容量失败抛错使任务/attempt/许可整体回滚。"""
        task, repo = tx.task, self.repository
        eligible = self._eligible(tx.session, task.lease_worker_id, tx.now)
        call = self.calls.get(tx.session, task.task_id)
        if (eligible is None or task.status != "running" or not task.lease_token
                or call is None or call.state != "queued"
                or call.config_fingerprint != eligible[1].config_fingerprint
                or task.model_id != eligible[1].config_snapshot["modelId"]
                or task.model_version != eligible[1].config_snapshot["modelVersion"]
                or repo.get_permit(tx.session, task.task_id) is not None):
            raise AppError("ASR_TARGET_CAPACITY_CONFLICT", "目标执行资格已变化，未取得执行许可。", 409)
        worker, revision, target = eligible
        permit = repo.add_permit(tx.session, task_id=task.task_id, revision_id=revision.revision_id,
            capacity_group_id=target.capacity_group_id, worker_session_id=worker.session_id,
            lease_token=task.lease_token, state="held", created_at=tx.now)
        tx.session.flush()
        return permit

    def finish(self, tx, *, confirmed):
        permit = self.repository.get_permit(tx.session, tx.task.task_id)
        if (permit is not None and permit.state == "held"
                and permit.worker_session_id == tx.task.lease_worker_id
                and permit.lease_token == tx.task.lease_token):
            self.repository.set_fields(permit, state="released" if confirmed else "uncertain", ended_at=tx.now)

    def resolve(self, session, task_id, now):
        # 人工核对资格和幂等裁决继续由RemoteAsrService.release_uncertain执行。
        permit = self.repository.get_permit(session, task_id)
        if permit is not None and permit.state == "uncertain":
            self.repository.set_fields(permit, state="released", resolved_at=now)

    def begin_drain(self, session_id):
        with self.database.transaction(immediate=True) as session:
            self.drain_in_transaction(session, session_id)

    def drain_in_transaction(self, session, session_id=None):
        workers = ([self.repository.get_worker(session, session_id)] if session_id is not None
                   else self.repository.active_workers(session))
        for worker in workers:
            if worker is not None and worker.active_slot_key is not None:
                self.repository.set_fields(worker, state="stopping")

    def end_worker(self, session_id):
        return self.report_worker(session_id, "stopped", False)

    def inventory(self):
        """本机维护只读查询；登记和心跳不代表硬件资源观测。"""
        with self.database.session_factory() as session:
            return {"targets": [row.to_dict() for row in self.repository.list_targets(session)],
                "groups": [row.to_dict() for row in self.repository.list_groups(session)],
                "revisions": [row.to_dict() for row in self.repository.list_revisions(session)],
                "workers": [row.to_dict() for row in self.repository.list_workers(session)],
                "permits": [row.to_dict() for row in self.repository.list_permits(session)],
                "capacity_changes": [row.to_dict() for row in self.repository.list_capacity_changes(session)]}
