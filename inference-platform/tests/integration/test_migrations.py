from datetime import datetime, timedelta, timezone
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.database.base import Base
from backend.data.database.database import Database, build_database_url
from backend.data.execution.attempt_models import ExecutionAttempt
from backend.data.tasks.task_models import DispatchEvent, TaskRecord
from backend.data.tasks.task_repository import TaskRepository


def test_baseline_upgrade_downgrade_and_metadata_check(tmp_path):
    settings = AppSettings(database_path=tmp_path / "db/migration.sqlite3", log_dir=tmp_path / "logs")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "20261012_0001")
    engine = create_engine(build_database_url(settings))
    try:
        assert inspect(engine).get_table_names() == ["alembic_version"]
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20261012_0001"
        command.upgrade(config, "head")
        command.check(config)
        expected_tables = ["agent_controls", "asr_call_permits", "asr_capacity_changes", "asr_capacity_groups", "asr_compute_targets",
                           "asr_target_revisions", "asr_target_worker_sessions", "compute_nodes", "dispatch_events", "execution_attempts", "idempotency_records",
                           "instance_sessions", "managed_instance_sessions", "model_versions", "models", "notification_records", "online_requests",
                           "remote_asr_calls", "runtime_components", "service_instances", "tasks"]
        assert sorted(Base.metadata.tables) == expected_tables
        assert inspect(engine).get_table_names() == sorted(["alembic_version", *expected_tables])
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == ScriptDirectory.from_config(config).get_current_head()
        command.downgrade(config, "20261012_0001")
        assert inspect(engine).get_table_names() == ["alembic_version"]
        command.downgrade(config, "base")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar_one() == 0
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine.dispose()


@pytest.fixture
def migrated_database(tmp_path):
    settings = AppSettings(database_path=tmp_path / "db/tasks.sqlite3", log_dir=tmp_path / "logs")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    database = Database(settings)
    try:
        yield database, config
    finally:
        database.dispose()


def make_records(*, caller_id="local", created_at=None):
    accepted_at = created_at or datetime.now(timezone.utc)
    task = TaskRecord(
        task_id=uuid4().hex,
        caller_id=caller_id,
        model_id="whisper-small",
        model_version="536b0662742c02347bc0e980a01041f333bce120",
        parameters={"language": "zh"},
        input_ref=uuid4().hex,
        input_filename="测试.wav",
        input_sha256="a" * 64,
        input_size_bytes=100,
        audio_duration_seconds=0.1,
        queue_key="asr:whisper-small:cpu",
        created_at=accepted_at,
        updated_at=accepted_at,
    )
    event = DispatchEvent(
        event_id=uuid4().hex,
        task_id=task.task_id,
        queue_key=task.queue_key,
        available_at=accepted_at,
        created_at=accepted_at,
    )
    return task, event


def test_task_records_survive_migration_recheck_and_new_connection(migrated_database):
    database, config = migrated_database
    repository = TaskRepository(database)
    task, event = make_records()
    repository.create_with_event(task=task, idempotency_key="request-one", request_fingerprint="f" * 64, event=event)
    task_id, input_ref = task.task_id, task.input_ref
    database.dispose()

    # 再次执行 upgrade head 是无操作，不删除已接受任务。
    command.upgrade(config, "head")
    command.check(config)
    reloaded = repository.get_task("local", task_id)
    assert reloaded is not None
    assert reloaded.input_filename == "测试.wav"
    assert reloaded.parameters == {"language": "zh"}
    assert reloaded.status == "created"
    assert reloaded.attempt == 0
    assert reloaded.execution_plan_version == reloaded.dispatch_version == 1
    assert reloaded.to_dict()["task_id"] == task_id
    fingerprint, duplicate = repository.find_idempotent("local", "request-one")
    assert fingerprint == "f" * 64
    assert duplicate.task_id == task_id
    assert repository.find_idempotent("other", "request-one") is None
    assert repository.get_task("other", task_id) is None
    assert repository.referenced_input_refs() == {input_ref}
    with database.engine.connect() as connection:
        row = connection.execute(text("SELECT status, event_type FROM dispatch_events")).one()
        assert tuple(row) == ("pending", "task.created")
        assert connection.execute(text("SELECT COUNT(*) FROM idempotency_records")).scalar_one() == 1


def test_idempotency_constraint_rolls_back_task_and_event(migrated_database):
    database, _ = migrated_database
    repository = TaskRepository(database)
    original, original_event = make_records()
    repository.create_with_event(task=original, idempotency_key="same-key", request_fingerprint="a" * 64, event=original_event)
    failed, failed_event = make_records()
    failed_id = failed.task_id
    with pytest.raises(IntegrityError):
        repository.create_with_event(task=failed, idempotency_key="same-key", request_fingerprint="b" * 64, event=failed_event)
    assert repository.get_task("local", failed_id) is None
    assert repository.list_tasks("local", page=1, page_size=10)[1] == 1
    with database.engine.connect() as connection:
        for table_name in ("tasks", "idempotency_records", "dispatch_events"):
            assert connection.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one() == 1


def test_failed_event_foreign_key_rolls_back_whole_submission(migrated_database):
    database, _ = migrated_database
    repository = TaskRepository(database)
    task, event = make_records()
    event.task_id = uuid4().hex
    with pytest.raises(IntegrityError):
        repository.create_with_event(task=task, idempotency_key="bad-event", request_fingerprint="f" * 64, event=event)
    assert repository.list_tasks("local", page=1, page_size=10) == ([], 0)
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM idempotency_records")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM dispatch_events")).scalar_one() == 0


def test_same_idempotency_key_is_scoped_to_caller_and_lists_are_paginated(migrated_database):
    database, _ = migrated_database
    repository = TaskRepository(database)
    accepted_at = datetime.now(timezone.utc)
    expected_ids = []
    for number in range(3):
        task, event = make_records(created_at=accepted_at + timedelta(seconds=number))
        repository.create_with_event(task=task, idempotency_key=f"key-{number}", request_fingerprint="a" * 64, event=event)
        expected_ids.insert(0, task.task_id)
    other, other_event = make_records(caller_id="another")
    repository.create_with_event(task=other, idempotency_key="key-0", request_fingerprint="b" * 64, event=other_event)
    first_page, total = repository.list_tasks("local", page=1, page_size=2)
    second_page, second_total = repository.list_tasks("local", page=2, page_size=2)
    assert total == second_total == 3
    assert [task.task_id for task in first_page + second_page] == expected_ids
    assert repository.list_tasks("local", page=3, page_size=2) == ([], 3)
    assert repository.list_tasks("another", page=1, page_size=10)[1] == 1


def test_migrated_constraints_and_indexes_match_metadata(migrated_database):
    database, config = migrated_database
    command.check(config)
    inspector = inspect(database.engine)
    for table in Base.metadata.sorted_tables:
        actual_checks = {constraint["name"] for constraint in inspector.get_check_constraints(table.name)}
        expected_checks = {constraint.name for constraint in table.constraints if constraint.__class__.__name__ == "CheckConstraint"}
        assert actual_checks == expected_checks
        actual_uniques = {tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints(table.name)}
        expected_uniques = {tuple(column.name for column in constraint.columns) for constraint in table.constraints if constraint.__class__.__name__ == "UniqueConstraint"}
        assert actual_uniques == expected_uniques
        assert {index["name"] for index in inspector.get_indexes(table.name)} == {index.name for index in table.indexes}


def test_part3_data_survives_part4_upgrade_and_downgrade(tmp_path):
    settings = AppSettings(database_path=tmp_path / "upgrade.sqlite3")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "20261012_0002")
    database = Database(settings)
    task_id, input_ref, event_id = uuid4().hex, uuid4().hex, uuid4().hex
    try:
        with database.engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO tasks (task_id, caller_id, model_id, model_version, parameters,
                    input_ref, input_filename, input_sha256, input_size_bytes, audio_duration_seconds, queue_key)
                VALUES (:task_id, 'local', 'whisper-small', 'revision', '{"language":"zh"}',
                    :input_ref, '原始任务.wav', :sha, 100, 0.1, 'asr:whisper-small:cpu')
            """), {"task_id": task_id, "input_ref": input_ref, "sha": "a" * 64})
            connection.execute(text("""
                INSERT INTO idempotency_records (id, caller_id, idempotency_key, request_fingerprint, task_id)
                VALUES (:id, 'local', 'old-key', :fingerprint, :task_id)
            """), {"id": uuid4().hex, "fingerprint": "f" * 64, "task_id": task_id})
            connection.execute(text("""
                INSERT INTO dispatch_events (event_id, task_id, queue_key)
                VALUES (:event_id, :task_id, 'asr:whisper-small:cpu')
            """), {"event_id": event_id, "task_id": task_id})
        command.upgrade(config, "head")
        command.check(config)
        task = TaskRepository(database).get_task("local", task_id)
        assert task.input_ref == input_ref
        assert task.input_filename == "原始任务.wav"
        assert task.status == "created"
        assert task.lease_token is task.result_json is task.started_at is None
        with database.session_factory() as session:
            event = session.get(DispatchEvent, event_id)
            assert event.status == "pending" and event.publish_attempts == 0
        assert TaskRepository(database).find_idempotent("local", "old-key")[1].task_id == task_id
        command.downgrade(config, "20261012_0002")
        with database.engine.connect() as connection:
            assert connection.execute(text("SELECT task_id, status FROM tasks")).one() == (task_id, "created")
            assert connection.execute(text("SELECT event_id, status FROM dispatch_events")).one() == (event_id, "pending")
        command.upgrade(config, "head")
        command.check(config)
    finally:
        database.dispose()


def test_part4_terminal_and_inflight_data_survive_part6_migration(tmp_path):
    settings = AppSettings(database_path=tmp_path / "part6-upgrade.sqlite3")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "20261012_0003")
    database = Database(settings)
    task_ids = {status: uuid4().hex for status in ("succeeded", "running", "created")}
    accepted = datetime(2026, 1, 1, 1, 2, 3, 125678)
    started = accepted + timedelta(seconds=7)
    try:
        with database.engine.begin() as connection:
            for status, task_id in task_ids.items():
                executed = status != "created"
                connection.execute(text("""
                    INSERT INTO tasks (task_id, caller_id, model_id, model_version, parameters,
                        input_ref, input_filename, input_sha256, input_size_bytes, audio_duration_seconds,
                        queue_key, status, attempt, lease_token, lease_worker_id, lease_expires_at,
                        created_at, updated_at, started_at, finished_at, result_json)
                    VALUES (:id, 'local', 'whisper-small', 'revision', '{"language":"zh"}', :ref,
                        '保留.wav', :sha, 100, 0.1, 'asr:whisper-small:cpu', :status, :attempt,
                        :token, :worker, :expires, :accepted, :accepted, :started, :finished, :result)
                """), {"id": task_id, "ref": uuid4().hex, "sha": "a" * 64, "status": status,
                         "attempt": 1 if executed else 0, "token": task_id if executed else None,
                         "worker": "original-worker" if executed else None,
                         "expires": started + timedelta(seconds=30) if executed else None,
                         "accepted": accepted, "started": started if executed else None,
                         "finished": started + timedelta(seconds=1) if status == "succeeded" else None,
                         "result": '{"text":"原有真实结果不可更改"}' if status == "succeeded" else None})
        command.upgrade(config, "head")
        command.check(config)
        with database.session_factory() as session:
            for status, task_id in task_ids.items():
                task = session.get(TaskRecord, task_id)
                assert task.status == status
                assert task.queue_deadline_at == accepted + timedelta(seconds=600)
                assert task.total_deadline_at == accepted + timedelta(seconds=1800)
                assert task.max_attempts == 2
                if status == "created":
                    assert task.attempt_deadline_at is None
                    assert session.get(ExecutionAttempt, task_id) is None
                else:
                    assert task.attempt_deadline_at == started + timedelta(seconds=900)
                    history = session.get(ExecutionAttempt, task_id)
                    assert history.outcome == status and history.worker_id == "original-worker"
            assert session.get(TaskRecord, task_ids["succeeded"]).result_json == {"text": "原有真实结果不可更改"}
        command.downgrade(config, "20261012_0003")
        with database.engine.connect() as connection:
            assert set(connection.execute(text("SELECT status FROM tasks")).scalars()) == set(task_ids)
        command.upgrade(config, "head")
        command.check(config)
    finally:
        database.dispose()


@pytest.mark.parametrize("status", ["retry_wait", "cancel_requested", "cancelled"])
def test_part6_downgrade_refuses_states_unreadable_by_part4(migrated_database, status):
    database, config = migrated_database
    task, event = make_records()
    task.status = status
    TaskRepository(database).create_with_event(task=task, idempotency_key="keep-part6",
                                               request_fingerprint="a" * 64, event=event)
    with pytest.raises(RuntimeError, match="不能降级"):
        command.downgrade(config, "20261012_0003")
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20261012_0004"
        assert connection.execute(text("SELECT status FROM tasks")).scalar_one() == status
    assert "execution_attempts" in inspect(database.engine).get_table_names()
