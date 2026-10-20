"""真实 SQLite/HTTP 状态叠加；报告为测试替身，不加载模型。"""

from alembic import command
from alembic.config import Config
import pytest

from backend.config.settings.app_settings import PROJECT_ROOT
from backend.data.agent.agent_models import AgentControl, ManagedInstanceSession
from backend.data.database.database import database_now
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.schema.nodes.node_schema import LocalNodeSnapshot


@pytest.fixture
def managed_runtime_api(app_factory):
    app = app_factory()
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = app.config["APP_SETTINGS"]
    command.upgrade(config, "head")
    registry = app.extensions["node_registry_service"]
    registry.probe = lambda: LocalNodeSnapshot(local_identity="a" * 64, hostname="test-only",
                                               os_name="Windows", os_version="test-only",
                                               architecture="AMD64", cpu_count=4)
    registered = registry.register_local()
    database = app.extensions["database"]
    runtime = RuntimeRepository(database)
    runtime.report(component_id="worker", session_id="b" * 32, pid=123, child_pid=456,
                   state="idle", model_ready=True)
    runtime.report(component_id="vision_worker", session_id="c" * 32, pid=789, child_pid=987,
                   state="idle", model_ready=True)
    return app, database, registered.instance.instance_id


def test_registered_instance_without_agent_does_not_expose_old_worker_as_ready(managed_runtime_api):
    app, *_ = managed_runtime_api
    data = app.test_client().get("/api/runtime").get_json()["data"]
    assert not data["worker"]["ready"] and not data["worker"]["modelReady"]
    assert data["worker"]["availability"] == "not_started"
    assert data["visionWorker"]["ready"]


def test_committed_stop_intent_immediately_overrides_idle_worker_without_waiting_heartbeat(managed_runtime_api):
    app, database, instance_id = managed_runtime_api
    with database.transaction(immediate=True) as session:
        now = database_now(session)
        session.add(AgentControl(instance_id=instance_id, agent_session_id="d" * 32,
                                 agent_pid=123, agent_started_at=now, heartbeat_at=now,
                                 desired_state="running", state="ready", command_revision=1,
                                 worker_session_id="b" * 32, child_pid=456, model_ready=True,
                                 updated_at=now))
        session.add(ManagedInstanceSession(worker_session_id="b" * 32, instance_id=instance_id,
                                           agent_session_id="d" * 32, agent_pid=123,
                                           child_pid=456, state="ready", started_at=now))
    client = app.test_client()
    data = client.get("/api/runtime").get_json()["data"]
    assert data["worker"]["ready"] and data["worker"]["modelReady"]
    with database.transaction(immediate=True) as session:
        row = session.get(AgentControl, instance_id)
        row.desired_state, row.state = "stopped", "stopping"
        row.command_revision += 1
    data = client.get("/api/runtime").get_json()["data"]
    assert not data["worker"]["ready"] and data["worker"]["modelReady"]
    assert data["worker"]["reportedState"] == "draining"
    assert data["visionWorker"]["ready"] and data["visionWorker"]["modelReady"]

