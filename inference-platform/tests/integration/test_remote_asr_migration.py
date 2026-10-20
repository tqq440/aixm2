"""0010 迁移、旧数据及外部调用占位约束；仅临时 SQLite。"""

from datetime import datetime
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.database.database import Database
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.data.tasks.task_models import TaskRecord


def setup_database(tmp_path, revision):
    settings = AppSettings(database_path=tmp_path / "migration.sqlite3", log_dir=tmp_path / "logs")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, revision)
    return Database(settings), config


def add_task(database):
    task_id = uuid4().hex
    with database.transaction() as session:
        session.add(TaskRecord(task_id=task_id, caller_id="local-user", task_type="asr",
            model_id="whisper-small", model_version="536b0662742c02347bc0e980a01041f333bce120",
            parameters={"language": "zh"}, input_ref=uuid4().hex, input_filename="原始.wav",
            input_sha256="a" * 64, input_size_bytes=100, audio_duration_seconds=0.1,
            queue_key="asr.whisper-small", status="succeeded", result_json={"text": "保留原有结果"},
            created_at=datetime(2026, 10, 14), updated_at=datetime(2026, 10, 14)))
    return task_id


def snapshot(database, names):
    with database.engine.connect() as connection:
        return {name: connection.execute(text(f'SELECT * FROM "{name}" ORDER BY rowid')).all() for name in names}


def test_upgrade_preserves_every_old_row_no_model_seed_and_empty_downgrade(tmp_path):
    database, config = setup_database(tmp_path, "20261014_0009")
    try:
        task_id = add_task(database)
        RuntimeRepository(database).report(component_id="worker", session_id="a" * 32, pid=123,
                                           state="stopped", model_ready=False)
        old_tables = sorted(set(inspect(database.engine).get_table_names()) - {"alembic_version"})
        before = snapshot(database, old_tables)
        command.upgrade(config, "20261015_0010")
        command.upgrade(config, "20261015_0010")
        assert snapshot(database, old_tables) == before
        assert set(inspect(database.engine).get_table_names()) - set(old_tables) == {"alembic_version", "remote_asr_calls"}
        with database.engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM remote_asr_calls")).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM runtime_components WHERE component_id='remote_asr_worker'")).scalar_one() == 0
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        command.downgrade(config, "20261014_0009")
        assert snapshot(database, old_tables) == before
        assert "remote_asr_calls" not in inspect(database.engine).get_table_names()
        command.upgrade(config, "head")
        command.check(config)
        with database.session_factory() as session:
            assert session.get(TaskRecord, task_id).result_json == {"text": "保留原有结果"}
    finally:
        database.dispose()


@pytest.mark.parametrize("kind", ["call", "runtime"])
def test_downgrade_refuses_losing_any_external_call_or_runtime_history(tmp_path, kind):
    database, config = setup_database(tmp_path, "20261015_0010")
    try:
        if kind == "call":
            task_id = add_task(database)
            with database.transaction() as session:
                session.add(RemoteAsrCall(task_id=task_id, profile_id="remote-v2-sensevoice",
                    config_fingerprint="b" * 64, request_id=task_id, state="released", created_at=datetime.utcnow()))
        else:
            RuntimeRepository(database).report(component_id="remote_asr_worker", session_id="c" * 32,
                                               pid=456, state="stopped")
        names = sorted(set(inspect(database.engine).get_table_names()) - {"alembic_version"})
        before = snapshot(database, names)
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "20261014_0009")
        assert snapshot(database, names) == before
        with database.engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20261015_0010"
    finally:
        database.dispose()


def test_single_active_slot_state_constraint_foreign_key_and_to_dict(tmp_path):
    # 这是0010的历史单槽契约；0012已由共享容量准入替代此唯一索引。
    database, config = setup_database(tmp_path, "20261015_0010")
    try:
        first, second = add_task(database), add_task(database)
        def add(task_id, state):
            with database.transaction() as session:
                row = RemoteAsrCall(task_id=task_id, profile_id="remote-v2-sensevoice",
                    config_fingerprint="a" * 64, request_id=task_id, state=state, created_at=datetime.utcnow())
                session.add(row)
                session.flush()
                assert row.to_dict()["task_id"] == task_id
        add(first, "held")
        with pytest.raises(IntegrityError):
            add(second, "uncertain")
        add(second, "queued")
        with database.transaction() as session:
            session.get(RemoteAsrCall, first).state = "released"
            session.flush()
            session.get(RemoteAsrCall, second).state = "uncertain"
        with pytest.raises(IntegrityError):
            add(uuid4().hex, "released")
        third = add_task(database)
        with pytest.raises(IntegrityError):
            add(third, "invalid")
    finally:
        database.dispose()
