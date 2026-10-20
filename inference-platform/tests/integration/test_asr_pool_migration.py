"""0012真实SQLite迁移保留0011历史，拒绝回退丢弃多槽事实。"""

from datetime import datetime
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import MetaData, inspect, text

from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.database.database import Database
from backend.service.asr.target_service import TargetAdmissionService


NOW = datetime(2026, 10, 19, 1, 2, 3)


def setup(tmp_path):
    settings = AppSettings(database_path=tmp_path / "migration.sqlite3", log_dir=tmp_path / "logs",
        remote_asr_base_url="https://migration-fixture.invalid:8003",
        remote_asr_model_id="fixture-model", remote_asr_model_version="fixture-1")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "20261019_0011")
    return settings, Database(settings), config


def seed_legacy(database):
    """反射旧结构，不让当前ORM列污染0011测试数据。"""
    metadata = MetaData()
    metadata.reflect(bind=database.engine)
    task_id = uuid4().hex
    with database.transaction() as session:
        session.execute(metadata.tables["tasks"].insert().values(
            task_id=task_id, caller_id="local-user", task_type="asr", model_id="fixture-model", model_version="fixture-1",
            parameters={"language": None}, input_ref=uuid4().hex, input_filename="fixture.wav", input_sha256="a" * 64,
            input_size_bytes=100, audio_duration_seconds=0.5, queue_key="asr.remote-v2-sensevoice", status="failed",
            created_at=NOW, updated_at=NOW))
        session.execute(metadata.tables["remote_asr_calls"].insert().values(
            task_id=task_id, profile_id="remote-v2-sensevoice", config_fingerprint="b" * 64,
            request_id=task_id, state="uncertain", created_at=NOW, worker_id="c" * 32, lease_token="d" * 32,
            reserved_at=NOW, ended_at=NOW))
        session.execute(metadata.tables["asr_capacity_groups"].insert().values(group_id="legacy-group", capacity_limit=1, created_at=NOW))
        session.execute(metadata.tables["asr_compute_targets"].insert().values(
            target_id="legacy-target", capacity_group_id="legacy-group", enabled=True, target_limit=1, created_at=NOW))
        session.execute(metadata.tables["asr_target_revisions"].insert().values(
            revision_id="e" * 64, target_id="legacy-target", config_fingerprint="b" * 64,
            endpoint_identity="f" * 64, config_snapshot={"history": "keep"}, device_kind="unknown", created_at=NOW))
        session.execute(metadata.tables["asr_target_worker_sessions"].insert().values(
            session_id="c" * 32, revision_id="e" * 64, worker_key="legacy-key", slot_index=0,
            active_slot_key=None, pid=123, state="stopped", model_ready=False,
            heartbeat_at=NOW, started_at=NOW, ended_at=NOW))
        session.execute(metadata.tables["asr_call_permits"].insert().values(
            task_id=task_id, revision_id="e" * 64, capacity_group_id="legacy-group",
            worker_session_id="c" * 32, lease_token="d" * 32, state="uncertain", created_at=NOW, ended_at=NOW))
    return task_id


def snapshot(database):
    with database.engine.connect() as connection:
        names = sorted(set(inspect(connection).get_table_names()) - {"alembic_version"})
        columns = {name: [item["name"] for item in inspect(connection).get_columns(name)] for name in names}
        rows = {name: connection.execute(text(f'SELECT * FROM "{name}" ORDER BY rowid')).all() for name in names}
        return columns, rows


def assert_old_rows(database, before):
    columns, rows = before
    with database.engine.connect() as connection:
        for name, fields in columns.items():
            projection = ",".join(f'"{field}"' for field in fields)
            assert connection.execute(text(f'SELECT {projection} FROM "{name}" ORDER BY rowid')).all() == rows[name]
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"


def test_0012_preserves_old_rows_and_changes_only_declared_index_and_columns(tmp_path):
    settings, database, config = setup(tmp_path)
    try:
        seed_legacy(database)
        before = snapshot(database)
        command.upgrade(config, "20261019_0012")
        command.upgrade(config, "20261019_0012")
        # 本段验证历史 0012；最新 ORM 的完整结构由末尾 head/check 核对。
        assert_old_rows(database, before)
        inspector = inspect(database.engine)
        assert {row["name"] for row in inspector.get_indexes("remote_asr_calls")} == {"ix_remote_asr_calls_profile_state"}
        assert not inspector.get_indexes("remote_asr_calls")[0]["unique"]
        fields = {item["name"] for item in inspector.get_columns("asr_target_worker_sessions")}
        assert fields - set(before[0]["asr_target_worker_sessions"]) == {"active_task_id", "child_pid", "last_error"}
        with database.engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM asr_capacity_changes")) == 0
            assert connection.execute(text("SELECT active_task_id,child_pid,last_error FROM asr_target_worker_sessions")).one() == (None, None, None)
        command.downgrade(config, "20261019_0011")
        assert snapshot(database) == before
        assert {row["name"] for row in inspect(database.engine).get_indexes("remote_asr_calls")} == {"uq_remote_asr_calls_active_profile"}
        command.upgrade(config, "head")
        command.check(config)
        assert_old_rows(database, before)
    finally:
        database.dispose()


def test_0012_audit_and_slot_report_prevent_lossy_downgrade(tmp_path):
    settings, database, config = setup(tmp_path)
    try:
        command.upgrade(config, "20261019_0012")
        target = TargetAdmissionService(database, settings)
        target.register_current()
        target.configure_capacity({"target_limit": 2, "group_limit": 2, "evidence": "fixture-only independent capacity evidence"})
        before = snapshot(database)
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "20261019_0011")
        assert snapshot(database) == before
    finally:
        database.dispose()


def test_0012_downgrade_rejects_multiple_old_profile_calls_even_without_audit(tmp_path):
    settings, database, config = setup(tmp_path)
    try:
        original = seed_legacy(database)
        command.upgrade(config, "20261019_0012")
        metadata = MetaData()
        metadata.reflect(bind=database.engine)
        with database.transaction() as session:
            old = dict(session.execute(metadata.tables["tasks"].select().where(
                metadata.tables["tasks"].c.task_id == original)).mappings().one())
            old["task_id"] = uuid4().hex
            old["input_ref"] = uuid4().hex
            session.execute(metadata.tables["tasks"].insert().values(**old))
            session.execute(metadata.tables["remote_asr_calls"].insert().values(
                task_id=old["task_id"], profile_id="remote-v2-sensevoice", config_fingerprint="b" * 64,
                request_id=old["task_id"], state="held", created_at=NOW))
        before = snapshot(database)
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "20261019_0011")
        assert snapshot(database) == before
    finally:
        database.dispose()
