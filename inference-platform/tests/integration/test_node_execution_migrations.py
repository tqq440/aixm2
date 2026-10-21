"""4C1 两条迁移链保留旧记录；非空档案拒绝降级。"""

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text

from backend.config.host_node.node_settings import PROJECT_ROOT
from backend.modules.host_node.app import close_node_app, create_node_app
from backend.service.host_node.migration_service import upgrade_node_database
from tests.integration.test_common_execution import case
from tests.integration.test_node_execution_bridge import linked, lease_for


def node_migration(settings):
    migration = Config(); migration.set_main_option("script_location", str(PROJECT_ROOT / "node-migrations"))
    migration.attributes["node_settings"] = settings
    return migration


def rows(connection):
    return {name: sorted(connection.execute(text('SELECT * FROM "' + name + '"')).all(), key=repr)
        for name in inspect(connection).get_table_names() if name not in {"host_node_alembic_versions", "host_node_executions"}}


def test_central_upgrade_keeps_existing_common_task_and_events(case):
    command.downgrade(case.migration, "20261021_0017")
    case.group();case.service();case.budget();task, invocation = case.task()
    before, events = case.core.get(invocation), case.events(task)
    command.upgrade(case.migration, "head");command.upgrade(case.migration, "head")
    assert case.core.get(invocation) == before and case.events(task) == events
    with case.db.engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM inference_node_dispatches")) == 0


def test_node_0002_upgrade_keeps_previous_reports_and_operations(linked):
    close_node_app(linked.node.app)
    migration = node_migration(linked.node.settings)
    command.downgrade(migration, "20261021_node_0002")
    engine = linked.node.service.database.engine
    with engine.connect() as connection: before = rows(connection)
    upgrade_node_database(linked.node.settings);upgrade_node_database(linked.node.settings)
    with engine.connect() as connection:
        assert rows(connection) == before
        assert connection.scalar(text("SELECT COUNT(*) FROM host_node_executions")) == 0
    linked.node.app = create_node_app(linked.node.settings, execution_adapter=linked.adapter)


def test_nonempty_dispatches_cannot_be_downgraded_or_lose_dedupe(linked):
    _, lease = lease_for(linked); assert linked.bridge.dispatch(lease)
    with pytest.raises(RuntimeError, match="拒绝删除"):
        command.downgrade(linked.case.migration, "20261021_0017")
    close_node_app(linked.node.app)
    with pytest.raises(RuntimeError, match="拒绝删除"):
        command.downgrade(node_migration(linked.node.settings), "20261021_node_0002")
    linked.node.app = create_node_app(linked.node.settings, execution_adapter=linked.adapter)
    assert linked.node.app.extensions["host_node_executions"].get(lease.invocation_id)["state"] == "succeeded"
