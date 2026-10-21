"""M05 执行许可证：通知领取、任务执行权和最终结果分别裁决。"""

from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from pydantic import ValidationError

from backend.common.exceptions.app_error import AppError
from backend.service.asr.adapter_catalog import configured_asr_plugin
from backend.service.agent.asr_gate import AsrGate
from backend.service.asr.remote_service import RemoteAsrService, CONFIRMED_FAILURES
from backend.config.vision.model_catalog import (CLASS_NAMES, MODEL_ID as VISION_MODEL_ID,
    MODEL_SHA256, MODEL_VERSION as VISION_MODEL_VERSION, QUEUE_KEY as VISION_QUEUE_KEY)
from backend.config.settings.app_settings import AppSettings
from backend.data.database.database import Database
from backend.data.execution.execution_repository import ExecutionRepository, ExecutionTransaction
from backend.data.tasks.task_models import TaskRecord
from backend.data.invocations.invocation_repository import InvocationRepository
from backend.service.invocations.invocation_service import InvocationService
from backend.schema.asr.asr_schema import TranscriptionResult
from backend.schema.vision.detection_schema import DetectionResult
from backend.schema.execution.execution_schema import (
    AcquireDecision, Delivery, ExecutionInput, ExecutionLease, FailureInfo, VisionExecutionInput,
)
from backend.service.recovery.failure_policy import FailurePolicy, TERMINAL_STATES, WAITING_STATES, waiting_timeout


def failure_fingerprint(error: FailureInfo) -> str:
    return hashlib.sha256(json.dumps(
        {"status": "failed", "error": error.model_dump(mode="json", by_alias=True)}, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()


class ExecutionService:
    def __init__(self, database: Database, settings: AppSettings, *, task_type: str = "asr", asr_profile: str = "local",
                 remote_target_id="remote-default", remote_slot_index=0, remote_pooled=False):
        if task_type not in {"asr", "vision"}:
            raise ValueError("执行器只支持 asr 或 vision")
        self.repository = ExecutionRepository(database)
        self.settings = settings
        self.task_type = task_type
        if asr_profile not in {"local", "remote"} or (task_type == "vision" and asr_profile != "local"):
            raise ValueError("执行通道无效")
        self.remote = task_type == "asr" and asr_profile == "remote"
        self.remote_service = RemoteAsrService(database, settings, target_id=remote_target_id,
                                               slot_index=remote_slot_index, pooled=remote_pooled)
        self.asr_plugin = configured_asr_plugin(settings, asr_profile) if task_type == "asr" else None
        self.asr_spec = self.asr_plugin.model_spec() if self.asr_plugin is not None else None
        self.queue_key = VISION_QUEUE_KEY if task_type == "vision" else self.asr_plugin.queue_key
        self.failure_policy = FailurePolicy(settings)
        self.asr_gate = AsrGate(database, settings) if task_type == "asr" and not self.remote else None

    def acquire(self, delivery: Delivery) -> AcquireDecision:
        delivery = Delivery.model_validate(delivery.model_dump())
        rejected = AcquireDecision(decision="terminal_or_stale")
        busy = AcquireDecision(decision="busy")
        with self.repository.lock_task(delivery.task_id, notification_id=delivery.notification_id) as tx:
            if self.asr_gate is not None and not self.asr_gate.can_claim(tx.session, delivery.worker_id):
                return busy
            task, notification = tx.task, tx.notification
            if notification is None:
                return busy
            if not (
                notification.status == "inflight"
                and notification.delivery_owner == delivery.worker_id
                and notification.delivery_token == delivery.delivery_token
                and notification.visible_at > tx.now
                and notification.task_id == delivery.task_id
                and notification.dispatch_version == delivery.dispatch_version
                and notification.execution_plan_version == delivery.execution_plan_version
                and notification.queue_key == delivery.queue_key
            ):
                # 消息领取权失效不等于任务完成，不能让 Worker 据此确认尚待执行的通知。
                return busy
            if task is None or task.status in TERMINAL_STATES:
                return rejected
            if InvocationRepository.available(tx.session):
                if InvocationRepository.for_owner(tx.session, "task", task.task_id):
                    return busy
                if InvocationRepository.common_occupied(tx.session):
                    return busy
            if (task.dispatch_version != delivery.dispatch_version
                    or task.execution_plan_version != delivery.execution_plan_version
                    or task.queue_key != delivery.queue_key):
                return rejected
            if task.status in {"running", "cancel_requested"}:
                # 由 M08 复核恢复后生成新版本事件；M12 单 Worker 锁独立保证本机槽位。
                return busy
            expired = waiting_timeout(tx)
            if expired is not None:
                tx.finish(status="failed", result=None, error=expired.model_dump(), fingerprint=None)
                if self.remote:
                    self.remote_service.finish(tx, confirmed=True)
                return rejected
            previous_attempt = tx.find_attempt(task.lease_token) if task.lease_token else None
            permitted_attempt = task.attempt == 0 or (
                previous_attempt is not None and previous_attempt.outcome == "retry_wait"
                and previous_attempt.dispatch_version < task.dispatch_version
            )
            routed = self.remote and self.remote_service.routed_task(tx.session, task.task_id)
            model_matches = (self.remote_service.matches_task(tx.session, task) if routed else (
                task.model_id == (VISION_MODEL_ID if self.task_type == "vision" else self.asr_spec.model_id)
                and task.model_version == (VISION_MODEL_VERSION if self.task_type == "vision" else self.asr_spec.model_version)
            ))
            if not (
                task.status == "queued" and task.attempt < task.max_attempts and permitted_attempt
                and task.task_type == self.task_type
                and model_matches
                and task.queue_key == self.queue_key
            ):
                return busy
            try:
                reference = dict(task_id=task.task_id, input_ref=task.input_ref,
                                 input_sha256=task.input_sha256, input_size_bytes=task.input_size_bytes,
                                 model_id=task.model_id, model_version=task.model_version)
                if routed:
                    # 逻辑任务身份保持；实际子进程只接收本槽冻结连接的模型身份。
                    reference.update(model_id=self.asr_spec.model_id, model_version=self.asr_spec.model_version)
                if self.task_type == "vision":
                    if task.input_media_type != "image":
                        return busy
                    execution_input = VisionExecutionInput(**reference, image_width=task.image_width,
                                                            image_height=task.image_height, **task.parameters)
                else:
                    if task.input_media_type != "audio":
                        return busy
                    execution_input = ExecutionInput(**reference, language=task.parameters.get("language"))
                    if (execution_input.language is not None
                            and execution_input.language not in self.asr_spec.parameter_schema["languages"]):
                        return busy
            except (ValidationError, TypeError):
                return busy
            if self.remote and not self.remote_service.can_claim(tx, delivery.worker_id):
                return busy
            if self.remote and not self.remote_service.prepare_claim(tx, delivery.worker_id):
                return busy
            budget = self.settings.attempt_timeout_seconds
            if self.remote:
                budget = min(budget, self.settings.remote_asr_total_timeout_seconds)
            attempt_deadline = tx.now + timedelta(seconds=budget)
            deadlines = [tx.now + timedelta(seconds=self.settings.lease_seconds), attempt_deadline]
            if task.total_deadline_at is not None:
                deadlines.append(task.total_deadline_at)
            tx.start(worker_id=delivery.worker_id, lease_token=uuid4().hex,
                     expires_at=min(deadlines),
                     attempt_deadline_at=attempt_deadline)
            if self.remote:
                self.remote_service.reserve(tx)
            lease = ExecutionLease(
                task_id=task.task_id, worker_id=task.lease_worker_id,
                lease_token=task.lease_token, attempt=task.attempt,
                dispatch_version=task.dispatch_version,
                execution_plan_version=task.execution_plan_version,
                lease_expires_at=task.lease_expires_at,
                attempt_budget_seconds=(min(deadlines[1:]) - tx.now).total_seconds(),
            )
            return AcquireDecision(decision="acquired", lease=lease, input=execution_input)

    @staticmethod
    def _same_execution(task: TaskRecord | None, lease: ExecutionLease) -> bool:
        return task is not None and (
            task.lease_worker_id == lease.worker_id and task.lease_token == lease.lease_token
            and task.attempt == lease.attempt and task.dispatch_version == lease.dispatch_version
            and task.execution_plan_version == lease.execution_plan_version
        )

    def renew(self, lease: ExecutionLease) -> bool:
        lease = ExecutionLease.model_validate(lease.model_dump())
        with self.repository.lock_task(lease.task_id) as tx:
            if self._control(tx, lease) != "continue":
                return False
            deadlines = [tx.now + timedelta(seconds=self.settings.lease_seconds)]
            deadlines.extend(value for value in (tx.task.attempt_deadline_at, tx.task.total_deadline_at) if value is not None)
            tx.renew(expires_at=min(deadlines))
            return True

    def control(self, lease: ExecutionLease) -> str:
        lease = ExecutionLease.model_validate(lease.model_dump())
        with self.repository.lock_task(lease.task_id) as tx:
            return self._control(tx, lease)

    def _control(self, tx: ExecutionTransaction, lease: ExecutionLease) -> str:
        task = tx.task
        if not self._same_execution(task, lease) or task.task_type != self.task_type:
            return "expired"
        if task.status == "cancel_requested":
            return "cancel"
        if task.status != "running":
            return "expired"
        if task.total_deadline_at is not None and tx.now >= task.total_deadline_at:
            return "total_timeout"
        if task.attempt_deadline_at is not None and tx.now >= task.attempt_deadline_at:
            return "attempt_timeout"
        if task.lease_expires_at is None or task.lease_expires_at <= tx.now:
            return "expired"
        return "continue"

    def commit_result(self, lease: ExecutionLease, result: TranscriptionResult | DetectionResult) -> bool:
        lease = ExecutionLease.model_validate(lease.model_dump())
        result_type = DetectionResult if self.task_type == "vision" else TranscriptionResult
        if not isinstance(result, result_type):
            return False
        result = result_type.model_validate(result.model_dump())
        payload = result.model_dump(mode="json", by_alias=True)
        fingerprint = hashlib.sha256(json.dumps(
            {"status": "succeeded", "result": payload}, sort_keys=True,
            separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")).hexdigest()
        with self.repository.lock_task(lease.task_id) as tx:
            task = tx.task
            if not self._same_execution(task, lease) or task.task_type != self.task_type:
                return False
            if task.status == "succeeded":
                if (self.remote and self.remote_service.routed_task(tx.session, task.task_id)
                        and not self.remote_service.matches_task(tx.session, task, require_selected=True)):
                    return False
                return task.result_fingerprint == fingerprint and task.result_json == payload
            if self._control(tx, lease) != "continue":
                return False
            if not self._result_matches_task(task, result, session=tx.session):
                return False
            tx.finish(status="succeeded", result=payload, error=None, fingerprint=fingerprint)
            tx.finish_attempt(outcome="succeeded")
            if self.remote:
                self.remote_service.finish(tx, confirmed=True)
            return True

    def _result_matches_task(self, task: TaskRecord, result, *, session=None) -> bool:
        if self.task_type == "vision":
            return (
                task.model_id == result.model_id == VISION_MODEL_ID
                and task.model_version == result.model_version == VISION_MODEL_VERSION
                and result.model_sha256 == MODEL_SHA256
                and task.input_media_type == "image"
                and (result.image_width, result.image_height) == (task.image_width, task.image_height)
                and len(result.detections) <= task.parameters["max_detections"]
                and all(item.class_name == CLASS_NAMES[item.class_id]
                        and item.confidence > task.parameters["confidence_threshold"] for item in result.detections)
            )
        routed = self.remote and session is not None and self.remote_service.routed_task(session, task.task_id)
        model_matches = (self.remote_service.matches_task(session, task, require_selected=True) if routed else (
            task.model_id == self.asr_spec.model_id and task.model_version == self.asr_spec.model_version
        ))
        return (
            model_matches
            and self.asr_plugin.result_matches(self.settings, result)
            and result.language_requested == task.parameters.get("language")
            and abs(result.audio_duration_seconds - task.audio_duration_seconds) <= 1 / 16000
        )

    def commit_failure(self, lease: ExecutionLease, error: FailureInfo) -> bool:
        lease = ExecutionLease.model_validate(lease.model_dump())
        error = FailureInfo.model_validate(error.model_dump())
        payload = error.model_dump(mode="json", by_alias=True)
        fingerprint = failure_fingerprint(error)
        with self.repository.lock_task(lease.task_id) as tx:
            task = tx.task
            if task is None or task.task_type != self.task_type:
                return False
            attempt = tx.find_attempt(lease.lease_token)
            if attempt is not None and attempt.finished_at is not None:
                return (
                    attempt.task_id == lease.task_id and attempt.worker_id == lease.worker_id
                    and attempt.attempt == lease.attempt and attempt.dispatch_version == lease.dispatch_version
                    and attempt.execution_plan_version == lease.execution_plan_version
                    and attempt.failure_fingerprint == fingerprint and attempt.error_json == payload
                )
            if not self._same_execution(task, lease):
                return False
            if task.status == "failed":
                return task.result_fingerprint == fingerprint and task.error_json == payload
            control = self._control(tx, lease)
            timeout_deadline = (task.total_deadline_at if error.code == "TOTAL_TIMEOUT"
                                else task.attempt_deadline_at if error.code == "ATTEMPT_TIMEOUT" else None)
            confirmed_timeout = (
                task.status == "running" and control == error.code.lower()
                and timeout_deadline is not None and task.lease_expires_at is not None
                and task.lease_expires_at >= timeout_deadline
            )
            if control != "continue" and not confirmed_timeout:
                return False
            self.failure_policy.apply(tx, error, fingerprint)
            if self.remote:
                confirmed = error.code in CONFIRMED_FAILURES
                self.remote_service.finish(tx, confirmed=confirmed)
                if not confirmed:
                    task.error_json = FailureInfo(code="REMOTE_ASR_RESULT_UNKNOWN",
                        message=f"远端请求结果未确认（{error.code}）；不会自动重发，须核对请求已结束。").model_dump()
            return True

    def request_cancel(self, task_id: str, caller_id: str) -> str:
        with self.repository.lock_task(task_id) as tx:
            task = tx.task
            if task is None or task.caller_id != caller_id:
                raise AppError("TASK_NOT_FOUND", "未找到此任务。", 404)
            if InvocationRepository.available(tx.session):
                invocation = InvocationRepository.for_owner(tx.session, "task", task_id)
                if invocation:
                    if not self.settings.common_execution_enabled:
                        raise AppError("COMMON_EXECUTION_DISABLED", "该任务属于公共通道，请恢复公共执行开关后处理取消。", 503)
                    return InvocationService(self.repository.database, self.settings).cancel_in_transaction(tx.session, invocation)
            if task.status in TERMINAL_STATES:
                return "already_finished"
            if task.status in WAITING_STATES:
                tx.finish(status="cancelled", result=None, error=None, fingerprint=None)
                if self.remote_service.is_remote(task):
                    self.remote_service.finish(tx, confirmed=True)
                return "cancelled"
            if task.status in {"running", "cancel_requested"}:
                if self.remote_service.is_remote(task):
                    raise AppError("REMOTE_ASR_CANCEL_UNSUPPORTED", "外部服务没有取消接口；领取后须等待结果。", 409)
                tx.request_cancellation()
                return "cancel_requested"
            raise AppError("TASK_STATE_CONFLICT", "任务状态不允许取消。", 409)

    def finish_cancel(self, lease: ExecutionLease) -> bool:
        """仅接受 M12 已确认子进程结束后的调用；租约超时恢复由 M08 负责。"""
        lease = ExecutionLease.model_validate(lease.model_dump())
        with self.repository.lock_task(lease.task_id) as tx:
            if not self._same_execution(tx.task, lease) or tx.task.task_type != self.task_type:
                return False
            if tx.task.status == "cancelled":
                return True
            if tx.task.status != "cancel_requested":
                return False
            tx.finish_attempt(outcome="cancelled")
            tx.finish(status="cancelled", result=None, error=None, fingerprint=None)
            return True
