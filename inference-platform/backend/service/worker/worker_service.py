"""M12/M14 共用单槽控制：能力队列独立，续租、取消和可靠提交共用。"""

from collections.abc import Callable
from contextlib import nullcontext
import logging
import os
from time import monotonic
from uuid import uuid4

from filelock import FileLock, Timeout
from pydantic import ValidationError

from backend.common.exceptions.app_error import AppError
from backend.common.process.managed_process import ManagedJsonProcess
from backend.config.settings.app_settings import AppSettings
from backend.config.vision.model_catalog import MODEL_ID as VISION_MODEL_ID, MODEL_VERSION as VISION_MODEL_VERSION, MODEL_SHA256 as VISION_MODEL_SHA256, QUEUE_KEY as VISION_QUEUE_KEY
from backend.data.broker.broker_repository import BrokerRepository
from backend.data.database.database import Database
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.schema.execution.execution_schema import FailureInfo
from backend.schema.asr.asr_schema import TranscriptionResult
from backend.schema.vision.detection_schema import DetectionResult
from backend.schema.worker.worker_schema import (
    EVENT_ADAPTER, FailedEvent, FatalEvent, ReadyEvent, ResultEvent, RunCommand, StartCommand,
)
from backend.service.execution.execution_service import ExecutionService
from backend.service.worker.inference_child import inference_child
from backend.service.asr.adapter_catalog import configured_asr_plugin
from backend.service.agent.asr_gate import AsrGate
from backend.service.asr.remote_service import RemoteAsrService
from backend.service.asr.target_binding import TargetSlotLock


class WorkerService:
    def __init__(
        self, database: Database, settings: AppSettings, logger: logging.Logger,
        *, runner=None, broker=None, execution=None, runtime=None,
        clock: Callable[[], float] = monotonic,
        task_type: str = "asr",
        asr_profile: str = "local",
        agent_session_id: str | None = None,
        worker_session_id: str | None = None,
        remote_service=None,
        remote_pooled: bool = False,
        remote_target_id: str = "remote-default",
        remote_slot_index: int = 0,
    ) -> None:
        if task_type not in {"asr", "vision"}:
            raise ValueError("Worker 只支持 asr 或 vision 能力")
        if asr_profile not in {"local", "remote"} or (task_type != "asr" and asr_profile != "local"):
            raise ValueError("无效的 ASR Worker 配置")
        self.task_type = task_type
        self.asr_profile = asr_profile
        self.is_remote = task_type == "asr" and asr_profile == "remote"
        if remote_pooled and not self.is_remote:
            raise ValueError("只有外部 ASR 调用可加入执行池")
        self.remote_pooled = remote_pooled
        self.component_id = ("remote_asr_worker" if self.is_remote else
                             "worker" if task_type == "asr" else "vision_worker")
        self.settings = settings
        self.asr_plugin = configured_asr_plugin(settings, profile=asr_profile) if task_type == "asr" else None
        self.queue_key = self.asr_plugin.queue_key if task_type == "asr" else VISION_QUEUE_KEY
        self.logger = logger
        self.runner = runner if runner is not None else ManagedJsonProcess(inference_child)
        self.broker = broker if broker is not None else BrokerRepository(database)
        self.execution = execution if execution is not None else ExecutionService(
            database, settings, task_type=task_type, asr_profile=asr_profile,
            remote_target_id=remote_target_id, remote_slot_index=remote_slot_index, remote_pooled=remote_pooled)
        self.runtime = runtime if runtime is not None else RuntimeRepository(database)
        self.clock = clock
        self.session_id = worker_session_id or uuid4().hex
        self.agent_session_id = agent_session_id
        self.gate = AsrGate(database, settings) if task_type == "asr" and not self.is_remote and database is not None else None
        self.remote = (remote_service if remote_service is not None else
                       RemoteAsrService(database, settings, target_id=remote_target_id,
                                        slot_index=remote_slot_index, pooled=remote_pooled)
                       if self.is_remote and database is not None else None)
        self._draining = False
        self.state = "starting"
        self.model_ready = False
        self.last_error = None
        self.delivery = None
        self.lease = None
        self.pending = None
        self._deadline = 0.0
        self._attempt_deadline = 0.0
        self._next_renew = 0.0
        self._next_report = 0.0
        self._next_commit = 0.0
        self._startup_deadline = 0.0
        self._next_remote_probe = 0.0
        self._owns_lock = False
        self._remote_bound = False
        self._started = False
        self._stop_confirmed = True
        self._stop_action = None
        self._stop_failure = None
        self._cancel_pending = False
        # ASR 保留既有锁名，防止升级期间旧版 ASR 绕过互斥；Vision 独立单槽。
        suffix = (".remote-asr-worker.lock" if self.is_remote else
                  ".worker.lock" if task_type == "asr" else ".vision-worker.lock")
        self._lock = FileLock(settings.database_path.with_name(settings.database_path.name + suffix), timeout=0)
        self._target_lock = TargetSlotLock(settings.database_path, remote_target_id, remote_slot_index) if self.is_remote else None

    @property
    def can_exit(self) -> bool:
        return self.state == "error" and self._stop_confirmed and self._stop_action is None

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def drained(self) -> bool:
        """在途任务已按原规则收尾；仍需 close() 确认模型进程确实退出。"""
        return (self._draining and self.lease is None and self.pending is None
                and not self._cancel_pending and self._stop_action is None)

    def request_drain(self) -> None:
        """单向停止接单；不取消当前音频，不释放锁，也不声称进程已结束。"""
        if self.remote is not None and not self._draining:
            # 数据库停止意图与 API 新提交、执行领取在同一写事务内排序。
            self.remote.begin_drain(session_id=self.session_id)
        self._draining = True

    def _refresh_drain(self) -> None:
        if self.remote is not None and not self._draining:
            try:
                if self.remote.draining(self.session_id):
                    self._draining = True
            except Exception:
                self.logger.exception("无法核对外部 ASR 调用停止意图，停止新增领取")
                self._draining = True
        if self.gate is None or self._draining:
            return
        try:
            if self.gate.stop_requested(agent_session_id=self.agent_session_id, worker_session_id=self.session_id):
                self.request_drain()
        except Exception:
            # 不确定管理权时停止新增工作；在途执行继续遵守原租约与截止时间。
            self.logger.exception("无法核对 ASR 管理状态，停止新增领取和模型重建")
            self.request_drain()

    def start(self) -> None:
        try:
            if not self.remote_pooled:
                self._lock.acquire()
        except Timeout as exc:
            raise AppError("WORKER_ALREADY_RUNNING", f"此数据库的 {self.task_type} 队列已有 Worker 运行；每类只允许一个 Worker。", 409) from exc
        self._owns_lock = True
        try:
            if self._target_lock is not None and not self._target_lock.acquire():
                raise AppError("ASR_TARGET_SLOT_IN_USE", "当前目标的调用槽已有执行者。", 409)
            if self.remote is not None:
                self.remote.begin_worker(self.session_id)
                self._remote_bound = True
            self._start_child()
        except BaseException:
            if not self._started and self._stop_confirmed and not self._remote_bound:
                # 身份拒绝没有启动进程，不得在 close 时覆盖真正 Agent 的运行报告。
                if self._target_lock is not None:
                    self._target_lock.release()
                if not self.remote_pooled:
                    self._lock.release()
                self._owns_lock = False
            raise

    def _start_child(self) -> None:
        """只在旧进程已确认结束后重建；跨进程单槽锁始终由本 Worker 持有。"""
        self.state = "starting"
        self.model_ready = False
        self.last_error = None
        self._startup_deadline = self.clock() + self.settings.worker_startup_timeout_seconds
        try:
            guard = (self.remote.spawn_guard(self.session_id) if self.remote is not None else
                     self.gate.spawn_guard(agent_session_id=self.agent_session_id, worker_session_id=self.session_id)
                     if self.gate is not None else nullcontext(True))
            with guard as allowed:
                spawned = allowed and not self._draining
                if spawned:
                    self.runner.start()
                    self._started = True
                    self._stop_confirmed = False
                    self.runner.send(StartCommand(settings=self.settings, task_type=self.task_type,
                                                  asr_profile=self.asr_profile).model_dump_json().encode("utf-8"))
            if not spawned:
                # 在guard写事务退出后才登记本槽排空，避免嵌套写事务。
                self.request_drain()
                self._started = True
                self.state = "draining"
                return
            self._report(force=True)
            self.logger.info("Worker 启动 session_id=%s child_pid=%s，等待模型加载", self.session_id, self.runner.pid)
        except BaseException:
            self._stop_child("WORKER_START_FAILED")
            raise

    def tick(self) -> None:
        """每次只处理短控制操作；模型推理始终在受管子进程。"""
        if not self._started:
            raise RuntimeError("Worker 尚未启动")
        self._refresh_drain()
        if self.drained:
            self._report()
            return
        if self._stop_action is not None:
            self._continue_stop()
            self._report()
            return
        if self.state == "unavailable":
            if not self._draining and self.clock() >= self._next_remote_probe:
                self._restart_child()
            self._report()
            return
        if self.state == "error":
            self._report()
            return
        # 提交可能已经在数据库成功但响应丢失；先幂等复查，避免终态 renew=False
        # 被误判为执行权丢失而退出本可继续工作的 Worker。
        if (self.pending is not None or self._cancel_pending) and self.state == "committing":
            self._commit_pending()
            if self.state == "error" or self._stop_action is not None:
                self._report()
                return
        if self.lease is not None:
            if not self._check_control():
                self._report()
                return
            self._renew_if_due()
            if self.state == "error" or self._stop_action is not None or self.state == "starting":
                self._report()
                return
        if self.state in {"starting", "ready", "running"}:
            self._receive_child()
        if self.state == "starting" and self.clock() >= self._startup_deadline:
            self._stop_child("MODEL_START_TIMEOUT")
        if self.state not in {"error", "stopped", "committing"} and not self.runner.is_alive():
            self.model_ready = False
            if self.lease is not None and self.pending is None:
                self._fail_child(FailureInfo(code="WORKER_CHILD_EXITED", message="推理子进程意外退出。"))
            elif self.lease is None:
                self._stop_child("WORKER_CHILD_EXITED")
        if (self.pending is not None or self._cancel_pending) and self.state == "committing":
            self._commit_pending()
        if self.state == "ready" and self.model_ready and not self._draining:
            self._claim()
        self._report()

    def _receive_child(self) -> None:
        try:
            payload = self.runner.poll()
            if payload is None:
                return
            event = EVENT_ADAPTER.validate_json(payload)
            if isinstance(event, ReadyEvent):
                if self.state != "starting":
                    raise ValueError("重复或非启动期 ready")
                self.model_ready = True
                self.state = "ready"
                self.last_error = None
                if self.is_remote:
                    # 领取准入检查数据库中的同会话就绪报告，不能先隐藏一条通知再报告。
                    self._report(force=True)
                self.logger.info("模型已就绪，Worker 可接单 task_type=%s model_load_seconds=%.3f warmup_seconds=%.3f", self.task_type, event.model_load_seconds, event.warmup_seconds)
            elif isinstance(event, FatalEvent):
                self._fail_child(event.failure)
            elif isinstance(event, (ResultEvent, FailedEvent)):
                if self.state != "running" or self.lease is None or event.task_id != self.lease.task_id:
                    raise ValueError("收到不属于当前执行的结果")
                if isinstance(event, ResultEvent):
                    self._validate_result_profile(event)
                self.pending = event
                self.state = "committing"
            else:
                raise ValueError("未知子进程消息")
        except (EOFError, OSError):
            # 由下面的存活检查转为执行失败；不能把空管道当成功。
            if self.runner.is_alive():
                self._fail_child(FailureInfo(code="WORKER_COMMUNICATION_FAILED", message="推理子进程通信失败。"))
        except (ValidationError, ValueError):
            self.logger.exception("Worker 子进程协议错误")
            self._fail_child(FailureInfo(code="WORKER_PROTOCOL_ERROR", message="推理子进程消息校验失败。"))

    def _claim(self) -> None:
        if self._draining:
            return
        try:
            check = ({"receive_check": lambda session: self.gate.can_claim(session, self.session_id)}
                     if self.gate is not None else {})
            if self.remote is not None:
                check = {"receive_check": lambda session: self.remote.can_receive(session, self.session_id),
                         "candidate_check": lambda session, candidate: self.remote.can_receive_task(
                             session, candidate.task_id, self.session_id)}
            delivery = self.broker.receive(worker_id=self.session_id, queue_key=self.queue_key,
                                           visibility_seconds=self.settings.notification_visibility_seconds, **check)
            if delivery is None:
                return
            acquired_at = self.clock()
            decision = self.execution.acquire(delivery)
            if decision.decision == "terminal_or_stale":
                self.broker.acknowledge(delivery)
                return
            if decision.decision == "busy":
                self.broker.release(delivery)
                return
            self.delivery = delivery
            self.lease = decision.lease
            if decision.input.task_type != self.task_type:
                self._fail_child(FailureInfo(code="WORKER_PROTOCOL_ERROR", message="执行输入与 Worker 能力不匹配。"))
                return
            budget = self.lease.attempt_budget_seconds
            self._attempt_deadline = acquired_at + (budget if budget is not None else self.settings.attempt_timeout_seconds)
            self._deadline = min(acquired_at + self.settings.lease_seconds, self._attempt_deadline)
            self._next_renew = acquired_at + self.settings.renew_interval_seconds
            if self.clock() >= self._deadline:
                # 获取许可的数据库等待已经耗尽预算，不能再发出 run。
                self._request_stop("abandon", "LEASE_EXPIRED")
                return
            self.state = "running"
            self.runner.send(RunCommand(input=decision.input).model_dump_json().encode("utf-8"))
            self.logger.info("开始执行 task_type=%s task_id=%s attempt=%s", self.task_type, self.lease.task_id, self.lease.attempt)
        except Exception:
            self.logger.exception("Worker 领取或派发失败；不确认通知")
            if self.lease is not None:
                self._fail_child(FailureInfo(code="WORKER_COMMUNICATION_FAILED", message="无法向推理子进程派发任务。"))

    def _validate_result_profile(self, event: ResultEvent) -> None:
        result = event.result
        if self.task_type == "asr":
            valid = isinstance(result, TranscriptionResult) and self.asr_plugin.result_matches(self.settings, result)
        else:
            valid = isinstance(result, DetectionResult) and (
                result.model_id == VISION_MODEL_ID and result.model_version == VISION_MODEL_VERSION
                and result.model_sha256 == VISION_MODEL_SHA256
            )
        if not valid:
            raise ValueError("结果类型或模型身份与当前 Worker 不匹配")

    def _check_control(self, *, rejected_outcome: bool = False) -> bool:
        """每次短轮询检查控制，不让取消等待下一次续租。数据库才是期限裁决者。"""
        try:
            control = self.execution.control(self.lease)
        except Exception:
            self.logger.exception("执行控制查询暂时失败；仍受本地保守租约期限约束")
            return True
        if control == "continue":
            return True
        if control == "cancel":
            self._request_stop("cancel", "CANCEL_REQUESTED")
        elif rejected_outcome:
            # 已提交的失败/取消遭拒绝，说明本次执行已无权裁决；不在旧租约上循环。
            self._request_stop("abandon", "RESULT_COMMIT_REJECTED")
        elif control in {"attempt_timeout", "total_timeout"}:
            failure = FailureInfo(
                code="ATTEMPT_TIMEOUT" if control == "attempt_timeout" else "TOTAL_TIMEOUT",
                message="本次执行超过期限。" if control == "attempt_timeout" else "任务超过总期限。",
            )
            self._request_stop("failure", failure.code, failure)
        else:
            self._request_stop("abandon", "LEASE_EXPIRED")
        return False

    def _renew_if_due(self) -> None:
        now = self.clock()
        if now >= self._deadline:
            self._request_stop("abandon", "LEASE_EXPIRED")
            return
        if now < self._next_renew:
            return
        self._next_renew = now + self.settings.renew_interval_seconds
        try:
            if not self.execution.renew(self.lease):
                if self.pending is not None:
                    self._next_commit = 0.0
                    self._commit_pending()
                    if self.lease is None:
                        return
                if self._check_control():
                    self._request_stop("abandon", "LEASE_LOST")
                return
            # 从请求开始计算，保守地不把数据库等待时间计为额外执行权。
            self._deadline = min(now + self.settings.lease_seconds, self._attempt_deadline)
            if self.clock() >= self._deadline:
                self._request_stop("abandon", "LEASE_EXPIRED")
                return
        except Exception:
            self.logger.exception("租约续期暂时失败；到期前重试，到期后停止子进程")
            self._next_renew = self.clock() + self.settings.poll_interval_seconds
            if self.clock() >= self._deadline:
                self._request_stop("abandon", "LEASE_EXPIRED")
            return
        try:
            if not self.broker.extend(self.delivery, visibility_seconds=self.settings.notification_visibility_seconds):
                self.logger.warning("通知可见期续期被拒绝；执行权仍以任务租约为准")
        except Exception:
            self.logger.exception("通知可见期续期失败；不据此重新执行任务")
        if self.clock() >= self._deadline:
            self._request_stop("abandon", "LEASE_EXPIRED")

    def _commit_pending(self) -> None:
        if self.clock() < self._next_commit:
            return
        try:
            if self._cancel_pending:
                saved = self.execution.finish_cancel(self.lease)
            elif isinstance(self.pending, ResultEvent):
                saved = self.execution.commit_result(self.lease, self.pending.result)
            else:
                saved = self.execution.commit_failure(self.lease, self.pending.failure)
        except Exception:
            # 保留结果且续租，下次重试保存；绝不再次调用模型。
            self.logger.exception("执行结果持久化暂时失败；保留结果，不确认通知")
            self._next_commit = self.clock() + self.settings.poll_interval_seconds
            return
        if not saved:
            # 取消可能恰好赢过成功提交；重新读取裁决，只有确认旧进程停止才结束取消。
            if self._check_control(rejected_outcome=self._cancel_pending or isinstance(self.pending, FailedEvent)):
                self._request_stop("abandon", "RESULT_COMMIT_REJECTED")
            return
        task_id = self.lease.task_id
        try:
            self.broker.acknowledge(self.delivery)
        except Exception:
            # 结果已经提交；重复通知再次领取时根据任务终态确认即可。
            self.logger.exception("结果已保存，但确认通知失败；重复通知不会重跑已终态任务")
        self._clear_execution()
        self.logger.info("任务结果已保存 task_id=%s", task_id)
        if self.runner.is_alive() and self.model_ready:
            self.state = "ready"
        else:
            if self._terminate_child():
                self._restart_child()
            else:
                self._request_stop("abandon", "WORKER_STOP_UNCONFIRMED")

    def _clear_execution(self) -> None:
        self.lease = None
        self.delivery = None
        self.pending = None
        self._cancel_pending = False
        self._next_commit = 0.0

    def _restart_child(self) -> None:
        if not self._stop_confirmed:
            raise RuntimeError("未确认旧子进程结束，禁止重新接单")
        self._refresh_drain()
        if self._draining:
            self.state = "draining"
            return
        try:
            self._start_child()
        except Exception:
            self.logger.exception("推理子进程重建失败；保持停止接单")

    def _terminate_child(self) -> bool:
        self.model_ready = False
        try:
            # 池内某槽退出未确认时，其他槽仍须及时续租/提交；每轮只非阻塞检查。
            self._stop_confirmed = self.runner.stop(0 if self.remote_pooled else self.settings.worker_stop_timeout_seconds)
        except Exception:
            self.logger.exception("无法确认推理子进程停止")
            self._stop_confirmed = False
        return self._stop_confirmed

    def _stop_child(self, reason: str) -> None:
        self._request_stop("unavailable" if self.is_remote and self.lease is None else "fatal", reason)

    def _request_stop(self, action: str, reason: str, failure: FailureInfo | None = None) -> None:
        self._stop_action = action
        self._stop_failure = failure
        self.last_error = reason
        self.state = "error"
        self._continue_stop()

    def _continue_stop(self) -> None:
        """停止不确认就保留动作、任务和锁，随后继续确认，而不是遗失待取消动作。"""
        if not self._terminate_child():
            self.last_error = "WORKER_STOP_UNCONFIRMED"
            self.state = "error"
            return
        action, failure = self._stop_action, self._stop_failure
        if self.remote is not None and action == "abandon" and self.lease is not None:
            try:
                # HTTP 子进程退出只证明本机连接结束；执行权丢失仍需保留远端未知事实。
                self.remote.confirm_local_exit(self.session_id)
            except Exception:
                self.logger.exception("外部调用未知结果未保存，保留执行槽位与锁")
                self.state = "error"
                return
        self._stop_action = self._stop_failure = None
        if action == "cancel" and self.lease is not None:
            self.pending = None
            self._cancel_pending = True
            self.state = "committing"
            self._next_commit = 0.0
        elif action == "failure" and self.lease is not None:
            self.pending = FailedEvent(task_id=self.lease.task_id, failure=failure)
            self._cancel_pending = False
            self.state = "committing"
            self._next_commit = 0.0
        else:
            self._clear_execution()
            if action == "unavailable":
                self.state = "unavailable"
                self._next_remote_probe = self.clock() + self.settings.runtime_stale_seconds
            elif action == "abandon":
                self._restart_child()
            else:
                self.state = "error"

    def _fail_child(self, failure: FailureInfo) -> None:
        """执行故障先确认子进程停止，再以尚有效的租约保存失败。"""
        action = ("failure" if self.lease is not None else "unavailable" if self.is_remote else "fatal")
        self._request_stop(action, failure.code, failure)

    def _report(self, *, force: bool = False) -> bool:
        now = self.clock()
        if not force and now < self._next_report:
            return True
        self._next_report = now + self.settings.runtime_heartbeat_seconds
        try:
            reported_state = {"starting": "model_loading", "ready": "idle", "running": "busy", "committing": "busy"}.get(self.state, self.state)
            if self.is_remote:
                reported_state = {"committing": "running", "draining": "stopping"}.get(self.state, self.state)
            if self._draining and self.state not in {"error", "stopped"}:
                reported_state = "stopping" if self.is_remote else "draining"
            if not self.remote_pooled:
                self.runtime.report(component_id=self.component_id, session_id=self.session_id, pid=os.getpid(), state=reported_state, model_ready=self.model_ready, active_task_id=self.lease.task_id if self.lease else None, child_pid=self.runner.pid, last_error=self.last_error)
            if self.remote is not None:
                accepted = self.remote.report_worker(self.session_id, reported_state, self.model_ready,
                    active_task_id=self.lease.task_id if self.lease else None,
                    child_pid=self.runner.pid, last_error=self.last_error)
                if accepted is False:
                    return False
            return True
        except Exception:
            self.logger.exception("Worker 心跳报告暂时失败")
            return False

    def close(self) -> bool:
        if not self._owns_lock:
            return True
        lease, delivery = self.lease, self.delivery
        if not self._terminate_child():
            self.state = "error"
            self.last_error = "WORKER_STOP_UNCONFIRMED"
            self._report(force=True)
            return False
        if lease is not None:
            try:
                # 未知成功仍先重放；取消与停止故障通过同一租约条件裁决。
                if isinstance(self.pending, ResultEvent):
                    saved = self.execution.commit_result(lease, self.pending.result)
                else:
                    failure = (self.pending.failure if isinstance(self.pending, FailedEvent) else
                               self._stop_failure or FailureInfo(code="WORKER_STOPPED", message="Worker 已停止，当前执行被中断。"))
                    saved = self.execution.commit_failure(lease, failure)
                if not saved and self.execution.control(lease) == "cancel":
                    saved = self.execution.finish_cancel(lease)
                if saved and delivery is not None:
                    self.broker.acknowledge(delivery)
            except Exception:
                self.logger.exception("停止时保存执行失败未成功；保留通知供后续检查")
        if self.remote is not None:
            try:
                self.remote.confirm_local_exit(self.session_id)
            except Exception:
                self.logger.exception("本机 HTTP 子进程已退出，但外部调用状态尚未持久化；保留锁重试")
                self.state = "error"
                self.last_error = "REMOTE_ASR_EXIT_UNRECORDED"
                self._report(force=True)
                return False
        self._clear_execution()
        self._stop_action = self._stop_failure = None
        self.state = "stopped" if self.is_remote else "error" if self.state == "error" else "stopped"
        reported = self._report(force=True)
        if self.is_remote and not reported:
            return False
        if self._target_lock is not None:
            self._target_lock.release()
        if not self.remote_pooled:
            self._lock.release()
        self._owns_lock = False
        self.logger.info("本机 HTTP 调用 Worker 已停止；远端执行状态以持久化调用记录为准" if self.is_remote
                         else "Worker 已停止，推理子进程结束、执行槽位已释放")
        return True
