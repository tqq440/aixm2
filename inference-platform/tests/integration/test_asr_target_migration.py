"""0011 加法迁移与持久许可；仅临时 SQLite，不代表真实 GPU 或并发容量。"""

from datetime import datetime
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.asr.target_models import (
    AsrCallPermit, AsrCapacityGroup, AsrComputeTarget, AsrTargetRevision, AsrTargetWorkerSession,
)
from backend.data.asr.target_repository import AsrTargetRepository
from backend.data.database.database import Database
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.data.tasks.task_models import TaskRecord


REVISION = "20261019_0011"
PREVIOUS = "20261015_0010"
NEW_TABLES = {"asr_capacity_groups", "asr_compute_targets", "asr_target_revisions",
              "asr_target_worker_sessions", "asr_call_permits"}
NOW = datetime(2026, 10, 19, 1, 2, 3, 456000)
REPOSITORY = AsrTargetRepository()


def setup_database(tmp_path, revision=REVISION):
    settings = AppSettings(database_path=tmp_path / "targets.sqlite3", log_dir=tmp_path / "logs")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, revision)
    return Database(settings), config


@pytest.fixture
def target_database(tmp_path):
    database, config = setup_database(tmp_path, "head")
    try:
        yield database, config
    finally:
        database.dispose()


def add_task(session):
    task_id = uuid4().hex
    session.add(TaskRecord(task_id=task_id, caller_id="local-user", task_type="asr",
        model_id="sensevoice-external", model_version="unknown-historical-binding",
        parameters={"language": None}, input_ref=uuid4().hex, input_filename="原始音频.wav",
        input_sha256="a" * 64, input_size_bytes=100, audio_duration_seconds=0.5,
        queue_key="asr.remote-v2-sensevoice", status="succeeded", result_json={"text": "保留原始结果"},
        created_at=NOW, updated_at=NOW))
    session.flush()
    return task_id


def snapshot(database, names):
    with database.engine.connect() as connection:
        return {name: {
            "columns": tuple(row[1] for row in connection.execute(text(f'PRAGMA table_info("{name}")'))),
            "rows": connection.execute(text(f'SELECT * FROM "{name}" ORDER BY rowid')).all(),
            "schema": connection.execute(text(
                "SELECT type, name, sql FROM sqlite_master WHERE tbl_name=:name ORDER BY type, name"),
                {"name": name}).all(),
        } for name in names}


def add_target(session, *, target_id="target-a", group_id="shared-a", endpoint="a" * 64,
               fingerprint="b" * 64, active_slot_key=None):
    if REPOSITORY.get_group(session, group_id) is None:
        REPOSITORY.add_group(session, group_id=group_id, created_at=NOW)
        session.flush()
    target = REPOSITORY.add_target(session, target_id=target_id, capacity_group_id=group_id, created_at=NOW)
    session.flush()
    revision = REPOSITORY.add_revision(session, revision_id=uuid4().hex + uuid4().hex,
        target_id=target_id, config_fingerprint=fingerprint, endpoint_identity=endpoint,
        config_snapshot={"testOnly": True}, device_kind="unknown", created_at=NOW)
    session.flush()
    worker = REPOSITORY.add_worker(session, session_id=uuid4().hex, revision_id=revision.revision_id,
        worker_key=target_id + ":0", slot_index=0, active_slot_key=active_slot_key,
        pid=1234, state="ready", model_ready=True, heartbeat_at=NOW, started_at=NOW)
    session.flush()
    return target, revision, worker


def add_permit(session, task_id, target, revision, worker, state="held"):
    row = REPOSITORY.add_permit(session, task_id=task_id, revision_id=revision.revision_id,
        capacity_group_id=target.capacity_group_id, worker_session_id=worker.session_id,
        lease_token=uuid4().hex, state=state, created_at=NOW)
    session.flush()
    return row


def test_0011_preserves_all_0010_rows_columns_and_indexes_without_seed(tmp_path):
    database, config = setup_database(tmp_path, PREVIOUS)
    try:
        with database.transaction() as session:
            first, second = add_task(session), add_task(session)
            session.add(RemoteAsrCall(task_id=first, profile_id="remote-v2-sensevoice",
                config_fingerprint="b" * 64, request_id=first, state="uncertain", created_at=NOW,
                worker_id="legacy-session", lease_token="c" * 32, reserved_at=NOW, ended_at=NOW))
            session.add(RemoteAsrCall(task_id=second, profile_id="remote-v2-sensevoice",
                config_fingerprint="b" * 64, request_id=second, state="queued", created_at=NOW))
        RuntimeRepository(database).report(component_id="remote_asr_worker", session_id="d" * 32,
                                           pid=1234, state="stopped", model_ready=False)
        old_tables = sorted(set(inspect(database.engine).get_table_names()) - {"alembic_version"})
        before = snapshot(database, old_tables)
        command.upgrade(config, REVISION)
        command.upgrade(config, REVISION)
        # 历史0011按当时DDL核对；当前ORM还包括0012，不能在旧head作metadata比较。
        assert snapshot(database, old_tables) == before
        assert set(inspect(database.engine).get_table_names()) - set(old_tables) == NEW_TABLES | {"alembic_version"}
        with database.engine.connect() as connection:
            for name in NEW_TABLES:
                assert connection.scalar(text(f'SELECT count(*) FROM "{name}"')) == 0
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        with database.session_factory() as session:
            assert REPOSITORY.untracked_legacy_active(session)
        command.downgrade(config, PREVIOUS)
        assert snapshot(database, old_tables) == before
        assert not NEW_TABLES & set(inspect(database.engine).get_table_names())
        command.upgrade(config, REVISION)
        assert snapshot(database, old_tables) == before
    finally:
        database.dispose()


def test_0011_refuses_losing_registered_targets_or_history(tmp_path):
    database, config = setup_database(tmp_path, REVISION)
    try:
        with database.engine.begin() as connection:
            connection.execute(text("INSERT INTO asr_capacity_groups(group_id) VALUES ('historical')"))
            connection.execute(text("INSERT INTO asr_compute_targets(target_id,capacity_group_id) VALUES ('target-a','historical')"))
        names = sorted(set(inspect(database.engine).get_table_names()) - {"alembic_version"})
        before = snapshot(database, names)
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, PREVIOUS)
        assert snapshot(database, names) == before
        with database.engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
    finally:
        database.dispose()


def test_target_revision_and_worker_constraints_preserve_fixed_identity(target_database):
    database, config = target_database
    with database.transaction() as session:
        target, revision, worker = add_target(session, active_slot_key="target-a:0")
        assert target.to_dict()["target_limit"] == 1 and target.enabled is False
        assert REPOSITORY.get_group(session, "shared-a").to_dict()["capacity_limit"] == 1
        assert revision.to_dict()["config_snapshot"] == {"testOnly": True}
        revision_id, worker_id = revision.revision_id, worker.session_id
    invalid_rows = [
        AsrCapacityGroup(group_id="invalid", capacity_limit=0),
        AsrComputeTarget(target_id="invalid", capacity_group_id="shared-a", target_limit=0),
        AsrComputeTarget(target_id="invalid", capacity_group_id="missing"),
        AsrTargetRevision(revision_id="invalid", target_id="target-a", config_fingerprint="c" * 64,
                          endpoint_identity="a" * 64, config_snapshot={}, device_kind="invented"),
        AsrTargetRevision(revision_id="duplicate", target_id="target-a", config_fingerprint="b" * 64,
                          endpoint_identity="a" * 64, config_snapshot={}, device_kind="unknown"),
        AsrTargetWorkerSession(session_id=uuid4().hex, revision_id=revision_id, worker_key="target-a:0",
            slot_index=0, active_slot_key="target-a:0", pid=999, state="starting"),
        AsrTargetWorkerSession(session_id=uuid4().hex, revision_id=revision_id, worker_key="target-a:1",
            slot_index=-1, pid=999, state="starting"),
        AsrTargetWorkerSession(session_id=uuid4().hex, revision_id="missing", worker_key="invalid",
            slot_index=0, pid=999, state="starting"),
    ]
    for row in invalid_rows:
        with pytest.raises(IntegrityError), database.transaction() as session:
            session.add(row)
            session.flush()
    with database.transaction() as session:
        assert REPOSITORY.active_worker(session, "target-a:0").session_id == worker_id
        original = REPOSITORY.get_worker(session, worker_id)
        REPOSITORY.set_fields(original, active_slot_key=None, ended_at=NOW, state="stopped")
        session.flush()
        # 空闲会话历史可有多行；活动槽位仍只允许一个所有者。
        REPOSITORY.add_worker(session, session_id=uuid4().hex, revision_id=revision_id,
            worker_key="target-a:0", slot_index=0, active_slot_key=None, pid=999, state="stopped")
        assert REPOSITORY.active_worker(session, None) is None
    command.check(config)


def test_repository_counts_groups_and_keeps_untracked_legacy_calls_blocking(target_database):
    database, _ = target_database
    with database.transaction(immediate=True) as session:
        first = add_target(session, target_id="target-a", group_id="shared-a")
        alias = add_target(session, target_id="target-b", group_id="shared-a")
        independent = add_target(session, target_id="target-c", group_id="shared-c", endpoint="c" * 64)
        first_task, alias_task, other_task = [add_task(session) for _ in range(3)]
        legacy_first = RemoteAsrCall(task_id=first_task, profile_id="legacy-one", config_fingerprint="b" * 64,
                                    request_id=first_task, state="held", created_at=NOW)
        session.add(legacy_first)
        session.add(RemoteAsrCall(task_id=alias_task, profile_id="legacy-two", config_fingerprint="b" * 64,
                                 request_id=alias_task, state="uncertain", created_at=NOW))
        session.flush()
        assert REPOSITORY.untracked_legacy_active(session)
        first_permit = add_permit(session, first_task, *first)
        assert REPOSITORY.untracked_legacy_active(session)
        add_permit(session, alias_task, *alias, state="uncertain")
        add_permit(session, other_task, *independent, state="released")
        assert not REPOSITORY.untracked_legacy_active(session)
        assert REPOSITORY.active_counts(session, "target-a", "shared-a") == (1, 2)
        assert REPOSITORY.active_counts(session, "target-c", "shared-c") == (0, 0)
        assert REPOSITORY.endpoint_groups(session, "a" * 64) == {"shared-a"}
        assert REPOSITORY.endpoint_groups(session, "c" * 64) == {"shared-c"}
        assert REPOSITORY.revision_by_config(session, "target-a", "b" * 64).revision_id == first[1].revision_id
        assert REPOSITORY.get_target(session, "target-a").target_id == "target-a"
        assert REPOSITORY.get_revision(session, first[1].revision_id).target_id == "target-a"
        assert REPOSITORY.get_permit(session, first_task).to_dict()["state"] == "held"
        assert len(REPOSITORY.list_groups(session)) == 2
        assert len(REPOSITORY.list_targets(session)) == len(REPOSITORY.list_revisions(session)) == 3
        assert len(REPOSITORY.list_workers(session)) == len(REPOSITORY.list_permits(session)) == 3
        REPOSITORY.set_fields(first_permit, state="released", ended_at=NOW)
        session.flush()
        assert REPOSITORY.untracked_legacy_active(session)
        assert REPOSITORY.active_counts(session, "target-a", "shared-a") == (0, 1)
        legacy_first.state = "released"
        session.flush()
        assert not REPOSITORY.untracked_legacy_active(session)


def test_permit_foreign_keys_unique_task_and_state_constraints(target_database):
    database, _ = target_database
    with database.transaction() as session:
        target, revision, worker = add_target(session)
        first_task, second_task = add_task(session), add_task(session)
        add_permit(session, first_task, target, revision, worker)
        fields = dict(task_id=second_task, revision_id=revision.revision_id,
                      capacity_group_id=target.capacity_group_id, worker_session_id=worker.session_id,
                      lease_token=uuid4().hex, state="held")
    for changes in ({"task_id": uuid4().hex}, {"revision_id": "missing"},
                    {"capacity_group_id": "missing"}, {"worker_session_id": uuid4().hex},
                    {"task_id": first_task}, {"state": "queued"}, {"lease_token": "invalid"}):
        with pytest.raises(IntegrityError), database.transaction() as session:
            REPOSITORY.add_permit(session, **(fields | changes))
            session.flush()
    with database.session_factory() as session:
        assert len(REPOSITORY.list_permits(session)) == 1
        assert REPOSITORY.get_permit(session, second_task) is None
    with database.engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
