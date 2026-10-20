"""本机-2A的真实SQLite/Flask隔离验证；不连接设备，不授权真实路由。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
import pytest
from sqlalchemy import event, inspect, text

from backend.common.exceptions.app_error import AppError
from backend.data.asr.target_models import AsrTargetRevision
from backend.data.database.database import Database
from backend.modules.api.app import close_app, create_app
from backend.schema.asr.catalog_schema import CatalogQuery, CatalogRegistration, ResourceObservationInput
from backend.service.asr.catalog_service import AsrCatalogService
from backend.service.asr.remote_catalog import config_fingerprint, remote_asr_model_spec
from backend.service.asr.target_service import TargetAdmissionService
from backend.service.asr.target_status import TargetStatusService
from tests.integration.test_asr_target_admission import target_case, ready_target, seed_queued, grant_target
from tests.integration.test_remote_asr_version_identity import unknown_version_case
from tests.unit.test_asr_catalog_contract import registration, observation


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("本机-2A隔离验证禁止网络访问")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.socket.connect_ex", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    yield
    assert not attempts, "被拦截的网络尝试也不能算无网络验证通过"


@pytest.fixture
def catalog_case(target_case, monkeypatch):
    clock = [datetime(2026, 10, 20, 1, 0, 0)]
    monkeypatch.setattr("backend.service.asr.catalog_service.database_now", lambda session: clock[0])
    service = AsrCatalogService(target_case.db, target_case.settings)
    app = create_app(target_case.settings)
    try:
        yield SimpleNamespace(**vars(target_case), service=service, app=app, client=app.test_client(), clock=clock)
    finally:
        close_app(app)


def register(case, **changes):
    return case.service.register(CatalogRegistration.model_validate(registration(**changes)))


def observe(case, revision_id, **changes):
    payload = observation(catalogRevisionId=revision_id,
                          observedAt=case.clock[0].replace(tzinfo=timezone.utc).isoformat(), **changes)
    return case.service.observe(ResourceObservationInput.model_validate(payload))


def database_snapshot(database, tables=None):
    names = tables or inspect(database.engine).get_table_names()
    with database.engine.connect() as connection:
        return {name: [dict(row) for row in connection.execute(text(f'SELECT * FROM "{name}" ORDER BY rowid')).mappings()]
                for name in names}


def test_empty_get_is_readonly_and_cannot_register_probe_or_start_anything(catalog_case, monkeypatch):
    case = catalog_case
    monkeypatch.setattr(case.service, "register", lambda *_: pytest.fail("GET cannot register"))
    before = database_snapshot(case.db)
    response = case.client.get("/api/asr/external/targets", headers={"X-Request-ID": "catalog-read-only"})
    assert response.status_code == 200
    assert response.json["requestId"] == "catalog-read-only"
    assert response.headers["Cache-Control"] == "no-store"
    value = response.json["data"]
    assert value["total"] == 0 and value["items"] == []
    assert value["offset"] == 0 and value["limit"] == 20 and value["routingEnabled"] is False
    assert database_snapshot(case.db) == before


def test_config_registration_persists_disabled_target_without_loading_model(catalog_case):
    case = catalog_case
    first = register(case)
    assert first.created and not first.routing_enabled
    result = case.service.list_targets()
    item = result.items[0]
    assert result.total == 1 and item.mode == "catalog_only"
    assert item.catalog_revision_id == first.catalog_revision_id
    assert not item.legacy_enabled and not item.routing_enabled
    assert item.target_limit == item.group_limit == 1
    assert item.device_kind == "gpu" and item.verification_state == "unverified"
    assert item.has_device_evidence and item.has_compatibility_evidence
    assert item.model_version.startswith("integration-") and item.observations == []
    assert database_snapshot(case.db, ["asr_target_worker_sessions", "asr_call_permits", "remote_asr_calls"]) == {
        "asr_target_worker_sessions": [], "asr_call_permits": [], "remote_asr_calls": []}
    with case.db.session_factory() as session:
        connection = session.get(AsrTargetRevision, item.connection_revision_id)
        assert connection.device_kind == "unknown" and connection.device_evidence is None
    with pytest.raises(AppError) as caught:
        TargetAdmissionService(case.db, case.settings, target_id=item.target_id).pool_limit()
    assert caught.value.code == "ASR_TARGET_SLOT_DISABLED"


def test_identical_registration_survives_new_service_and_database_connection(catalog_case):
    case = catalog_case
    first = register(case)
    before = database_snapshot(case.db)
    database = Database(case.settings)
    try:
        restarted = AsrCatalogService(database, case.settings)
        repeated = restarted.register(CatalogRegistration.model_validate(registration()))
        assert not repeated.created and repeated.catalog_revision_id == first.catalog_revision_id
        assert restarted.list_targets().items == case.service.list_targets().items
    finally:
        database.dispose()
    assert database_snapshot(case.db) == before


def test_concurrent_registration_is_idempotent_without_alias_capacity(catalog_case):
    case = catalog_case
    barrier = Barrier(2)

    def one():
        barrier.wait(timeout=5)
        return register(case)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: one(), range(2)))
    assert sorted(row.created for row in receipts) == [False, True]
    assert len({row.catalog_revision_id for row in receipts}) == 1
    assert case.service.list_targets().total == 1
    assert len(database_snapshot(case.db)["asr_capacity_groups"]) == 1


def test_metadata_revision_is_append_only_and_does_not_replace_connection_fingerprint(catalog_case):
    case = catalog_case
    first = register(case)
    first_item = case.service.list_targets().items[0]
    first_rows = database_snapshot(case.db, ["asr_target_revisions", "asr_catalog_revisions"])
    second = register(case, deviceEvidence="new isolated device note, still unverified")
    assert second.created and second.catalog_revision_id != first.catalog_revision_id
    latest = case.service.list_targets().items[0]
    assert latest.catalog_revision_id == second.catalog_revision_id
    assert latest.connection_revision_id == first_item.connection_revision_id
    after = database_snapshot(case.db, ["asr_target_revisions", "asr_catalog_revisions"])
    assert after["asr_target_revisions"] == first_rows["asr_target_revisions"]
    assert after["asr_catalog_revisions"][0] == first_rows["asr_catalog_revisions"][0]
    assert not register(case).created
    assert case.service.list_targets().items[0].catalog_revision_id == second.catalog_revision_id


def test_connection_change_creates_new_revision_without_overwriting_prior_model_identity(catalog_case):
    case = catalog_case
    first = register(case)
    old_item = case.service.list_targets().items[0]
    before = database_snapshot(case.db, ["asr_target_revisions"])["asr_target_revisions"]
    connection = {**registration()["connection"], "modelVersion": "provided-fixture-version"}
    new = register(case, connection=connection)
    item = case.service.list_targets().items[0]
    assert new.catalog_revision_id != first.catalog_revision_id
    assert item.connection_revision_id != old_item.connection_revision_id
    assert item.model_version == "provided-fixture-version"
    assert database_snapshot(case.db, ["asr_target_revisions"])["asr_target_revisions"][0] == before[0]


def test_origin_aliases_share_capacity_and_cannot_create_an_independent_group(catalog_case):
    case = catalog_case
    register(case)
    alias_connection = {**registration()["connection"], "baseUrl": "https://CATALOG-FIXTURE.invalid:8003/"}
    alias = register(case, targetId="fixture-alias", connection=alias_connection)
    assert alias.created
    rows = case.service.list_targets().items
    assert len(rows) == 2 and {row.capacity_group_id for row in rows} == {"remote-asr-shared"}
    assert all(row.target_limit == row.group_limit == 1 for row in rows)
    before = database_snapshot(case.db)
    with pytest.raises(AppError) as caught:
        register(case, targetId="another-alias", capacityGroupId="pretend-independent",
                 independenceEvidence="isolated note cannot split same endpoint")
    assert caught.value.code == "ASR_CATALOG_SHARED_CAPACITY" and caught.value.status_code == 409
    assert database_snapshot(case.db) == before


def test_new_capacity_group_requires_evidence_and_old_target_group_is_immutable(catalog_case):
    case = catalog_case
    with pytest.raises(AppError) as caught:
        register(case, capacityGroupId="fixture-independent")
    assert caught.value.code == "ASR_CATALOG_GROUP_EVIDENCE_REQUIRED"
    assert case.service.list_targets().total == 0
    register(case, capacityGroupId="fixture-independent", independenceEvidence="isolated independence note")
    before = database_snapshot(case.db)
    with pytest.raises(AppError) as caught:
        register(case, capacityGroupId="another-group", independenceEvidence="isolated replacement")
    assert caught.value.status_code == 409
    assert database_snapshot(case.db) == before


@pytest.mark.parametrize("target_id", ["remote-default", "enabled-fixture"])
def test_catalog_cannot_overwrite_the_legacy_or_any_enabled_target(catalog_case, target_id):
    case = catalog_case
    legacy = TargetAdmissionService(case.db, case.settings, target_id=target_id)
    legacy.register_current()
    before = database_snapshot(case.db)
    with pytest.raises(AppError) as caught:
        register(case, targetId=target_id)
    assert caught.value.code in {"ASR_CATALOG_LEGACY_RESERVED", "ASR_CATALOG_TARGET_ACTIVE"}
    assert caught.value.status_code == 409 and database_snapshot(case.db) == before


@pytest.mark.parametrize("connection", [
    {"baseUrl": "http://catalog-fixture.invalid"},
    {"baseUrl": "https://user:password@catalog-fixture.invalid"},
    {"baseUrl": "https://catalog-fixture.invalid/?token=secret"},
    {"baseUrl": "https://catalog-fixture.invalid/#secret"},
    {"baseUrl": "https://catalog-fixture.invalid/path"},
    {"baseUrl": "https://catalog-fixture.invalid:99999"},
    {"insecureTlsOrigin": "https://different-fixture.invalid"},
    {"caFile": "\\\\server.invalid\\share\\ca.pem"},
])
def test_invalid_connection_fails_before_registration_without_exposing_original(catalog_case, connection):
    case = catalog_case
    payload = registration()
    payload["connection"].update(connection)
    with pytest.raises(AppError) as caught:
        case.service.register(CatalogRegistration.model_validate(payload))
    assert caught.value.code == "ASR_CATALOG_INVALID_CONNECTION" and caught.value.status_code == 422
    assert "catalog-fixture.invalid" not in caught.value.message and "password" not in caught.value.message
    assert case.service.list_targets().total == 0


def test_registration_cannot_widen_existing_tls_exception(catalog_case):
    case = catalog_case
    payload = registration()
    payload["connection"]["insecureTlsOrigin"] = payload["connection"]["baseUrl"]
    with pytest.raises(AppError) as caught:
        case.service.register(CatalogRegistration.model_validate(payload))
    assert caught.value.code == "ASR_CATALOG_TLS_NOT_AUTHORIZED"
    assert case.service.list_targets().total == 0


def test_resource_samples_are_idempotent_persisted_and_never_execution_permission(catalog_case):
    case = catalog_case
    revision = register(case).catalog_revision_id
    first = observe(case, revision, healthState="ready", availableConcurrency=8,
                    gpuUtilizationPercent=0, gpuMemoryFreeMiB=32000)
    assert first.created and not first.routing_enabled
    repeated = observe(case, revision, healthState="ready", availableConcurrency=8,
                       gpuUtilizationPercent=0, gpuMemoryFreeMiB=32000)
    assert not repeated.created and repeated.observation_id == first.observation_id
    database = Database(case.settings)
    try:
        service = AsrCatalogService(database, case.settings)
        item = service.list_targets().items[0]
        report = item.observations[0]
        assert report.freshness == "fresh" and report.reported_available_concurrency == 8
        assert report.reported_health == "ready" and not report.usable_for_scheduling
        assert not item.routing_enabled and not item.legacy_enabled
        assert item.target_limit == item.group_limit == 1
        assert report.observation_id == first.observation_id
    finally:
        database.dispose()


def test_observation_timezone_alias_is_same_sample(catalog_case):
    case = catalog_case
    revision = register(case).catalog_revision_id
    first = observe(case, revision)
    payload = observation(catalogRevisionId=revision, observedAt="2026-10-20T09:00:00+08:00")
    second = case.service.observe(ResourceObservationInput.model_validate(payload))
    assert not second.created and first.observation_id == second.observation_id


@pytest.mark.parametrize("delta,expected", [(-1, "unknown"), (0, "fresh"), (29.999999, "fresh"), (30, "stale"), (31, "stale")])
def test_freshness_respects_exact_expiry_and_clock_regression(catalog_case, delta, expected):
    case = catalog_case
    revision = register(case).catalog_revision_id
    observe(case, revision)
    before = database_snapshot(case.db)
    case.clock[0] += timedelta(seconds=delta)
    report = case.service.list_targets().items[0].observations[0]
    assert report.freshness == expected and report.reported_health == "unknown"
    assert report.reported_available_concurrency is None and not report.usable_for_scheduling
    assert database_snapshot(case.db) == before


def test_clock_before_delayed_recording_time_cannot_report_a_fresh_sample(catalog_case):
    case = catalog_case
    revision = register(case).catalog_revision_id
    case.clock[0] += timedelta(seconds=20)
    case.service.observe(ResourceObservationInput.model_validate(observation(
        catalogRevisionId=revision, observedAt="2026-10-20T01:00:00Z", ttlSeconds=60,
        healthState="ready", availableConcurrency=1)))
    before = database_snapshot(case.db)
    case.clock[0] -= timedelta(seconds=10)
    report = case.service.list_targets().items[0].observations[0]
    assert report.observed_at == datetime(2026, 10, 20, 1, tzinfo=timezone.utc)
    assert report.recorded_at == datetime(2026, 10, 20, 1, 0, 20, tzinfo=timezone.utc)
    assert report.freshness == "unknown" and not report.usable_for_scheduling
    response = case.client.get("/api/asr/external/targets")
    assert response.status_code == 200
    assert response.json["data"]["items"][0]["observations"][0]["freshness"] == "unknown"
    case.clock[0] += timedelta(seconds=10)
    assert case.service.list_targets().items[0].observations[0].freshness == "fresh"
    case.clock[0] += timedelta(seconds=40)
    assert case.service.list_targets().items[0].observations[0].freshness == "stale"
    assert database_snapshot(case.db) == before


def test_missing_and_unknown_samples_are_not_reported_as_zero_free_capacity(catalog_case):
    case = catalog_case
    revision = register(case).catalog_revision_id
    assert case.service.list_targets().items[0].observations == []
    observe(case, revision)
    item = case.service.list_targets().items[0]
    assert item.observations[0].reported_available_concurrency is None
    assert item.observations[0].reported_health == "unknown"
    assert item.target_limit == 1 and item.group_limit == 1


def test_late_arrival_does_not_replace_newer_sample_and_each_scope_is_separate(catalog_case):
    case = catalog_case
    revision = register(case).catalog_revision_id
    first = observe(case, revision, availableConcurrency=1)
    case.clock[0] += timedelta(seconds=10)
    latest = observe(case, revision, availableConcurrency=0)
    group = observe(case, revision, scope="capacity_group", availableConcurrency=0,
                    gpuUtilizationPercent=10, gpuMemoryFreeMiB=1024)
    late = case.service.observe(ResourceObservationInput.model_validate(observation(
        catalogRevisionId=revision, observedAt="2026-10-20T01:00:05Z", availableConcurrency=7)))
    assert late.created and len({first.observation_id, latest.observation_id, late.observation_id}) == 3
    reports = {item.scope: item for item in case.service.list_targets().items[0].observations}
    assert reports["target"].observation_id == latest.observation_id
    assert reports["capacity_group"].observation_id == group.observation_id
    assert all(not report.usable_for_scheduling for report in reports.values())
    assert len(database_snapshot(case.db)["asr_resource_observations"]) == 4


@pytest.mark.parametrize("variant,code", [
    ("future", "ASR_OBSERVATION_FUTURE"), ("wrong_evidence", "ASR_OBSERVATION_EVIDENCE_MISMATCH"),
    ("missing_revision", "ASR_CATALOG_NOT_FOUND"), ("old_revision", "ASR_OBSERVATION_OLD_REVISION"),
])
def test_invalid_observation_leaves_history_unchanged(catalog_case, variant, code):
    case = catalog_case
    revision = register(case).catalog_revision_id
    if variant == "old_revision":
        register(case, deviceEvidence="updated isolated note")
    payload = observation(catalogRevisionId=revision)
    if variant == "future":
        payload["observedAt"] = "2026-10-20T01:00:00.000001+00:00"
    elif variant == "wrong_evidence":
        payload["evidenceKind"] = "provided"
    elif variant == "missing_revision":
        payload["catalogRevisionId"] = "f" * 64
    before = database_snapshot(case.db)
    with pytest.raises(AppError) as caught:
        case.service.observe(ResourceObservationInput.model_validate(payload))
    assert caught.value.code == code
    assert database_snapshot(case.db) == before


def test_new_metadata_revision_does_not_inherit_old_resource_readiness(catalog_case):
    case = catalog_case
    old = register(case).catalog_revision_id
    observe(case, old, healthState="ready", availableConcurrency=1)
    new = register(case, deviceEvidence="new isolated identity claim").catalog_revision_id
    latest = case.service.list_targets().items[0]
    assert latest.catalog_revision_id == new and latest.observations == []
    assert len(database_snapshot(case.db)["asr_resource_observations"]) == 1


def test_get_is_private_paginated_and_constant_query_count(catalog_case, tmp_path):
    case = catalog_case
    ca = tmp_path / "private-ca.pem"
    ca.write_text("not-a-real-certificate", encoding="utf-8")
    for number in range(4):
        command_value = registration(targetId=f"fixture-{number}")
        command_value["connection"]["caFile"] = str(ca)
        result = case.service.register(CatalogRegistration.model_validate(command_value))
        observe(case, result.catalog_revision_id, evidence="private-sample-evidence-do-not-expose")
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    before = database_snapshot(case.db)
    event.listen(case.db.engine, "before_cursor_execute", capture)
    try:
        first = case.service.list_targets(CatalogQuery(limit=2))
    finally:
        event.remove(case.db.engine, "before_cursor_execute", capture)
    first_query_count = len(statements)
    statements.clear()
    event.listen(case.db.engine, "before_cursor_execute", capture)
    try:
        all_items = case.service.list_targets(CatalogQuery(limit=100))
    finally:
        event.remove(case.db.engine, "before_cursor_execute", capture)
    assert first_query_count == len(statements) and first_query_count <= 5
    second = case.client.get("/api/asr/external/targets?offset=2&limit=2")
    assert second.status_code == 200
    assert [item.target_id for item in first.items] + [item["targetId"] for item in second.json["data"]["items"]] == [
        item.target_id for item in all_items.items]
    assert second.json["data"]["total"] == 4
    exposed = case.client.get("/api/asr/external/targets").get_data(as_text=True)
    for private in ("catalog-fixture.invalid", "private-ca.pem", "device mapping", "compatibility only",
                    "private-sample-evidence", "baseUrl", "caFile", "insecureTlsOrigin", "config_snapshot", "deviceId", "nodeId"):
        assert private not in exposed
    assert database_snapshot(case.db) == before


@pytest.mark.parametrize("query", [
    "offset=-1", "offset=1000001", "offset=0&offset=1", "limit=0", "limit=101", "limit=1&limit=2",
    "limit=1.0", "limit=true", "offset=%2B1", "offset=1e1", "offset=01a", "limit=99999999",
    "targetId=arbitrary", "baseUrl=https://do-not-fetch.invalid", "command=start", "routingEnabled=true",
])
def test_get_rejects_ambiguous_paging_and_unknown_parameters(catalog_case, query):
    case = catalog_case
    before = database_snapshot(case.db)
    response = case.client.get("/api/asr/external/targets?" + query)
    assert response.status_code == 422 and response.json["error"]["code"] == "VALIDATION_ERROR"
    assert "do-not-fetch.invalid" not in response.get_data(as_text=True)
    assert database_snapshot(case.db) == before


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_targets_http_is_readonly(catalog_case, method):
    case = catalog_case
    before = database_snapshot(case.db)
    response = getattr(case.client, method)("/api/asr/external/targets", json={"enabled": True})
    assert response.status_code == 405 and response.json["error"]["code"] == "HTTP_405"
    assert database_snapshot(case.db) == before


def test_catalog_does_not_change_legacy_limits_permits_queued_fingerprint_or_uncertain_call(catalog_case):
    case = catalog_case
    legacy = ready_target(case)
    active, queued = seed_queued(case), seed_queued(case)
    assert grant_target(case, legacy, active)
    with case.execution.lock_task(active) as tx:
        legacy.service.finish(tx, confirmed=False)
    before = database_snapshot(case.db)
    original_status = TargetStatusService(case.db, case.settings).status()
    fingerprint = config_fingerprint(case.settings)
    revision = register(case).catalog_revision_id
    observe(case, revision, healthState="ready", availableConcurrency=1024)
    current = TargetStatusService(case.db, case.settings).status()
    assert current.target_limit == current.group_limit == 1
    assert current.device_kind == "unknown" and current.remaining_capacity == original_status.remaining_capacity == 0
    assert current.uncertain == original_status.uncertain == 1
    assert config_fingerprint(case.settings) == fingerprint
    after = database_snapshot(case.db)
    for table, rows in before.items():
        if table in {"asr_catalog_revisions", "asr_resource_observations", "asr_compute_targets", "asr_target_revisions"}:
            continue
        assert after[table] == rows, table
    assert all(row in after["asr_compute_targets"] for row in before["asr_compute_targets"])
    assert all(row in after["asr_target_revisions"] for row in before["asr_target_revisions"])
    legacy_view = next(item for item in case.service.list_targets().items if item.target_id == "remote-default")
    assert legacy_view.mode == "legacy" and legacy_view.legacy_enabled
    assert legacy_view.catalog_revision_id is None and legacy_view.device_kind == "unknown"
    assert legacy_view.observations == [] and not legacy_view.routing_enabled


def test_history_result_and_optional_version_remain_unchanged(unknown_version_case):
    case = unknown_version_case
    task = case.succeed()
    task_id = task["taskId"]
    before_task = case.client.get(f"/api/tasks/{task_id}").json
    before_result = case.client.get(f"/api/tasks/{task_id}/result").json
    legacy_spec = remote_asr_model_spec(case.settings)
    service = AsrCatalogService(case.db, case.settings)
    service.register(CatalogRegistration.model_validate(registration()))
    assert case.client.get("/api/asr/external/targets").status_code == 200
    assert case.client.get(f"/api/tasks/{task_id}").json["data"] == before_task["data"]
    assert case.client.get(f"/api/tasks/{task_id}/result").json["data"] == before_result["data"]
    assert remote_asr_model_spec(case.settings) == legacy_spec
    assert case.settings.asr_max_file_bytes == 20 * 1024 * 1024 and case.settings.asr_max_audio_seconds == 300
