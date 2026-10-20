"""外部 ASR 准入、调用确认与维护；本地退出不证明远端停止。"""

from datetime import datetime, timezone
from contextlib import contextmanager
import os

from sqlalchemy import text

from backend.common.exceptions.app_error import AppError
from backend.data.asr.remote_repository import RemoteAsrRepository
from backend.data.database.database import database_now
from backend.data.execution.execution_repository import ExecutionRepository
from backend.data.tasks.task_models import TaskRecord
from backend.schema.asr.remote_schema import RemoteAsrRelease, RemoteAsrStatus
from backend.schema.asr.target_schema import TargetBinding
from backend.schema.execution.execution_schema import FailureInfo
from backend.service.asr.adapter_catalog import config_fingerprint, remote_asr_configured
from backend.service.asr.remote_v2_adapter import probe_remote_asr
from backend.service.asr.remote_catalog import remote_asr_model_spec
from backend.service.asr.target_service import TargetAdmissionService
from backend.service.asr.routing_service import AsrRoutingService
from backend.utils.time.time_utils import as_utc

REMOTE_QUEUE = "asr.remote-v2-sensevoice"
REMOTE_PROFILE = "remote-v2-sensevoice"
CONFIRMED_FAILURES = {"REMOTE_ASR_NOT_SENT", "REMOTE_ASR_REJECTED", "INPUT_UNAVAILABLE",
                      "INPUT_INTEGRITY_FAILED", "AUDIO_DECODE_FAILED", "AUDIO_TOO_LONG"}


class RemoteAsrService:
    def __init__(self, database, settings, *, target_id="remote-default", slot_index=0, pooled=False):
        self.database, self.settings = database, settings
        binding = TargetBinding(target_id=target_id, slot_index=slot_index)
        self.target_id, self.slot_index, self.pooled = binding.target_id, binding.slot_index, pooled
        self.repository = RemoteAsrRepository()
        self.targets = TargetAdmissionService(database, settings, target_id=target_id)
        self.routing = AsrRoutingService(database, settings)

    @staticmethod
    def is_remote(task):
        return task is not None and task.queue_key == REMOTE_QUEUE

    def _worker_available(self, row, now):
        return (row is not None and row.state in {"ready", "running", "idle", "busy", "committing"}
                and row.model_ready
                and 0 <= (as_utc(now) - as_utc(row.heartbeat_at)).total_seconds() <= self.settings.runtime_stale_seconds)

    def _call_owner_observed(self, row, active, now):
        return (row is not None and row.session_id == active.worker_id
                and row.state not in {"stopped", "error"}
                and 0 <= (as_utc(now) - as_utc(row.heartbeat_at)).total_seconds() <= self.settings.runtime_stale_seconds)

    def _bound_owner_observed(self, session, active, runtime, now):
        worker = self.targets.repository.get_worker(session, active.worker_id) if active.worker_id else None
        if worker is None:
            return self._call_owner_observed(runtime, active, now)
        if worker.active_slot_key is None or worker.state in {"stopped", "lost", "error"}:
            return False
        # 旧direct入口仍报告固定runtime；池的汇总session绝不能替代槽session。
        observed = runtime if runtime is not None and runtime.session_id == worker.session_id else worker
        return self._call_owner_observed(observed, active, now)

    def _submission_admission(self, session, now):
        """允许正常held时排队；未知请求按组计额，无法归属的旧请求保守阻断。"""
        repo, runtime = self.targets.repository, self.repository.runtime(session)
        calls = self.repository.active_calls(session)
        unknown = [call for call in calls if call.state == "uncertain"
                   or not self._bound_owner_observed(session, call, runtime, now)]
        unknown_id = unknown[0].task_id if unknown else None
        if repo.untracked_legacy_active(session):
            return "uncertain", False, None, unknown_id or calls[0].task_id
        if runtime is not None and runtime.state in {"stopping", "draining"}:
            held = next((call.task_id for call in calls if call.state == "held"), None)
            return "stopping", False, held, unknown_id
        target = repo.get_target(session, self.target_id)
        revision = repo.revision_by_config(session, self.target_id, config_fingerprint(self.settings))
        if target is None or not target.enabled or revision is None:
            return "unavailable", False, None, unknown_id
        group = repo.get_group(session, target.capacity_group_id)
        active_permits = repo.active_permits_with_target(session, group.group_id)
        permits = {item.task_id: item for item, _ in active_permits}
        own_tasks = {item.task_id for item, target_id in active_permits if target_id == self.target_id}
        target_unknown = sum(call.task_id in own_tasks for call in unknown)
        group_unknown = sum(call.task_id in permits and permits[call.task_id].capacity_group_id == group.group_id for call in unknown)
        if target_unknown >= target.target_limit or group_unknown >= group.capacity_limit:
            return "uncertain", False, None, unknown_id
        available_workers = []
        for worker in repo.active_workers(session, self.target_id):
            if (worker.revision_id != revision.revision_id or worker.active_slot_key is None
                    or worker.slot_index >= min(target.target_limit, group.capacity_limit)
                    or worker.state in {"stopped", "stopping", "draining", "lost", "error"}):
                continue
            observed = runtime if runtime is not None and runtime.session_id == worker.session_id else worker
            if self._worker_available(observed, now):
                available_workers.append(worker)
        if not self._worker_available(runtime, now) or not available_workers:
            return "unavailable", False, None, unknown_id
        own_held = [call for call in calls if call.state == "held" and call not in unknown
                    and call.task_id in own_tasks]
        return ("running" if own_held else "ready"), True, own_held[0].task_id if own_held else None, unknown_id

    def status(self):
        from backend.service.asr.target_status import TargetStatusService

        configured = remote_asr_configured(self.settings)
        spec = remote_asr_model_spec(self.settings) if configured else None
        fields = dict(configured=configured, observed_at=datetime.now(timezone.utc),
                      model_id=spec.model_id if spec else None,
                      model_version=spec.model_version if spec else None,
                      version_info=spec.version_info if spec else None,
                      capacity=TargetStatusService(self.database, self.settings).status())
        # 即使移除连接配置，既有未确认事实仍必须可见。
        with self.database.session_factory() as session:
            session.execute(text("BEGIN"))
            self.repository.runtime(session)  # 先读取实际表建立快照，strftime自身不锁定数据库快照。
            now = database_now(session)
            fields["observed_at"] = as_utc(now)
            if self.settings.remote_asr_routing_revision:
                try:
                    self.routing.validate_submission(session)
                    return RemoteAsrStatus(**fields, available=True, state="ready",
                        message="隔离路由调用池已就绪；任务按资源资格排队领取，不代表真实设备已核实。")
                except AppError as exc:
                    return RemoteAsrStatus(**fields, state="unavailable", message=exc.message)
            if not configured:
                active = self.repository.active_calls(session)
                if active:
                    return RemoteAsrStatus(**fields, state="uncertain", uncertain_task_id=active[0].task_id,
                        message="连接配置已移除，但既有远端请求仍须核对，不能自动重发。")
                return RemoteAsrStatus(**fields, state="unconfigured", message="尚未配置外部 ASR 地址与模型标识。")
            state, available, task_id, unknown_id = self._submission_admission(session, now)
            if state == "uncertain":
                return RemoteAsrStatus(**fields, state=state, uncertain_task_id=unknown_id,
                    message="未确认请求已占满可用额度或旧请求缺少目标归属；须先人工核对。")
            if state == "stopping":
                return RemoteAsrStatus(**fields, state=state, current_task_id=task_id, uncertain_task_id=unknown_id,
                                       message="本机调用进程正在停止接单，等待在途请求结束。")
            if not available:
                return RemoteAsrStatus(**fields, state="unavailable", uncertain_task_id=unknown_id,
                    message="外部调用池或绑定槽未就绪、配置不匹配或报告已过期。")
        if state == "running":
            return RemoteAsrStatus(**fields, available=True, state=state, current_task_id=task_id, uncertain_task_id=unknown_id,
                                   message="正在调用外部服务；新任务可排队，领取后不支持取消。")
        try:
            health = probe_remote_asr(self.settings)
            ready = health.ready
        except AppError:
            ready = False
        return RemoteAsrStatus(**fields, available=ready, state="ready" if ready else "unavailable", uncertain_task_id=unknown_id,
            message="外部服务已就绪，仅提供调用能力。" if ready else "外部服务不可达或模型尚未就绪。")

    def ensure_available(self):
        status = self.status()
        if not status.available:
            code = "REMOTE_ASR_RESULT_UNKNOWN" if status.state == "uncertain" else "REMOTE_ASR_UNAVAILABLE"
            raise AppError(code, status.message, 503)

    def validate_submission(self, session, task):
        if self.settings.remote_asr_routing_revision:
            return self.routing.validate_submission(session, task)
        now = database_now(session)
        if not remote_asr_configured(self.settings):
            raise AppError("REMOTE_ASR_NOT_CONFIGURED", "尚未配置外部 ASR 服务。", 503)
        state, available, _, _ = self._submission_admission(session, now)
        if state == "uncertain":
            raise AppError("REMOTE_ASR_RESULT_UNKNOWN", "远端请求未确认，需先人工核对。", 503)
        if not available:
            raise AppError("REMOTE_ASR_UNAVAILABLE", "外部调用 Worker 未就绪或正在停止。", 503)

    def record_submission(self, session, task):
        self.repository.add(session, task_id=task.task_id, profile_id=REMOTE_PROFILE,
            config_fingerprint=self.settings.remote_asr_routing_revision or config_fingerprint(self.settings), request_id=task.task_id,
            state="queued", created_at=database_now(session))
        if self.settings.remote_asr_routing_revision:
            self.routing.record_submission(session, task)

    def routed_task(self, session, task_id):
        return self.routing.repository.get_task_route(session, task_id) is not None

    def matches_task(self, session, task, *, require_selected=False):
        return self.routing.matches_task(session, task, self.target_id, require_selected=require_selected)

    def prepare_claim(self, tx, worker_id):
        if not self.routed_task(tx.session, tx.task.task_id):
            return True
        return self.routing.choose(tx.session, tx.task, worker_id, tx.now, freeze=True)

    def can_claim(self, tx, worker_id):
        row = self.repository.runtime(tx.session)
        if not self.pooled:
            self.targets.observe_worker(tx.session, row)
        if self.routed_task(tx.session, tx.task.task_id):
            return bool(self.targets._eligible(tx.session, worker_id, tx.now)
                and self.routing.choose(tx.session, tx.task, worker_id, tx.now))
        call = self.repository.get(tx.session, tx.task.task_id)
        if call is None or call.config_fingerprint != config_fingerprint(self.settings):
            return False
        return call.state == "queued" and self.targets.can_claim(tx, worker_id)

    def can_receive(self, session, worker_id):
        """在通知领取的同一写事务内阻止停止、旧会话及未知调用继续隐藏通知。"""
        row = self.repository.runtime(session)
        if not self.pooled:
            self.targets.observe_worker(session, row)
        return self.targets.can_receive(session, worker_id, database_now(session))

    def can_receive_task(self, session, task_id, worker_id):
        task = session.get(TaskRecord, task_id)
        # 终态/旧版本通知仍交给原执行器确认；活跃候选按不可变配置预筛。
        if task is None or task.status in {"succeeded", "failed", "cancelled"}:
            return True
        if self.routed_task(session, task_id):
            return self.routing.choose(session, task, worker_id, database_now(session))
        return self.targets.can_claim_task(session, task, worker_id, database_now(session))

    def reserve(self, tx):
        if self.routed_task(tx.session, tx.task.task_id):
            if self.targets._eligible(tx.session, tx.task.lease_worker_id, tx.now) is None:
                raise AppError("ASR_TARGET_CAPACITY_CONFLICT", "目标执行资格已变化，未取得执行许可。", 409)
            self.routing.reserve(tx, self.target_id)
        else:
            self.targets.reserve(tx)
        self.repository.reserve(self.repository.get(tx.session, tx.task.task_id), tx.task, tx.now)

    def finish(self, tx, *, confirmed):
        call = self.repository.get(tx.session, tx.task.task_id)
        if call is not None and call.state in {"held", "queued"}:
            self.repository.finish(call, tx.now, confirmed=confirmed)
            self.targets.finish(tx, confirmed=confirmed)

    def begin_worker(self, session_id):
        # 必须在 Worker 已持有独占文件锁后调用；不清除旧调用占位。
        with self.database.transaction(immediate=True) as session:
            if not self.pooled:
                self.repository.begin_worker(session, session_id, os.getpid(), database_now(session))
            self.targets.bind_in_transaction(session, session_id, os.getpid(), self.slot_index, slot_owned=True)

    def start_pool(self, pool_id, pid):
        """仅由已持有旧global锁的调用池入口使用。"""
        with self.database.transaction(immediate=True) as session:
            if self.settings.remote_asr_routing_revision:
                self.routing.validate_start(session)
            else:
                self.targets._register(session)
            self.repository.begin_worker(session, pool_id, pid, database_now(session))

    def pool_limit(self):
        return self.targets.pool_limit()

    @contextmanager
    def spawn_guard(self, session_id):
        """启动子进程与停止意图在同一SQLite写事务内排序；不借心跳授予接管权。"""
        with self.database.transaction(immediate=True) as session:
            repo = self.targets.repository
            worker = repo.get_worker(session, session_id)
            runtime = self.repository.runtime(session)
            allowed = (worker is not None and worker.active_slot_key is not None
                and worker.state not in {"stopping", "draining", "stopped", "lost"}
                and (runtime is None or runtime.state not in {"stopping", "draining", "stopped"}))
            if allowed:
                revision = repo.get_revision(session, worker.revision_id)
                target = repo.get_target(session, revision.target_id)
                group = repo.get_group(session, target.capacity_group_id)
                allowed = (target.target_id == self.target_id and target.enabled
                    and worker.slot_index == self.slot_index
                    and worker.slot_index < min(target.target_limit, group.capacity_limit)
                    and revision.config_fingerprint == config_fingerprint(self.settings))
            yield allowed

    def begin_drain(self, session_id=None):
        with self.database.transaction(immediate=True) as session:
            self.targets.drain_in_transaction(session, session_id)
            if session_id is None or not self.pooled:
                self.repository.drain(session, session_id)

    def report_worker(self, session_id, state, model_ready, *, active_task_id=None, child_pid=None, last_error=None):
        return self.targets.report_worker(session_id, state, model_ready,
            active_task_id=active_task_id, child_pid=child_pid, last_error=last_error)

    def draining(self, session_id):
        with self.database.session_factory() as session:
            row = self.repository.runtime(session)
            worker = self.targets.repository.get_worker(session, session_id)
            return (worker is None or worker.active_slot_key is None or worker.state in {"stopping", "draining", "stopped", "lost"}
                    or (row is not None and (row.state in {"stopping", "draining"}
                        or (not self.pooled and row.session_id != session_id))))

    def has_uncertain(self):
        with self.database.session_factory() as session:
            return any(row.state == "uncertain" for row in self.repository.active_calls(session))

    def confirm_local_exit(self, worker_session_id):
        with self.database.session_factory() as session:
            task_ids = [call.task_id for call in self.repository.active_calls(session, worker_session_id) if call.state == "held"]
        for task_id in task_ids:
            with ExecutionRepository(self.database).lock_task(task_id) as tx:
                call = self.repository.get(tx.session, task_id)
                if call is None or call.worker_id != worker_session_id or call.state != "held":
                    continue
                self.finish(tx, confirmed=False)
                if tx.task.status == "running":
                    error = FailureInfo(code="REMOTE_ASR_RESULT_UNKNOWN", message="本机调用进程已退出，远端请求结果未确认；不会自动重发。")
                    tx.finish(status="failed", result=None, error=error.model_dump(), fingerprint=None)
                    tx.finish_attempt(outcome="failed", error=error.model_dump())

    def local_shutdown_confirmed(self, pid, started_after):
        with self.database.session_factory() as session:
            row = self.repository.runtime(session)
            active = self.repository.active_calls(session)
            threshold = (datetime.min.replace(tzinfo=timezone.utc) if started_after is None else
                         datetime.fromtimestamp(started_after, timezone.utc) if isinstance(started_after, (int, float)) else as_utc(started_after))
            if any(call.state == "held" for call in active):
                return False
            workers = self.targets.repository.workers_for_process(session, pid, threshold.replace(tzinfo=None))
            if any(worker.state != "stopped" or worker.active_slot_key is not None for worker in workers):
                return False
            # 启动前必须先写入本轮运行行；尚无行时本轮不可能发出请求。
            if row is None:
                return True
            if row.state != "stopped":
                return False
            if as_utc(row.started_at) < threshold:
                return True  # 旧轮已确认退出，新轮尚未取得启动资格。
            return row.pid == pid

    def release_uncertain(self, command: RemoteAsrRelease):
        command = RemoteAsrRelease.model_validate(command)
        if not command.confirm_request_ended:
            raise AppError("REMOTE_ASR_CONFIRMATION_REQUIRED", "须先核对远端请求已结束，再明确确认。", 409)
        with self.database.transaction(immediate=True) as session:
            call = self.repository.get(session, command.task_id)
            task = session.get(TaskRecord, command.task_id)
            if call is None:
                raise AppError("REMOTE_ASR_CALL_NOT_FOUND", "没有该外部调用记录。", 404)
            if call.state == "released" and call.resolved_at is not None:
                return {"taskId": command.task_id, "released": True, "replayed": True}
            row = self.repository.runtime(session)
            worker = self.targets.repository.get_worker(session, call.worker_id) if call.worker_id else None
            if (call.state != "uncertain" or task.status != "failed"
                    or (worker is not None and worker.active_task_id == command.task_id and worker.state != "stopped")
                    or (row is not None and row.session_id == call.worker_id
                        and row.active_task_id == command.task_id and row.state != "stopped")):
                raise AppError("REMOTE_ASR_RELEASE_CONFLICT", "须先确认本机对应调用已结束，且记录处于未确认状态。", 409)
            self.repository.resolve(call, database_now(session), command.note)
            self.targets.resolve(session, command.task_id, database_now(session))
            return {"taskId": command.task_id, "released": True, "replayed": False}
