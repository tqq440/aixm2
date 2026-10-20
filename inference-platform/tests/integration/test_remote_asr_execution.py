"""外部 ASR 的真实 SQLite/HTTP 事务；健康与转写结果均为隔离夹具。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import BytesIO
from threading import Barrier
from types import SimpleNamespace
import wave

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import select

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import PROJECT_ROOT
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.broker.broker_repository import BrokerRepository
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.data.tasks.task_models import DispatchEvent, TaskRecord
from backend.schema.asr.asr_schema import AsrEngineMetadata, TranscriptionResult
from backend.schema.asr.remote_schema import RemoteAsrRelease
from backend.schema.execution.execution_schema import FailureInfo
from backend.service.asr.adapter_catalog import config_fingerprint, remote_asr_model_spec
from backend.service.asr.remote_service import REMOTE_QUEUE, RemoteAsrService
from backend.service.dispatch.dispatch_service import DispatchService
from backend.service.execution.execution_service import ExecutionService
from backend.service.models.model_registry_service import ModelRegistryService
from backend.service.recovery.recovery_service import RecoveryService


@pytest.fixture
def remote_case(app_factory, tmp_path, monkeypatch):
    app = app_factory(upload_dir=tmp_path / "uploads", remote_asr_base_url="https://isolated.test:8003",
                      remote_asr_model_id="fixture-asr", remote_asr_model_version="fixture-release-1")
    settings = app.config["APP_SETTINGS"]
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    database = app.extensions["database"]
    registry = ModelRegistryService(database, settings)
    spec = remote_asr_model_spec(settings)
    registry.register(spec)
    registry.enable(spec.model_id, spec.model_version)
    registry.set_default(spec.model_id, spec.model_version)
    worker_id = "1" * 32
    runtime = RuntimeRepository(database)
    remote = RemoteAsrService(database, settings)
    remote.begin_worker(worker_id)
    runtime.report(component_id="remote_asr_worker", session_id=worker_id, pid=123,
                   state="ready", model_ready=True)
    monkeypatch.setattr("backend.service.asr.remote_service.probe_remote_asr", lambda _: SimpleNamespace(ready=True))
    buffer = BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 1600)
    content = buffer.getvalue()
    client = app.test_client()
    executor = ExecutionService(database, settings, asr_profile="remote")
    dispatcher = DispatchService(database, settings, app.extensions["app_logger"])
    broker = BrokerRepository(database)

    def submit(key="fixture-request", **fields):
        return client.post("/api/tasks/asr", headers={"Idempotency-Key": key},
                           data={"modelId": spec.model_id, "file": (BytesIO(content), "fixture.wav"), **fields})

    def claim():
        dispatcher.tick()
        delivery = broker.receive(worker_id=worker_id, queue_key=REMOTE_QUEUE, visibility_seconds=30)
        assert delivery is not None
        acquired = executor.acquire(delivery)
        if acquired.lease:
            runtime.report(component_id="remote_asr_worker", session_id=worker_id, pid=123,
                           state="running", model_ready=True, active_task_id=acquired.lease.task_id)
        return delivery, acquired

    return SimpleNamespace(app=app, client=client, settings=settings, db=database, registry=registry,
        spec=spec, worker_id=worker_id, runtime=runtime, remote=remote, executor=executor,
        dispatcher=dispatcher, broker=broker, submit=submit, claim=claim)


def fixture_result():
    return TranscriptionResult(text="仅隔离事务测试", segments=[], audio_duration_seconds=0.1,
                               engine_metadata=AsrEngineMetadata(adapter_key="remote-v2-sensevoice"))


def read_rows(case, task_id):
    with case.db.session_factory() as session:
        return session.get(TaskRecord, task_id), session.get(RemoteAsrCall, task_id)


def submit_task(case, key="fixture-request"):
    response = case.submit(key)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["task"]["taskId"]


def test_remote_submission_is_registered_atomic_and_does_not_claim_model_running(remote_case):
    case = remote_case
    task_id = submit_task(case)
    task, call = read_rows(case, task_id)
    assert task.status == "created" and task.attempt == 0 and task.max_attempts == 1
    assert task.queue_key == REMOTE_QUEUE and task.parameters == {"language": None}
    assert call.state == "queued" and call.request_id == task_id
    assert call.config_fingerprint == config_fingerprint(case.settings)
    assert call.reserved_at is call.worker_id is None
    status = case.client.get("/api/asr/external/status").get_json()["data"]
    assert status["state"] == "ready" and status["available"]
    assert "isolated.test" not in str(status)


def test_registry_disable_and_language_validation_prevent_new_task(remote_case):
    case = remote_case
    invalid = case.submit(language="zh")
    assert invalid.status_code == 422
    case.registry.disable(case.spec.model_id, case.spec.model_version)
    response = case.submit()
    assert response.status_code == 422 and response.get_json()["error"]["code"] == "MODEL_DISABLED"
    with case.db.session_factory() as session:
        assert session.scalars(select(RemoteAsrCall)).all() == []


@pytest.mark.parametrize("queued", [False, True])
def test_cancel_before_claim_releases_without_an_attempt(remote_case, queued):
    case = remote_case
    task_id = submit_task(case)
    if queued:
        case.dispatcher.tick()
    response = case.client.post(f"/api/tasks/{task_id}/cancel")
    assert response.status_code == 200
    task, call = read_rows(case, task_id)
    assert task.status == "cancelled" and task.attempt == 0 and call.state == "released"
    assert call.reserved_at is None


def test_claim_and_cancel_compete_in_one_sqlite_transaction(remote_case):
    case = remote_case
    task_id = submit_task(case)
    case.dispatcher.tick()
    delivery = case.broker.receive(worker_id=case.worker_id, queue_key=REMOTE_QUEUE, visibility_seconds=30)
    barrier = Barrier(2)
    def claim():
        barrier.wait()
        return case.executor.acquire(delivery).decision
    def cancel():
        barrier.wait()
        try:
            return case.executor.request_cancel(task_id, "local-user")
        except AppError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        claim_future, cancel_future = pool.submit(claim), pool.submit(cancel)
        outcomes = claim_future.result(), cancel_future.result()
    task, call = read_rows(case, task_id)
    assert outcomes in {("acquired", "REMOTE_ASR_CANCEL_UNSUPPORTED"), ("terminal_or_stale", "cancelled")}
    assert (task.status, task.attempt, call.state) in {("running", 1, "held"), ("cancelled", 0, "released")}


def test_success_commit_replay_and_post_commit_crash_do_not_resend(remote_case):
    case = remote_case
    task_id = submit_task(case)
    delivery, decision = case.claim()
    assert decision.decision == "acquired"
    rejected = case.client.post(f"/api/tasks/{task_id}/cancel")
    assert rejected.status_code == 409
    assert rejected.get_json()["error"]["code"] == "REMOTE_ASR_CANCEL_UNSUPPORTED"
    assert case.executor.commit_result(decision.lease, fixture_result())
    assert case.executor.commit_result(decision.lease, fixture_result())
    # 模拟提交事务完成后 Worker 退出、未确认 broker；成功结果不能被改成未知。
    case.remote.confirm_local_exit(case.worker_id)
    assert case.executor.acquire(delivery).decision == "terminal_or_stale"
    task, call = read_rows(case, task_id)
    assert task.status == "succeeded" and task.attempt == 1 and call.state == "released"
    assert case.client.get(f"/api/tasks/{task_id}/result").get_json()["data"]["output"]["text"] == "仅隔离事务测试"
    case.registry.disable(case.spec.model_id, case.spec.model_version)
    replay = case.submit()
    assert replay.status_code == 200 and replay.get_json()["data"]["replayed"]


@pytest.mark.parametrize("code,confirmed", [
    ("REMOTE_ASR_NOT_SENT", True), ("REMOTE_ASR_REJECTED", True),
    ("REMOTE_ASR_UNKNOWN", False), ("WORKER_CHILD_EXITED", False),
])
def test_failures_never_retry_and_only_proven_outcomes_release(remote_case, code, confirmed):
    case = remote_case
    task_id = submit_task(case)
    delivery, decision = case.claim()
    error = FailureInfo(code=code, message="隔离注入")
    assert case.executor.commit_failure(decision.lease, error)
    assert case.executor.commit_failure(decision.lease, error)
    task, call = read_rows(case, task_id)
    assert task.status == "failed" and task.attempt == task.max_attempts == 1
    assert call.state == ("released" if confirmed else "uncertain")
    assert task.next_retry_at is None
    with case.db.session_factory() as session:
        assert len(session.scalars(select(DispatchEvent).where(DispatchEvent.task_id == task_id)).all()) == 1
    assert case.executor.acquire(delivery).decision == "terminal_or_stale"


def test_unknown_survives_restart_and_requires_explicit_manual_resolution(remote_case):
    case = remote_case
    task_id = submit_task(case)
    _, decision = case.claim()
    assert case.executor.commit_failure(decision.lease, FailureInfo(code="REMOTE_ASR_UNKNOWN", message="断线"))
    case.db.dispose()
    case.remote.begin_worker("2" * 32)
    case.runtime.report(component_id="remote_asr_worker", session_id="2" * 32, pid=456, state="ready", model_ready=True)
    assert case.remote.status().state == "uncertain"
    assert case.submit("second").status_code == 503
    with pytest.raises(AppError, match="须先核对"):
        case.remote.release_uncertain(RemoteAsrRelease(task_id=task_id, note="未明确确认"))
    command = RemoteAsrRelease(task_id=task_id, confirm_request_ended=True, note="仅隔离测试：假上游已结束")
    assert case.remote.release_uncertain(command)["replayed"] is False
    assert case.remote.release_uncertain(command)["replayed"] is True
    task, call = read_rows(case, task_id)
    assert call.state == "released" and call.resolved_at is not None
    assert task.status == "failed" and task.error_json["code"] == "REMOTE_ASR_RESULT_UNKNOWN"
    assert not case.executor.commit_result(decision.lease, fixture_result())
    assert case.submit("third").status_code == 201


def test_expired_lease_and_crash_after_reservation_are_unknown_not_retry(remote_case):
    case = remote_case
    task_id = submit_task(case)
    _, decision = case.claim()
    with case.db.transaction(immediate=True) as session:
        session.get(TaskRecord, task_id).lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    assert RecoveryService(case.db, case.settings).recover_task(task_id)
    task, call = read_rows(case, task_id)
    assert task.status == "failed" and task.attempt == 1 and call.state == "uncertain"
    assert not case.executor.commit_result(decision.lease, fixture_result())


def test_exit_after_reserved_before_post_is_conservatively_unknown(remote_case):
    case = remote_case
    task_id = submit_task(case)
    case.claim()  # 没有启动任何网络调用，模拟此后崩溃；持久化事实仍不能证明未发送。
    case.remote.confirm_local_exit(case.worker_id)
    task, call = read_rows(case, task_id)
    assert task.status == "failed" and call.state == "uncertain"


def test_config_change_cannot_redirect_previously_queued_task(remote_case):
    case = remote_case
    task_id = submit_task(case)
    case.dispatcher.tick()
    delivery = case.broker.receive(worker_id=case.worker_id, queue_key=REMOTE_QUEUE, visibility_seconds=30)
    other = case.settings.model_copy(update={"remote_asr_base_url": "https://another-isolated.test:8003"})
    assert ExecutionService(case.db, other, asr_profile="remote").acquire(delivery).decision == "busy"
    task, call = read_rows(case, task_id)
    assert task.attempt == 0 and task.status == "queued" and call.state == "queued"
    assert call.config_fingerprint == config_fingerprint(case.settings)


def test_drain_retains_deadlines_and_blocks_new_admission_and_claim(remote_case):
    case = remote_case
    first, second = submit_task(case), submit_task(case, "second")
    _, decision = case.claim()
    before, _ = read_rows(case, second)
    case.remote.begin_drain(case.worker_id)
    status = case.remote.status()
    assert status.state == "stopping" and status.current_task_id == first and not status.available
    assert case.submit("third").status_code == 503
    assert case.executor.commit_result(decision.lease, fixture_result())
    delivery = case.broker.receive(worker_id=case.worker_id, queue_key=REMOTE_QUEUE, visibility_seconds=30)
    assert case.executor.acquire(delivery).decision == "busy"
    after, call = read_rows(case, second)
    assert after.queue_deadline_at == before.queue_deadline_at and after.total_deadline_at == before.total_deadline_at
    assert after.attempt == 0 and call.state == "queued"


def test_unknown_external_channel_does_not_block_default_whisper_submission(remote_case):
    case = remote_case
    submit_task(case)
    case.claim()
    case.remote.confirm_local_exit(case.worker_id)
    assert case.remote.status().state == "uncertain"
    local = case.submit("local", modelId="whisper-small")
    assert local.status_code == 201
    task_id = local.get_json()["data"]["task"]["taskId"]
    task, call = read_rows(case, task_id)
    assert task.queue_key == "asr.whisper-small" and call is None and task.max_attempts == 2


@pytest.mark.parametrize("transition", ["new_session", "drain"])
def test_final_admission_transaction_rechecks_worker_after_early_observation(remote_case, monkeypatch, transition):
    case = remote_case
    submit_task(case)
    case.claim()
    service = case.app.extensions["task_service"].remote_asr
    original = service.ensure_available
    def switch_after_observation():
        original()
        if transition == "new_session":
            case.remote.begin_worker("2" * 32)
            case.runtime.report(component_id="remote_asr_worker", session_id="2" * 32, pid=456,
                               state="ready", model_ready=True)
        else:
            case.remote.begin_drain(case.worker_id)
    monkeypatch.setattr(service, "ensure_available", switch_after_observation)
    response = case.submit("must-not-enter")
    assert response.status_code == 503
    with case.db.session_factory() as session:
        assert len(session.scalars(select(RemoteAsrCall)).all()) == 1
        assert len(session.scalars(select(TaskRecord)).all()) == 1


def test_repeated_drain_does_not_overwrite_confirmed_local_stopped_state(remote_case):
    case = remote_case
    case.runtime.report(component_id="remote_asr_worker", session_id=case.worker_id, pid=123,
                       state="stopped", model_ready=False)
    case.remote.begin_drain(case.worker_id)
    case.remote.begin_drain()
    assert case.remote.local_shutdown_confirmed(123, None)


def test_expiry_at_claim_releases_unsent_call_without_attempt(remote_case):
    case = remote_case
    task_id = submit_task(case)
    case.dispatcher.tick()
    delivery = case.broker.receive(worker_id=case.worker_id, queue_key=REMOTE_QUEUE, visibility_seconds=30)
    with case.db.transaction(immediate=True) as session:
        session.get(TaskRecord, task_id).queue_deadline_at = datetime.utcnow() - timedelta(seconds=1)
    assert case.executor.acquire(delivery).decision == "terminal_or_stale"
    task, call = read_rows(case, task_id)
    assert task.status == "failed" and task.attempt == 0
    assert call.state == "released" and call.reserved_at is None


def test_remote_stop_intent_survives_error_and_ready_reports_until_confirmed_stopped(remote_case):
    case = remote_case
    task_id = submit_task(case)
    case.dispatcher.tick()
    case.remote.begin_drain(case.worker_id)
    for state in ("error", "ready"):
        case.runtime.report(component_id="remote_asr_worker", session_id=case.worker_id, pid=123,
                           state=state, model_ready=state == "ready",
                           last_error="WORKER_STOP_UNCONFIRMED" if state == "error" else None)
        with case.db.session_factory() as session:
            row = case.remote.repository.runtime(session)
            assert row.state == "stopping"
            if state == "error":
                assert row.last_error == "WORKER_STOP_UNCONFIRMED"
            assert not case.remote.can_receive(session, case.worker_id)
        assert case.remote.draining(case.worker_id)
        assert case.remote.status().state == "stopping"
        assert case.submit("rejected-" + state).status_code == 503
        assert case.broker.receive(worker_id=case.worker_id, queue_key=REMOTE_QUEUE, visibility_seconds=30,
            receive_check=lambda session: case.remote.can_receive(session, case.worker_id)) is None
    task, call = read_rows(case, task_id)
    assert task.status == "queued" and task.attempt == 0 and call.state == "queued"
    case.runtime.report(component_id="remote_asr_worker", session_id=case.worker_id, pid=123,
                       state="stopped", model_ready=False)
    assert case.remote.local_shutdown_confirmed(123, None)
