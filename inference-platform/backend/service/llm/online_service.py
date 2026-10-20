"""单静态实例在线准入与生命周期；不进入离线队列，不自动重放生成。"""

from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256
import json
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from backend.common.exceptions.app_error import AppError
from backend.data.database.database import database_now
from backend.data.llm.online_models import OnlineRequestRecord
from backend.data.llm.online_repository import ACTIVE_STATES, OnlineRepository
from backend.schema.llm.online_schema import (LlmStatus, OnlineCancelReceipt, OnlineRequestInput,
                                            OnlineRequestPage, OnlineRequestView)
from backend.service.llm.engine_adapter import HttpChatAdapter

CALLER_ID = "local-user"


class OnlineRun:
    """只存有界内存中的本次连接数据；监督线程不依赖客户端继续读取。"""
    def __init__(self, record, deadline):
        self.record = record
        self.deadline = deadline
        self.outbox = Queue(maxsize=16)
        self.inbox = Queue(maxsize=16)
        self.cancel = Event()
        self.finished = Event()
        self.terminal = None
        self.thread = None


class OnlineService:
    def __init__(self, settings, database, models, logger, adapter=None):
        self.settings, self.database, self.models, self.logger = settings, database, models, logger
        self.repository = OnlineRepository()
        self.owner_id = uuid4().hex
        self.instance_id = ("static-" + sha256(settings.llm_endpoint.rstrip("/").encode()).hexdigest()[:32]
                            if settings.llm_endpoint else None)
        self.adapter = adapter
        if self.adapter is None and settings.llm_endpoint:
            self.adapter = HttpChatAdapter(
                settings.llm_endpoint, settings.llm_upstream_model,
                settings.llm_api_key.get_secret_value() if settings.llm_api_key else None,
                settings.llm_connect_timeout_seconds, settings.llm_read_timeout_seconds)
        self._runs, self._lock, self._closed = {}, Lock(), False

    @contextmanager
    def _transaction(self, write=False):
        try:
            with self.database.transaction(immediate=write) as session:
                yield session
        except SQLAlchemyError:
            raise AppError("LLM_STORAGE_UNAVAILABLE", "在线请求记录暂不可用，请检查数据库与迁移。", 503) from None

    @staticmethod
    def _view(row):
        values = row.to_dict()
        return OnlineRequestView.model_validate({name: values["error_json" if name == "error" else name]
                                                  for name in OnlineRequestView.model_fields})

    def _reap(self, session, now):
        for row in self.repository.expired(session, now):
            self.repository.update(row, state="failed", ended_at=now, updated_at=now,
                error_json={"code": "LLM_OWNER_LOST", "message": "在线处理进程失联，本次生成不会自动重放。"},
                permit_state="uncertain" if row.permit_state == "held" else "released")

    def status(self):
        with self._transaction(True) as session:
            self._reap(session, database_now(session))
            session.flush()
            active, _, uncertain = self.repository.counts(session)
        configured = self.settings.llm_endpoint is not None
        return LlmStatus(configured=configured, availability="configured" if configured else "unconfigured",
                         message=("已配置连接信息，真实模型服务尚待联调验证。" if configured
                                  else "尚未配置大模型服务，请先配置兼容的文本流式服务并登记启用模型。"),
                         max_concurrent_requests=self.settings.llm_max_concurrent_requests,
                         active_requests=active, uncertain_permits=uncertain)

    def get(self, request_id):
        with self._transaction(True) as session:
            self._reap(session, database_now(session))
            row = self.repository.get(session, request_id, CALLER_ID)
            if row is None:
                raise AppError("LLM_REQUEST_NOT_FOUND", "未找到该在线请求。", 404)
            return self._view(row)

    def list_requests(self, query):
        with self._transaction(True) as session:
            self._reap(session, database_now(session))
            session.flush()
            rows, total = self.repository.page(session, CALLER_ID, query.page, query.page_size)
            return OnlineRequestPage(items=[self._view(row) for row in rows], total=total,
                                     page=query.page, page_size=query.page_size)

    def open(self, request: OnlineRequestInput):
        request = OnlineRequestInput.model_validate(request)
        if self.adapter is None or self.settings.llm_endpoint is None:
            raise AppError("LLM_NOT_CONFIGURED", "尚未配置大模型服务，不能提交生成请求。", 503)
        with self._lock:
            if self._closed:
                raise AppError("LLM_SHUTTING_DOWN", "在线通道正在停止。", 503)
            with self._transaction(True) as session:
                now = database_now(session)
                self._reap(session, now)
                session.flush()
                if self.repository.get(session, request.online_request_id) is not None:
                    raise AppError("LLM_REQUEST_EXISTS", "该请求编号已受理；请查询原请求，不要重发生成。", 409)
                self.models.validate_admission(session, request.model_id, request.model_version, task_type="llm")
                if self.repository.inflight(session) >= self.settings.max_inflight_tasks:
                    raise AppError("INFLIGHT_LIMIT_EXCEEDED", "平台在途请求已达上限，请稍后重新提交。", 429)
                row = OnlineRequestRecord(
                    online_request_id=request.online_request_id, caller_id=CALLER_ID,
                    model_id=request.model_id, model_version=request.model_version, instance_id=None,
                    owner_id=self.owner_id, request_fingerprint=sha256(request.model_dump_json().encode()).hexdigest(),
                    state="accepted", permit_state="waiting", accepted_at=now, updated_at=now,
                    deadline_at=now + timedelta(seconds=self.settings.llm_total_timeout_seconds),
                    lease_expires_at=now + timedelta(seconds=self.settings.llm_lease_seconds),
                    output_characters=0, cancel_requested=False, cancellation_confirmed=False)
                self.repository.add(session, row)
                record = self._view(row)
            run = OnlineRun(record, monotonic() + self.settings.llm_total_timeout_seconds)
            self._runs[request.online_request_id] = run
            run.thread = Thread(target=self._supervise, args=(run, request), name="llm-supervisor", daemon=True)
            try:
                run.thread.start()
            except RuntimeError:
                self._runs.pop(request.online_request_id, None)
                self._finalize(run, "failed", "released", False,
                               AppError("LLM_START_FAILED", "无法启动在线处理。", 503))
                raise AppError("LLM_START_FAILED", "无法启动在线处理。", 503) from None
            return run

    def cancel(self, request_id):
        with self._transaction(True) as session:
            now = database_now(session)
            self._reap(session, now)
            row = self.repository.get(session, request_id, CALLER_ID)
            if row is None:
                raise AppError("LLM_REQUEST_NOT_FOUND", "未找到该在线请求。", 404)
            decision = "already_finished"
            if row.state in ACTIVE_STATES:
                self.repository.update(row, cancel_requested=True, updated_at=now)
                decision = "cancel_requested"
            result = OnlineCancelReceipt(request=self._view(row), decision=decision)
        with self._lock:
            run = self._runs.get(request_id)
        if run is not None and decision == "cancel_requested":
            run.cancel.set()
        return result

    def disconnect(self, run):
        if not run.finished.is_set():
            run.cancel.set()

    def _touch(self, run, *, acquire=False, text=None):
        with self._transaction(True) as session:
            now = database_now(session)
            self._reap(session, now)
            session.flush()
            row = self.repository.get(session, run.record.online_request_id)
            if row.state not in ACTIVE_STATES or row.owner_id != self.owner_id:
                raise AppError("LLM_OWNER_LOST", "在线处理权已失效，本次生成不会自动重放。", 409)
            if row.cancel_requested or run.cancel.is_set():
                raise AppError("LLM_CANCELLED", "已请求停止在线生成；上游停止情况可能尚未确认。", 409)
            if now >= row.deadline_at or monotonic() >= run.deadline:
                raise AppError("LLM_TIMEOUT", "在线生成超过本次截止时间。", 504)
            self.repository.update(row, updated_at=now,
                                   lease_expires_at=now + timedelta(seconds=self.settings.llm_lease_seconds))
            if acquire:
                _, occupied, _ = self.repository.counts(session, self.instance_id)
                if occupied >= self.settings.llm_max_concurrent_requests:
                    self.repository.update(row, state="waiting_instance")
                    return False
                self.repository.update(row, instance_id=self.instance_id, permit_state="held", state="streaming")
            if text is not None:
                if row.output_characters + len(text) > 131072:
                    raise AppError("LLM_STREAM_LIMIT_EXCEEDED", "生成文本超过接收上限。", 502)
                self.repository.update(row, first_token_at=row.first_token_at or now,
                                       output_characters=row.output_characters + len(text))
            return True

    @staticmethod
    def _read_engine(run, stream):
        try:
            for chunk in stream.events():
                while not run.cancel.is_set() and not run.finished.is_set():
                    try:
                        run.inbox.put(chunk, timeout=0.1)
                        break
                    except Full:
                        continue
                else:
                    return
            # A valid done precedes EOF; otherwise EOF is failure, never success.
            failure = AppError("LLM_STREAM_INTERRUPTED", "上游流没有正常结束，本次生成不会自动重放。", 502)
        except AppError as error:
            failure = error
        except Exception:
            failure = AppError("LLM_STREAM_INTERRUPTED", "上游流处理异常，本次生成不会自动重放。", 502)
        finally:
            stream.close()
        while not run.cancel.is_set() and not run.finished.is_set():
            try:
                run.inbox.put(failure, timeout=0.1)
                return
            except Full:
                continue

    def _supervise(self, run, request):
        stream, acquired, completed, error, finish = None, False, False, None, None
        try:
            admission_end = monotonic() + self.settings.llm_admission_wait_seconds
            while not (acquired := self._touch(run, acquire=True)):
                if monotonic() >= admission_end:
                    raise AppError("LLM_CAPACITY_UNAVAILABLE", "在线实例繁忙或有未确认释放的占位，请稍后新建请求。", 429)
                run.cancel.wait(0.1)
            stream = self.adapter.open([message.model_dump() for message in request.messages],
                                       request.parameters.model_dump(), run.deadline)
            # Clear prompt references in the supervision frame once adapter owns its bounded payload.
            request = None
            Thread(target=self._read_engine, args=(run, stream), name="llm-engine-reader", daemon=True).start()
            next_touch = monotonic()
            while True:
                if run.cancel.is_set() or monotonic() >= min(next_touch, run.deadline):
                    self._touch(run)
                    next_touch = monotonic() + 0.5
                try:
                    chunk = run.inbox.get(timeout=0.1)
                except Empty:
                    continue
                if isinstance(chunk, AppError):
                    raise chunk
                if chunk.kind == "done":
                    completed, finish = True, chunk.finish_reason
                    break
                if chunk.kind != "delta" or not isinstance(chunk.text, str):
                    raise AppError("LLM_UPSTREAM_PROTOCOL_ERROR", "收到不支持的上游内容。", 502)
                if not chunk.text:
                    continue
                self._touch(run, text=chunk.text)
                slow_end = monotonic() + self.settings.llm_slow_consumer_seconds
                while True:
                    try:
                        run.outbox.put(chunk.text, timeout=0.1)
                        break
                    except Full:
                        self._touch(run)
                        if monotonic() >= slow_end:
                            raise AppError("LLM_SLOW_CONSUMER", "客户端读取过慢，已终止本次流式传输。", 408)
        except AppError as caught:
            error = caught
        except Exception:
            error = AppError("LLM_INTERNAL_ERROR", "在线处理异常，本次生成不会自动重放。", 500)
        finally:
            confirmed = completed or not acquired or stream is None
            if stream is not None:
                try:
                    confirmed = stream.cancel() or completed
                    stream.close()
                except Exception:
                    confirmed = completed
            state = "completed" if completed else "cancelled" if error and error.code == "LLM_CANCELLED" else "failed"
            permit = "released" if confirmed else "uncertain"
            try:
                self._finalize(run, state, permit, confirmed if state == "cancelled" else False, error, finish)
            except AppError:
                # Fail closed: persisted held permit remains until reconciliation marks it uncertain.
                run.terminal = {"type": "error", "error": {"code": "LLM_STORAGE_UNAVAILABLE",
                                 "message": "无法确认请求最终状态，请按请求编号查询。"}}
                self.logger.error("online_finalize_failed online_request_id=%s", run.record.online_request_id)
            run.finished.set()
            with self._lock:
                self._runs.pop(run.record.online_request_id, None)

    def _finalize(self, run, state, permit, confirmed, error, finish=None):
        with self._transaction(True) as session:
            self._reap(session, database_now(session))
            row = self.repository.get(session, run.record.online_request_id)
            if row.state in ACTIVE_STATES and row.owner_id == self.owner_id:
                now = database_now(session)
                if state == "completed" and (row.cancel_requested or run.cancel.is_set()):
                    state, confirmed, finish = "cancelled", True, None
                    error = AppError("LLM_CANCELLED", "生成结束前已请求取消，上游已确认结束。", 409)
                elif state == "completed" and (now >= row.deadline_at or monotonic() >= run.deadline):
                    state, finish = "failed", None
                    error = AppError("LLM_TIMEOUT", "在线生成超过本次截止时间。", 504)
                self.repository.update(row, state=state, permit_state=permit, cancellation_confirmed=confirmed,
                    ended_at=now, updated_at=now, finish_reason=finish,
                    error_json={"code": error.code, "message": error.message} if error else None,
                    cancel_requested=row.cancel_requested or state == "cancelled")
            view = self._view(row)
        event = {"type": "error" if view.state == "failed" else "done", "record": view.model_dump(mode="json", by_alias=True)}
        if view.error:
            event["error"] = view.error.model_dump()
        run.terminal = event
        self.logger.info("online_finalized online_request_id=%s state=%s permit=%s", view.online_request_id, view.state, view.permit_state)

    def events(self, run, request_id):
        sequence, delivered = 0, False
        def encode(payload):
            nonlocal sequence
            sequence += 1
            data = {**payload, "sequence": sequence, "requestId": request_id,
                    "onlineRequestId": run.record.online_request_id}
            return f"event: {data['type']}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        try:
            yield encode({"type": "accepted", "record": run.record.model_dump(mode="json", by_alias=True)})
            while True:
                try:
                    text = run.outbox.get(timeout=0.2)
                    yield encode({"type": "delta", "text": text})
                except Empty:
                    if run.finished.is_set():
                        delivered = True
                        yield encode(run.terminal)
                        return
                    yield encode({"type": "heartbeat"})
        finally:
            if not delivered:
                self.disconnect(run)

    def release_uncertain(self, request_id, *, engine_stopped=False):
        if not engine_stopped:
            raise AppError("LLM_STOP_CONFIRMATION_REQUIRED", "必须先在模型服务端确认该请求已经停止。", 409)
        with self._transaction(True) as session:
            row = self.repository.get(session, request_id, CALLER_ID)
            if row is None:
                raise AppError("LLM_REQUEST_NOT_FOUND", "未找到该在线请求。", 404)
            if row.state in ACTIVE_STATES or row.permit_state != "uncertain":
                raise AppError("LLM_RELEASE_NOT_ALLOWED", "仅可核对释放已结束且占位未确认的请求。", 409)
            self.repository.update(row, permit_state="released", updated_at=database_now(session))
            return self._view(row)

    def close(self):
        with self._lock:
            self._closed = True
            runs = list(self._runs.values())
        for run in runs:
            run.cancel.set()
        end = monotonic() + 6
        for run in runs:
            run.thread.join(timeout=max(0, end - monotonic()))
