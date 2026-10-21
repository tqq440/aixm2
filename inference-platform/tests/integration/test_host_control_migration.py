"""0001→0002 保留旧报告身份、指纹、事件和状态，不升级日常平台库。"""

import json
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text

from backend.config.host_node.node_settings import PROJECT_ROOT
from backend.modules.host_node.app import close_node_app, create_node_app
from backend.schema.host_node.node_schema import NodeServiceConfig, NodeServiceReport
from backend.service.host_node.migration_service import upgrade_node_database
from backend.utils.json.json_utils import canonical_json_sha256
from tests.integration.host_node_support import config, node_settings, report_body


def migration(settings):
    result = Config(); result.set_main_option("script_location", str(PROJECT_ROOT / "node-migrations"))
    result.attributes["node_settings"] = settings
    return result


def test_upgrade_preserves_old_report_fingerprint_and_duplicate_semantics(tmp_path):
    settings = node_settings(tmp_path)
    command.upgrade(migration(settings), "20261021_node_0001")
    engine = create_engine("sqlite:///" + settings.database_path.as_posix())
    definition = NodeServiceConfig.model_validate(config()).model_dump(mode="json", by_alias=True)
    revision = canonical_json_sha256(definition); instance_id = str(uuid4()); epoch = str(uuid4())
    entry = {"instanceId": instance_id, "configRevision": revision}
    body = report_body(entry)
    normalized = NodeServiceReport.model_validate(body).model_dump(mode="json", by_alias=True)
    del normalized["controlOperationId"]; del normalized["controlAction"]
    fingerprint = canonical_json_sha256(normalized)
    stamp = "2026-10-21 08:00:00"
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO host_node_identities VALUES (1,:node,:epoch,:stamp,NULL)"),
                           dict(node=settings.node_id, epoch=epoch, stamp=stamp))
        connection.execute(text("INSERT INTO host_node_services VALUES ('asr',:revision,:json,1,1,:instance,:stamp,:stamp)"),
                           dict(revision=revision,json=json.dumps(definition),instance=instance_id,stamp=stamp))
        connection.execute(text("INSERT INTO host_node_instances (instance_id,service_id,config_revision,generation,sequence,report_hash,reported_state,model_ready,accepting_requests,health,created_at,received_at,node_epoch_id) VALUES (:instance,'asr',:revision,1,1,:fingerprint,'ready',1,1,'healthy',:stamp,:stamp,:epoch)"),
                           dict(instance=instance_id,revision=revision,fingerprint=fingerprint,stamp=stamp,epoch=epoch))
        connection.execute(text("INSERT INTO host_node_events (service_id,event_type,source,details,created_at) VALUES ('asr','HISTORICAL_EVENT','service','{}',:stamp)"),dict(stamp=stamp))
    upgrade_node_database(settings); upgrade_node_database(settings)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT report_hash FROM host_node_instances")) == fingerprint
        assert connection.scalar(text("SELECT COUNT(*) FROM host_node_events")) == 1
        assert connection.scalar(text("SELECT version_num FROM host_node_alembic_versions")) == "20261021_node_0004"
    app = create_node_app(settings)
    try:
        result = app.extensions["host_node_service"].report("asr", body)
        assert result["duplicate"] is True and not result["service"]["reportFresh"]
        assert result["service"]["instanceId"] == instance_id
        assert result["service"]["lastReceivedAt"].startswith("2026-10-21T08:00:00")
    finally:
        close_node_app(app);engine.dispose()


def test_nonempty_resource_history_blocks_downgrade_without_erasing_it(tmp_path):
    settings = node_settings(tmp_path); upgrade_node_database(settings)
    engine = create_engine("sqlite:///" + settings.database_path.as_posix())
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO host_node_resource_snapshots VALUES (1,'test-epoch','{}','2026-10-21 08:00:00')"))
    with pytest.raises(RuntimeError, match="拒绝降级"):
        command.downgrade(migration(settings), "20261021_node_0001")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM host_node_resource_snapshots")) == 1
        assert connection.scalar(text("SELECT version_num FROM host_node_alembic_versions")) == "20261021_node_0002"
    engine.dispose()
