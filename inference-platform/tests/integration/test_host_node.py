"""独立 Node 4A 隔离验收：状态事实、并发、恢复和权限边界。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import re
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pydantic import SecretStr
import pytest
from sqlalchemy import create_engine, event, func, inspect, select, text

from backend.common.exceptions.app_error import AppError, StartupError
from backend.data.database.base import Base
from backend.data.host_node.node_database import NodeDatabase
from backend.data.host_node.node_models import HostNodeBase, HostNodeEvent, HostNodeIdentity, HostNodeInstance, HostNodeService
from backend.data.host_node.node_repository import NodeRepository
from backend.modules.host_node.app import close_node_app, create_node_app
from backend.service.host_node.migration_service import upgrade_node_database
from tests.integration.host_node_support import (
    ADMIN, OBSERVER, REPORT, VISION_REPORT, auth, bind, config, data, node_case, node_settings, register, report_body, send,
)


@pytest.fixture
def case(tmp_path):
    value = node_case(tmp_path)
    yield value
    close_node_app(value.app)


def event_count(case):
    with case.service.database.transaction() as session:
        return session.scalar(select(func.count()).select_from(HostNodeEvent))


@pytest.mark.parametrize("kind", ["asr", "vision", "llm"])
def test_three_service_kinds_have_independent_records_and_no_fabricated_capabilities(case, kind):
    entry = register(case, kind, kind=kind)
    assert entry["kind"] == kind and entry["state"] == "unknown"
    assert entry["containerState"] == "unknown" and entry["actualExecutionDevice"] is None
    assert entry["progress"] is None and entry["executionReady"] is False
    assert entry["issues"] == ["INSTANCE_NOT_BOUND"]
    assert entry["capabilities"]["businessProxy"] is False


def test_registration_records_real_capability_gap(case):
    definition = config()
    definition.update(reportProtocol="none", reportTokenEnv=None)
    entry = register(case, definition=definition)
    assert entry["issues"] == ["LIFECYCLE_REPORT_UNSUPPORTED"]
    assert case.client.post("/node/v1/admin/services/asr/instances", headers=auth(), json={
        "instanceId": str(uuid4()), "expectedGeneration": 0, "configRevision": entry["configRevision"]}).status_code == 409


@pytest.mark.parametrize("token,method,path", [
    (None, "get", "/node/v1/status"), (REPORT, "get", "/node/v1/services"),
    (OBSERVER, "put", "/node/v1/admin/services/asr"),
    (OBSERVER, "post", "/node/v1/admin/services/asr/instances"),
    (ADMIN, "post", "/node/internal/v1/services/asr/reports"),
    (VISION_REPORT, "post", "/node/internal/v1/services/asr/reports"),
])
def test_authentication_precedes_body_parsing_and_roles_do_not_cross(case, token, method, path):
    register(case)
    before = event_count(case)
    response = getattr(case.client, method)(path, headers=auth(token) if token else {}, data="invalid-json", content_type="application/json")
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "NODE_UNAUTHORIZED"
    assert event_count(case) == before


def test_readonly_observer_can_read_but_configuration_is_admin_only(case):
    register(case)
    for path in ("/node/v1/status", "/node/v1/services", "/node/v1/services/asr", "/node/v1/events", "/node/v1/openapi.json"):
        assert case.client.get(path, headers=auth(OBSERVER)).status_code == 200
    assert case.client.get("/node/v1/admin/services/asr", headers=auth(OBSERVER)).status_code == 401


def test_config_revisions_are_immutable_versioned_and_not_shared_by_credentials(case):
    entry = register(case)
    duplicate = register(case, expected=1)
    assert duplicate["version"] == 1
    assert case.client.put("/node/v1/admin/services/asr", headers=auth(), json={"config": config()}).status_code == 409
    changed = config(); changed["name"] = "新的资料"
    entry = register(case, definition=changed, expected=1)
    assert entry["version"] == 2
    assert case.client.put("/node/v1/admin/services/alias", headers=auth(), json={"config": config()}).status_code == 409
    with case.service.database.transaction() as session:
        assert session.scalar(text("SELECT COUNT(*) FROM host_node_service_revisions")) == 2


def test_missing_report_credential_fails_registration_without_partial_rows(case):
    definition = config(); definition["reportTokenEnv"] = "NODE_REPORT_TOKEN_MISSING"
    response = case.client.put("/node/v1/admin/services/asr", headers=auth(), json={"config": definition})
    assert response.status_code == 409
    assert data(case.client.get("/node/v1/services", headers=auth()))["total"] == 0


def test_simulated_asr_lifecycle_keeps_online_ready_and_accepting_distinct(case):
    entry = bind(case, register(case))
    for sequence, state in enumerate(("starting", "warming", "ready", "busy", "draining", "stopped"), 1):
        entry = data(send(case, entry, sequence, state))["service"]
        assert entry["state"] == state and entry["reportFresh"] is True
        assert entry["acceptingRequests"] == (state in {"ready", "busy"})
        assert entry["containerState"] == "unknown" and entry["executionReady"] is False
    assert data(case.client.get("/node/v1/status", headers=auth()))["status"] == "online"


def test_duplicate_report_does_not_refresh_heartbeat_or_write_an_event(case):
    entry = bind(case, register(case))
    first = data(send(case, entry))
    before = event_count(case)
    case.clock.advance(15)
    duplicate = data(send(case, entry))
    assert duplicate["duplicate"] is True and duplicate["service"]["reportFresh"] is False
    assert duplicate["service"]["lastReceivedAt"] == first["service"]["lastReceivedAt"]
    assert event_count(case) == before


def test_old_sequence_and_same_sequence_different_content_cannot_override(case):
    entry = bind(case, register(case))
    data(send(case, entry, 3))
    before = event_count(case)
    assert send(case, entry, 2, "failed").status_code == 409
    assert send(case, entry, 3, "failed").status_code == 409
    current = data(case.client.get("/node/v1/services/asr", headers=auth()))
    assert current["state"] == "ready" and current["sequence"] == 3
    assert event_count(case) == before


def test_heartbeat_updates_snapshot_without_repeating_state_event(case):
    entry = bind(case, register(case))
    data(send(case, entry))
    before = event_count(case)
    case.clock.advance(5)
    latest = data(send(case, entry, 2, queuedTasks=3))["service"]
    assert latest["sequence"] == 2 and latest["queuedTasks"] == 3
    assert event_count(case) == before


@pytest.mark.parametrize("age", [15, 40, -5])
def test_expired_or_backward_clock_snapshot_is_unknown_not_stopped(case, age):
    entry = bind(case, register(case))
    data(send(case, entry))
    case.clock.advance(age)
    before = event_count(case)
    for _ in range(3):
        snapshot = data(case.client.get("/node/v1/services/asr", headers=auth()))
        assert snapshot["state"] == "unknown" and snapshot["reportedState"] == "ready"
        assert snapshot["acceptingRequests"] is False and snapshot["lastReceivedAt"]
    assert event_count(case) == before


def test_service_clock_does_not_keep_a_report_fresh(case):
    entry = bind(case, register(case))
    data(send(case, entry, reportedAt=(case.clock() + timedelta(days=365)).isoformat()))
    case.clock.advance(16)
    assert data(case.client.get("/node/v1/services/asr", headers=auth()))["reportFresh"] is False


def test_node_restart_requires_new_report_without_rebinding_or_starting_business(case):
    entry = bind(case, register(case))
    data(send(case, entry))
    epoch = case.service.epoch_id
    close_node_app(case.app)
    case.app = create_node_app(case.settings, clock=case.clock)
    case.client = case.app.test_client(); case.service = case.app.extensions["host_node_service"]
    current = data(case.client.get("/node/v1/services/asr", headers=auth()))
    assert case.service.epoch_id != epoch and current["instanceId"] == entry["instanceId"]
    assert current["issues"] == ["REPORT_AFTER_NODE_RESTART_REQUIRED"]
    assert data(send(case, entry))["service"]["reportFresh"] is False
    assert data(send(case, entry, 2))["service"]["state"] == "ready"


def test_closed_node_cannot_accept_reports_or_mutations_through_an_old_object(case):
    entry = bind(case, register(case))
    previous = case.service
    close_node_app(case.app)
    case.app = create_node_app(case.settings, clock=case.clock)
    case.client = case.app.test_client(); case.service = case.app.extensions["host_node_service"]
    with pytest.raises(AppError, match="控制会话"):
        previous.report("asr", report_body(entry))
    assert case.service.get_service("asr")["sequence"] == 0


def test_credential_rotation_requires_new_process_secret_and_keeps_service_identity(case):
    entry = bind(case, register(case))
    data(send(case, entry))
    close_node_app(case.app)
    rotated = "TEST_ONLY_ROTATED_" + "x" * 40
    settings = case.settings.model_copy(update={"report_tokens": {**case.settings.report_tokens,
        "NODE_REPORT_TOKEN_ASR": SecretStr(rotated)}})
    case.app = create_node_app(settings, clock=case.clock)
    case.client = case.app.test_client(); case.service = case.app.extensions["host_node_service"]
    assert send(case, entry, 2).status_code == 401
    current = data(send(case, entry, 2, token=rotated))["service"]
    assert current["instanceId"] == entry["instanceId"] and current["reportFresh"]


def test_single_node_process_and_identity_are_enforced(case):
    before = event_count(case)
    with pytest.raises(StartupError, match="NODE_ALREADY_RUNNING"):
        create_node_app(case.settings)
    with pytest.raises(StartupError, match="NODE_ALREADY_RUNNING"):
        upgrade_node_database(case.settings)
    assert event_count(case) == before
    close_node_app(case.app)
    wrong = case.settings.model_copy(update={"node_id": "another-node"})
    with pytest.raises(StartupError, match="NODE_ID_MISMATCH"):
        create_node_app(wrong)
    case.app = create_node_app(case.settings, clock=case.clock)
    case.client = case.app.test_client(); case.service = case.app.extensions["host_node_service"]


def test_unconfirmed_instance_blocks_config_and_rebinding_even_if_stale(case):
    entry = bind(case, register(case))
    data(send(case, entry))
    case.clock.advance(100)
    changed = config(); changed["name"] = "禁止在途修改"
    assert case.client.put("/node/v1/admin/services/asr", headers=auth(), json={"config": changed, "expectedVersion": 1}).status_code == 409
    assert case.client.post("/node/v1/admin/services/asr/instances", headers=auth(), json={
        "instanceId": str(uuid4()), "expectedGeneration": 1, "configRevision": entry["configRevision"]}).status_code == 409


def test_stopped_instance_cannot_revive_and_old_instance_reports_are_fenced(case):
    entry = bind(case, register(case))
    data(send(case, entry, 1, "stopped"))
    assert send(case, entry, 2).status_code == 409
    newer = bind(case, {**entry, "generation": 1})
    assert send(case, entry, 3, "stopped").status_code == 409
    assert data(send(case, newer))["service"]["state"] == "ready"
    assert send(case, newer, 2, configRevision="0" * 64).status_code == 409


def test_duplicate_instance_binding_is_idempotent_and_stale_generation_is_rejected(case):
    saved = register(case)
    instance_id = str(uuid4())
    entry = bind(case, saved, instance_id=instance_id)
    before = event_count(case)
    assert bind(case, saved, instance_id=instance_id)["instanceId"] == instance_id
    assert event_count(case) == before
    assert case.client.post("/node/v1/admin/services/asr/instances", headers=auth(), json={
        "instanceId": str(uuid4()), "expectedGeneration": 0, "configRevision": entry["configRevision"]}).status_code == 409


def test_concurrent_identical_reports_persist_one_transition(case):
    entry = bind(case, register(case))
    before = event_count(case)
    command = report_body(entry)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: case.service.report("asr", command), range(12)))
    assert sum(not item["duplicate"] for item in results) == 1
    assert event_count(case) == before + 1


def test_event_and_snapshot_are_rolled_back_together(case, monkeypatch):
    entry = bind(case, register(case))
    before = event_count(case)
    def broken(*_args, **_kwargs):
        raise RuntimeError("isolated event persistence failure")
    monkeypatch.setattr(NodeRepository, "event", broken)
    with pytest.raises(RuntimeError):
        case.service.report("asr", report_body(entry))
    current = case.service.get_service("asr")
    assert current["sequence"] == 0 and current["lastReceivedAt"] is None
    assert event_count(case) == before


@pytest.mark.parametrize("override", [
    {"modelReady": "true"}, {"sequence": True}, {"queuedTasks": -1}, {"activeTasks": "3"},
    {"acceptingRequests": True, "state": "warming"}, {"state": "ready", "health": "unknown"},
    {"reportedAt": "2026-10-21T15:00:00"}, {"reportedAt": 1730000000},
    {"state": "stopped", "modelReady": False, "acceptingRequests": False, "queuedTasks": None},
    {"rawResponse": "PRIVATE_REQUEST_BODY"}, {"errorCode": "secret with spaces"},
])
def test_invalid_reports_are_rejected_without_echoing_input(case, override):
    entry = bind(case, register(case))
    response = send(case, entry, **override)
    assert response.status_code == 422
    assert "PRIVATE_REQUEST_BODY" not in response.get_data(as_text=True)
    assert case.service.get_service("asr")["sequence"] == 0


def test_events_are_paged_readonly_and_capped(case):
    entry = bind(case, register(case))
    data(send(case, entry))
    before = event_count(case)
    first = data(case.client.get("/node/v1/events?limit=2", headers=auth()))
    second = data(case.client.get(f"/node/v1/events?limit=2&afterSequence={first['nextSequence']}", headers=auth()))
    assert first["hasMore"] and not ({x["sequence"] for x in first["items"]} & {x["sequence"] for x in second["items"]})
    assert case.client.get("/node/v1/events?limit=201", headers=auth()).status_code == 422
    assert case.client.get("/node/v1/services?pageSize=101", headers=auth()).status_code == 422
    assert case.client.get("/node/v1/services?page=1&page=2", headers=auth()).status_code == 422
    assert case.client.get("/node/v1/status?refresh=1", headers=auth()).status_code == 422
    assert event_count(case) == before


def test_service_list_uses_a_join_instead_of_one_query_per_item(case):
    for kind in ("asr", "vision", "llm"):
        register(case, kind, kind=kind)
    statements = []
    def observe(_connection, _cursor, statement, *_args):
        statements.append(statement)
    event.listen(case.service.database.engine, "before_cursor_execute", observe)
    try:
        page = data(case.client.get("/node/v1/services?pageSize=2", headers=auth(OBSERVER)))
    finally:
        event.remove(case.service.database.engine, "before_cursor_execute", observe)
    assert page["total"] == 3 and len(page["items"]) == 2
    assert len(statements) == 2


def test_secrets_are_not_stored_in_database_responses_events_or_logs(case):
    entry = bind(case, register(case))
    data(send(case, entry))
    responses = "".join(case.client.get(path, headers=auth()).get_data(as_text=True)
                        for path in ("/node/v1/status", "/node/v1/services", "/node/v1/events", "/node/v1/openapi.json"))
    with case.service.database.engine.connect() as connection:
        contents = "".join(repr(connection.execute(text(f'SELECT * FROM "{name}"')).fetchall())
                           for name in inspect(connection).get_table_names())
    logs = "".join(p.read_text(encoding="utf-8") for p in case.settings.log_dir.glob("*.log"))
    for token in (ADMIN, OBSERVER, REPORT, VISION_REPORT):
        assert token not in responses + contents + logs


def test_openapi_matches_actual_routes_and_is_not_a_pretend_business_gateway(case):
    document = case.client.get("/node/v1/openapi.json", headers=auth()).get_json()
    actual = {(rule.rule.replace("<service_id>", "{serviceId}").replace("<operation_id>", "{operationId}").replace("<invocation_id>", "{invocationId}").replace("<path:path>","{path}"), method.lower())
              for rule in case.app.url_map.iter_rules() if rule.endpoint != "static"
              for method in rule.methods - {"OPTIONS", "HEAD"}}
    described = {(path, method) for path, methods in document["paths"].items() for method in methods}
    assert actual == described and document["openapi"] == "3.1.0"
    assert all(re.fullmatch(r"[a-zA-Z0-9._-]+", name) for name in document["components"]["schemas"])
    def check_references(value):
        if isinstance(value, dict):
            if "$ref" in value:
                assert value["$ref"].removeprefix("#/components/schemas/") in document["components"]["schemas"]
            for item in value.values():
                check_references(item)
        elif isinstance(value, list):
            for item in value:
                check_references(item)
    check_references(document)
    unavailable=case.client.post("/api/v1/tasks/asr", headers=auth())
    assert unavailable.status_code == 503 and unavailable.get_json()["error"]["code"]=="CENTER_UNAVAILABLE"
    assert case.client.post("/node/v1/admin/services/asr/start", headers=auth()).status_code == 404
    assert case.client.post("/node/internal/v1/services/asr/reports", headers=auth(REPORT),
                            data="x" * 16385, content_type="application/json").status_code == 401
    register(case)
    assert case.client.post("/node/internal/v1/services/asr/reports", headers=auth(REPORT),
                            data="x" * 16385, content_type="application/json").status_code == 413


def test_node_migration_has_dedicated_metadata_is_repeatable_and_preserves_records(case):
    register(case)
    before = event_count(case)
    close_node_app(case.app)
    upgrade_node_database(case.settings)
    engine = create_engine("sqlite:///" + case.settings.database_path.as_posix())
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"version_table": "host_node_alembic_versions"})
            assert compare_metadata(context, HostNodeBase.metadata) == []
            assert connection.scalar(text("SELECT COUNT(*) FROM host_node_events")) == before + 1
    finally:
        engine.dispose()
    assert not any(name.startswith("host_node_") for name in Base.metadata.tables)
    case.app = create_node_app(case.settings, clock=case.clock)
    case.client = case.app.test_client(); case.service = case.app.extensions["host_node_service"]
    assert case.service.list_services({})["total"] == 1


def test_uninitialized_start_does_not_create_a_database(tmp_path):
    settings = node_settings(tmp_path)
    with pytest.raises(StartupError, match="NODE_MIGRATION_REQUIRED"):
        create_node_app(settings)
    assert not settings.database_path.exists() and not settings.log_dir.exists()


def test_node_migration_refuses_a_platform_or_unknown_database_without_changing_it(tmp_path):
    settings = node_settings(tmp_path)
    engine = create_engine("sqlite:///" + settings.database_path.as_posix())
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tasks (task_id TEXT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO tasks VALUES ('protected-task')"))
    with pytest.raises(StartupError, match="NODE_DATABASE_NOT_ISOLATED"):
        upgrade_node_database(settings)
    with engine.connect() as connection:
        assert inspect(connection).get_table_names() == ["tasks"]
        assert connection.scalar(text("SELECT task_id FROM tasks")) == "protected-task"
    engine.dispose()
