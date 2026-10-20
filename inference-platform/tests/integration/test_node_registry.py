"""登记、查询与 Worker 报告观测的隔离测试；不启动真实 Agent 或模型。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from threading import Barrier

from alembic import command
from alembic.config import Config
from pydantic import ValidationError
import pytest
from sqlalchemy import inspect, text

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.database.database import Database
from backend.data.models.model_models import ModelVersionRecord
from backend.data.nodes.node_models import ServiceInstance
from backend.data.runtime.runtime_models import RuntimeComponent
from backend.modules.nodes.node_registry import main
from backend.schema.models.model_schema import RegistryListQuery
from backend.schema.nodes.node_schema import InstanceListQuery, LocalNodeSnapshot
from backend.service.models.model_registry_service import ModelRegistryService
from backend.service.nodes.node_registry_service import NodeRegistryService


@pytest.fixture
def registry(app_factory, monkeypatch):
    app = app_factory()
    settings = app.config["APP_SETTINGS"]
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    clock = [datetime(2026, 10, 14, 3, 4, 5, 123456)]
    monkeypatch.setattr("backend.service.nodes.node_registry_service.database_now", lambda session: clock[0])
    snapshot = LocalNodeSnapshot(local_identity="a" * 64, hostname="test-laptop",
                                 os_name="Windows", os_version="test-only", architecture="AMD64",
                                 cpu_count=8)
    service = app.extensions["node_registry_service"]
    service.probe = lambda: snapshot
    return service, app, clock, snapshot


def worker_report(service, clock, **overrides):
    values = dict(component_id="worker", session_id="b" * 32, pid=1234, child_pid=5678,
                  state="idle", model_ready=True, active_task_id=None, heartbeat_at=clock[0],
                  started_at=clock[0], last_error=None)
    values.update(overrides)
    with service.database.transaction(immediate=True) as session:
        current = session.get(RuntimeComponent, values["component_id"])
        if current is None:
            session.add(RuntimeComponent(**values))
        else:
            for name, value in values.items():
                setattr(current, name, value)


def registry_rows(service):
    with service.database.engine.connect() as connection:
        return {table: connection.execute(text(f"SELECT * FROM {table} ORDER BY rowid")).all()
                for table in ("compute_nodes", "service_instances", "instance_sessions", "runtime_components")}


def test_registration_is_configuration_without_runtime_or_fabricated_session(registry):
    service, _, _, _ = registry
    receipt = service.register_local()
    assert receipt.node_created and receipt.instance_created
    assert receipt.node.registration_state == receipt.instance.registration_state == "registered"
    assert receipt.node.agent_status == "not_started" and receipt.node.gpu_status == "not_probed"
    assert receipt.instance.execution_device == "cpu" and receipt.instance.management_mode == "legacy_worker"
    assert receipt.instance.runtime_state == "unknown"
    assert receipt.instance.model_version == service.settings.asr_model_revision
    assert service.get_instance(receipt.instance.instance_id).latest_observation is None
    assert service.list_sessions(receipt.instance.instance_id, RegistryListQuery()).items == []
    observation = service.observe_local()
    assert not observation.recorded and observation.reason == "no_worker_report" and observation.session is None
    assert registry_rows(service)["runtime_components"] == []
    assert "localIdentity" not in receipt.node.model_dump(by_alias=True)


def test_registration_is_idempotent_across_new_database_service_and_metadata_changes(registry):
    service, _, clock, snapshot = registry
    first = service.register_local()
    first_time = first.node.registered_at
    clock[0] += timedelta(seconds=1)
    changed = snapshot.model_copy(update={"hostname": "renamed-laptop", "cpu_count": None})
    database = Database(service.settings)
    try:
        restarted = NodeRegistryService(database, service.settings, probe=lambda: changed)
        second = restarted.register_local()
        assert not second.node_created and not second.instance_created
        assert first.node.node_id == second.node.node_id
        assert first.instance == second.instance
        assert second.node.hostname == "renamed-laptop" and second.node.cpu_count is None
        assert second.node.registered_at == first_time
        assert second.node.metadata_observed_at == clock[0]
        assert restarted.list_nodes(RegistryListQuery()).total == 1
        assert restarted.list_instances(InstanceListQuery()).total == 1
    finally:
        database.dispose()


def test_two_concurrent_registrations_create_one_stable_node_and_instance(registry):
    service, _, _, _ = registry
    barrier = Barrier(2)

    def register():
        barrier.wait(timeout=5)
        return service.register_local()

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: register(), range(2)))
    assert sorted(item.node_created for item in receipts) == [False, True]
    assert sorted(item.instance_created for item in receipts) == [False, True]
    assert len({item.node.node_id for item in receipts}) == len({item.instance.instance_id for item in receipts}) == 1


def test_copied_database_cannot_register_or_observe_as_another_machine(registry):
    service, _, _, snapshot = registry
    service.register_local()
    before = registry_rows(service)
    other = NodeRegistryService(service.database, service.settings,
                                probe=lambda: snapshot.model_copy(update={"local_identity": "c" * 64}))
    for operation, code in ((other.register_local, "LOCAL_NODE_CONFLICT"),
                            (other.observe_local, "LOCAL_NODE_NOT_REGISTERED")):
        with pytest.raises(AppError) as caught:
            operation()
        assert caught.value.code == code and caught.value.status_code == 409
    assert registry_rows(service) == before


def test_disabled_model_and_new_default_never_rebind_registered_instance(registry, monkeypatch):
    service, _, _, _ = registry
    first = service.register_local()
    models = ModelRegistryService(service.database, service.settings)
    original = models.resolve("whisper-small")
    models.disable(original.model_id, original.model_version)
    assert service.register_local().instance == first.instance
    future = original.model_copy(update={"model_version": "test-only-new-default"})
    models.register(future)
    monkeypatch.setattr(models, "execution_supported", lambda spec: True)
    models.enable(future.model_id, future.model_version)
    models.set_default(future.model_id, future.model_version)
    assert models.resolve("whisper-small").model_version == future.model_version
    assert service.register_local().instance == first.instance


def test_instance_binding_conflict_rolls_back_metadata_refresh(registry):
    service, _, clock, _ = registry
    registered = service.register_local()
    with service.database.transaction() as session:
        session.get(ServiceInstance, registered.instance.instance_id).adapter_key = "wrong-test-adapter"
    before = registry_rows(service)
    clock[0] += timedelta(seconds=5)
    with pytest.raises(AppError) as caught:
        service.register_local()
    assert caught.value.code == "INSTANCE_BINDING_CONFLICT" and caught.value.status_code == 409
    assert registry_rows(service) == before


def test_model_definition_mismatch_never_creates_registry_records(registry):
    service, _, _, _ = registry
    with service.database.transaction() as session:
        row = session.get(ModelVersionRecord, ("whisper-small", service.settings.asr_model_revision))
        row.spec_json = {**row.spec_json, "adapterKey": "unsupported-test-adapter"}
    with pytest.raises(AppError) as caught:
        service.register_local()
    assert caught.value.code == "INSTANCE_BINDING_CONFLICT"
    assert service.list_nodes(RegistryListQuery()).total == 0


def test_missing_configured_model_version_never_creates_node(registry):
    service, _, _, _ = registry
    with service.database.transaction() as session:
        row = session.get(ModelVersionRecord, ("whisper-small", service.settings.asr_model_revision))
        session.delete(row)
    with pytest.raises(AppError) as caught:
        service.register_local()
    assert caught.value.code == "MODEL_VERSION_NOT_FOUND" and caught.value.status_code == 404
    assert service.list_nodes(RegistryListQuery()).total == 0


@pytest.mark.parametrize("invalid", [{"cpu_count": 0}, {"hostname": ""}, {"local_identity": "short"}])
def test_invalid_probe_data_returns_controlled_error_without_partial_registration(registry, invalid):
    service, _, _, snapshot = registry
    service.probe = lambda: {**snapshot.model_dump(), **invalid}
    with pytest.raises(AppError) as caught:
        service.register_local()
    assert caught.value.code == "NODE_PROBE_FAILED" and caught.value.status_code == 503
    assert service.list_nodes(RegistryListQuery()).total == 0


def test_probe_io_failure_is_not_a_fake_empty_cpu_node(registry):
    service, _, _, _ = registry

    def unavailable():
        raise OSError("test-only local identity unavailable")

    service.probe = unavailable
    with pytest.raises(AppError) as caught:
        service.register_local()
    assert caught.value.code == "NODE_PROBE_FAILED"
    assert registry_rows(service)["compute_nodes"] == []


def test_observation_requires_registration_and_ignores_other_component_reports(registry):
    service, _, clock, _ = registry
    with pytest.raises(AppError) as caught:
        service.observe_local()
    assert caught.value.code == "LOCAL_NODE_NOT_REGISTERED"
    service.register_local()
    worker_report(service, clock, component_id="vision_worker")
    worker_report(service, clock, component_id="dispatcher", model_ready=False, child_pid=None)
    receipt = service.observe_local()
    assert not receipt.recorded and receipt.reason == "no_worker_report"


def test_worker_report_before_instance_registration_is_not_a_new_startup(registry):
    service, _, clock, _ = registry
    worker_report(service, clock)
    clock[0] += timedelta(seconds=5)
    registration = service.register_local()
    receipt = service.observe_local()
    assert not receipt.recorded and receipt.reason == "pre_registration_report"
    assert service.list_sessions(registration.instance.instance_id, RegistryListQuery()).total == 0


def test_observation_keeps_report_identity_and_busy_is_not_model_unloaded(registry):
    service, _, clock, _ = registry
    registration = service.register_local()
    clock[0] += timedelta(seconds=1)
    worker_report(service, clock, state="busy", active_task_id="d" * 32)
    receipt = service.observe_local()
    assert receipt.recorded and receipt.reason == "recorded"
    observed = receipt.session
    assert observed.source == "legacy_worker_observation" and observed.worker_session_id == "b" * 32
    assert observed.worker_pid == 1234 and observed.child_pid == 5678
    assert observed.reported_state == "busy" and observed.reported_model_ready
    assert observed.active_task_id == "d" * 32 and observed.freshness == "fresh"
    assert not observed.exit_confirmed
    detail = service.get_instance(registration.instance.instance_id)
    assert detail.instance.runtime_state == "unknown" and detail.latest_observation == observed


def test_repeated_and_older_report_does_not_rewrite_saved_observation(registry):
    service, _, clock, _ = registry
    registration = service.register_local()
    clock[0] += timedelta(seconds=2)
    started_at = clock[0]
    worker_report(service, clock)
    first = service.observe_local()
    clock[0] += timedelta(seconds=1)
    repeat = service.observe_local()
    assert not repeat.recorded and repeat.reason == "unchanged"
    assert repeat.session.session_id == first.session.session_id
    assert repeat.session.observed_at == first.session.observed_at
    worker_report(service, clock, started_at=started_at, state="stopped", model_ready=False)
    latest = service.observe_local()
    assert latest.recorded and latest.session.session_id == first.session.session_id
    assert latest.session.reported_state == "stopped" and not latest.session.exit_confirmed
    worker_report(service, clock, started_at=started_at, heartbeat_at=started_at, state="idle")
    older = service.observe_local()
    assert not older.recorded and older.reason == "older_report"
    assert older.session.reported_state == "stopped" and older.session.observed_at == latest.session.observed_at
    assert service.list_sessions(registration.instance.instance_id, RegistryListQuery()).total == 1


def test_state_change_in_same_database_millisecond_is_not_lost(registry):
    service, _, clock, _ = registry
    service.register_local()
    worker_report(service, clock)
    first = service.observe_local().session
    worker_report(service, clock, state="stopped", model_ready=False, child_pid=None)
    changed = service.observe_local()
    assert changed.recorded and changed.session.session_id == first.session_id
    assert changed.session.heartbeat_at == first.heartbeat_at
    assert changed.session.reported_state == "stopped" and changed.session.child_pid is None
    assert not changed.session.reported_model_ready and not changed.session.exit_confirmed


@pytest.mark.parametrize("state", ["idle", "busy", "model_loading", "stopped", "error"])
def test_stale_report_cannot_claim_runtime_or_exit_even_when_model_reported_ready(registry, state):
    service, _, clock, _ = registry
    registration = service.register_local()
    clock[0] += timedelta(seconds=1)
    worker_report(service, clock, state=state)
    clock[0] += timedelta(seconds=service.settings.runtime_stale_seconds + 1)
    receipt = service.observe_local()
    assert receipt.recorded and receipt.session.freshness == "stale"
    assert receipt.session.reported_model_ready and not receipt.session.exit_confirmed
    assert service.get_instance(registration.instance.instance_id).instance.runtime_state == "unknown"


def test_new_worker_session_supersedes_previous_and_late_observation_cannot_revive_it(registry):
    service, _, clock, _ = registry
    registration = service.register_local()
    clock[0] += timedelta(seconds=1)
    first_started = clock[0]
    worker_report(service, clock)
    first = service.observe_local().session
    clock[0] += timedelta(seconds=1)
    worker_report(service, clock, session_id="c" * 32, pid=4321)
    latest = service.observe_local().session
    clock[0] += timedelta(seconds=1)
    worker_report(service, clock, started_at=first_started, state="stopped", model_ready=False)
    old = service.observe_local().session
    assert old.session_id == first.session_id and old.freshness == "superseded"
    assert not old.exit_confirmed
    detail = service.get_instance(registration.instance.instance_id)
    assert detail.latest_observation.session_id == latest.session_id
    first_page = service.list_sessions(registration.instance.instance_id, RegistryListQuery(page_size=1))
    second_page = service.list_sessions(registration.instance.instance_id, RegistryListQuery(page=2, page_size=1))
    assert first_page.total == second_page.total == 2
    assert [row.session_id for row in first_page.items + second_page.items] == [latest.session_id, first.session_id]
    assert service.list_sessions(registration.instance.instance_id, RegistryListQuery(page=3, page_size=1)).items == []


@pytest.mark.parametrize("field,value", [("pid", 4321), ("started_at", timedelta(seconds=1))])
def test_same_worker_session_cannot_change_launch_identity(registry, field, value):
    service, _, clock, _ = registry
    service.register_local()
    started = clock[0]
    worker_report(service, clock)
    service.observe_local()
    before = registry_rows(service)["instance_sessions"]
    clock[0] += timedelta(seconds=2)
    values = {"started_at": started, field: started + value if field == "started_at" else value}
    worker_report(service, clock, **values)
    with pytest.raises(AppError) as caught:
        service.observe_local()
    assert caught.value.code == "WORKER_SESSION_CONFLICT" and caught.value.status_code == 409
    assert registry_rows(service)["instance_sessions"] == before


@pytest.mark.parametrize("bad_time", ["future_heartbeat", "start_after_heartbeat"])
def test_inconsistent_report_times_do_not_create_session(registry, bad_time):
    service, _, clock, _ = registry
    service.register_local()
    values = {"heartbeat_at": clock[0] + timedelta(seconds=1)} if bad_time == "future_heartbeat" else {
        "started_at": clock[0] + timedelta(seconds=1)}
    worker_report(service, clock, **values)
    with pytest.raises(AppError) as caught:
        service.observe_local()
    assert caught.value.code == "INVALID_WORKER_REPORT" and caught.value.status_code == 409
    assert registry_rows(service)["instance_sessions"] == []


def test_clock_regression_is_unknown_freshness_without_mutating_saved_record(registry):
    service, _, clock, _ = registry
    registered = service.register_local()
    worker_report(service, clock)
    service.observe_local()
    before = registry_rows(service)
    clock[0] -= timedelta(seconds=1)
    observed = service.get_instance(registered.instance.instance_id).latest_observation
    assert observed.freshness == "clock_skew" and observed.heartbeat_age_seconds is None
    assert not observed.exit_confirmed and registry_rows(service) == before


def test_read_only_http_endpoints_preserve_registration_and_raw_reports(registry):
    service, app, clock, _ = registry
    registered = service.register_local()
    node_id, instance_id = registered.node.node_id, registered.instance.instance_id
    worker_report(service, clock)
    before = registry_rows(service)
    client = app.test_client()
    routes = ["/api/nodes", f"/api/nodes/{node_id}", "/api/instances",
              f"/api/instances?nodeId={node_id}", f"/api/instances/{instance_id}",
              f"/api/instances/{instance_id}/sessions"]
    for path in routes:
        response = client.get(path, headers={"X-Request-ID": "registry-read-only"})
        assert response.status_code == 200
        assert response.json["requestId"] == "registry-read-only" and "data" in response.json
    detail = client.get(f"/api/instances/{instance_id}").json["data"]
    assert detail["instance"]["runtimeState"] == "unknown" and detail["latestObservation"] is None
    assert registry_rows(service) == before
    for path in ("/api/nodes", "/api/instances", f"/api/instances/{instance_id}/start"):
        assert client.post(path, json={"command": "never-execute"}).status_code in (404, 405)


@pytest.mark.parametrize("path", [
    "/api/nodes?page=0", "/api/nodes?pageSize=101", "/api/nodes?page=bad",
    "/api/nodes?page=1&page=2", "/api/nodes?command=start",
    "/api/nodes/not-an-id", "/api/instances?page=-1", "/api/instances?pageSize=0",
    "/api/instances?nodeId=bad", "/api/instances?nodeId=" + "A" * 32,
    "/api/instances/not-an-id", "/api/instances/" + "a" * 32 + "/sessions?page=0",
])
def test_query_and_path_validation_uses_unified_error_response(registry, path):
    _, app, _, _ = registry
    response = app.test_client().get(path)
    assert response.status_code == 422 and "data" not in response.json
    assert response.json["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("path,code", [
    ("/api/nodes/" + "f" * 32, "NODE_NOT_FOUND"),
    ("/api/instances/" + "f" * 32, "INSTANCE_NOT_FOUND"),
    ("/api/instances/" + "f" * 32 + "/sessions", "INSTANCE_NOT_FOUND"),
    ("/api/instances?nodeId=" + "f" * 32, "NODE_NOT_FOUND"),
])
def test_missing_node_and_instance_return_404_not_empty_success(registry, path, code):
    _, app, _, _ = registry
    response = app.test_client().get(path)
    assert response.status_code == 404 and response.json["error"]["code"] == code


def test_unmigrated_database_returns_503_without_creating_tables(app_factory):
    app = app_factory()
    response = app.test_client().get("/api/nodes")
    assert response.status_code == 503 and response.json["error"]["code"] == "NODE_REGISTRY_UNAVAILABLE"
    assert inspect(app.extensions["database"].engine).get_table_names() == []


def test_pagination_stable_empty_pages_and_detail_extra_query_rejected(registry):
    service, app, _, _ = registry
    registered = service.register_local()
    assert service.list_nodes(RegistryListQuery(page=2, page_size=1)).items == []
    assert service.list_instances(InstanceListQuery(page=2, page_size=1)).items == []
    for path in (f"/api/nodes/{registered.node.node_id}?page=1",
                 f"/api/instances/{registered.instance.instance_id}?ignored=1"):
        assert app.test_client().get(path).status_code == 422
    with pytest.raises(ValidationError):
        service.get_node("not-an-id")


def test_cli_uses_same_service_and_does_not_create_fake_launch_sessions(registry, monkeypatch, capsys):
    service, _, _, snapshot = registry
    monkeypatch.setattr("backend.common.cli.execution.load_settings", lambda: service.settings)
    monkeypatch.setattr("backend.service.nodes.node_registry_service.probe_local_node", lambda: snapshot)
    assert main(["register-local"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["nodeCreated"] and first["instanceCreated"]
    assert first["instance"]["runtimeState"] == "unknown"
    assert main(["register-local"]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert not repeated["nodeCreated"] and repeated["instance"]["instanceId"] == first["instance"]["instanceId"]
    assert main(["observe-local"]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "no_worker_report"
    for args in (["list-nodes"], ["list-instances", "--node-id", first["node"]["nodeId"]]):
        assert main(args) == 0
        assert json.loads(capsys.readouterr().out)["total"] == 1
    assert main(["list-sessions", "--instance-id", first["instance"]["instanceId"]]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 0
    assert main(["list-nodes", "--page", "0"]) == 2
    assert '"code": "VALIDATION_ERROR"' in capsys.readouterr().err
    with pytest.raises(SystemExit) as caught:
        main(["register-local", "--command", "do-not-execute"])
    assert caught.value.code == 2
