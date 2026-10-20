"""M05 真实 SQLite 租约/提交事务测试；结果内容是测试夹具，不代表模型验收。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from uuid import uuid4
import logging
import hashlib
import json

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from backend.config.asr.model_catalog import MODEL_ID, QUEUE_KEY
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.broker.broker_models import NotificationRecord
from backend.data.broker import broker_repository
from backend.data.broker.broker_repository import BrokerRepository
from backend.data.database.database import Database
from backend.data.execution import execution_repository
from backend.data.execution.attempt_models import ExecutionAttempt
from backend.data.recovery import recovery_repository
from backend.data.broker import dispatch_repository
from backend.data.tasks.task_models import DispatchEvent, TaskRecord
from backend.schema.asr.asr_schema import TranscriptionResult
from backend.schema.execution.execution_schema import Delivery, FailureInfo
from backend.service.execution.execution_service import ExecutionService
from backend.service.dispatch import dispatch_service
from backend.service.dispatch.dispatch_service import DispatchService
from backend.service.recovery.recovery_service import RecoveryService
from backend.common.exceptions.app_error import AppError


@pytest.fixture
def execution_case(tmp_path, monkeypatch):
    settings = AppSettings(database_path=tmp_path / "lease.sqlite3", log_dir=tmp_path / "logs",
                           upload_dir=tmp_path / "uploads")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    database = Database(settings)
    clock = {"now": datetime(2026, 10, 12, 10, 0, 0)}
    monkeypatch.setattr(execution_repository, "database_now", lambda session: clock["now"])
    task_id, event_id, notification_id = uuid4().hex, uuid4().hex, uuid4().hex
    with database.transaction() as session:
        session.add(TaskRecord(
            task_id=task_id, caller_id="local-user", task_type="asr", model_id=MODEL_ID,
            model_version=settings.asr_model_revision, parameters={"language": "zh"},
            input_ref=uuid4().hex, input_filename="测试.wav", input_sha256="1" * 64,
            input_size_bytes=32044, audio_duration_seconds=1.0, status="queued", attempt=0,
            dispatch_version=1, execution_plan_version=1, queue_key=QUEUE_KEY,
            created_at=clock["now"], updated_at=clock["now"],
        ))
        session.flush()
        session.add(DispatchEvent(event_id=event_id, task_id=task_id, queue_key=QUEUE_KEY,
            status="published", dispatch_version=1, execution_plan_version=1,
            available_at=clock["now"], created_at=clock["now"]))
        session.flush()
        session.add(NotificationRecord(notification_id=notification_id, event_id=event_id,
            task_id=task_id, queue_key=QUEUE_KEY, dispatch_version=1, execution_plan_version=1,
            status="inflight", delivery_token="delivery-token", delivery_owner="worker-one",
            visible_at=clock["now"] + timedelta(seconds=120), created_at=clock["now"], deliveries=1))
    delivery = Delivery(notification_id=notification_id, task_id=task_id, worker_id="worker-one",
        delivery_token="delivery-token", dispatch_version=1, execution_plan_version=1, queue_key=QUEUE_KEY)
    service = ExecutionService(database, settings)
    yield {"settings": settings, "db": database, "service": service, "clock": clock, "delivery": delivery}
    database.dispose()


@pytest.fixture
def saved_task(execution_case):
    def read():
        with execution_case["db"].session_factory() as session:
            return session.get(TaskRecord, execution_case["delivery"].task_id)
    return read


@pytest.fixture
def lease(execution_case):
    decision = execution_case["service"].acquire(execution_case["delivery"])
    assert decision.decision == "acquired"
    return decision.lease


@pytest.fixture
def result(execution_case):
    return TranscriptionResult(
        text="仅用于事务测试", segments=[{"start": 0, "end": 1, "text": "仅用于事务测试"}],
        language="zh", language_requested="zh", language_probability=1.0,
        audio_duration_seconds=1.0, model_repo=execution_case["settings"].asr_model_repo,
        model_revision=execution_case["settings"].asr_model_revision,
        device="cpu", compute_type="int8_float32", cpu_threads=4,
        model_load_seconds=0.1, transcription_seconds=0.2, total_seconds=0.3,
        faster_whisper_version="test", ctranslate2_version="test",
    )


def test_acquire_uses_authoritative_task_and_only_increments_attempt_once(execution_case, saved_task):
    service, delivery = execution_case["service"], execution_case["delivery"]
    acquired = service.acquire(delivery)
    task = saved_task()
    assert acquired.decision == "acquired"
    assert acquired.input.task_id == task.task_id
    assert acquired.input.input_ref == task.input_ref
    assert acquired.input.language == "zh"
    assert acquired.lease.lease_token == task.lease_token
    assert task.status == "running" and task.attempt == 1
    assert task.started_at == execution_case["clock"]["now"]
    assert task.lease_expires_at == task.started_at + timedelta(seconds=30)
    assert service.acquire(delivery).decision == "busy"
    assert saved_task().attempt == 1


@pytest.mark.parametrize("changes", [
    {"notification_id": "0" * 32}, {"task_id": "0" * 32}, {"worker_id": "another-worker"},
    {"delivery_token": "another-token"}, {"dispatch_version": 2}, {"execution_plan_version": 2},
    {"queue_key": "another.queue"},
])
def test_altered_notification_cannot_grant_execution(execution_case, saved_task, changes):
    delivery = execution_case["delivery"].model_copy(update=changes)
    assert execution_case["service"].acquire(delivery).decision == "busy"
    assert saved_task().attempt == 0 and saved_task().status == "queued"


@pytest.mark.parametrize("changes", [
    {"status": "ready"}, {"status": "acked"}, {"delivery_token": "lost-token"},
    {"delivery_owner": "another-worker"}, {"visible_at": datetime(2026, 10, 12, 10)},
    {"queue_key": "another.queue"}, {"execution_plan_version": 2}, {"dispatch_version": 2},
])
def test_notification_database_record_is_checked(execution_case, saved_task, changes):
    with execution_case["db"].transaction() as session:
        notification = session.get(NotificationRecord, execution_case["delivery"].notification_id)
        for name, value in changes.items():
            setattr(notification, name, value)
    assert execution_case["service"].acquire(execution_case["delivery"]).decision == "busy"
    assert saved_task().attempt == 0


@pytest.mark.parametrize(("changes", "decision"), [
    ({"status": "created"}, "busy"), ({"status": "succeeded"}, "terminal_or_stale"),
    ({"status": "failed"}, "terminal_or_stale"), ({"status": "cancelled"}, "terminal_or_stale"),
    ({"attempt": 1}, "busy"), ({"model_id": "wrong-model"}, "busy"),
    ({"model_version": "wrong-version"}, "busy"),
    ({"queue_key": "wrong.queue"}, "terminal_or_stale"),
    ({"execution_plan_version": 2}, "terminal_or_stale"), ({"dispatch_version": 2}, "terminal_or_stale"),
])
def test_only_unattempted_queued_matching_plan_can_start(execution_case, changes, decision):
    with execution_case["db"].transaction() as session:
        task = session.get(TaskRecord, execution_case["delivery"].task_id)
        for name, value in changes.items():
            setattr(task, name, value)
    assert execution_case["service"].acquire(execution_case["delivery"]).decision == decision


def test_expired_notification_stays_unconfirmed_and_can_be_reclaimed(execution_case, saved_task, monkeypatch):
    clock = execution_case["clock"]
    clock["now"] += timedelta(seconds=120)
    decision = execution_case["service"].acquire(execution_case["delivery"])
    # 领取到通知后调度暂停，直到可见期到期；busy 明确要求 Worker 不确认。
    assert decision.decision == "busy" and decision.lease is None
    assert saved_task().status == "queued" and saved_task().attempt == 0
    with execution_case["db"].session_factory() as session:
        notification = session.get(NotificationRecord, execution_case["delivery"].notification_id)
        assert notification.status == "inflight" and notification.acked_at is None
    monkeypatch.setattr(broker_repository, "database_now", lambda session: clock["now"])
    reclaimed = BrokerRepository(execution_case["db"]).receive(
        worker_id="worker-two", queue_key=QUEUE_KEY, visibility_seconds=30,
    )
    assert reclaimed.delivery_token != execution_case["delivery"].delivery_token
    assert execution_case["service"].acquire(reclaimed).decision == "acquired"
    assert saved_task().attempt == 1


def test_running_task_is_not_taken_over_after_lease_expiration(execution_case, lease, saved_task):
    execution_case["clock"]["now"] += timedelta(seconds=31)
    assert execution_case["service"].acquire(execution_case["delivery"]).decision == "busy"
    assert saved_task().attempt == 1 and saved_task().lease_token == lease.lease_token


def test_renewal_extends_server_deadline_and_original_dto_remains_usable(execution_case, lease, result, saved_task):
    execution_case["clock"]["now"] += timedelta(seconds=20)
    assert execution_case["service"].renew(lease) is True
    assert saved_task().lease_expires_at == execution_case["clock"]["now"] + timedelta(seconds=30)
    execution_case["clock"]["now"] += timedelta(seconds=20)
    assert lease.lease_expires_at < execution_case["clock"]["now"]
    assert execution_case["service"].commit_result(lease, result) is True


@pytest.mark.parametrize("changes", [
    {"task_id": "0" * 32}, {"worker_id": "other-worker"}, {"lease_token": "other-token"},
    {"attempt": 2}, {"dispatch_version": 2}, {"execution_plan_version": 2},
])
def test_foreign_lease_cannot_renew_or_finish(execution_case, lease, result, saved_task, changes):
    stale = lease.model_copy(update=changes)
    service = execution_case["service"]
    assert service.renew(stale) is False
    assert service.commit_result(stale, result) is False
    assert service.commit_failure(stale, FailureInfo(code="TEST_ERROR", message="测试失败")) is False
    assert saved_task().status == "running"


@pytest.mark.parametrize("late_seconds", [30, 31])
def test_expired_lease_rejects_all_writes_including_exact_deadline(execution_case, lease, result, saved_task, late_seconds):
    execution_case["clock"]["now"] += timedelta(seconds=late_seconds)
    service = execution_case["service"]
    assert service.renew(lease) is False
    assert service.commit_result(lease, result) is False
    assert service.commit_failure(lease, FailureInfo(code="TEST_ERROR", message="测试失败")) is False
    task = saved_task()
    assert task.status == "running" and task.result_json is None and task.error_json is None
    assert task.finished_at is None


@pytest.mark.parametrize("changes", [
    {"model_repo": "other/repo"}, {"model_revision": "0" * 40}, {"language_requested": "en"},
    {"language": "en"}, {"audio_duration_seconds": 2.0},
])
def test_result_must_match_task_model_language_and_audio(execution_case, lease, result, saved_task, changes):
    assert execution_case["service"].commit_result(lease, result.model_copy(update=changes)) is False
    assert saved_task().status == "running"


def test_success_is_persistent_idempotent_and_cannot_be_overwritten(execution_case, lease, result, saved_task):
    service = execution_case["service"]
    assert service.commit_result(lease, result) is True
    task = saved_task()
    assert task.status == "succeeded" and task.attempt == 1
    assert task.result_json == result.model_dump(mode="json", by_alias=True)
    assert task.error_json is None and task.finished_at == execution_case["clock"]["now"]
    fingerprint, finished_at = task.result_fingerprint, task.finished_at
    execution_case["clock"]["now"] += timedelta(days=1)
    assert service.commit_result(lease, result) is True
    assert service.commit_result(lease, result.model_copy(update={"text": "不能覆盖"})) is False
    assert service.commit_result(lease.model_copy(update={"lease_token": "another-token"}), result) is False
    assert service.commit_failure(lease, FailureInfo(code="TEST_ERROR", message="不能覆盖")) is False
    assert service.renew(lease) is False
    assert saved_task().result_fingerprint == fingerprint and saved_task().finished_at == finished_at


def test_historical_whisper_payload_and_fingerprint_remain_exactly_replayable(execution_case, lease, saved_task):
    """按旧版实际字段形状保存，不能靠新 Schema 生成所谓旧数据。"""
    settings = execution_case["settings"]
    legacy = {
        "text": "历史事务测试结果", "segments": [{"start": 0.0, "end": 1.0, "text": "历史事务测试结果"}],
        "language": "zh", "languageRequested": "zh", "languageProbability": 1.0,
        "audioDurationSeconds": 1.0, "modelRepo": settings.asr_model_repo,
        "modelRevision": settings.asr_model_revision, "device": "cpu", "computeType": "int8_float32",
        "cpuThreads": 4, "modelLoadSeconds": 0.1, "transcriptionSeconds": 0.2, "totalSeconds": 0.3,
        "fasterWhisperVersion": "test", "ctranslate2Version": "test",
    }
    fingerprint = hashlib.sha256(json.dumps(
        {"status": "succeeded", "result": legacy}, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()
    service = execution_case["service"]
    with service.repository.lock_task(lease.task_id) as tx:
        tx.finish(status="succeeded", result=legacy, error=None, fingerprint=fingerprint)
        tx.finish_attempt(outcome="succeeded")
    parsed = TranscriptionResult.model_validate(legacy)
    assert parsed.engine_metadata is None
    assert parsed.model_dump(mode="json", by_alias=True) == legacy
    assert service.commit_result(lease, parsed)
    assert saved_task().result_json == legacy
    assert saved_task().result_fingerprint == fingerprint


@pytest.mark.parametrize("metadata", [
    {"adapterKey": "unknown-adapter"},
    {"adapterKey": "future-http-asr", "runtimeVersions": {"future-library": "1.0"}},
])
def test_generic_metadata_does_not_authorize_an_unknown_adapter_for_whisper_task(execution_case, lease, result, saved_task, metadata):
    invalid = TranscriptionResult.model_validate({**result.model_dump(), "engine_metadata": metadata})
    assert not execution_case["service"].commit_result(lease, invalid)
    assert saved_task().status == "running" and saved_task().result_json is None


def test_generic_shape_without_model_identity_is_not_a_whisper_success(execution_case, lease, saved_task):
    generic = TranscriptionResult(text="不能冒充Whisper执行", segments=[], language="zh", language_requested="zh",
                                  audio_duration_seconds=1)
    assert not execution_case["service"].commit_result(lease, generic)
    assert saved_task().status == "running" and saved_task().result_json is None


def test_failure_is_persistent_idempotent_and_protects_terminal_state(execution_case, lease, result, saved_task):
    service = execution_case["service"]
    failure = FailureInfo(code="ASR_FAILED", message="本次推理失败")
    assert service.commit_failure(lease, failure) is True
    task = saved_task()
    assert task.status == "failed" and task.result_json is None
    assert task.error_json == {"code": "ASR_FAILED", "message": "本次推理失败"}
    assert task.finished_at == execution_case["clock"]["now"]
    execution_case["clock"]["now"] += timedelta(days=1)
    assert service.commit_failure(lease, failure) is True
    assert service.commit_failure(lease, FailureInfo(code="OTHER_ERROR", message="其他错误")) is False
    assert service.commit_failure(lease.model_copy(update={"worker_id": "another-worker"}), failure) is False
    assert service.commit_result(lease, result) is False


@pytest.mark.parametrize("operation", ["acquire", "renew", "success", "failure"])
def test_database_write_failure_rolls_back_entire_transition(execution_case, result, saved_task, operation):
    service = execution_case["service"]
    lease = None if operation == "acquire" else service.acquire(execution_case["delivery"]).lease
    before = saved_task().to_dict()
    execution_case["clock"]["now"] += timedelta(seconds=1)
    engine = execution_case["db"].engine

    def fail_after_update(connection, cursor, statement, parameters, context, executemany):
        if statement.upper().startswith("UPDATE TASKS"):
            raise OperationalError("test-update", {}, RuntimeError("injected execution write failure"))

    event.listen(engine, "after_cursor_execute", fail_after_update)
    try:
        with pytest.raises(OperationalError):
            if operation == "acquire":
                service.acquire(execution_case["delivery"])
            elif operation == "renew":
                service.renew(lease)
            elif operation == "success":
                service.commit_result(lease, result)
            else:
                service.commit_failure(lease, FailureInfo(code="TEST_ERROR", message="测试失败"))
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_update)
    assert saved_task().to_dict() == before


def test_concurrent_duplicate_acquisition_grants_only_one_lease(execution_case, saved_task):
    barrier = Barrier(2)

    def acquire():
        barrier.wait(timeout=5)
        return execution_case["service"].acquire(execution_case["delivery"]).decision

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: acquire(), range(2)))
    assert sorted(results) == ["acquired", "busy"]
    assert saved_task().attempt == 1


def test_concurrent_success_and_failure_cannot_both_commit(execution_case, lease, result, saved_task):
    barrier = Barrier(2)
    service = execution_case["service"]

    def finish(succeeded):
        barrier.wait(timeout=5)
        if succeeded:
            return service.commit_result(lease, result)
        return service.commit_failure(lease, FailureInfo(code="TEST_ERROR", message="测试失败"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(finish, [True, False]))
    assert sum(outcomes) == 1
    task = saved_task()
    assert task.status == ("succeeded" if outcomes[0] else "failed")
    assert (task.result_json is None) != (task.error_json is None)


def test_mutated_external_dtos_are_revalidated_before_database_writes(execution_case, lease, result, saved_task):
    service = execution_case["service"]
    with pytest.raises(ValidationError):
        service.acquire(execution_case["delivery"].model_copy(update={"worker_id": ""}))
    with pytest.raises(ValidationError):
        service.renew(lease.model_copy(update={"attempt": 0}))
    with pytest.raises(ValidationError):
        service.commit_result(lease, result.model_copy(update={"audio_duration_seconds": -1}))
    with pytest.raises(ValidationError):
        service.commit_failure(lease, FailureInfo.model_construct(code="bad code", message="错误"))
    assert saved_task().status == "running"


@pytest.fixture
def recovery_case(execution_case, monkeypatch):
    case = execution_case
    for module in (broker_repository, dispatch_repository, dispatch_service, recovery_repository):
        monkeypatch.setattr(module, "database_now", lambda session: case["clock"]["now"])
    with case["db"].transaction() as session:
        task = session.get(TaskRecord, case["delivery"].task_id)
        task.queue_deadline_at = case["clock"]["now"] + timedelta(seconds=600)
        task.total_deadline_at = case["clock"]["now"] + timedelta(seconds=1800)
    case["recovery"] = RecoveryService(case["db"], case["settings"])
    case["dispatcher"] = DispatchService(case["db"], case["settings"], logging.getLogger("recovery-tests"))
    case["broker"] = BrokerRepository(case["db"])
    return case


def test_retry_is_atomic_bounded_preserves_deadlines_and_replays_old_failure(recovery_case, saved_task, result):
    case, service = recovery_case, recovery_case["service"]
    original = saved_task()
    first = service.acquire(case["delivery"]).lease
    failure = FailureInfo(code="WORKER_CHILD_EXITED", message="测试：子进程故障")
    assert service.commit_failure(first, failure)
    waiting = saved_task()
    assert (waiting.status, waiting.attempt, waiting.dispatch_version) == ("retry_wait", 1, 2)
    assert waiting.next_retry_at == case["clock"]["now"] + timedelta(seconds=5)
    assert waiting.finished_at is None
    assert service.commit_failure(first, failure)  # 模拟失败事务已提交但回执丢失
    assert not service.commit_result(first, result)
    assert case["dispatcher"].tick() == 0
    assert case["broker"].acknowledge(case["delivery"])
    case["clock"]["now"] += timedelta(seconds=5)
    assert case["dispatcher"].tick() == 1
    delivery = case["broker"].receive(worker_id="worker-two", queue_key=QUEUE_KEY, visibility_seconds=30)
    second = service.acquire(delivery).lease
    assert second.attempt == 2 and second.lease_token != first.lease_token
    assert not service.renew(first)
    assert service.commit_failure(second, failure)
    final = saved_task()
    assert final.status == "failed" and final.attempt == final.max_attempts == 2
    assert final.queue_deadline_at == original.queue_deadline_at
    assert final.total_deadline_at == original.total_deadline_at
    assert service.commit_failure(first, failure)  # 只确认历史回执，不改后来终态
    assert saved_task().to_dict() == final.to_dict()
    assert service.commit_failure(second, failure)
    assert case["recovery"].tick() == case["dispatcher"].tick() == 0
    with case["db"].session_factory() as session:
        history = session.scalars(select(ExecutionAttempt).order_by(ExecutionAttempt.attempt)).all()
        assert [attempt.outcome for attempt in history] == ["retry_wait", "failed"]
        assert len(session.scalars(select(DispatchEvent)).all()) == 2


@pytest.mark.parametrize("code", ["INPUT_UNAVAILABLE", "AUDIO_INVALID", "TRANSCRIPTION_FAILED", "UNKNOWN_ERROR"])
def test_input_and_unclassified_errors_are_not_retried(recovery_case, saved_task, code):
    case = recovery_case
    lease = case["service"].acquire(case["delivery"]).lease
    assert case["service"].commit_failure(lease, FailureInfo(code=code, message="测试：不可重试故障"))
    assert saved_task().status == "failed"
    assert saved_task().attempt == 1
    with case["db"].session_factory() as session:
        assert len(session.scalars(select(DispatchEvent)).all()) == 1


def test_retry_event_write_failure_rolls_back_task_and_attempt_history(recovery_case, saved_task):
    case = recovery_case
    lease = case["service"].acquire(case["delivery"]).lease
    before = saved_task().to_dict()
    def fail_event(connection, cursor, statement, parameters, context, executemany):
        if statement.upper().startswith("INSERT INTO DISPATCH_EVENTS"):
            raise OperationalError("test", {}, RuntimeError("injected outbox failure"))
    event.listen(case["db"].engine, "after_cursor_execute", fail_event)
    try:
        with pytest.raises(OperationalError):
            case["service"].commit_failure(lease, FailureInfo(code="WORKER_STOPPED", message="测试停止"))
    finally:
        event.remove(case["db"].engine, "after_cursor_execute", fail_event)
    assert saved_task().to_dict() == before
    with case["db"].session_factory() as session:
        assert session.get(ExecutionAttempt, lease.lease_token).outcome == "running"


def test_concurrent_recovery_only_creates_one_new_dispatch(recovery_case, saved_task, result):
    case = recovery_case
    lease = case["service"].acquire(case["delivery"]).lease
    case["clock"]["now"] += timedelta(seconds=30)
    barrier = Barrier(2)
    def recover(_):
        barrier.wait(timeout=5)
        return case["recovery"].recover_task(lease.task_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(recover, range(2))) == [False, True]
    assert saved_task().status == "retry_wait"
    assert saved_task().error_json["code"] == "LEASE_EXPIRED"
    assert saved_task().dispatch_version == 2 and saved_task().attempt == 1
    assert not case["service"].commit_result(lease, result)
    with case["db"].session_factory() as session:
        assert len(session.scalars(select(DispatchEvent)).all()) == 2


def test_scanned_recovery_candidate_is_rechecked_after_renewal(recovery_case, saved_task):
    case = recovery_case
    lease = case["service"].acquire(case["delivery"]).lease
    # 候选编号不能授予修改权；当前事务读取到的是已经续期的权威记录。
    case["clock"]["now"] += timedelta(seconds=20)
    assert case["service"].renew(lease)
    case["clock"]["now"] += timedelta(seconds=11)
    assert not case["recovery"].recover_task(lease.task_id)
    assert saved_task().status == "running" and saved_task().attempt == 1


@pytest.mark.parametrize("state", ["created", "queued", "retry_wait"])
def test_waiting_timeout_does_not_consume_an_execution(recovery_case, saved_task, state):
    case = recovery_case
    with case["db"].transaction() as session:
        session.get(TaskRecord, case["delivery"].task_id).status = state
    case["clock"]["now"] += timedelta(seconds=600)
    assert case["recovery"].tick() == 1
    assert saved_task().status == "failed" and saved_task().attempt == 0
    assert saved_task().error_json["code"] == "QUEUE_TIMEOUT"


def test_expired_waiting_task_cannot_start_before_dispatcher_recovery(recovery_case, saved_task):
    case = recovery_case
    with case["db"].transaction() as session:
        session.get(TaskRecord, case["delivery"].task_id).queue_deadline_at = case["clock"]["now"]
    assert case["service"].acquire(case["delivery"]).decision == "terminal_or_stale"
    assert saved_task().status == "failed" and saved_task().attempt == 0


def test_queue_deadline_does_not_cancel_an_already_running_attempt(recovery_case, saved_task, result):
    case = recovery_case
    with case["db"].transaction() as session:
        session.get(TaskRecord, case["delivery"].task_id).queue_deadline_at = case["clock"]["now"] + timedelta(seconds=1)
    lease = case["service"].acquire(case["delivery"]).lease
    case["clock"]["now"] += timedelta(seconds=2)
    assert case["recovery"].tick() == 0
    assert case["service"].commit_result(lease, result)
    assert saved_task().status == "succeeded"


@pytest.mark.parametrize(("deadline", "code"), [("queue_deadline_at", "QUEUE_TIMEOUT"), ("total_deadline_at", "TOTAL_TIMEOUT")])
def test_retry_wait_must_fit_original_deadline(recovery_case, saved_task, deadline, code):
    case = recovery_case
    with case["db"].transaction() as session:
        setattr(session.get(TaskRecord, case["delivery"].task_id), deadline, case["clock"]["now"] + timedelta(seconds=5))
    lease = case["service"].acquire(case["delivery"]).lease
    error = FailureInfo(code="WORKER_STOPPED", message="测试：停止")
    assert case["service"].commit_failure(lease, error)
    assert saved_task().status == "failed" and saved_task().error_json["code"] == code
    assert case["service"].commit_failure(lease, error)
    with case["db"].session_factory() as session:
        assert session.get(ExecutionAttempt, lease.lease_token).error_json["code"] == "WORKER_STOPPED"


@pytest.mark.parametrize(("total", "expected"), [(False, "attempt_timeout"), (True, "total_timeout")])
def test_deadline_caps_renewal_and_confirmed_stop_can_commit_timeout(recovery_case, saved_task, result, total, expected):
    case = recovery_case
    case["service"].settings = case["settings"].model_copy(update={"attempt_timeout_seconds": 100 if total else 10})
    if total:
        with case["db"].transaction() as session:
            session.get(TaskRecord, case["delivery"].task_id).total_deadline_at = case["clock"]["now"] + timedelta(seconds=10)
    lease = case["service"].acquire(case["delivery"]).lease
    limit = case["clock"]["now"] + timedelta(seconds=10)
    assert lease.lease_expires_at == limit
    assert lease.attempt_budget_seconds == 10
    case["clock"]["now"] += timedelta(seconds=8)
    assert case["service"].renew(lease) and saved_task().lease_expires_at == limit
    case["clock"]["now"] += timedelta(seconds=2)
    assert case["service"].control(lease) == expected
    assert not case["service"].renew(lease)
    assert not case["service"].commit_result(lease, result)
    assert case["service"].commit_failure(lease, FailureInfo(code=expected.upper(), message="测试：已停止超时执行"))
    assert saved_task().status == ("failed" if total else "retry_wait")


def test_earlier_expired_lease_cannot_claim_timeout_commit_right(recovery_case, saved_task):
    case = recovery_case
    lease = case["service"].acquire(case["delivery"]).lease
    case["clock"]["now"] += timedelta(seconds=900)
    assert case["service"].control(lease) == "attempt_timeout"
    assert not case["service"].commit_failure(lease, FailureInfo(code="ATTEMPT_TIMEOUT", message="过期持有者"))
    assert saved_task().status == "running"


@pytest.mark.parametrize("state", ["created", "queued", "retry_wait"])
def test_waiting_cancel_finishes_immediately_and_is_idempotent(recovery_case, saved_task, state):
    case = recovery_case
    with case["db"].transaction() as session:
        session.get(TaskRecord, case["delivery"].task_id).status = state
    assert case["service"].request_cancel(case["delivery"].task_id, "local-user") == "cancelled"
    assert case["service"].request_cancel(case["delivery"].task_id, "local-user") == "already_finished"
    assert saved_task().status == "cancelled" and saved_task().attempt == 0
    assert case["service"].acquire(case["delivery"]).decision == "terminal_or_stale"


def test_running_cancel_blocks_result_until_stop_confirmation(recovery_case, saved_task, result):
    case = recovery_case
    lease = case["service"].acquire(case["delivery"]).lease
    assert case["service"].request_cancel(lease.task_id, "local-user") == "cancel_requested"
    assert case["service"].control(lease) == "cancel"
    assert not case["service"].renew(lease)
    assert not case["service"].commit_result(lease, result)
    assert not case["service"].commit_failure(lease, FailureInfo(code="WORKER_STOPPED", message="取消优先"))
    assert saved_task().finished_at is None
    assert not case["service"].finish_cancel(lease.model_copy(update={"worker_id": "wrong"}))
    assert case["service"].finish_cancel(lease)
    assert case["service"].finish_cancel(lease)
    assert saved_task().status == "cancelled"


def test_cancel_recovers_on_expired_lease_without_creating_retry(recovery_case, saved_task):
    case = recovery_case
    lease = case["service"].acquire(case["delivery"]).lease
    case["service"].request_cancel(lease.task_id, "local-user")
    assert case["recovery"].tick() == 0
    case["clock"]["now"] += timedelta(seconds=30)
    assert case["recovery"].tick() == 1
    assert saved_task().status == "cancelled"
    assert case["service"].finish_cancel(lease)
    with case["db"].session_factory() as session:
        assert len(session.scalars(select(DispatchEvent)).all()) == 1


def test_cancel_and_success_race_has_one_serialized_outcome(recovery_case, saved_task, result):
    case = recovery_case
    lease = case["service"].acquire(case["delivery"]).lease
    barrier = Barrier(2)
    def race(operation):
        barrier.wait(timeout=5)
        return case["service"].commit_result(lease, result) if operation == "result" else case["service"].request_cancel(lease.task_id, "local-user")
    with ThreadPoolExecutor(max_workers=2) as pool:
        committed, cancelled = list(pool.map(race, ["result", "cancel"]))
    if committed:
        assert cancelled == "already_finished" and saved_task().status == "succeeded"
        assert not case["service"].finish_cancel(lease)
    else:
        assert cancelled == "cancel_requested" and saved_task().status == "cancel_requested"
        assert case["service"].finish_cancel(lease)
        assert saved_task().status == "cancelled" and saved_task().result_json is None


def test_cancel_hides_other_callers_task_and_unknown_id(recovery_case, saved_task):
    service, task_id = recovery_case["service"], recovery_case["delivery"].task_id
    for requested_id, caller in [(task_id, "another-user"), ("f" * 32, "local-user")]:
        with pytest.raises(AppError) as caught:
            service.request_cancel(requested_id, caller)
        assert caught.value.code == "TASK_NOT_FOUND" and caught.value.status_code == 404
    assert saved_task().status == "queued"


def test_saved_result_with_lost_ack_never_runs_again(recovery_case, saved_task, result):
    case = recovery_case
    lease = case["service"].acquire(case["delivery"]).lease
    assert case["service"].commit_result(lease, result)
    case["clock"]["now"] += timedelta(seconds=120)
    redelivery = case["broker"].receive(worker_id="worker-two", queue_key=QUEUE_KEY, visibility_seconds=30)
    assert redelivery is not None
    assert case["service"].acquire(redelivery).decision == "terminal_or_stale"
    assert case["broker"].acknowledge(redelivery)
    assert case["recovery"].tick() == 0 and saved_task().attempt == 1


def test_recovery_batch_is_bounded_and_later_tasks_are_not_starved(recovery_case, saved_task):
    case = recovery_case
    template = saved_task().to_dict()
    with case["db"].transaction() as session:
        for _ in range(24):
            values = dict(template, task_id=uuid4().hex, input_ref=uuid4().hex)
            session.add(TaskRecord(**values))
    case["clock"]["now"] += timedelta(seconds=600)
    assert case["recovery"].tick() == 20
    assert case["recovery"].tick() == 5
    assert case["recovery"].tick() == 0
