"""真实 SQLite 验证停止与准入排序；控制记录/音频均为隔离测试夹具。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import io
import logging
from threading import Event
from unittest.mock import Mock
import wave

from alembic import command
from alembic.config import Config
from PIL import Image
import pytest

from backend.common.exceptions.app_error import AppError
from backend.config.asr.model_catalog import QUEUE_KEY
from backend.config.settings.app_settings import PROJECT_ROOT
from backend.config.vision.model_catalog import (
    MODEL_ID as VISION_MODEL_ID, MODEL_VERSION as VISION_VERSION,
    QUEUE_KEY as VISION_QUEUE, REGISTRY_SPEC as VISION_SPEC,
)
from backend.data.agent.agent_models import AgentControl
from backend.data.broker.broker_models import NotificationRecord
from backend.data.broker.broker_repository import BrokerRepository
from backend.data.database.database import database_now
from backend.data.tasks.task_models import TaskRecord
from backend.schema.nodes.node_schema import LocalNodeSnapshot
from backend.schema.models.model_schema import ModelRegistration
from backend.service.agent.asr_gate import AsrGate
from backend.service.dispatch.dispatch_service import DispatchService
from backend.service.execution.execution_service import ExecutionService
from backend.service.worker.worker_service import WorkerService


@pytest.fixture
def managed_case(app_factory, tmp_path):
    app = app_factory(upload_dir=tmp_path / "uploads")
    settings = app.config["APP_SETTINGS"]
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    registry = app.extensions["node_registry_service"]
    registry.probe = lambda: LocalNodeSnapshot(local_identity="a" * 64, hostname="test-only",
        os_name="Windows", os_version="test-only", architecture="AMD64", cpu_count=4)
    receipt = registry.register_local()
    database = app.extensions["database"]
    gate = AsrGate(database, settings)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 1600)
    payload = buffer.getvalue()

    def set_control(**changes):
        with database.transaction(immediate=True) as session:
            now = database_now(session)
            row = session.get(AgentControl, receipt.instance.instance_id)
            if row is None:
                row = AgentControl(instance_id=receipt.instance.instance_id,
                    agent_session_id="b" * 32, agent_pid=1234, agent_started_at=now,
                    heartbeat_at=now, desired_state="running", state="ready", command_revision=1,
                    worker_session_id="c" * 32, child_pid=5678, model_ready=True, updated_at=now)
                session.add(row)
            for key, value in changes.items():
                setattr(row, key, value)

    def submit(key="managed-task", **fields):
        content = fields.pop("content", payload)
        return app.test_client().post("/api/tasks/asr", headers={"Idempotency-Key": key},
            data={"file": (io.BytesIO(content), "managed.wav"), **fields})

    return app, settings, database, gate, set_control, submit


def test_registered_instance_requires_agent_before_new_submission(managed_case):
    app, settings, _, _, _, submit = managed_case
    response = submit()
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "ASR_AGENT_UNAVAILABLE"
    assert not list(settings.upload_dir.rglob("*.audio"))
    assert app.test_client().get("/api/tasks").get_json()["data"]["total"] == 0


@pytest.mark.parametrize("state,desired,ready,code", [
    ("ready", "running", True, None), ("busy", "running", True, None),
    ("starting", "running", False, "ASR_SERVICE_NOT_READY"),
    ("failed", "running", False, "ASR_SERVICE_NOT_READY"),
    ("stopped", "stopped", False, "ASR_SERVICE_STOPPED"),
    ("stopping", "stopped", True, "ASR_SERVICE_STOPPED"),
    ("stop_unconfirmed", "stopped", True, "ASR_SERVICE_STOPPED"),
])
def test_managed_state_admission_distinguishes_busy_from_unloaded(managed_case, state, desired, ready, code):
    _, settings, _, _, control, submit = managed_case
    control(state=state, desired_state=desired, model_ready=ready)
    response = submit()
    assert response.status_code == (201 if code is None else 503)
    if code:
        assert response.get_json()["error"]["code"] == code
        assert not list(settings.upload_dir.rglob("*.audio"))


def test_stopped_service_preserves_idempotent_replay_and_conflict(managed_case):
    app, _, _, _, control, submit = managed_case
    control()
    original = submit().get_json()["data"]["task"]
    control(state="stopping", desired_state="stopped")
    assert submit().get_json()["data"] == {"replayed": True, "task": original}
    assert submit(language="auto").status_code == 409
    assert submit("new-after-stop").status_code == 503
    assert app.test_client().get(f"/api/tasks/{original['taskId']}").status_code == 200


def test_stop_during_upload_is_rechecked_in_task_write_transaction(managed_case, monkeypatch):
    app, settings, _, _, control, submit = managed_case
    control()
    store = app.extensions["task_service"].store
    save = store.save

    def stop_after_save(stream, *, max_bytes):
        result = save(stream, max_bytes=max_bytes)
        control(state="stopping", desired_state="stopped")
        return result

    monkeypatch.setattr(store, "save", stop_after_save)
    assert submit().status_code == 503
    assert not list(settings.upload_dir.rglob("*.audio"))
    assert app.test_client().get("/api/tasks").get_json()["data"]["total"] == 0


def test_submission_write_lock_orders_concurrent_stop(managed_case, monkeypatch):
    app, _, _, _, control, submit = managed_case
    control()
    checked, stopping = Event(), Event()
    gate = app.extensions["task_service"].asr_gate
    validate = gate.validate_submission

    def check(session, model_id, version):
        validate(session, model_id, version)
        checked.set()
        assert stopping.wait(5)

    def stop():
        assert checked.wait(5)
        stopping.set()
        control(state="stopping", desired_state="stopped")

    monkeypatch.setattr(gate, "validate_submission", check)
    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(submit)
        stopping_future = pool.submit(stop)
        assert creating.result(timeout=10).status_code == 201
        stopping_future.result(timeout=10)
    assert submit("after-stop").status_code == 503


def _dispatch(case):
    app, settings, database, gate, control, submit = case
    control()
    task_id = submit().get_json()["data"]["task"]["taskId"]
    assert DispatchService(database, settings, app.extensions["app_logger"]).tick() == 1
    return task_id, BrokerRepository(database)


def test_stop_before_notification_receive_keeps_message_visible(managed_case):
    _, _, database, gate, control, _ = managed_case
    task_id, broker = _dispatch(managed_case)
    control(state="stopping", desired_state="stopped")
    delivery = broker.receive(worker_id="c" * 32, queue_key=QUEUE_KEY, visibility_seconds=30,
        receive_check=lambda session: gate.can_claim(session, "c" * 32))
    assert delivery is None
    with database.session_factory() as session:
        task = session.get(TaskRecord, task_id)
        assert task.status == "queued" and task.attempt == 0
        rows = session.query(NotificationRecord).all()
        assert len(rows) == 1 and rows[0].status == "ready" and rows[0].deliveries == 0


def test_stop_between_receive_and_acquire_preserves_queued_deadlines(managed_case):
    _, settings, database, gate, control, _ = managed_case
    task_id, broker = _dispatch(managed_case)
    delivery = broker.receive(worker_id="c" * 32, queue_key=QUEUE_KEY, visibility_seconds=30,
        receive_check=lambda session: gate.can_claim(session, "c" * 32))
    with database.session_factory() as session:
        task = session.get(TaskRecord, task_id)
        deadlines = task.queue_deadline_at, task.total_deadline_at
    control(state="stopping", desired_state="stopped")
    assert ExecutionService(database, settings).acquire(delivery).decision == "busy"
    with database.session_factory() as session:
        task = session.get(TaskRecord, task_id)
        assert task.status == "queued" and task.attempt == 0
        assert (task.queue_deadline_at, task.total_deadline_at) == deadlines


def test_stopping_does_not_invalidate_already_granted_lease(managed_case):
    _, settings, database, _, control, _ = managed_case
    _, broker = _dispatch(managed_case)
    delivery = broker.receive(worker_id="c" * 32, queue_key=QUEUE_KEY, visibility_seconds=30)
    execution = ExecutionService(database, settings)
    acquired = execution.acquire(delivery)
    assert acquired.decision == "acquired"
    control(state="stopping", desired_state="stopped")
    assert execution.control(acquired.lease) == "continue"
    assert execution.renew(acquired.lease)


@pytest.mark.parametrize("change", ["stale", "old_worker", "missing_worker"])
def test_expired_or_old_identity_cannot_claim(managed_case, change):
    _, settings, database, gate, control, _ = managed_case
    _, broker = _dispatch(managed_case)
    delivery = broker.receive(worker_id="c" * 32, queue_key=QUEUE_KEY, visibility_seconds=30)
    if change == "stale":
        with database.session_factory() as session:
            stale = database_now(session) - timedelta(seconds=settings.runtime_stale_seconds + 1)
        control(heartbeat_at=stale)
    else:
        control(worker_session_id="d" * 32 if change == "old_worker" else None)
    assert ExecutionService(database, settings).acquire(delivery).decision == "busy"
    with database.session_factory() as session:
        assert not gate.can_claim(session, "c" * 32)


def test_standalone_worker_cannot_start_registered_instance_or_overwrite_runtime(managed_case):
    _, settings, database, _, control, _ = managed_case
    control(state="stopped", desired_state="stopped", model_ready=False)
    runner, runtime = Mock(), Mock()
    runner.stop.return_value = True
    worker = WorkerService(database, settings, logging.getLogger("managed-test"), runner=runner, runtime=runtime)
    with pytest.raises(AppError) as caught:
        worker.start()
    assert caught.value.code == "WORKER_AGENT_REQUIRED"
    runner.start.assert_not_called()
    assert worker.close()
    runtime.report.assert_not_called()


def test_spawn_guard_orders_concurrent_stop_and_rejects_later_spawn(managed_case):
    _, _, _, gate, control, _ = managed_case
    control(state="starting", model_ready=False)
    launching, stopping = Event(), Event()
    calls = []

    def launch():
        with gate.spawn_guard(agent_session_id="b" * 32, worker_session_id="c" * 32) as allowed:
            assert allowed
            launching.set()
            assert stopping.wait(5)
            calls.append("spawn/send")

    def stop():
        assert launching.wait(5)
        stopping.set()
        control(state="stopping", desired_state="stopped")
        calls.append("stop committed")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(launch), pool.submit(stop)]
        for future in futures:
            future.result(timeout=10)
    assert calls == ["spawn/send", "stop committed"]
    with gate.spawn_guard(agent_session_id="b" * 32, worker_session_id="c" * 32) as allowed:
        assert not allowed


def test_registered_but_agent_never_started_cannot_bypass_with_worker(managed_case):
    _, settings, database, gate, _, _ = managed_case
    with database.session_factory() as session:
        assert not gate.can_claim(session, "c" * 32)
    with pytest.raises(AppError, match="Agent"):
        with gate.spawn_guard(agent_session_id=None, worker_session_id="c" * 32):
            pytest.fail("登记库不能走未受管入口")


def test_vision_admission_remains_outside_asr_gate(managed_case):
    app, settings, database, _, control, _ = managed_case
    control(state="stopped", desired_state="stopped", model_ready=False)
    registry = app.extensions["model_service"].registry
    registry.register(ModelRegistration.model_validate(VISION_SPEC))
    registry.enable(VISION_MODEL_ID, VISION_VERSION)
    registry.set_default(VISION_MODEL_ID, VISION_VERSION)
    buffer = io.BytesIO()
    with Image.new("RGB", (16, 16), (10, 20, 30)) as image:
        image.save(buffer, format="PNG")
    response = app.test_client().post("/api/tasks/vision", headers={"Idempotency-Key": "vision-independent"},
        data={"file": (io.BytesIO(buffer.getvalue()), "fixture.png")})
    assert response.status_code == 201
    DispatchService(database, settings, app.extensions["app_logger"]).tick()
    delivery = BrokerRepository(database).receive(worker_id="vision-worker", queue_key=VISION_QUEUE,
        visibility_seconds=30)
    assert ExecutionService(database, settings, task_type="vision").acquire(delivery).decision == "acquired"
