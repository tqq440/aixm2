"""旧执行档案跨监控迁移字节保持；不触碰日常库。"""

from alembic import command
from sqlalchemy import text

from tests.integration.test_common_execution import case
from tests.integration.test_node_execution_bridge import linked,lease_for
from tests.integration.test_node_execution_migrations import node_migration
from backend.modules.host_node.app import close_node_app,create_node_app


def round_trip(engine,migration,table,previous):
    with engine.begin() as connection:
        old=dict(connection.execute(text(f'SELECT * FROM "{table}"')).mappings().one())
        old.pop("monitoring_json")
        connection.execute(text(f'DELETE FROM "{table}"'))
    # Downgrade only an empty execution table; no unknown history is discarded.
    command.downgrade(migration,previous)
    with engine.begin() as connection:
        names=",".join(f'"{name}"' for name in old)
        parameters=",".join(":"+name for name in old)
        connection.execute(text(f'INSERT INTO "{table}" ({names}) VALUES ({parameters})'),old)
    command.upgrade(migration,"head");command.upgrade(migration,"head")
    with engine.connect() as connection:
        actual=dict(connection.execute(text(f'SELECT * FROM "{table}"')).mappings().one())
    assert actual.pop("monitoring_json")=="{}"
    assert actual==old


def test_node_0003_result_and_identity_survive_monitoring_upgrade(linked):
    _,lease=lease_for(linked);assert linked.bridge.dispatch(lease)
    close_node_app(linked.node.app)
    try:
        round_trip(linked.node.service.database.engine,node_migration(linked.node.settings),
                   "host_node_executions","20261021_node_0003")
    finally:linked.node.app=create_node_app(linked.node.settings,execution_adapter=linked.adapter)
    assert linked.node.app.extensions["host_node_executions"].get(lease.invocation_id)["state"]=="succeeded"


def test_center_0018_dispatch_and_results_survive_monitoring_upgrade(linked):
    _,lease=lease_for(linked);assert linked.bridge.dispatch(lease)
    # Simulate an 0018 file using SQLite's lossless column removal; the actual
    # forward Alembic migration must retain the complete old row and result.
    engine=linked.case.db.engine
    # This fixture manually reconstructs an 0018 file; remove the newer empty
    # budget ledger through its migration before stamping the historical file.
    command.downgrade(linked.case.migration,"20261021_0020")
    with engine.begin() as connection:
        old=dict(connection.execute(text("SELECT * FROM inference_node_dispatches")).mappings().one())
        old.pop("monitoring_json")
        connection.execute(text("ALTER TABLE inference_node_dispatches DROP COLUMN monitoring_json"))
        connection.execute(text("DROP TABLE gateway_api_keys"))
        connection.execute(text("DROP TABLE gateway_node_observations"))
        connection.execute(text("UPDATE alembic_version SET version_num='20261021_0018'"))
    result=linked.case.core.get(lease.invocation_id)
    command.upgrade(linked.case.migration,"head");command.upgrade(linked.case.migration,"head")
    with engine.connect() as connection:
        actual=dict(connection.execute(text("SELECT * FROM inference_node_dispatches")).mappings().one())
    assert actual.pop("monitoring_json")=="{}"
    assert actual==old and linked.case.core.get(lease.invocation_id)==result
