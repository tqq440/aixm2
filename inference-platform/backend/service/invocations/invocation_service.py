"""公共执行内核：短事务准入与结算，协议发送由后续适配器持有租约调用。"""

from contextlib import contextmanager
from datetime import timedelta
from time import monotonic, sleep
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from backend.common.exceptions.app_error import AppError
from backend.data.catalog.catalog_models import InferenceResourceGroup, InferenceService, InferenceServiceRevision, InferenceTargetCheck
from backend.data.database.database import database_now
from backend.data.execution.execution_repository import ExecutionTransaction
from backend.data.invocations.invocation_models import (
    InferenceApplicationBudget, InferenceCpuProfile, InferenceInvocation, InferenceReservationItem, InferenceSchedulerCursor,
)
from backend.data.invocations.invocation_repository import InvocationRepository
from backend.data.llm.online_models import OnlineRequestRecord
from backend.data.tasks.task_models import TaskRecord
from backend.data.queue.queue_repository import QueueRepository
from backend.schema.asr.asr_schema import TranscriptionResult
from backend.schema.invocations.invocation_schema import ApplicationBudget, CpuProfileRegistration, InvocationBinding, InvocationLease, OnlineCompletion
from backend.schema.vision.detection_schema import DetectionResult
from backend.schema.host_node.upstream_schema import RemoteDetectionResult
from backend.config.vision.model_catalog import CLASS_NAMES, MODEL_SHA256
from backend.schema.llm.online_schema import OnlineRequestInput
from backend.service.catalog.catalog_service import ServiceCatalog
from backend.service.invocations.target_policy import select_target, target_identity
from backend.utils.json.json_utils import canonical_json_sha256
from backend.utils.time.time_utils import as_utc


class InvocationService:
    def __init__(self, database, settings, *, queue_gate=None):
        self.database, self.settings = database, settings
        self.repository = InvocationRepository()
        self.queue_gate = queue_gate

    def _check_queue(self):
        if self.settings.common_queue_backend == "redis":
            if self.queue_gate is None:
                from backend.service.queue.redis_gate import RedisGate
                self.queue_gate = RedisGate(self.database, self.settings)
            self.queue_gate.check()

    @contextmanager
    def _transaction(self, write=False):
        if not self.settings.common_execution_enabled:
            raise AppError("COMMON_EXECUTION_DISABLED", "公共执行模块未启用，当前任务继续使用原执行链路。", 503)
        try:
            with self.database.transaction(immediate=write) as session:
                yield session
        except SQLAlchemyError:
            raise AppError("COMMON_EXECUTION_STORAGE_UNAVAILABLE", "公共执行资料暂不可用，请核对 0016 迁移。", 503) from None

    def configure_application(self, command: ApplicationBudget):
        command = ApplicationBudget.model_validate(command.model_dump())
        with self._transaction(True) as session:
            row = session.get(InferenceApplicationBudget, command.application_id)
            if (row.version if row else None) != command.expected_version:
                raise AppError("APPLICATION_VERSION_CONFLICT", "应用额度已变化，请刷新。", 409)
            if row is None:
                row = InferenceApplicationBudget(application_id=command.application_id, version=1)
                session.add(row)
            else:
                row.version += 1
            row.enabled, row.concurrency_limit = command.enabled, command.concurrency_limit
            row.allowed_services, row.updated_at = list(dict.fromkeys(command.allowed_services)), database_now(session)
            return {"applicationId": row.application_id, "version": row.version, "enabled": row.enabled,
                    "concurrencyLimit": row.concurrency_limit, "allowedServices": row.allowed_services}

    def register_cpu_profile(self, command: CpuProfileRegistration):
        command = CpuProfileRegistration.model_validate(command.model_dump())
        with self._transaction(True) as session:
            revision = session.get(InferenceServiceRevision, command.service_revision)
            if revision is None:
                raise AppError("CATALOG_NOT_FOUND", "未找到目录修订。", 404)
            config = ServiceCatalog._definition(revision)
            target = next((item for item in config.targets if item.target_id == command.target_id), None)
            if (config.kind != "asr" or target is None or target.device_kind != "cpu"
                    or command.profile.target_revision != target_identity(target)):
                raise AppError("CPU_PROFILE_TARGET_MISMATCH", "性能样本必须对应当前修订的 ASR CPU 目标身份。", 422)
            key = (command.service_revision, command.target_id)
            existing = session.get(InferenceCpuProfile, key)
            profile = command.profile.model_dump(mode="json", by_alias=True)
            if existing:
                if existing.profile != profile or existing.cpu_budget_seconds != command.cpu_budget_seconds:
                    raise AppError("CPU_PROFILE_IMMUTABLE", "该修订的性能依据已固定，请新建目录修订后登记。", 409)
            else:
                session.add(InferenceCpuProfile(service_revision=key[0], target_id=key[1], profile=profile,
                    cpu_budget_seconds=command.cpu_budget_seconds, created_at=database_now(session)))
            return {"serviceRevision": key[0], "targetId": key[1], "registered": True}

    def bind(self, command: InvocationBinding):
        command = InvocationBinding.model_validate(command.model_dump())
        with self._transaction(True) as session:
            row = self.bind_in_transaction(session, command)
            return self._view(row)

    def accept_online(self, application_id, service_id, request: OnlineRequestInput):
        """内部接入边界；正文仅用于指纹计算，不持久化，也不在此发起 HTTP。"""
        request = OnlineRequestInput.model_validate(request.model_dump())
        binding = InvocationBinding(application_id=application_id, service_id=service_id,
                                    owner_id=request.online_request_id, owner_kind="online")
        fingerprint = canonical_json_sha256(request.model_dump(mode="json", by_alias=True))
        self._check_queue()
        with self._transaction(True) as session:
            existing = session.get(OnlineRequestRecord, request.online_request_id)
            if existing:
                if existing.caller_id != application_id or existing.request_fingerprint != fingerprint:
                    raise AppError("ONLINE_REQUEST_CONFLICT", "该请求编号已被不同请求使用。", 409)
                row = self.bind_in_transaction(session, binding)
            else:
                now = database_now(session)
                session.add(OnlineRequestRecord(online_request_id=request.online_request_id,
                    caller_id=application_id, model_id=request.model_id, model_version=request.model_version,
                    owner_id="common:" + uuid4().hex[:25], request_fingerprint=fingerprint,
                    state="accepted", permit_state="waiting", accepted_at=now, updated_at=now,
                    deadline_at=now + timedelta(seconds=self.settings.llm_total_timeout_seconds),
                    lease_expires_at=now + timedelta(seconds=self.settings.llm_lease_seconds),
                    output_characters=0, cancel_requested=False, cancellation_confirmed=False))
                session.flush()
                row = self.bind_in_transaction(session, binding)
            return {**self._view(row), "workerId": self._owner(session, row).owner_id}

    def bind_in_transaction(self, session, command):
        """受理方在写原任务的同一事务调用；不创建另一份业务任务或结果。"""
        if not self.settings.common_execution_enabled:
            raise AppError("COMMON_EXECUTION_DISABLED", "公共执行未启用。", 503)
        existing = self.repository.for_owner(session, command.owner_kind, command.owner_id)
        if existing:
            if existing.application_id != command.application_id or existing.service_id != command.service_id:
                raise AppError("INVOCATION_OWNER_CONFLICT", "该请求已绑定其他调用身份。", 409)
            return existing
        owner = session.get(TaskRecord if command.owner_kind == "task" else OnlineRequestRecord, command.owner_id)
        if owner is None or owner.caller_id != command.application_id:
            raise AppError("INVOCATION_OWNER_NOT_FOUND", "未找到属于该应用的请求。", 404)
        if command.owner_kind == "task":
            if owner.status not in {"created", "queued"} or owner.attempt or owner.lease_token:
                raise AppError("INVOCATION_ALREADY_STARTED", "仅能接入尚未执行的任务。", 409)
        elif (owner.state not in {"accepted", "waiting_instance"} or owner.permit_state != "waiting"
              or not owner.owner_id.startswith("common:")):
            raise AppError("INVOCATION_ONLINE_OWNER_REQUIRED", "在线请求须由公共通道创建，不能接管旧通道运行中的生成。", 409)
        application = session.get(InferenceApplicationBudget, command.application_id)
        catalog = session.get(InferenceService, command.service_id)
        if not application or not application.enabled or command.service_id not in application.allowed_services:
            raise AppError("APPLICATION_SERVICE_FORBIDDEN", "应用未获准使用此服务。", 403)
        if not catalog or not catalog.enabled or not catalog.active_revision:
            raise AppError("CATALOG_SERVICE_DISABLED", "服务目录未启用。", 409)
        config = ServiceCatalog._definition(session.get(InferenceServiceRevision, catalog.active_revision))
        kind = owner.task_type if command.owner_kind == "task" else "llm"
        queue_key = "common." + canonical_json_sha256({"service": config.service_id, "model": config.model_id})
        if command.owner_kind == "task" and owner.queue_key != queue_key:
            raise AppError("LEGACY_TASK_NOT_REBOUND", "旧队列任务保留原配置和执行通道，不能重新绑定公共目录。", 409)
        if kind != config.kind or owner.model_id != config.model_id or (config.model_version and owner.model_version != config.model_version):
            raise AppError("INVOCATION_MODEL_MISMATCH", "原请求的模型或类型与目录不兼容。", 422)
        if command.owner_kind == "task" and (owner.input_size_bytes > config.max_file_bytes or (
                kind == "asr" and owner.audio_duration_seconds > config.max_audio_seconds)):
            raise AppError("INVOCATION_INPUT_LIMIT", "输入超出该服务配置的限制。", 422)
        count = session.scalar(select(func.count()).select_from(InferenceInvocation).where(InferenceInvocation.state != "finished")) or 0
        if count >= self.settings.max_inflight_tasks:
            raise AppError("COMMON_ADMISSION_FULL", "公共通道在途请求达到受理上限。", 429)
        now = database_now(session)
        accepted = owner.created_at if kind != "llm" else owner.accepted_at
        deadline = owner.total_deadline_at if kind != "llm" else owner.deadline_at
        deadline = deadline or accepted + timedelta(seconds=self.settings.total_timeout_seconds)
        queue_deadline = (owner.queue_deadline_at or accepted + timedelta(seconds=self.settings.queue_timeout_seconds)) if kind != "llm" else min(
            deadline, accepted + timedelta(seconds=self.settings.llm_admission_wait_seconds))
        if min(queue_deadline, deadline) <= now:
            raise AppError("INVOCATION_DEADLINE_EXPIRED", "原请求期限已到，不重新计时。", 409)
        row = InferenceInvocation(invocation_id=uuid4().hex,
            task_id=owner.task_id if kind != "llm" else None, online_request_id=owner.online_request_id if kind == "llm" else None,
            application_id=command.application_id, service_id=config.service_id, service_revision=catalog.active_revision,
            kind=kind, queue_key=queue_key,
            state="waiting", permit_state="waiting", attempt=0, accepted_at=accepted,
            queue_deadline_at=queue_deadline, deadline_at=deadline, updated_at=now)
        session.add(row); session.flush()
        if QueueRepository.available(session):
            QueueRepository.add(session, row, required=self.settings.common_queue_backend == "redis")
        elif self.settings.common_queue_backend == "redis":
            raise AppError("REDIS_QUEUE_STORAGE_UNAVAILABLE", "Redis 队列需先完成 0017 迁移。", 503)
        self.repository.event(session, row, "invocation.accepted", now, {"serviceRevision": row.service_revision})
        return row

    def _owner(self, session, row):
        return session.get(TaskRecord, row.task_id) if row.task_id else session.get(OnlineRequestRecord, row.online_request_id)

    def _reason(self, session, row, reason, now):
        if row.last_reason != reason:
            row.last_reason, row.updated_at = reason, now
            self.repository.event(session, row, "invocation.waiting_reason", now, {"reason": reason})

    def _reserve(self, session, row, worker_id, *, require_node=False, node_observations=None):
        now = database_now(session)
        if row.state != "waiting":
            return None
        outbox = QueueRepository.for_invocation(session, row.invocation_id) if QueueRepository.available(session) else None
        if outbox and outbox.redis_required and self.settings.common_queue_backend != "redis":
            self._reason(session, row, "REDIS_BACKEND_REQUIRED", now); return None
        if self.settings.common_queue_backend == "redis":
            if outbox is None:
                raise AppError("REDIS_QUEUE_STORAGE_UNAVAILABLE", "公共调用缺少队列 outbox，未执行。", 503)
            outbox.redis_required = True
            if row.kind != "llm" and outbox.received_at is None:
                self._reason(session, row, "WAITING_REDIS_NOTIFICATION", now); return None
        owner = self._owner(session, row)
        if owner is None or owner.caller_id != row.application_id:
            raise AppError("INVOCATION_OWNER_NOT_FOUND", "调用所属请求不一致。", 409)
        if (row.task_id and owner.status not in {"created", "queued"}) or (
                row.online_request_id and (owner.state not in {"accepted", "waiting_instance"} or owner.cancel_requested)):
            row.state, row.permit_state, row.outcome = "finished", "released", "owner_changed"
            row.finished_at, row.updated_at = now, now
            self.repository.event(session, row, "invocation.owner_changed", now)
            return None
        if min(row.deadline_at, row.queue_deadline_at) <= now:
            self._finish(session, row, "expired", True, now)
            return None
        app = session.get(InferenceApplicationBudget, row.application_id)
        catalog = session.get(InferenceService, row.service_id)
        if not app or not app.enabled or row.service_id not in app.allowed_services:
            self._reason(session, row, "APPLICATION_DISABLED", now); return None
        if not catalog or not catalog.enabled:
            self._reason(session, row, "SERVICE_DISABLED", now); return None
        if self.repository.legacy_occupied(session):
            self._reason(session, row, "LEGACY_OCCUPANCY_UNMAPPED", now); return None
        resources = {"platform": self.settings.max_inflight_tasks, "application:" + row.application_id: app.concurrency_limit}
        if any(self.repository.occupied(session, key) >= limit for key, limit in resources.items()):
            self._reason(session, row, "APPLICATION_OR_PLATFORM_FULL", now); return None
        config = ServiceCatalog._definition(session.get(InferenceServiceRevision, row.service_revision))
        checks = {check.target_id: check for check in session.scalars(select(InferenceTargetCheck).where(
            InferenceTargetCheck.revision == row.service_revision))}
        # 同 URL/路径的别名与不同模型共用目标额度，取所有当前声明的较小值。
        limits = {}
        for snapshot in self.repository.active_definitions(session):
            for item in snapshot["targets"]:
                if item["enabled"]:
                    key = canonical_json_sha256({"origin": item["baseUrl"], "path": item["invokePath"]})
                    limits[key] = min(limits.get(key, 64), item["concurrencyLimit"])
        candidates = []
        for target in config.targets:
            if not target.enabled:
                continue
            key = target_identity(target)
            budget = {**resources, "target:" + key: min(target.concurrency_limit, limits.get(key, target.concurrency_limit))}
            groups = [session.get(InferenceResourceGroup, group) for group in target.resource_group_ids]
            reason = "AVAILABLE"
            if any(group is None for group in groups):
                reason = "CAPACITY_UNCONFIRMED"
            for group in groups:
                if group:
                    budget["group:" + group.group_id] = group.config["concurrencyLimit"]
                    if (config.policy != "fixed" or group.config["concurrencyLimit"] > 1) and not group.config.get("capacityEvidence"):
                        reason = "CAPACITY_UNCONFIRMED"
            check = checks.get(target.target_id)
            observation=(node_observations or {}).get(canonical_json_sha256(target.node.model_dump(mode="json",by_alias=True))) if target.node else None
            fresh_node=bool(observation and 0<=(as_utc(now)-as_utc(observation["checkedAt"])).total_seconds()<15)
            node_ready=bool(fresh_node and observation["nodeId"]==target.node.node_id and observation["serviceId"]==target.node.service_id
                and observation["configRevision"]==target.node.config_revision and observation["kind"]==config.kind
                and observation["modelId"]==config.model_id and observation["acceptingRequests"] and observation["adapterConfigured"]
                and observation.get("invocationMode") in (None,config.invocation_mode)
                and config.protocol in observation["protocols"])
            if node_ready:
                pass  # 新鲜的 Node 生命周期门禁替代旧手动 health 的运行时保鲜，不代表设备证明。
            elif not check or (now - check.checked_at).total_seconds() < 0 or now - check.checked_at > timedelta(minutes=5):
                reason = "HEALTH_UNCONFIRMED"
            elif not check.success:
                reason = "HEALTH_UNAVAILABLE"
            if config.policy != "fixed" and (target.device_kind == "unknown" or not target.device_evidence
                    or not target.compatibility_evidence or not target.capacity_evidence or not groups):
                reason = "DEVICE_OR_CAPACITY_UNCONFIRMED"
            if target.concurrency_limit > 1 and not target.capacity_evidence:
                reason = "CAPACITY_UNCONFIRMED"
            if reason == "AVAILABLE" and any(self.repository.occupied(session, resource) >= limit for resource, limit in budget.items()):
                reason = "CAPACITY_FULL"
            if require_node and target.node is None:reason="NODE_BINDING_REQUIRED"
            elif target.node and node_observations is not None and not node_ready:
                reason="NODE_NOT_READY" if fresh_node else "NODE_OBSERVATION_STALE"
            candidates.append({"target": target, "key": key, "resources": budget, "reason": reason,
                "group_limits": [group.config["concurrencyLimit"] for group in groups if group],
                "occupied": self.repository.occupied(session, "target:" + key)})
        profiles = {profile.target_id: profile for profile in session.scalars(select(InferenceCpuProfile).where(
            InferenceCpuProfile.service_revision == row.service_revision))}
        selected, reason = select_target(config, candidates, task=owner if row.task_id else None, invocation=row,
                                         now=now, profiles=profiles, attempt_budget=self.settings.attempt_timeout_seconds)
        if selected is None:
            self._reason(session, row, reason, now); return None
        token = uuid4().hex
        row.state, row.permit_state, row.attempt = "reserved", "held", 1
        row.worker_id, row.lease_token = worker_id, token
        row.selected_target_id, row.selected_target_key, row.last_reason = selected["target"].target_id, selected["key"], reason
        row.deadline_at = min(row.deadline_at, now + timedelta(seconds=min(
            self.settings.attempt_timeout_seconds, selected["target"].total_timeout_seconds)))
        row.lease_expires_at = min(row.deadline_at, now + timedelta(seconds=self.settings.lease_seconds))
        row.updated_at = now
        for key, limit in selected["resources"].items():
            session.add(InferenceReservationItem(invocation_id=row.invocation_id, resource_key=key, limit_snapshot=limit))
        if row.task_id:
            ExecutionTransaction(session, now, owner, None).start(worker_id=worker_id, lease_token=token,
                expires_at=row.lease_expires_at, attempt_deadline_at=row.deadline_at)
        else:
            owner.state, owner.permit_state, owner.instance_id = "streaming", "held", row.selected_target_key
            owner.lease_expires_at, owner.updated_at = row.lease_expires_at, now
        self.repository.event(session, row, "invocation.target_selected", now, {"targetId": row.selected_target_id,
            "assignedDevice": selected["target"].device_kind, "assignedDeviceId": selected["target"].device_id,
            "actualDevice": None, "reason": reason, "serviceRevision": row.service_revision})
        self.repository.event(session, row, "invocation.capacity_reserved", now, {"resourceCount": len(selected["resources"]),
            "resources": [{"resourceKey": key, "concurrencyLimit": value, "units": 1}
                          for key, value in sorted(selected["resources"].items())]})
        session.flush()
        return InvocationLease(invocation_id=row.invocation_id, lease_token=token, worker_id=worker_id, attempt=1,
                               service_revision=row.service_revision, target_id=row.selected_target_id, deadline_at=row.deadline_at)

    def claim_round(self, worker_id, *, limit=20, require_node=False, node_observations=None):
        if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 64 or not 1 <= limit <= 20:
            raise ValueError("执行器编号或轮询上限无效")
        leases = []
        self._check_queue()
        with self._transaction(True) as session:
            cursor = session.get(InferenceSchedulerCursor, "offline")
            if cursor is None:
                cursor = InferenceSchedulerCursor(cursor_id="offline", last_queue_key=None); session.add(cursor)
            rows = self.repository.waiting(session, self.settings.max_inflight_tasks)
            queues = sorted({row.queue_key for row in rows})
            queues = [key for key in queues if key > (cursor.last_queue_key or "")] + [key for key in queues if key <= (cursor.last_queue_key or "")]
            for key in queues:
                for row in (row for row in rows if row.queue_key == key):
                    lease = self._reserve(session, row, worker_id,require_node=require_node,node_observations=node_observations)
                    if lease:
                        leases.append(lease); cursor.last_queue_key = key; break
                if len(leases) >= limit:
                    break
        return leases

    def claim_online(self, invocation_id, worker_id, *, require_node=False, node_observations=None):
        self._check_queue()
        with self._transaction(True) as session:
            row = session.get(InferenceInvocation, invocation_id)
            if row is None or row.kind != "llm" or self._owner(session, row).owner_id != worker_id:
                raise AppError("ONLINE_EXECUTOR_MISMATCH", "在线执行者身份不匹配。", 409)
            return self._reserve(session, row, worker_id,require_node=require_node,node_observations=node_observations)

    def wait_online(self, invocation_id, worker_id):
        end = monotonic() + self.settings.llm_admission_wait_seconds
        while True:
            lease = self.claim_online(invocation_id, worker_id)
            if lease or monotonic() >= end:
                return lease
            sleep(min(0.05, max(0, end - monotonic())))  # 不持数据库事务等待。

    def _lease(self, session, lease, *, live=True):
        row = session.get(InferenceInvocation, lease.invocation_id)
        if row is None or (row.worker_id, row.lease_token, row.attempt, row.service_revision, row.selected_target_id) != (
                lease.worker_id, lease.lease_token, lease.attempt, lease.service_revision, lease.target_id):
            return None
        now = database_now(session)
        if live and (row.state not in {"reserved", "sent"} or row.lease_expires_at <= now or row.deadline_at <= now):
            return None
        return row

    def mark_sent(self, lease: InvocationLease):
        lease = InvocationLease.model_validate(lease.model_dump())
        with self._transaction() as session:
            if self._lease(session, lease) is None:
                return False
            outbox = QueueRepository.for_invocation(session, lease.invocation_id) if QueueRepository.available(session) else None
            if outbox and outbox.redis_required and self.settings.common_queue_backend != "redis":
                raise AppError("REDIS_BACKEND_REQUIRED", "调用已绑定 Redis 通道，不能通过切回 SQLite 绕过。", 503)
        self._check_queue()
        with self._transaction(True) as session:
            row = self._lease(session, lease)
            if row is None or row.state != "reserved":
                return False
            owner = self._owner(session, row)
            if (row.task_id and owner.status == "cancel_requested") or (row.online_request_id and owner.cancel_requested):
                if row.state == "reserved":
                    self._finish(session, row, "cancelled", True, database_now(session))
                return False
            if row.state == "reserved":
                now = database_now(session)
                row.state, row.sent_at, row.updated_at = "sent", now, now
                self.repository.event(session, row, "invocation.send_started", now)
            return True

    def renew(self, lease):
        with self._transaction(True) as session:
            row = self._lease(session, lease)
            if row is None:
                return False
            now = database_now(session)
            row.lease_expires_at = min(row.deadline_at, now + timedelta(seconds=self.settings.lease_seconds))
            self._owner(session, row).lease_expires_at = row.lease_expires_at
            return True

    def dispatch(self, lease, sender):
        """适配器回调边界：HTTP 在事务外；长调用由 sender 通过 renew 保活。"""
        with self._transaction() as session:
            row = self._lease(session, lease)
            if row is None:
                return False
            config = ServiceCatalog._definition(session.get(InferenceServiceRevision, row.service_revision))
            target = next(item for item in config.targets if item.target_id == row.selected_target_id)
        if not self.mark_sent(lease):
            return False
        try:
            result = sender(config, target, lease)
            if self.complete(lease, result):
                return True
        except Exception:
            # 不保存任意异常正文（可能含连接秘密）；已发送的调用保留未知占位。
            self.fail(lease)
            raise AppError("COMMON_UPSTREAM_UNCONFIRMED", "上游调用未取得可确认结果；保留占位，不自动重发。", 502) from None
        self.fail(lease)
        return False

    def _finish(self, session, row, outcome, confirmed, now, result=None):
        previous = row.permit_state
        row.state, row.outcome, row.finished_at, row.updated_at = "finished", outcome, now, now
        row.permit_state = "released" if confirmed or row.sent_at is None else "uncertain"
        owner = self._owner(session, row)
        success = outcome == "succeeded"
        error = None if success or outcome == "cancelled" else {"code": "COMMON_" + outcome.upper(),
            "message": "公共调用未能完成；结果未知时不会自动重发。"}
        if row.task_id:
            tx = ExecutionTransaction(session, now, owner, None)
            tx.finish(status="succeeded" if success else "cancelled" if outcome == "cancelled" else "failed",
                result=result if success else None, error=error, fingerprint=row.result_fingerprint)
            tx.finish_attempt(outcome=outcome)
        else:
            owner.state = "completed" if success else "cancelled" if outcome == "cancelled" else "failed"
            owner.permit_state, owner.ended_at, owner.updated_at, owner.error_json = row.permit_state, now, now, error
            owner.cancellation_confirmed = outcome == "cancelled" and row.permit_state == "released"
        self.repository.event(session, row, "invocation.finished", now, {"outcome": outcome, "permitState": row.permit_state})
        if previous == "held" and row.permit_state == "released":
            self.repository.event(session, row, "invocation.capacity_released", now)

    def complete(self, lease, result):
        if not isinstance(result, (TranscriptionResult, DetectionResult, RemoteDetectionResult, OnlineCompletion)):
            raise ValueError("完成提交需要经过类型校验的结果，不接受任意 dict")
        result = type(result).model_validate(result.model_dump())
        payload = result.model_dump(mode="json", by_alias=True)
        fingerprint = canonical_json_sha256(payload)
        with self._transaction(True) as session:
            return self.complete_in_transaction(session, lease, result, payload, fingerprint)

    def complete_in_transaction(self, session, lease, result, payload, fingerprint):
        """供 Node 结算复用，调用者持有同一 SQLite 写事务。"""
        row = self._lease(session, lease, live=False)
        if row is None:
            return False
        if row.state == "finished":
            return row.outcome == "succeeded" and row.result_fingerprint == fingerprint
        if self._lease(session, lease) is None or row.state != "sent":
            return False
        owner = self._owner(session, row)
        if not self.result_matches(row, owner, result):
            return False
        row.result_fingerprint = fingerprint
        if row.kind == "llm":
            owner.output_characters, owner.finish_reason = result.output_characters, result.finish_reason
        self.repository.event(session,row,"invocation.result_saved",database_now(session),{"resultFingerprint":fingerprint})
        self._finish(session, row, "succeeded", True, database_now(session), payload)
        return True

    @staticmethod
    def result_matches(row, owner, result):
        """普通与迟到结果使用相同业务校验，不放松旧模型指纹。"""
        if row.kind == "asr":
            if (not isinstance(result, TranscriptionResult)
                    or result.language_requested != owner.parameters.get("language")
                    or abs(result.audio_duration_seconds - owner.audio_duration_seconds) > 1 / 16000):
                return False
            if any(segment.end > owner.audio_duration_seconds + 1 / 16000 for segment in result.segments):
                return False
        elif row.kind == "vision":
            if isinstance(result, RemoteDetectionResult):
                return ((result.image_width,result.image_height)==(owner.image_width,owner.image_height)
                    and result.model_id==owner.model_id and result.model_version in (None,owner.model_version)
                    and len(result.detections)<=owner.parameters["max_detections"]
                    and all(d.confidence>owner.parameters["confidence_threshold"] for d in result.detections))
            if (not isinstance(result, DetectionResult) or (result.image_width, result.image_height) != (owner.image_width, owner.image_height)
                    or result.model_id != owner.model_id or result.model_version != owner.model_version):
                return False
            if (result.model_sha256 != MODEL_SHA256 or len(result.detections) > owner.parameters["max_detections"]
                    or any(item.class_name != CLASS_NAMES[item.class_id]
                        or item.confidence <= owner.parameters["confidence_threshold"] for item in result.detections)):
                return False
        elif not isinstance(result, OnlineCompletion):
            return False
        return True

    def fail(self, lease, *, confirmed_not_sent=False, confirmed_terminal=False):
        with self._transaction(True) as session:
            row = self._lease(session, lease)
            if row is None:
                return False
            if confirmed_not_sent and row.sent_at is not None:
                raise AppError("SEND_ALREADY_STARTED", "发送阶段已开始，不能按未发送释放。", 409)
            self._finish(session, row, "not_sent" if row.sent_at is None else "failed" if confirmed_terminal else "unknown",
                         row.sent_at is None or confirmed_terminal, database_now(session))
            return True

    def cancel_in_transaction(self, session, row):
        if row.state == "finished":
            return "already_finished"
        now = database_now(session)
        if row.state in {"waiting", "reserved"}:
            self._finish(session, row, "cancelled", True, now)
            return "cancelled"
        owner = self._owner(session, row)
        if row.task_id:
            owner.status, owner.updated_at = "cancel_requested", now
        else:
            owner.cancel_requested, owner.updated_at = True, now
        self._reason(session, row, "CANCEL_NOT_CONFIRMED", now)
        return "cancel_requested"

    def recover(self):
        count = 0
        with self._transaction(True) as session:
            now = database_now(session)
            for row in self.repository.due(session, now, 20):
                self._finish(session, row, "unknown" if row.sent_at else "expired", row.sent_at is None, now)
                count += 1
        return count

    def resolve_unknown(self, invocation_id, application_id, *, outcome, evidence):
        if outcome not in {"succeeded", "failed", "cancelled"} or not isinstance(evidence, str) or not 1 <= len(evidence.strip()) <= 2048:
            raise ValueError("核对需要明确远端终态和有界证据说明")
        with self._transaction(True) as session:
            row = session.get(InferenceInvocation, invocation_id)
            if row is None or row.application_id != application_id:
                raise AppError("INVOCATION_NOT_FOUND", "未找到应用所属调用。", 404)
            if row.resolved_outcome:
                if row.resolved_outcome != outcome:
                    raise AppError("REMOTE_TERMINAL_CONFLICT", "核对终态冲突，未覆盖原记录。", 409)
                return self._view(row)
            if row.state != "finished" or row.permit_state != "uncertain":
                raise AppError("INVOCATION_NOT_UNCERTAIN", "仅能核对已结束且占位未知的调用。", 409)
            row.resolved_outcome, row.permit_state = outcome, "released"
            if row.online_request_id:
                self._owner(session, row).permit_state = "released"
            now = database_now(session)
            # 不写证据原文，避免维护者误粘贴请求正文或秘密。
            self.repository.event(session, row, "invocation.late_terminal_confirmed", now,
                {"remoteOutcome": outcome, "evidenceHash": canonical_json_sha256(evidence)})
            self.repository.event(session, row, "invocation.capacity_released", now)
            return self._view(row)

    @staticmethod
    def _view(row):
        return {"invocationId": row.invocation_id, "taskId": row.task_id, "onlineRequestId": row.online_request_id,
            "applicationId": row.application_id, "serviceId": row.service_id, "serviceRevision": row.service_revision,
            "kind": row.kind, "state": row.state, "permitState": row.permit_state, "targetId": row.selected_target_id,
            "attempt": row.attempt, "reason": row.last_reason, "outcome": row.outcome, "resolvedOutcome": row.resolved_outcome,
            "acceptedAt": as_utc(row.accepted_at), "deadlineAt": as_utc(row.deadline_at),
            "finishedAt": as_utc(row.finished_at) if row.finished_at else None}

    def get(self, invocation_id):
        with self._transaction() as session:
            row = session.get(InferenceInvocation, invocation_id)
            if row is None:
                raise AppError("INVOCATION_NOT_FOUND", "未找到调用。", 404)
            return self._view(row)

    def capacity(self):
        with self._transaction() as session:
            limits = {"platform": self.settings.max_inflight_tasks}
            for app in session.scalars(select(InferenceApplicationBudget)):
                limits["application:" + app.application_id] = app.concurrency_limit
            for group in session.scalars(select(InferenceResourceGroup)):
                limits["group:" + group.group_id] = group.config["concurrencyLimit"]
            for config in self.repository.active_definitions(session):
                for target in config["targets"]:
                    if target["enabled"]:
                        key = "target:" + canonical_json_sha256({"origin": target["baseUrl"], "path": target["invokePath"]})
                        limits[key] = min(limits.get(key, 64), target["concurrencyLimit"])
            rows = list(session.scalars(select(InferenceInvocation).where(
                InferenceInvocation.permit_state.in_(("held", "uncertain")))))
            counters = {key: {"held": 0, "uncertain": 0} for key in limits}
            for row in rows:
                for item in session.scalars(select(InferenceReservationItem).where(
                        InferenceReservationItem.invocation_id == row.invocation_id)):
                    counts = counters.setdefault(item.resource_key, {"held": 0, "uncertain": 0})
                    counts[row.permit_state] += 1
                    limits.setdefault(item.resource_key, item.limit_snapshot)
            return {"resources": [{"resourceKey": key, "concurrencyLimit": limits[key], **value} for key, value in sorted(counters.items())],
                    "legacyOccupancyUnmapped": self.repository.legacy_occupied(session),
                    "notice": "平台许可账本；不表示真实 CPU/GPU 利用率或模型加载状态。"}

    def events(self, owner_kind, owner_id, query, *, application_id):
        with self._transaction() as session:
            owner = session.get(TaskRecord if owner_kind == "task" else OnlineRequestRecord, owner_id)
            if owner is None or owner.caller_id != application_id:
                raise AppError("INVOCATION_OWNER_NOT_FOUND", "未找到应用所属请求。", 404)
            events = self.repository.events(session, owner_kind, owner_id, query.after_sequence, query.limit)
            visible = events[:query.limit]
            return {"items": [{"sequence": item.sequence, "eventId": item.event_id, "invocationId": item.invocation_id,
                "eventType": item.event_type, "source": item.source, "occurredAt": as_utc(item.occurred_at), "details": item.details}
                for item in visible], "hasMore": len(events) > query.limit,
                "nextSequence": visible[-1].sequence if visible else query.after_sequence,
                "historyNotice": "详细事件从启用本模块后开始记录，旧过程不补造。"}
