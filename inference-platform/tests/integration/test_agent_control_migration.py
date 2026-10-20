"""受管记录迁移和身份隔离测试；不构成真实 Agent 启停证据。"""

from datetime import datetime, timedelta
from io import StringIO
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.agent.agent_models import AgentControl, ManagedInstanceSession
from backend.data.agent.agent_repository import AgentRepository
from backend.data.database.base import Base
from backend.data.database.database import Database
from backend.data.nodes.node_models import ComputeNode, InstanceSession, ServiceInstance


NEW_TABLES = {"agent_controls", "managed_instance_sessions"}
NOW = datetime(2026, 10, 14, 4, 5, 6)
INSTANCE_ID = "c" * 32
AGENT_ID = "d" * 32
WORKER_ID = "e" * 32


def make_config(settings, *, output_buffer=None):
    config = Config(str(PROJECT_ROOT / "alembic.ini"), output_buffer=output_buffer)
    config.attributes["settings"] = settings
    return config


def seed_registration(database, settings):
    with database.transaction() as session:
        session.add(ComputeNode(node_id="a" * 32, local_identity="b" * 64, hostname="test-only",
                                os_name="Windows", os_version="test", architecture="AMD64",
                                cpu_count=4, registered_at=NOW, updated_at=NOW))
        session.flush()
        session.add(ServiceInstance(instance_id=INSTANCE_ID, node_id="a" * 32,
                                    instance_key="asr-default", model_id="whisper-small",
                                    model_version=settings.asr_model_revision, adapter_key="faster-whisper",
                                    execution_device="cpu", registered_at=NOW))
        session.flush()
        session.add(InstanceSession(session_id="f" * 32, instance_id=INSTANCE_ID,
                                    source="legacy_worker_observation", worker_session_id="1" * 32,
                                    worker_pid=111, child_pid=222, reported_state="stopped",
                                    reported_model_ready=False, heartbeat_at=NOW,
                                    worker_started_at=NOW, observed_at=NOW))


def control_row(**overrides):
    values = dict(instance_id=INSTANCE_ID, agent_session_id=AGENT_ID, agent_pid=333,
                  agent_started_at=NOW, heartbeat_at=NOW, desired_state="running", state="ready",
                  command_revision=4, worker_session_id=WORKER_ID, child_pid=444,
                  model_ready=True, updated_at=NOW, shutdown_confirmed_at=None)
    values.update(overrides)
    return AgentControl(**values)


def managed_row(**overrides):
    values = dict(worker_session_id=WORKER_ID, instance_id=INSTANCE_ID, agent_session_id=AGENT_ID,
                  agent_pid=333, child_pid=444, state="ready", started_at=NOW,
                  ended_at=None, exit_confirmed_at=None)
    values.update(overrides)
    return ManagedInstanceSession(**values)


def snapshot(database, tables):
    with database.engine.connect() as connection:
        return {
            name: (connection.execute(text("SELECT sql FROM sqlite_master WHERE name=:name"), {"name": name}).scalar_one(),
                   connection.execute(text(f'SELECT * FROM "{name}" ORDER BY rowid')).all())
            for name in tables
        }


@pytest.fixture
def agent_database(tmp_path):
    settings = AppSettings(database_path=tmp_path / "agent.sqlite3", log_dir=tmp_path / "logs")
    config = make_config(settings)
    command.upgrade(config, "head")
    database = Database(settings)
    seed_registration(database, settings)
    try:
        yield database, config
    finally:
        database.dispose()


def test_0009_additive_upgrade_preserves_all_old_tables_and_creates_no_runtime_rows(tmp_path):
    settings = AppSettings(database_path=tmp_path / "old.sqlite3")
    config = make_config(settings)
    command.upgrade(config, "20261014_0008")
    database = Database(settings)
    try:
        seed_registration(database, settings)
        old_tables = sorted(set(inspect(database.engine).get_table_names()) - {"alembic_version"})
        before = snapshot(database, old_tables)
        command.upgrade(config, "20261014_0009")
        command.upgrade(config, "20261014_0009")
        assert snapshot(database, old_tables) == before
        assert set(inspect(database.engine).get_table_names()) - set(old_tables) == NEW_TABLES | {"alembic_version"}
        with database.engine.connect() as connection:
            for name in NEW_TABLES:
                assert connection.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar_one() == 0
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        command.downgrade(config, "20261014_0008")
        assert snapshot(database, old_tables) == before
        command.upgrade(config, "head")
        command.check(config)
        current = snapshot(database, old_tables)
        # 0010 明确扩展 runtime 的组件约束；0009 的无改表保证已在上方独立验证。
        assert {name: value for name, value in current.items() if name != "runtime_components"} == {
            name: value for name, value in before.items() if name != "runtime_components"}
        assert current["runtime_components"][1] == before["runtime_components"][1]
        assert "remote_asr_worker" in current["runtime_components"][0]
    finally:
        database.dispose()


def test_0009_metadata_constraints_indexes_and_to_dict(agent_database):
    database, config = agent_database
    command.check(config)
    inspector = inspect(database.engine)
    for name in NEW_TABLES:
        table = Base.metadata.tables[name]
        assert {c["name"] for c in inspector.get_columns(name)} == set(table.columns.keys())
        assert {c["name"] for c in inspector.get_check_constraints(name)} == {
            c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"}
        assert {c["name"] for c in inspector.get_foreign_keys(name)} == {
            c.name for c in table.constraints if c.__class__.__name__ == "ForeignKeyConstraint"}
        assert {c["name"] for c in inspector.get_indexes(name)} == {i.name for i in table.indexes}
    assert control_row().to_dict()["agent_session_id"] == AGENT_ID
    assert managed_row().to_dict()["worker_session_id"] == WORKER_ID


@pytest.mark.parametrize("kind,field,value", [
    ("control", "instance_id", "0" * 32), ("control", "agent_session_id", "short"),
    ("control", "agent_pid", 0), ("control", "desired_state", "arbitrary-command"),
    ("control", "state", "unknown-in-storage"), ("control", "command_revision", -1),
    ("control", "worker_session_id", "short"), ("control", "child_pid", -1),
    ("session", "instance_id", "0" * 32), ("session", "worker_session_id", "short"),
    ("session", "agent_session_id", "short"), ("session", "agent_pid", 0),
    ("session", "child_pid", -1), ("session", "state", "unknown-in-storage"),
])
def test_0009_rejects_invalid_records(agent_database, kind, field, value):
    database, _ = agent_database
    row = control_row() if kind == "control" else managed_row()
    setattr(row, field, value)
    with pytest.raises(IntegrityError), database.transaction() as session:
        session.add(row)


@pytest.mark.parametrize("kind", ["control", "session"])
def test_0009_unique_control_and_worker_identity(agent_database, kind):
    database, _ = agent_database
    factory = control_row if kind == "control" else managed_row
    with database.transaction() as session:
        session.add(factory())
    with pytest.raises(IntegrityError), database.transaction() as session:
        session.add(factory())


@pytest.mark.parametrize("unconfirmed", ["agent", "worker"])
def test_0009_downgrade_refuses_unconfirmed_exit_even_if_state_says_stopped(agent_database, unconfirmed):
    database, config = agent_database
    with database.transaction() as session:
        session.add(control_row(state="stopped", desired_state="stopped", model_ready=False,
                                shutdown_confirmed_at=None if unconfirmed == "agent" else NOW))
        session.add(managed_row(state="stopped", ended_at=NOW,
                                exit_confirmed_at=None if unconfirmed == "worker" else NOW))
    before = snapshot(database, NEW_TABLES)
    with pytest.raises(RuntimeError, match="尚未确认退出"):
        command.downgrade(config, "20261014_0008")
    assert snapshot(database, NEW_TABLES) == before
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20261014_0009"


def test_0009_downgrade_after_confirmations_keeps_legacy_history(agent_database):
    database, config = agent_database
    command.downgrade(config, "20261014_0009")
    old_tables = sorted(set(inspect(database.engine).get_table_names()) - NEW_TABLES - {"alembic_version"})
    before = snapshot(database, old_tables)
    with database.transaction() as session:
        session.add(control_row(state="stopped", desired_state="stopped", model_ready=False, shutdown_confirmed_at=NOW))
        session.add(managed_row(state="stopped", ended_at=NOW, exit_confirmed_at=NOW))
    command.downgrade(config, "20261014_0008")
    assert not NEW_TABLES & set(inspect(database.engine).get_table_names())
    assert snapshot(database, old_tables) == before


def test_0009_offline_upgrade_is_self_contained_and_downgrade_requires_live_check(tmp_path):
    output = StringIO()
    config = make_config(AppSettings(database_path=tmp_path / "unused.sqlite3"), output_buffer=output)
    command.upgrade(config, "20261014_0008:20261014_0009", sql=True)
    sql = output.getvalue()
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version VALUES ('20261014_0008')")
        connection.executescript(sql)
        for name in NEW_TABLES:
            assert connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261014_0009"
    with pytest.raises(RuntimeError, match="不能离线"):
        command.downgrade(config, "20261014_0009:20261014_0008", sql=True)
    assert not (tmp_path / "unused.sqlite3").exists()


def test_reports_cannot_resurrect_old_agent_worker_or_command_revision(agent_database):
    database, _ = agent_database
    repo = AgentRepository()
    with database.transaction(immediate=True) as session:
        repo.add_control(session, control_row())
    for agent_id, worker_id, revision in (("0" * 32, WORKER_ID, 4), (AGENT_ID, "0" * 32, 4), (AGENT_ID, WORKER_ID, 3)):
        with database.transaction(immediate=True) as session:
            assert not repo.report_control(session, instance_id=INSTANCE_ID, agent_session_id=agent_id,
                                           expected_worker_session_id=worker_id, expected_revision=revision,
                                           values={"state": "busy", "heartbeat_at": NOW + timedelta(seconds=1)})
    with database.transaction(immediate=True) as session:
        row = repo.get_local_control(session)
        repo.update_control(row, desired_state="stopped", command_revision=5, state="stopping")
    with database.transaction(immediate=True) as session:
        assert not repo.report_control(session, instance_id=INSTANCE_ID, agent_session_id=AGENT_ID,
                                       expected_worker_session_id=WORKER_ID, expected_revision=4,
                                       values={"state": "ready"})
        assert repo.report_control(session, instance_id=INSTANCE_ID, agent_session_id=AGENT_ID,
                                   expected_worker_session_id=WORKER_ID, expected_revision=5,
                                   values={"state": "stopping", "heartbeat_at": NOW + timedelta(seconds=2)})
        current = repo.get_control(session, INSTANCE_ID)
        assert current.desired_state == "stopped" and current.command_revision == 5
        assert current.state == "stopping"


@pytest.mark.parametrize("field,value", [("desired_state", "running"), ("command_revision", 99), ("agent_session_id", "0" * 32), ("worker_session_id", "0" * 32)])
def test_report_rejects_overwriting_control_intent_or_identity(agent_database, field, value):
    database, _ = agent_database
    with database.transaction() as session:
        AgentRepository.add_control(session, control_row())
    with pytest.raises(ValueError, match="不能覆盖"), database.transaction(immediate=True) as session:
        AgentRepository.report_control(session, instance_id=INSTANCE_ID, agent_session_id=AGENT_ID,
                                       expected_worker_session_id=WORKER_ID, values={field: value})


def test_managed_history_survives_reopen_and_paginates_without_overwriting_identity(agent_database):
    database, _ = agent_database
    repo = AgentRepository()
    with database.transaction(immediate=True) as session:
        repo.add_control(session, control_row())
        repo.add_managed_session(session, managed_row(state="stopped", ended_at=NOW, exit_confirmed_at=NOW))
        repo.add_managed_session(session, managed_row(worker_session_id="2" * 32, started_at=NOW + timedelta(seconds=1)))
    database.dispose()
    with database.session_factory() as session:
        assert repo.get_local_control(session).instance_id == INSTANCE_ID
        first, total = repo.list_managed_sessions(session, INSTANCE_ID, page=1, page_size=1)
        second, second_total = repo.list_managed_sessions(session, INSTANCE_ID, page=2, page_size=1)
        assert total == second_total == 2
        assert first[0].worker_session_id == "2" * 32
        assert second[0].worker_session_id == WORKER_ID
        assert repo.latest_managed_session(session, INSTANCE_ID).worker_session_id == "2" * 32
        assert repo.get_managed_session(session, WORKER_ID).exit_confirmed_at == NOW
        with pytest.raises(ValueError):
            repo.update_managed_session(first[0], started_at=NOW)
        with pytest.raises(ValueError):
            repo.update_control(repo.get_local_control(session), instance_id="0" * 32)
