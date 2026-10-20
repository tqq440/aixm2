"""隔离 SQLite + 明确注入的人工 engine；不表示真实 LLM 模型通过。"""

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import io
import json
from threading import Event, Lock
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4
import wave

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text

from backend.common.exceptions.app_error import AppError
from backend.common.llm.engine_protocol import EngineChunk
from backend.config.llm.model_catalog import configured_spec
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.llm.online_models import OnlineRequestRecord
from backend.utils.stream.sse_utils import decode_sse_json_object, iter_sse_events


class ControlledStream:
    """可控制完成/中断/取消确认的协议替身，无任何真实网络或模型。"""

    def __init__(self, scenario="complete"):
        self.scenario = scenario
        self.entered = Event()
        self.release = Event()
        self.cancelled = Event()
        self.closed = Event()
        self.completed = False

    def events(self):
        self.entered.set()
        if self.scenario != "silent":
            yield EngineChunk("delta", "私密回答片段-仅测试")
        if self.scenario == "interrupted":
            raise AppError("LLM_STREAM_INTERRUPTED", "测试上游流中断。", 502)
        if self.scenario in ("blocked", "silent", "confirmed_cancel"):
            self.release.wait(3)
            if self.cancelled.is_set():
                return
        if self.scenario == "late_done":
            self.release.wait(3)
        if self.scenario == "flood":
            for _ in range(1000):
                if self.cancelled.is_set():
                    return
                yield EngineChunk("delta", "x" * 256)
        self.completed = True
        yield EngineChunk("done", finish_reason="stop")

    def cancel(self):
        self.cancelled.set()
        if self.scenario != "late_done":
            self.release.set()
        return self.completed or self.scenario == "confirmed_cancel"

    def close(self):
        self.closed.set()
        self.cancel()


class ControlledAdapter:
    def __init__(self, scenario="complete"):
        self.scenario = scenario
        self.calls = []
        self.streams = []
        self.lock = Lock()

    def open(self, messages, parameters, deadline):
        with self.lock:
            stream = ControlledStream(self.scenario)
            self.calls.append((messages, parameters, deadline))
            self.streams.append(stream)
        return stream


def request_payload(request_id=None, **changes):
    value = {
        "onlineRequestId": request_id or uuid4().hex, "modelId": "test-online", "modelVersion": "protocol-v1",
        "messages": [{"role": "user", "content": "私密提示词-不得落盘"}],
        "parameters": {"temperature": 0.7, "maxTokens": 32},
    }
    value.update(changes)
    return value


def stream_events(response):
    return [(event.event, decode_sse_json_object(event.data)) for event in iter_sse_events(response.response)]


@pytest.fixture
def online_factory(app_factory, tmp_path):
    adapters = []
    cases = []

    def create(*, configured=True, register=True, scenario="complete", **changes):
        folder = tmp_path / f"online-{len(cases)}"
        values = dict(database_path=folder / "online.sqlite3", log_dir=folder / "logs", upload_dir=folder / "uploads",
                      llm_admission_wait_seconds=0.15, llm_total_timeout_seconds=2,
                      llm_slow_consumer_seconds=0.2, llm_connect_timeout_seconds=0.2,
                      llm_read_timeout_seconds=0.2, llm_lease_seconds=3)
        if configured:
            values.update(llm_endpoint="http://127.0.0.1:65534/v1/chat/completions", llm_model_id="test-online",
                          llm_model_version="protocol-v1", llm_upstream_model="test-upstream",
                          llm_api_key="fixture-secret-never-persist")
        values.update(changes)
        settings = AppSettings(**values)
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.attributes["settings"] = settings
        command.upgrade(config, "head")
        app = app_factory(**values)
        registry = app.extensions["model_service"].registry
        if configured and register:
            registry.register(configured_spec(settings))
            registry.enable(settings.llm_model_id, settings.llm_model_version)
            registry.set_default(settings.llm_model_id, settings.llm_model_version)
        adapter = ControlledAdapter(scenario)
        if configured:
            app.extensions["online_service"].adapter = adapter
        adapters.append(adapter)
        case = SimpleNamespace(app=app, client=app.test_client(), settings=settings, adapter=adapter,
                               database=app.extensions["database"], registry=registry)
        cases.append(case)
        return case

    yield create
    for adapter in adapters:
        for stream in adapter.streams:
            stream.release.set()
            stream.close()


def table_counts(case):
    with case.database.engine.connect() as connection:
        return {name: connection.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar_one()
                for name in ("online_requests", "tasks", "dispatch_events", "idempotency_records", "notification_records")}


def submit_audio(case):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 1600)
    return case.client.post("/api/tasks/asr", headers={"Idempotency-Key": uuid4().hex},
                            data={"file": (io.BytesIO(output.getvalue()), "isolation.wav")})


def wait_record(case, request_id, predicate=lambda value: value["state"] in ("completed", "failed", "cancelled")):
    deadline = monotonic() + 3
    while monotonic() < deadline:
        response = case.client.get(f"/api/llm/requests/{request_id}")
        assert response.status_code == 200, response.get_json()
        record = response.get_json()["data"]
        if predicate(record):
            return record
        sleep(0.01)
    pytest.fail(f"Online record did not reach expected state: {record['state']}")


def test_unconfigured_channel_does_not_advertise_or_accept_model(online_factory):
    case = online_factory(configured=False)
    status = case.client.get("/api/llm/status").get_json()["data"]
    assert status["configured"] is False and status["availability"] == "unconfigured"
    assert status["serviceVerified"] is False
    assert case.client.get("/api/models?taskType=llm").get_json()["data"]["items"] == []
    response = case.client.post("/api/llm/requests", json=request_payload())
    assert response.status_code == 503 and response.get_json()["error"]["code"] == "LLM_NOT_CONFIGURED"
    assert all(value == 0 for value in table_counts(case).values())


def test_configuration_alone_does_not_register_or_enable_model(online_factory):
    case = online_factory(register=False)
    assert case.client.get("/api/llm/status").get_json()["data"]["configured"] is True
    assert case.client.get("/api/models?taskType=llm").get_json()["data"]["items"] == []
    response = case.client.post("/api/llm/requests", json=request_payload())
    assert response.status_code == 404 and response.get_json()["error"]["code"] == "MODEL_NOT_FOUND"
    assert not case.adapter.calls
    assert table_counts(case)["online_requests"] == 0


def test_stream_success_uses_independent_record_and_never_persists_body_or_credentials(online_factory):
    case = online_factory()
    body = request_payload()
    response = case.client.post("/api/llm/requests", json=body, headers={"X-Request-ID": "online-protocol-trace"})
    assert response.status_code == 200 and response.mimetype == "text/event-stream"
    assert response.headers["X-Request-ID"] == "online-protocol-trace"
    assert response.headers["Cache-Control"] == "no-store"
    events = stream_events(response)
    assert events[0][0] == "accepted" and events[-1][0] == "done"
    assert [event[1]["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert {event[1]["onlineRequestId"] for event in events} == {body["onlineRequestId"]}
    assert {event[1]["requestId"] for event in events} == {"online-protocol-trace"}
    assert "".join(data["text"] for name, data in events if name == "delta") == "私密回答片段-仅测试"
    record = wait_record(case, body["onlineRequestId"])
    assert record["state"] == "completed" and record["permitState"] == "released"
    assert record["firstTokenAt"] and record["endedAt"] and record["finishReason"] == "stop"
    assert record["outputCharacters"] == len("私密回答片段-仅测试")
    assert len(case.adapter.calls) == 1
    assert table_counts(case) == {"online_requests": 1, "tasks": 0, "dispatch_events": 0, "idempotency_records": 0, "notification_records": 0}
    with case.database.engine.connect() as connection:
        stored = json.dumps([dict(row) for row in connection.execute(text("SELECT * FROM online_requests")).mappings()], default=str, ensure_ascii=False)
    for secret in (body["messages"][0]["content"], "私密回答片段-仅测试", "fixture-secret-never-persist", case.settings.llm_endpoint):
        assert secret not in stored and secret.encode() not in case.settings.database_path.read_bytes()
        assert all(secret not in path.read_text(encoding="utf-8") for path in case.settings.log_dir.glob("*.log"))
    assert case.client.get("/api/tasks?taskType=all").get_json()["data"]["total"] == 0
    assert case.client.get("/api/llm/requests").get_json()["data"]["total"] == 1


def test_repeated_id_is_rejected_without_replaying_generation(online_factory):
    case = online_factory()
    body = request_payload()
    stream_events(case.client.post("/api/llm/requests", json=body))
    for replay in (body, {**body, "messages": [{"role": "user", "content": "different"}]}):
        response = case.client.post("/api/llm/requests", json=replay)
        assert response.status_code == 409 and response.get_json()["error"]["code"] == "LLM_REQUEST_EXISTS"
    assert len(case.adapter.calls) == 1 and table_counts(case)["online_requests"] == 1


def test_partial_stream_failure_keeps_metadata_and_never_reports_done(online_factory):
    case = online_factory(scenario="interrupted")
    body = request_payload()
    events = stream_events(case.client.post("/api/llm/requests", json=body))
    assert any(name == "delta" for name, _ in events)
    assert events[-1][0] == "error" and all(name != "done" for name, _ in events)
    record = wait_record(case, body["onlineRequestId"])
    assert record["state"] == "failed" and record["error"]["code"] == "LLM_STREAM_INTERRUPTED"
    assert record["permitState"] == "uncertain" and record["outputCharacters"] > 0
    assert len(case.adapter.calls) == 1


@pytest.mark.parametrize("confirmed", [False, True])
def test_cancel_persists_truth_and_only_confirmed_engine_releases_slot(online_factory, confirmed):
    case = online_factory(scenario="confirmed_cancel" if confirmed else "blocked")
    body = request_payload()
    response = case.client.post("/api/llm/requests", json=body)
    wait_record(case, body["onlineRequestId"], lambda value: value["outputCharacters"] > 0)
    cancelled = case.client.post(f"/api/llm/requests/{body['onlineRequestId']}/cancel", json={})
    assert cancelled.status_code == 200 and cancelled.get_json()["data"]["decision"] == "cancel_requested"
    events = stream_events(response)
    assert events[-1][0] == "done"
    record = wait_record(case, body["onlineRequestId"])
    assert record["state"] == "cancelled" and record["cancelRequested"] is True
    assert record["cancellationConfirmed"] is confirmed
    assert record["permitState"] == ("released" if confirmed else "uncertain")
    assert case.adapter.streams[0].cancelled.is_set() and case.adapter.streams[0].closed.is_set()
    assert case.client.post(f"/api/llm/requests/{body['onlineRequestId']}/cancel").get_json()["data"]["decision"] == "already_finished"


def test_browser_disconnect_requests_cancel_and_does_not_invent_upstream_confirmation(online_factory):
    case = online_factory(scenario="blocked")
    body = request_payload()
    response = case.client.post("/api/llm/requests", json=body)
    wait_record(case, body["onlineRequestId"], lambda value: value["outputCharacters"] > 0)
    response.close()
    record = wait_record(case, body["onlineRequestId"])
    assert record["state"] == "cancelled" and record["permitState"] == "uncertain"
    assert record["cancellationConfirmed"] is False and len(case.adapter.calls) == 1


def test_uncertain_permit_blocks_next_engine_until_explicit_verified_release(online_factory):
    case = online_factory(scenario="interrupted")
    first = request_payload()
    stream_events(case.client.post("/api/llm/requests", json=first))
    status = case.client.get("/api/llm/status").get_json()["data"]
    assert status["uncertainPermits"] == 1 and status["activeRequests"] == 0
    second = request_payload()
    second_events = stream_events(case.client.post("/api/llm/requests", json=second))
    assert second_events[-1][0] == "error"
    record = wait_record(case, second["onlineRequestId"])
    assert record["error"]["code"] == "LLM_CAPACITY_UNAVAILABLE" and record["permitState"] == "released"
    assert len(case.adapter.calls) == 1
    service = case.app.extensions["online_service"]
    with pytest.raises(AppError) as raised:
        service.release_uncertain(first["onlineRequestId"])
    assert raised.value.code == "LLM_STOP_CONFIRMATION_REQUIRED"
    service.release_uncertain(first["onlineRequestId"], engine_stopped=True)
    case.adapter.scenario = "complete"
    third = stream_events(case.client.post("/api/llm/requests", json=request_payload()))
    assert third[-1][0] == "done" and len(case.adapter.calls) == 2


def test_deadline_stops_silent_engine_without_relying_on_browser_consumption(online_factory):
    case = online_factory(scenario="silent", llm_total_timeout_seconds=0.2)
    body = request_payload()
    response = case.client.post("/api/llm/requests", json=body)
    record = wait_record(case, body["onlineRequestId"])
    assert record["state"] == "failed" and record["error"]["code"] == "LLM_TIMEOUT"
    assert record["firstTokenAt"] is None and record["outputCharacters"] == 0
    assert record["permitState"] == "uncertain" and case.adapter.streams[0].cancelled.is_set()
    assert stream_events(response)[-1][0] == "error"


def test_slow_consumer_is_bounded_and_finalizes_without_more_browser_reads(online_factory):
    case = online_factory(scenario="flood")
    body = request_payload()
    response = case.client.post("/api/llm/requests", json=body)
    record = wait_record(case, body["onlineRequestId"])
    assert record["state"] == "failed" and record["error"]["code"] == "LLM_SLOW_CONSUMER"
    assert record["outputCharacters"] < 1000 * 256
    assert stream_events(response)[-1][0] == "error"


def test_completed_metadata_remains_accessible_after_model_disabled(online_factory):
    case = online_factory()
    body = request_payload()
    stream_events(case.client.post("/api/llm/requests", json=body))
    case.registry.disable("test-online", "protocol-v1")
    assert case.client.get("/api/models?taskType=llm").get_json()["data"]["items"] == []
    assert case.client.get(f"/api/llm/requests/{body['onlineRequestId']}").get_json()["data"]["state"] == "completed"
    assert case.client.post("/api/llm/requests", json=request_payload()).status_code == 422
    assert len(case.adapter.calls) == 1


@pytest.mark.parametrize("method,url,body,status", [
    ("get", "/api/llm/status?extra=1", None, 422),
    ("get", "/api/llm/requests?page=0", None, 422),
    ("get", "/api/llm/requests?page=1&page=2", None, 422),
    ("get", "/api/llm/requests/invalid", None, 422),
    ("get", "/api/llm/requests/" + "f" * 32, None, 404),
    ("post", "/api/llm/requests", {"messages": []}, 422),
    ("post", "/api/llm/requests/" + "f" * 32 + "/cancel", {"reason": "not-supported"}, 422),
])
def test_bad_input_is_json_error_without_generation(online_factory, method, url, body, status):
    case = online_factory()
    response = getattr(case.client, method)(url, json=body) if body is not None else getattr(case.client, method)(url)
    assert response.status_code == status and response.is_json
    assert "requestId" in response.get_json() and not case.adapter.calls


@pytest.mark.parametrize("change,expected", [("cancel", "cancelled"), ("deadline", "failed"), ("lease", "failed")])
def test_terminal_commit_rechecks_prior_cancel_and_deadline(online_factory, monkeypatch, change, expected):
    case = online_factory()
    service = case.app.extensions["online_service"]
    commit_ready, allow_commit = Event(), Event()
    original = service._finalize

    def paused_commit(run, state, permit, confirmed, error, finish=None):
        if state == "completed":
            commit_ready.set()
            assert allow_commit.wait(2)
        return original(run, state, permit, confirmed, error, finish)

    monkeypatch.setattr(service, "_finalize", paused_commit)
    body = request_payload()
    response = case.client.post("/api/llm/requests", json=body)
    try:
        assert commit_ready.wait(1)
        if change == "cancel":
            cancellation = case.client.post(f"/api/llm/requests/{body['onlineRequestId']}/cancel").get_json()["data"]
            assert cancellation["decision"] == "cancel_requested"
        else:
            with case.database.transaction(immediate=True) as session:
                row = session.get(OnlineRequestRecord, body["onlineRequestId"])
                setattr(row, "deadline_at" if change == "deadline" else "lease_expires_at", datetime.utcnow() - timedelta(seconds=1))
    finally:
        allow_commit.set()
    stream_events(response)
    record = wait_record(case, body["onlineRequestId"])
    assert record["state"] == expected
    # An expired owner no longer has authority to release; reconciliation remains conservative.
    assert record["permitState"] == ("uncertain" if change == "lease" else "released")


@pytest.mark.parametrize("permit", ["waiting", "held"])
def test_orphaned_owner_is_closed_without_replay_and_preserves_unknown_permit(online_factory, permit):
    case = online_factory()
    now = datetime.utcnow()
    request_id = uuid4().hex
    with case.database.transaction() as session:
        session.add(OnlineRequestRecord(
            online_request_id=request_id, caller_id="local-user", model_id="test-online", model_version="protocol-v1",
            owner_id=uuid4().hex, instance_id=case.app.extensions["online_service"].instance_id,
            request_fingerprint="a" * 64, state="streaming" if permit == "held" else "accepted", permit_state=permit,
            accepted_at=now - timedelta(seconds=5), updated_at=now - timedelta(seconds=5),
            deadline_at=now + timedelta(seconds=10), lease_expires_at=now - timedelta(seconds=1),
        ))
    record = case.client.get(f"/api/llm/requests/{request_id}").get_json()["data"]
    assert record["state"] == "failed" and record["error"]["code"] == "LLM_OWNER_LOST"
    assert record["permitState"] == ("uncertain" if permit == "held" else "released")
    assert not case.adapter.calls


def test_finalize_storage_failure_never_announces_success_or_releases_persisted_permit(online_factory, monkeypatch):
    case = online_factory()
    service = case.app.extensions["online_service"]

    def unavailable(*_args, **_kwargs):
        raise AppError("LLM_STORAGE_UNAVAILABLE", "测试：数据库不可写。", 503)

    monkeypatch.setattr(service, "_finalize", unavailable)
    body = request_payload()
    events = stream_events(case.client.post("/api/llm/requests", json=body))
    assert events[-1][0] == "error" and events[-1][1]["error"]["code"] == "LLM_STORAGE_UNAVAILABLE"
    assert all(name != "done" for name, _ in events)
    with case.database.transaction() as session:
        row = session.get(OnlineRequestRecord, body["onlineRequestId"])
        assert row.state == "streaming" and row.permit_state == "held"
        row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    # A later read reconciles metadata, while the unconfirmed occupancy remains conservative.
    record = wait_record(case, body["onlineRequestId"])
    assert record["state"] == "failed" and record["permitState"] == "uncertain"
    assert len(case.adapter.calls) == 1


def test_waiting_request_can_cancel_without_opening_second_engine(online_factory):
    case = online_factory(scenario="blocked", llm_admission_wait_seconds=1)
    first = request_payload()
    first_response = case.client.post("/api/llm/requests", json=first)
    wait_record(case, first["onlineRequestId"], lambda value: value["outputCharacters"] > 0)
    second = request_payload()
    second_response = case.client.post("/api/llm/requests", json=second)
    wait_record(case, second["onlineRequestId"], lambda value: value["state"] == "waiting_instance")
    case.client.post(f"/api/llm/requests/{second['onlineRequestId']}/cancel")
    second_events = stream_events(second_response)
    record = wait_record(case, second["onlineRequestId"])
    assert second_events[-1][0] == "done" and record["state"] == "cancelled"
    assert record["permitState"] == "released" and len(case.adapter.calls) == 1
    first_response.close()
    wait_record(case, first["onlineRequestId"])


def test_late_done_after_cancellation_cannot_replace_terminal_or_release_uncertain_permit(online_factory):
    case = online_factory(scenario="late_done")
    body = request_payload()
    response = case.client.post("/api/llm/requests", json=body)
    wait_record(case, body["onlineRequestId"], lambda value: value["outputCharacters"] > 0)
    case.client.post(f"/api/llm/requests/{body['onlineRequestId']}/cancel")
    stream_events(response)
    before = wait_record(case, body["onlineRequestId"])
    stream = case.adapter.streams[0]
    stream.release.set()
    until = monotonic() + 1
    while not stream.completed and monotonic() < until:
        sleep(0.01)
    assert stream.completed and stream.closed.is_set()
    after = wait_record(case, body["onlineRequestId"])
    assert after == before and after["state"] == "cancelled" and after["permitState"] == "uncertain"


def test_online_active_request_counts_against_asr_global_admission(online_factory):
    case = online_factory(scenario="blocked", max_inflight_tasks=1)
    body = request_payload()
    response = case.client.post("/api/llm/requests", json=body)
    wait_record(case, body["onlineRequestId"], lambda value: value["outputCharacters"] > 0)
    rejected = submit_audio(case)
    assert rejected.status_code == 429 and rejected.get_json()["error"]["code"] == "TASK_CAPACITY_EXCEEDED"
    assert table_counts(case)["tasks"] == 0
    response.close()
    wait_record(case, body["onlineRequestId"])


def test_asr_inflight_task_counts_against_online_global_admission(online_factory):
    case = online_factory(max_inflight_tasks=1)
    assert submit_audio(case).status_code == 201
    rejected = case.client.post("/api/llm/requests", json=request_payload())
    assert rejected.status_code == 429 and rejected.get_json()["error"]["code"] == "INFLIGHT_LIMIT_EXCEEDED"
    assert table_counts(case)["online_requests"] == 0 and not case.adapter.calls


def test_concurrent_same_id_creates_exactly_one_generation(online_factory):
    case = online_factory()
    body = request_payload()

    def post():
        response = case.app.test_client().post("/api/llm/requests", json=body)
        if response.status_code == 200:
            stream_events(response)
        return response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _value: post(), range(2)))
    assert sorted(statuses) == [200, 409]
    assert len(case.adapter.calls) == 1 and table_counts(case)["online_requests"] == 1
