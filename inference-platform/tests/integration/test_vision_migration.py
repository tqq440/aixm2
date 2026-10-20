"""仅使用临时 SQLite 验证第十部分迁移和跨类型持久化边界。"""

from datetime import datetime, timedelta
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.database.base import Base
from backend.data.database.database import Database
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.data.tasks.task_models import DispatchEvent, TaskRecord
from backend.data.tasks.task_repository import TaskRepository


@pytest.fixture
def vision_database(tmp_path):
    settings = AppSettings(database_path=tmp_path / "vision.sqlite3", log_dir=tmp_path / "logs")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    database = Database(settings)
    try:
        yield database, config
    finally:
        database.dispose()


def make_task(*, task_type="asr", caller="local-user", accepted_at=None, **overrides):
    now = accepted_at or datetime(2026, 10, 13, 10)
    values = dict(
        task_id=uuid4().hex, caller_id=caller, task_type=task_type,
        model_id="whisper-small" if task_type == "asr" else "yolo-general",
        model_version="fixed-test-version", parameters={"language": "zh"} if task_type == "asr" else {"confidence_threshold": 0.25},
        input_ref=uuid4().hex, input_filename="fixture.wav" if task_type == "asr" else "fixture.png",
        input_sha256="a" * 64, input_size_bytes=100,
        queue_key="asr.whisper-small" if task_type == "asr" else "vision.yolo-general",
        created_at=now, updated_at=now,
    )
    if task_type == "asr":
        # 旧调用方没有媒体字段，ORM 默认必须继续生效。
        values["audio_duration_seconds"] = 1.25
    else:
        values.update(input_media_type="image", audio_duration_seconds=None, image_width=810, image_height=1080)
    values.update(overrides)
    task = TaskRecord(**values)
    event = DispatchEvent(event_id=uuid4().hex, task_id=task.task_id, queue_key=task.queue_key,
                          created_at=now, available_at=now)
    return task, event


def test_0006_preserves_all_old_columns_and_foreign_keys_without_seeding_models(tmp_path):
    settings = AppSettings(database_path=tmp_path / "upgrade.sqlite3")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "20261013_0005")
    database = Database(settings)
    task_id, event_id = "a" * 32, "b" * 32
    tables = ("tasks", "idempotency_records", "dispatch_events", "notification_records",
              "execution_attempts", "runtime_components", "models", "model_versions")
    try:
        with database.engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO tasks (task_id, caller_id, task_type, model_id, model_version, parameters,
                    input_ref, input_filename, input_sha256, input_size_bytes, audio_duration_seconds,
                    status, attempt, queue_key, lease_token, lease_worker_id, created_at, updated_at,
                    started_at, finished_at, queue_deadline_at, total_deadline_at, result_json, result_fingerprint)
                VALUES (:id, 'local-user', 'asr', 'whisper-small', 'original-version', '{"language":"zh"}',
                    :ref, '原始音频.wav', :sha, 100, 1.25, 'succeeded', 1, 'asr.whisper-small',
                    'original-lease', 'original-worker', '2026-10-12 01:02:03.123456', '2026-10-12 01:03:03.123456',
                    '2026-10-12 01:03:00.123456', '2026-10-12 01:03:03.123456',
                    '2026-10-12 01:12:03.123456', '2026-10-12 01:32:03.123456',
                    '{"text":"保留已完成结果"}', :fingerprint)
            """), {"id": task_id, "ref": "c" * 32, "sha": "d" * 64, "fingerprint": "e" * 64})
            connection.execute(text("""
                INSERT INTO idempotency_records (id, caller_id, idempotency_key, request_fingerprint, task_id)
                VALUES (:id, 'local-user', 'original-key', :fingerprint, :task)
            """), {"id": "f" * 32, "fingerprint": "1" * 64, "task": task_id})
            connection.execute(text("""
                INSERT INTO dispatch_events (event_id, task_id, queue_key, status)
                VALUES (:event, :task, 'asr.whisper-small', 'published')
            """), {"event": event_id, "task": task_id})
            connection.execute(text("""
                INSERT INTO notification_records (notification_id, event_id, task_id, queue_key,
                    dispatch_version, execution_plan_version, visible_at, status)
                VALUES (:notification, :event, :task, 'asr.whisper-small', 1, 1, '2026-10-12 01:02:03', 'acked')
            """), {"notification": "2" * 32, "event": event_id, "task": task_id})
            connection.execute(text("""
                INSERT INTO execution_attempts (lease_token, task_id, attempt, worker_id, dispatch_version,
                    execution_plan_version, started_at, finished_at, outcome)
                VALUES ('original-lease', :task, 1, 'original-worker', 1, 1,
                    '2026-10-12 01:03:00.123456', '2026-10-12 01:03:03.123456', 'succeeded')
            """), {"task": task_id})
            connection.execute(text("""
                INSERT INTO runtime_components (component_id, session_id, pid, state, heartbeat_at, started_at)
                VALUES ('worker', :session, 1234, 'stopped', '2026-10-12 01:03:04', '2026-10-12 01:02:00')
            """), {"session": "3" * 32})
            before = {table: [dict(row) for row in connection.execute(text(f"SELECT * FROM {table}")).mappings()]
                      for table in tables}
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        command.check(config)
        with database.engine.connect() as connection:
            for table, expected in before.items():
                columns = ", ".join(expected[0])
                assert [dict(row) for row in connection.execute(text(f"SELECT {columns} FROM {table}")).mappings()] == expected
            assert connection.execute(text("SELECT input_media_type, image_width, image_height FROM tasks")).one() == ("audio", None, None)
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            assert connection.execute(text("SELECT COUNT(*) FROM model_versions")).scalar_one() == 1
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == ScriptDirectory.from_config(config).get_current_head()
        command.downgrade(config, "20261013_0005")
        with database.engine.connect() as connection:
            for table, expected in before.items():
                assert [dict(row) for row in connection.execute(text(f"SELECT * FROM {table}")).mappings()] == expected
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        command.upgrade(config, "head")
        command.check(config)
    finally:
        database.dispose()


def test_asr_defaults_and_vision_metadata_persist(vision_database):
    database, _ = vision_database
    repository = TaskRepository(database)
    for kind in ("asr", "vision"):
        task, event = make_task(task_type=kind)
        repository.create_with_event(task=task, event=event, idempotency_key=kind, request_fingerprint="a" * 64)
        saved = repository.get_task("local-user", task.task_id)
        assert saved.to_dict()["input_media_type"] == ("audio" if kind == "asr" else "image")
        assert saved.audio_duration_seconds == (1.25 if kind == "asr" else None)
        assert (saved.image_width, saved.image_height) == ((None, None) if kind == "asr" else (810, 1080))


@pytest.mark.parametrize("kind,invalid", [
    ("asr", {"audio_duration_seconds": None}),
    ("asr", {"audio_duration_seconds": 0}),
    ("asr", {"input_media_type": "image"}),
    ("asr", {"image_width": 1, "image_height": 1}),
    ("vision", {"input_media_type": "audio"}),
    ("vision", {"audio_duration_seconds": 1}),
    ("vision", {"image_width": 0}),
    ("vision", {"image_width": None}),
    ("vision", {"image_height": -1}),
    ("vision", {"image_height": None}),
    ("vision", {"task_type": "llm"}),
])
def test_database_rejects_mismatched_or_incomplete_input_metadata(vision_database, kind, invalid):
    database, _ = vision_database
    task, event = make_task(task_type=kind)
    for name, value in invalid.items():
        setattr(task, name, value)
    with pytest.raises(IntegrityError):
        TaskRepository(database).create_with_event(task=task, event=event, idempotency_key="invalid", request_fingerprint="a" * 64)
    with database.engine.connect() as connection:
        for table in ("tasks", "idempotency_records", "dispatch_events"):
            assert connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0


def test_list_filters_items_totals_callers_and_pages_by_task_type(vision_database):
    database, _ = vision_database
    repository = TaskRepository(database)
    expected = {"asr": [], "vision": []}
    for number, kind in enumerate(("asr", "vision", "asr", "vision", "vision")):
        task, event = make_task(task_type=kind, accepted_at=datetime(2026, 10, 13, 10) + timedelta(seconds=number))
        repository.create_with_event(task=task, event=event, idempotency_key=str(number), request_fingerprint="a" * 64)
        expected[kind].insert(0, task.task_id)
    other, event = make_task(task_type="vision", caller="another-user")
    repository.create_with_event(task=other, event=event, idempotency_key="0", request_fingerprint="a" * 64)
    default, total = repository.list_tasks("local-user", page=1, page_size=10)
    assert total == 2 and [task.task_id for task in default] == expected["asr"]
    first, total = repository.list_tasks("local-user", page=1, page_size=2, task_type="vision")
    second, second_total = repository.list_tasks("local-user", page=2, page_size=2, task_type="vision")
    assert total == second_total == 3
    assert [task.task_id for task in first + second] == expected["vision"]
    assert repository.list_tasks("local-user", page=3, page_size=2, task_type="vision") == ([], 3)
    all_tasks, total = repository.list_tasks("local-user", page=1, page_size=10, task_type=None)
    assert total == len(all_tasks) == 5
    assert repository.referenced_input_refs() == {task.input_ref for task in all_tasks} | {other.input_ref}


def test_inflight_count_stays_global_across_media_types(vision_database):
    database, _ = vision_database
    repository = TaskRepository(database)
    counts = []
    for number, kind in enumerate(("asr", "vision", "asr")):
        task, event = make_task(task_type=kind)
        repository.create_with_event(task=task, event=event, idempotency_key=str(number), request_fingerprint="a" * 64,
                                     before_create=lambda accepted_at, inflight: counts.append(inflight))
    assert counts == [0, 1, 2]


def test_idempotency_key_and_input_reference_remain_unique_across_task_types(vision_database):
    database, _ = vision_database
    repository = TaskRepository(database)
    audio, event = make_task()
    repository.create_with_event(task=audio, event=event, idempotency_key="shared", request_fingerprint="a" * 64)
    image, event = make_task(task_type="vision")
    with pytest.raises(IntegrityError):
        repository.create_with_event(task=image, event=event, idempotency_key="shared", request_fingerprint="b" * 64)
    image, event = make_task(task_type="vision", input_ref=audio.input_ref)
    with pytest.raises(IntegrityError):
        repository.create_with_event(task=image, event=event, idempotency_key="different", request_fingerprint="b" * 64)
    assert repository.find_idempotent("local-user", "shared")[1].task_id == audio.task_id
    assert repository.list_tasks("local-user", page=1, page_size=10, task_type=None)[1] == 1


def test_runtime_persists_three_distinct_components_and_rejects_other_ids(vision_database):
    database, _ = vision_database
    runtime = RuntimeRepository(database)
    for number, component_id in enumerate(("worker", "vision_worker", "dispatcher")):
        runtime.report(component_id=component_id, session_id=uuid4().hex, pid=number + 1, state="ready")
    components = runtime.list_components()
    assert {component.component_id for component in components} == {"worker", "vision_worker", "dispatcher"}
    with pytest.raises(IntegrityError):
        runtime.report(component_id="unknown", session_id=uuid4().hex, pid=100, state="ready")
    assert len(runtime.list_components()) == 3


@pytest.mark.parametrize("kind", ["task", "runtime"])
def test_downgrade_refuses_vision_records_without_deleting_anything(vision_database, kind):
    database, config = vision_database
    if kind == "task":
        task, event = make_task(task_type="vision")
        TaskRepository(database).create_with_event(task=task, event=event, idempotency_key="keep", request_fingerprint="a" * 64)
    else:
        RuntimeRepository(database).report(component_id="vision_worker", session_id=uuid4().hex, pid=123, state="stopped")
    with pytest.raises(RuntimeError, match="不能降级"):
        command.downgrade(config, "20261013_0005")
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20261013_0006"
        table = "tasks" if kind == "task" else "runtime_components"
        assert connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 1
    assert "image_width" in {column["name"] for column in inspect(database.engine).get_columns("tasks")}


def test_vision_migration_constraints_and_indexes_match_orm(vision_database):
    database, config = vision_database
    command.check(config)
    inspector = inspect(database.engine)
    for table in Base.metadata.sorted_tables:
        assert {constraint["name"] for constraint in inspector.get_check_constraints(table.name)} == {
            constraint.name for constraint in table.constraints if constraint.__class__.__name__ == "CheckConstraint"}
        assert {index["name"] for index in inspector.get_indexes(table.name)} == {index.name for index in table.indexes}
