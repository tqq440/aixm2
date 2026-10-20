"""真实 SQLite 通知/租约/终态事务；检测结果为夹具，不运行 YOLO。"""

from datetime import datetime, timedelta
import importlib
import logging
from uuid import uuid4

from alembic import command
from alembic.config import Config
from pydantic import ValidationError
import pytest
from sqlalchemy import select

from backend.config.asr.model_catalog import QUEUE_KEY as ASR_QUEUE_KEY
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.config.vision.model_catalog import MODEL_ID, MODEL_VERSION, MODEL_SHA256, QUEUE_KEY, REGISTRY_SPEC
from backend.data.broker.broker_models import NotificationRecord
from backend.data.broker.broker_repository import BrokerRepository
from backend.data.database.database import Database
from backend.data.execution.attempt_models import ExecutionAttempt
from backend.data.tasks.task_models import DispatchEvent, TaskRecord
from backend.data.tasks.task_repository import TaskRepository
from backend.schema.asr.asr_schema import TranscriptionResult
from backend.schema.execution.execution_schema import FailureInfo, VisionExecutionInput
from backend.schema.models.model_schema import ModelRegistration
from backend.schema.vision.detection_schema import BoundingBox, Detection, DetectionResult
from backend.service.dispatch.dispatch_service import DispatchService
from backend.service.execution.execution_service import ExecutionService
from backend.service.models.model_registry_service import ModelRegistryService
from backend.service.recovery.recovery_service import RecoveryService


@pytest.fixture
def execution_case(tmp_path, monkeypatch):
    settings = AppSettings(database_path=tmp_path / "execution.sqlite3", log_dir=tmp_path / "logs",
                           upload_dir=tmp_path / "uploads")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    database = Database(settings)
    clock = {"now": datetime(2026, 10, 13, 10)}
    for module in ("backend.data.tasks.task_repository", "backend.data.execution.execution_repository",
                   "backend.data.broker.broker_repository", "backend.data.broker.dispatch_repository",
                   "backend.data.recovery.recovery_repository", "backend.service.dispatch.dispatch_service"):
        monkeypatch.setattr(importlib.import_module(module), "database_now", lambda session: clock["now"])
    registry = ModelRegistryService(database, settings)
    registry.register(ModelRegistration.model_validate(REGISTRY_SPEC))
    registry.enable(MODEL_ID, MODEL_VERSION)
    registry.set_default(MODEL_ID, MODEL_VERSION)
    task = TaskRecord(
        task_id=uuid4().hex, caller_id="local-user", task_type="vision", model_id=MODEL_ID,
        model_version=MODEL_VERSION, parameters={"confidence_threshold": 0.25, "iou_threshold": 0.45, "max_detections": 2},
        input_ref=uuid4().hex, input_filename="fixture.png", input_sha256="a" * 64, input_size_bytes=100,
        input_media_type="image", audio_duration_seconds=None, image_width=32, image_height=24,
        queue_key=QUEUE_KEY, created_at=clock["now"], updated_at=clock["now"],
        queue_deadline_at=clock["now"] + timedelta(seconds=600), total_deadline_at=clock["now"] + timedelta(seconds=1800),
    )
    event = DispatchEvent(event_id=uuid4().hex, task_id=task.task_id, queue_key=QUEUE_KEY,
                          created_at=clock["now"], available_at=clock["now"])
    repository = TaskRepository(database)
    repository.create_with_event(task=task, event=event, idempotency_key="vision", request_fingerprint="b" * 64)
    dispatch = DispatchService(database, settings, logging.getLogger("vision-execution-test"))
    broker = BrokerRepository(database)
    assert dispatch.tick() == 1
    delivery = broker.receive(worker_id="vision-worker-one", queue_key=QUEUE_KEY, visibility_seconds=30)
    assert delivery is not None
    service = ExecutionService(database, settings, task_type="vision")
    yield dict(database=database, settings=settings, clock=clock, repository=repository, registry=registry,
               dispatch=dispatch, broker=broker, delivery=delivery, service=service, task_id=task.task_id)
    database.dispose()


def current_task(case):
    return case["repository"].get_task("local-user", case["task_id"])


def result_with(*, detections=None, **updates):
    data = dict(model_sha256=MODEL_SHA256, image_width=32, image_height=24, detections=detections or [],
                runtime_version="test-only", load_seconds=0, inference_seconds=0.01, total_seconds=0.02)
    data.update(updates)
    return DetectionResult(**data)


def person(*, confidence=0.9, class_name="person"):
    return Detection(class_id=0, class_name=class_name, confidence=confidence,
                     box=BoundingBox(x_min=1, y_min=1, x_max=12, y_max=15))


@pytest.fixture
def lease(execution_case):
    acquired = execution_case["service"].acquire(execution_case["delivery"])
    assert acquired.decision == "acquired"
    return acquired.lease


def test_asr_executor_cannot_acquire_vision_delivery_but_matching_executor_can(execution_case):
    case = execution_case
    asr = ExecutionService(case["database"], case["settings"])
    assert asr.acquire(case["delivery"]).decision == "busy"
    assert current_task(case).attempt == 0
    acquired = case["service"].acquire(case["delivery"])
    assert acquired.decision == "acquired"
    assert isinstance(acquired.input, VisionExecutionInput)
    assert acquired.input.task_type == "vision"
    assert (acquired.input.image_width, acquired.input.image_height) == (32, 24)
    assert (acquired.input.confidence_threshold, acquired.input.iou_threshold, acquired.input.max_detections) == (0.25, 0.45, 2)
    assert current_task(case).status == "running" and current_task(case).attempt == 1
    assert case["broker"].receive(worker_id="asr-worker", queue_key=ASR_QUEUE_KEY, visibility_seconds=30) is None


@pytest.mark.parametrize("change", [{"queue_key": ASR_QUEUE_KEY}, {"worker_id": "wrong-worker"},
    {"delivery_token": "wrong-token"}, {"dispatch_version": 2}, {"execution_plan_version": 2}])
def test_vision_acquire_requires_current_notification_identity(execution_case, change):
    forged = execution_case["delivery"].model_copy(update=change)
    assert execution_case["service"].acquire(forged).decision != "acquired"
    assert current_task(execution_case).attempt == 0


def test_duplicate_delivery_does_not_start_second_attempt(execution_case, lease):
    assert execution_case["service"].acquire(execution_case["delivery"]).decision == "busy"
    assert current_task(execution_case).attempt == 1
    with execution_case["database"].session_factory() as session:
        assert len(session.scalars(select(ExecutionAttempt)).all()) == 1


def test_empty_detection_is_success_and_result_commit_is_idempotent(execution_case, lease):
    service = execution_case["service"]
    result = result_with()
    assert service.commit_result(lease, result)
    assert service.commit_result(lease, result)
    task = current_task(execution_case)
    assert task.status == "succeeded" and task.result_json["detections"] == []
    fingerprint = task.result_fingerprint
    assert not service.commit_result(lease, result_with(inference_seconds=0.02))
    assert current_task(execution_case).result_fingerprint == fingerprint
    with execution_case["database"].session_factory() as session:
        attempt = session.get(ExecutionAttempt, lease.lease_token)
        assert attempt.outcome == "succeeded" and attempt.finished_at is not None


@pytest.mark.parametrize("violation", ["model_sha", "width", "height", "label", "confidence", "threshold_equal", "count", "asr_result"])
def test_semantically_invalid_detection_is_not_committed(execution_case, lease, violation):
    values = {
        "model_sha": lambda: result_with(model_sha256="0" * 64),
        "width": lambda: result_with(image_width=33),
        "height": lambda: result_with(image_height=25),
        "label": lambda: result_with(detections=[person(class_name="bus")]),
        "confidence": lambda: result_with(detections=[person(confidence=0.2)]),
        "threshold_equal": lambda: result_with(detections=[person(confidence=0.25)]),
        "count": lambda: result_with(detections=[person(), person(), person()]),
        "asr_result": lambda: TranscriptionResult.model_construct(),
    }
    assert not execution_case["service"].commit_result(lease, values[violation]())
    task = current_task(execution_case)
    assert task.status == "running" and task.result_json is None


@pytest.mark.parametrize("change", [{"model_id": "wrong-model"}, {"model_version": "wrong-version"},
                                    {"image_width": 0}, {"device": "cuda"}, {"input_type": "float32"}])
def test_constructed_result_cannot_bypass_output_schema_validation(execution_case, lease, change):
    malformed = result_with().model_copy(update=change)
    with pytest.raises(ValidationError):
        execution_case["service"].commit_result(lease, malformed)
    assert current_task(execution_case).result_json is None


def test_asr_executor_cannot_control_or_fail_a_vision_lease(execution_case, lease):
    asr = ExecutionService(execution_case["database"], execution_case["settings"])
    assert asr.control(lease) == "expired"
    assert not asr.renew(lease)
    assert not asr.commit_result(lease, result_with())
    assert not asr.commit_failure(lease, FailureInfo(code="IMAGE_DECODE_FAILED", message="wrong executor"))
    assert current_task(execution_case).status == "running"


def test_disable_after_acceptance_does_not_revoke_existing_task_or_result(execution_case, lease):
    execution_case["registry"].disable(MODEL_ID, MODEL_VERSION)
    assert execution_case["service"].renew(lease)
    assert execution_case["service"].commit_result(lease, result_with(detections=[person()]))
    assert current_task(execution_case).model_version == MODEL_VERSION


def test_cancel_wins_against_result_and_only_matching_executor_finishes_cancel(execution_case, lease):
    service = execution_case["service"]
    # 公共取消入口仍使用默认 Service；取消受理必须支持所有任务类型。
    asr = ExecutionService(execution_case["database"], execution_case["settings"])
    assert asr.request_cancel(execution_case["task_id"], "local-user") == "cancel_requested"
    assert service.control(lease) == "cancel"
    assert not service.commit_result(lease, result_with())
    assert not asr.finish_cancel(lease)
    assert service.finish_cancel(lease)
    assert service.finish_cancel(lease)
    assert current_task(execution_case).status == "cancelled"
    assert not service.commit_result(lease, result_with())


def test_waiting_vision_task_can_be_cancelled_without_execution(execution_case):
    service = execution_case["service"]
    assert service.request_cancel(execution_case["task_id"], "local-user") == "cancelled"
    assert service.acquire(execution_case["delivery"]).decision == "terminal_or_stale"
    assert current_task(execution_case).attempt == 0


def test_success_cannot_be_replaced_by_later_cancellation_or_failure(execution_case, lease):
    service = execution_case["service"]
    assert service.commit_result(lease, result_with())
    assert service.request_cancel(execution_case["task_id"], "local-user") == "already_finished"
    assert not service.commit_failure(lease, FailureInfo(code="WORKER_CHILD_EXITED", message="late"))
    assert current_task(execution_case).status == "succeeded"


def test_expired_lease_result_is_rejected_before_recovery(execution_case, lease):
    execution_case["clock"]["now"] += timedelta(seconds=31)
    assert execution_case["service"].control(lease) == "expired"
    assert not execution_case["service"].commit_result(lease, result_with())
    assert not execution_case["service"].renew(lease)
    assert current_task(execution_case).result_json is None


def test_lease_recovery_preserves_vision_queue_and_rejects_old_result_after_new_success(execution_case, lease):
    case = execution_case
    initial = current_task(case)
    case["clock"]["now"] += timedelta(seconds=31)
    recovery = RecoveryService(case["database"], case["settings"])
    assert recovery.recover_task(case["task_id"])
    retry = current_task(case)
    assert retry.status == "retry_wait" and retry.dispatch_version == 2
    assert retry.queue_key == QUEUE_KEY and retry.model_version == MODEL_VERSION
    assert retry.queue_deadline_at == initial.queue_deadline_at and retry.total_deadline_at == initial.total_deadline_at
    case["clock"]["now"] += timedelta(seconds=case["settings"].retry_wait_seconds)
    assert case["dispatch"].tick() == 1
    # 旧消息可能先被新 Worker 领取；必须根据计划/投递版本确认陈旧后再领取新通知。
    decision = None
    for _ in range(2):
        delivery = case["broker"].receive(worker_id="vision-worker-two", queue_key=QUEUE_KEY, visibility_seconds=30)
        assert delivery is not None
        decision = case["service"].acquire(delivery)
        if decision.decision == "acquired":
            break
        assert decision.decision == "terminal_or_stale"
        assert case["broker"].acknowledge(delivery)
    assert decision.decision == "acquired" and decision.lease.attempt == 2
    assert not case["service"].commit_result(lease, result_with())
    new_result = result_with(detections=[person()])
    assert case["service"].commit_result(decision.lease, new_result)
    assert not case["service"].commit_result(lease, result_with())
    assert current_task(case).result_json == new_result.model_dump(mode="json", by_alias=True)


def test_vision_transient_failure_retries_once_then_exhausts_limit(execution_case, lease):
    case = execution_case
    error = FailureInfo(code="WORKER_CHILD_EXITED", message="test-only stopped child")
    assert case["service"].commit_failure(lease, error)
    assert case["service"].commit_failure(lease, error)
    assert current_task(case).status == "retry_wait"
    assert case["broker"].acknowledge(case["delivery"])
    case["clock"]["now"] += timedelta(seconds=case["settings"].retry_wait_seconds)
    assert case["dispatch"].tick() == 1
    new_delivery = case["broker"].receive(worker_id="vision-worker-two", queue_key=QUEUE_KEY, visibility_seconds=30)
    acquired = case["service"].acquire(new_delivery)
    assert acquired.decision == "acquired" and acquired.lease.attempt == 2
    assert case["service"].commit_failure(acquired.lease, error)
    assert current_task(case).status == "failed" and current_task(case).attempt == 2
    case["clock"]["now"] += timedelta(seconds=10)
    assert case["dispatch"].tick() == 0
    with case["database"].session_factory() as session:
        attempts = session.scalars(select(ExecutionAttempt).order_by(ExecutionAttempt.attempt)).all()
        assert [item.outcome for item in attempts] == ["retry_wait", "failed"]
        assert {item.queue_key for item in session.scalars(select(NotificationRecord)).all()} == {QUEUE_KEY}


def test_invalid_image_failure_is_not_retried(execution_case, lease):
    assert execution_case["service"].commit_failure(lease, FailureInfo(code="IMAGE_DECODE_FAILED", message="test-only corrupt input"))
    task = current_task(execution_case)
    assert task.status == "failed" and task.error_json["code"] == "IMAGE_DECODE_FAILED"
    assert task.dispatch_version == 1
    assert execution_case["dispatch"].tick() == 0
