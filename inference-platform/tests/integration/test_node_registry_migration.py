"""隔离数据库验证 0008；会话行是测试报告，不代表真实进程或模型验收。"""

from datetime import datetime, timedelta
from io import StringIO
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.database.base import Base
from backend.data.database.database import Database
from backend.data.nodes.node_models import ComputeNode, InstanceSession, ServiceInstance
from backend.data.runtime.runtime_repository import RuntimeRepository


NEW_TABLES = {"compute_nodes", "service_instances", "instance_sessions"}


@pytest.fixture
def node_database(tmp_path):
    settings = AppSettings(database_path=tmp_path / "nodes.sqlite3", log_dir=tmp_path / "logs")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    database = Database(settings)
    try:
        yield database, config, settings
    finally:
        database.dispose()


def registration_rows(settings):
    now = datetime(2026, 10, 14, 1, 2, 3, 123456)
    node = ComputeNode(node_id="a" * 32, local_identity="b" * 64, hostname="test-only",
                       os_name="Windows", os_version="test", architecture="AMD64", cpu_count=8,
                       registered_at=now, updated_at=now)
    instance = ServiceInstance(instance_id="c" * 32, node_id=node.node_id,
                               instance_key="asr-default", model_id="whisper-small",
                               model_version=settings.asr_model_revision, adapter_key="faster-whisper",
                               execution_device="cpu", registered_at=now)
    observation = InstanceSession(session_id="d" * 32, instance_id=instance.instance_id,
                                  source="legacy_worker_observation", worker_session_id="e" * 32,
                                  worker_pid=1234, child_pid=5678, reported_state="idle",
                                  reported_model_ready=True, active_task_id=None,
                                  heartbeat_at=now + timedelta(seconds=3), worker_started_at=now,
                                  observed_at=now + timedelta(seconds=4), last_error=None)
    return node, instance, observation


def old_tables_snapshot(database, tables):
    with database.engine.connect() as connection:
        return {
            table: {
                "ddl": connection.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"), {"name": table}).scalar_one(),
                "rows": [dict(row) for row in connection.execute(text(f'SELECT * FROM "{table}" ORDER BY rowid')).mappings()],
            } for table in tables
        }


def test_0008_is_additive_does_not_seed_running_state_and_preserves_old_rows(tmp_path):
    settings = AppSettings(database_path=tmp_path / "upgrade.sqlite3", log_dir=tmp_path / "logs")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "20261013_0007")
    database = Database(settings)
    try:
        old_tables = sorted(set(inspect(database.engine).get_table_names()) - {"alembic_version"})
        with database.engine.begin() as connection:
            for kind, task_id in (("asr", "1" * 32), ("vision", "2" * 32)):
                connection.execute(text("""
                    INSERT INTO tasks (task_id, caller_id, task_type, model_id, model_version,
                        parameters, input_ref, input_filename, input_sha256, input_size_bytes,
                        input_media_type, audio_duration_seconds, image_width, image_height,
                        queue_key, status, attempt, result_json)
                    VALUES (:id, 'local-user', :kind, :model, 'historical-version', '{}', :id,
                        '原始文件', :sha, 123, :media, :duration, :width, :height,
                        :queue, 'succeeded', 1, :result)
                """), {"id": task_id, "kind": kind, "model": "whisper-small" if kind == "asr" else "yolo-general",
                        "sha": "3" * 64, "media": "audio" if kind == "asr" else "image",
                        "duration": 1.25 if kind == "asr" else None,
                        "width": None if kind == "asr" else 100, "height": None if kind == "asr" else 80,
                        "queue": f"{kind}.original", "result": '{"text":"原始结果"}' if kind == "asr" else '{"detections":[]}'})
                connection.execute(text("""
                    INSERT INTO idempotency_records (id, caller_id, idempotency_key, request_fingerprint, task_id)
                    VALUES (:id, 'local-user', :kind, :fingerprint, :id)
                """), {"id": task_id, "kind": kind, "fingerprint": "4" * 64})
                connection.execute(text("""
                    INSERT INTO dispatch_events (event_id, task_id, queue_key, status)
                    VALUES (:id, :id, :queue, 'published')
                """), {"id": task_id, "queue": f"{kind}.original"})
                connection.execute(text("""
                    INSERT INTO notification_records (notification_id, event_id, task_id, queue_key,
                        dispatch_version, execution_plan_version, visible_at, status)
                    VALUES (:id, :id, :id, :queue, 1, 1, '2026-10-13 01:02:03', 'acked')
                """), {"id": task_id, "queue": f"{kind}.original"})
                connection.execute(text("""
                    INSERT INTO execution_attempts (lease_token, task_id, attempt, worker_id,
                        dispatch_version, execution_plan_version, started_at, finished_at, outcome)
                    VALUES (:id, :id, 1, 'historical-worker', 1, 1,
                        '2026-10-13 01:02:03', '2026-10-13 01:03:04', 'succeeded')
                """), {"id": task_id})
            connection.execute(text("""
                INSERT INTO online_requests (online_request_id, caller_id, model_id, model_version,
                    owner_id, request_fingerprint, accepted_at, updated_at, deadline_at, lease_expires_at)
                VALUES (:id, 'local-user', 'test-llm', 'fixed', :owner, :fingerprint,
                    '2026-10-13 01:02:03', '2026-10-13 01:02:03',
                    '2026-10-13 01:03:03', '2026-10-13 01:02:33')
            """), {"id": "5" * 32, "owner": "6" * 32, "fingerprint": "7" * 64})
        RuntimeRepository(database).report(component_id="worker", session_id="8" * 32,
                                           pid=1234, state="stopped")
        before = old_tables_snapshot(database, old_tables)
        # 该用例只裁决 0008 的加法迁移；后续受管表由各自阶段验收。
        command.upgrade(config, "20261014_0008")
        command.upgrade(config, "20261014_0008")
        assert old_tables_snapshot(database, old_tables) == before
        assert set(inspect(database.engine).get_table_names()) - set(old_tables) == NEW_TABLES | {"alembic_version"}
        with database.engine.connect() as connection:
            for name in NEW_TABLES:
                assert connection.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar_one() == 0
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        node, instance, observation = registration_rows(settings)
        with database.transaction() as session:
            session.add(node)
            session.flush()
            session.add(instance)
            session.flush()
            session.add(observation)
        command.downgrade(config, "20261013_0007")
        assert not NEW_TABLES & set(inspect(database.engine).get_table_names())
        assert old_tables_snapshot(database, old_tables) == before
        command.upgrade(config, "head")
        command.check(config)
        current = old_tables_snapshot(database, old_tables)
        assert {name: value for name, value in current.items() if name != "runtime_components"} == {
            name: value for name, value in before.items() if name != "runtime_components"}
        assert current["runtime_components"]["rows"] == before["runtime_components"]["rows"]
        assert "remote_asr_worker" in current["runtime_components"]["ddl"]
    finally:
        database.dispose()


def test_0008_columns_constraints_indexes_and_foreign_keys_match_orm(node_database):
    database, config, settings = node_database
    command.check(config)
    inspector = inspect(database.engine)
    for name in NEW_TABLES:
        table = Base.metadata.tables[name]
        assert {item["name"] for item in inspector.get_columns(name)} == set(table.columns.keys())
        assert {item["name"] for item in inspector.get_check_constraints(name)} == {
            item.name for item in table.constraints if item.__class__.__name__ == "CheckConstraint"}
        assert {item["name"] for item in inspector.get_unique_constraints(name)} == {
            item.name for item in table.constraints if item.__class__.__name__ == "UniqueConstraint"}
        assert {item["name"] for item in inspector.get_foreign_keys(name)} == {
            item.name for item in table.constraints if item.__class__.__name__ == "ForeignKeyConstraint"}
        assert {item["name"] for item in inspector.get_indexes(name)} == {item.name for item in table.indexes}
    for row in registration_rows(settings):
        assert row.to_dict()


@pytest.mark.parametrize("kind,field,value", [
    ("node", "node_id", "short"), ("node", "local_identity", "short"),
    ("node", "cpu_count", 0), ("node", "cpu_count", -1),
    ("instance", "instance_id", "short"), ("instance", "node_id", "f" * 32),
    ("instance", "model_version", "missing"), ("instance", "instance_key", "other"),
    ("instance", "execution_device", "cuda"),
    ("session", "session_id", "short"), ("session", "worker_session_id", "short"),
    ("session", "source", "agent"), ("session", "worker_pid", 0),
    ("session", "child_pid", -1), ("session", "instance_id", "f" * 32),
])
def test_0008_rejects_invalid_identity_and_orphan_records(node_database, kind, field, value):
    database, _, settings = node_database
    node, instance, observation = registration_rows(settings)
    target = {"node": node, "instance": instance, "session": observation}[kind]
    setattr(target, field, value)
    if kind != "node":
        with database.transaction() as session:
            session.add(node)
    if kind == "session":
        with database.transaction() as session:
            session.add(instance)
    with pytest.raises(IntegrityError), database.transaction() as session:
        session.add(target)


@pytest.mark.parametrize("kind", ["node", "instance", "session"])
def test_0008_unique_constraints_block_duplicate_identity(node_database, kind):
    database, _, settings = node_database
    rows = registration_rows(settings)
    for row in rows:
        with database.transaction() as session:
            session.add(row)
    other = registration_rows(settings)[{"node": 0, "instance": 1, "session": 2}[kind]]
    setattr(other, {"node": "node_id", "instance": "instance_id", "session": "session_id"}[kind], "f" * 32)
    with pytest.raises(IntegrityError), database.transaction() as session:
        session.add(other)


def test_0008_offline_sql_is_self_contained_and_does_not_seed_sessions(tmp_path):
    output = StringIO()
    config = Config(str(PROJECT_ROOT / "alembic.ini"), output_buffer=output)
    config.attributes["settings"] = AppSettings(database_path=tmp_path / "unused.sqlite3")
    command.upgrade(config, "20261013_0007:20261014_0008", sql=True)
    script = output.getvalue()
    assert "INSERT INTO instance_sessions" not in script
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version VALUES ('20261013_0007')")
        connection.executescript(script)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261014_0008"
        for name in NEW_TABLES:
            assert connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0
