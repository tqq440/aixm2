from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import logging
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import func, select

from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.broker.broker_models import NotificationRecord
from backend.data.broker.broker_repository import BrokerRepository
from backend.data.database.database import Database, database_now
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.data.tasks.task_models import DispatchEvent, TaskRecord
from backend.data.tasks.task_repository import TaskRepository
from backend.service.dispatch.dispatch_service import DispatchService


@pytest.fixture
def environment(tmp_path):
    settings = AppSettings(database_path=tmp_path / "broker.sqlite3", log_dir=tmp_path / "logs")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    database = Database(settings)
    try:
        yield database, settings, DispatchService(database, settings, logging.getLogger("test.dispatch"))
    finally:
        database.dispose()


def seed(database, *, status="created", event_version=1):
    now = datetime(2025, 1, 1)
    task = TaskRecord(
        task_id=uuid4().hex, caller_id="local", model_id="whisper-small", model_version="revision",
        parameters={"language": "zh"}, input_ref=uuid4().hex, input_filename="音频.wav",
        input_sha256="a" * 64, input_size_bytes=100, audio_duration_seconds=0.1,
        queue_key="asr:whisper-small:cpu", status=status, created_at=now, updated_at=now,
    )
    event = DispatchEvent(
        event_id=uuid4().hex, task_id=task.task_id, queue_key=task.queue_key,
        dispatch_version=event_version, created_at=now, available_at=now,
    )
    TaskRepository(database).create_with_event(
        task=task, event=event, idempotency_key=uuid4().hex, request_fingerprint="f" * 64,
    )
    return task, event


def test_dispatch_persists_atomic_publication_and_is_idempotent(environment):
    database, _, dispatcher = environment
    task, event = seed(database)
    assert dispatcher.tick() == 1
    assert dispatcher.tick() == 0
    with database.session_factory() as session:
        saved_task = session.get(TaskRecord, task.task_id)
        saved_event = session.get(DispatchEvent, event.event_id)
        notification = session.scalar(select(NotificationRecord))
        assert saved_task.status == "queued" and saved_task.attempt == 0
        assert saved_event.status == "published" and saved_event.publish_attempts == 1
        assert saved_event.notification_id == notification.notification_id
        assert notification.status == "ready" and notification.task_id == task.task_id
    # 模拟投递登记丢失：同事件重复发布仍然只有一条通知。
    with database.transaction() as session:
        session.get(DispatchEvent, event.event_id).status = "pending"
    assert dispatcher.tick() == 1
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(NotificationRecord)) == 1


def test_publish_failure_rolls_back_and_only_retries_after_available_at(environment, monkeypatch):
    database, settings, dispatcher = environment
    task, event = seed(database)
    original_publish = dispatcher.broker.publish

    def broken_publish(session, current_event):
        original_publish(session, current_event)
        raise OSError("injected publication failure")

    monkeypatch.setattr(dispatcher.broker, "publish", broken_publish)
    assert dispatcher.tick() == 0
    with database.session_factory() as session:
        saved_event = session.get(DispatchEvent, event.event_id)
        assert session.get(TaskRecord, task.task_id).status == "created"
        assert session.scalar(select(func.count()).select_from(NotificationRecord)) == 0
        assert saved_event.status == "pending" and saved_event.publish_attempts == 1
        assert saved_event.notification_id is None and saved_event.published_at is None
        assert saved_event.last_error == "通知发布失败（OSError）"
        assert saved_event.available_at > database_now(session)
    monkeypatch.setattr(dispatcher.broker, "publish", original_publish)
    assert dispatcher.tick() == 0
    with database.transaction() as session:
        session.get(DispatchEvent, event.event_id).available_at = datetime(2025, 1, 1)
    assert dispatcher.tick() == 1
    with database.session_factory() as session:
        saved = session.get(DispatchEvent, event.event_id)
        assert saved.publish_attempts == 2 and saved.last_error is None


def test_failure_after_all_updates_still_rolls_back_task_event_and_notification(environment, monkeypatch):
    database, _, dispatcher = environment
    task, event = seed(database)
    original_record = dispatcher.repository.record_published

    def fail_after_updates(*args, **kwargs):
        original_record(*args, **kwargs)
        raise RuntimeError("injected failure after all writes and flush")

    monkeypatch.setattr(dispatcher.repository, "record_published", fail_after_updates)
    assert dispatcher.tick() == 0
    with database.session_factory() as session:
        assert session.get(TaskRecord, task.task_id).status == "created"
        assert session.scalar(select(func.count()).select_from(NotificationRecord)) == 0
        saved_event = session.get(DispatchEvent, event.event_id)
        assert saved_event.status == "pending" and saved_event.publish_attempts == 1
        assert saved_event.notification_id is None and saved_event.published_at is None


@pytest.mark.parametrize("status,event_version,expected_published", [
    ("running", 1, 1), ("queued", 1, 1), ("succeeded", 1, 0),
    ("failed", 1, 0), ("cancelled", 1, 0), ("created", 2, 0),
])
def test_dispatch_never_reverts_running_or_republishes_stale(environment, status, event_version, expected_published):
    database, _, dispatcher = environment
    task, event = seed(database, status=status, event_version=event_version)
    assert dispatcher.tick() == expected_published
    with database.session_factory() as session:
        assert session.get(TaskRecord, task.task_id).status == status
        assert session.get(DispatchEvent, event.event_id).status == ("published" if expected_published else "discarded")


def test_concurrent_dispatchers_publish_one_notification(environment):
    database, settings, dispatcher = environment
    seed(database)
    other = DispatchService(database, settings, logging.getLogger("test.other"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda service: service.tick(), (dispatcher, other)))
    assert sum(results) == 1
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(NotificationRecord)) == 1


def test_dispatch_scan_is_bounded(environment):
    database, _, dispatcher = environment
    for _ in range(21):
        seed(database)
    assert dispatcher.tick() == 20
    assert dispatcher.tick() == 1


def test_receive_reclaim_acknowledge_and_ownership(environment):
    database, _, dispatcher = environment
    task, _ = seed(database)
    dispatcher.tick()
    broker = BrokerRepository(database)
    assert broker.receive(worker_id="a", queue_key="unsupported", visibility_seconds=30) is None
    first = broker.receive(worker_id="a", queue_key=task.queue_key, visibility_seconds=30)
    assert first is not None
    assert broker.receive(worker_id="b", queue_key=task.queue_key, visibility_seconds=30) is None
    assert broker.extend(first, visibility_seconds=60)
    forged = first.model_copy(update={"worker_id": "other"})
    assert not broker.extend(forged, visibility_seconds=60)
    assert not broker.acknowledge(forged)
    with database.transaction() as session:
        session.get(NotificationRecord, first.notification_id).visible_at = datetime(2025, 1, 1)
    assert not broker.extend(first, visibility_seconds=60)
    second = broker.receive(worker_id="b", queue_key=task.queue_key, visibility_seconds=30)
    assert second.delivery_token != first.delivery_token
    assert not broker.acknowledge(first)
    assert not broker.extend(first, visibility_seconds=30)
    assert broker.acknowledge(second)
    assert broker.acknowledge(second)
    assert not broker.extend(second, visibility_seconds=30)
    assert broker.receive(worker_id="c", queue_key=task.queue_key, visibility_seconds=30) is None
    with database.session_factory() as session:
        record = session.get(NotificationRecord, first.notification_id)
        assert record.status == "acked" and record.deliveries == 2 and record.acked_at is not None
        # M07 操作不会把任务伪装为完成或增加执行次数。
        task_record = session.get(TaskRecord, task.task_id)
        assert task_record.status == "queued" and task_record.attempt == 0


def test_concurrent_receivers_only_one_owns_delivery(environment):
    database, _, dispatcher = environment
    task, _ = seed(database)
    dispatcher.tick()
    broker = BrokerRepository(database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda worker: broker.receive(worker_id=worker, queue_key=task.queue_key, visibility_seconds=30),
            ("a", "b"),
        ))
    assert sum(delivery is not None for delivery in results) == 1


def test_runtime_reports_actual_components_and_database_time(environment, monkeypatch):
    database, _, _ = environment
    repository = RuntimeRepository(database)
    assert repository.list_components() == []
    now = datetime(2030, 1, 1)
    monkeypatch.setattr("backend.data.runtime.runtime_repository.database_now", lambda session: now)
    session_id = uuid4().hex
    repository.report(component_id="worker", session_id=session_id, pid=123, state="model_loading", child_pid=456)
    now += timedelta(seconds=2)
    repository.report(component_id="worker", session_id=session_id, pid=123, state="idle", model_ready=True, child_pid=456)
    current = repository.list_components()[0]
    assert current.started_at == now - timedelta(seconds=2)
    assert current.heartbeat_at == repository.now() == now
    assert current.model_ready and current.child_pid == 456
    repository.report(component_id="dispatcher", session_id=uuid4().hex, pid=321, state="idle")
    assert len(repository.list_components()) == 2
    now += timedelta(seconds=3)
    repository.report(component_id="worker", session_id=uuid4().hex, pid=789, state="model_loading")
    assert repository.list_components()[1].started_at == now
