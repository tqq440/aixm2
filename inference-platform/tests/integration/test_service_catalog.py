"""公共目录隔离验证：所有数据库、配置和网络替身均位于测试边界。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256

from alembic import command
from alembic.config import Config
import httpx2 as httpx
import pytest
from sqlalchemy import func, inspect, select, text

from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.catalog.catalog_models import InferenceCatalogEvent, InferenceService, InferenceServiceRevision, InferenceTargetCheck
from backend.data.asr.service_config_store import ServiceConfigStore
from backend.config.asr.service_config import service_revision
from backend.schema.asr.service_config_schema import AsrServiceConfig
from backend.schema.catalog.service_schema import ServiceDefinition
from backend.modules.api.app import close_app, create_app
from backend.service.catalog.connection_probe import probe_target


ROOT = "/api/admin/service-catalog"


@pytest.fixture
def catalog_case(tmp_path):
    settings = AppSettings(database_path=tmp_path / "db/platform.sqlite3", log_dir=tmp_path / "logs", upload_dir=tmp_path / "uploads")
    migration = Config(str(PROJECT_ROOT / "alembic.ini"))
    migration.attributes["settings"] = settings
    command.upgrade(migration, "head")
    app = create_app(settings)
    app.extensions["service_catalog"].probe = lambda config, target: {
        "success": True, "errorCode": None, "durationMs": 1, "phase": "health", "inferenceVerified": False, "actualDevice": None,
        "message": "隔离连接替身通过；未推理。"}
    yield app, app.test_client(), migration, settings
    close_app(app)


def definition(kind="asr"):
    protocol, invoke, health = {
        "asr": ("remote-v2", "/api/v1/asr/transcripts", "/api/v1/asr/health"),
        "vision": ("vision-json-v1", "/v1/vision/detections", "/health"),
        "llm": ("chat-completions-sse", "/v1/chat/completions", "/v1/models"),
    }[kind]
    return {"serviceId": f"{kind}-main", "name": f"测试 {kind}", "kind": kind, "modelId": f"{kind}-model",
            "protocol": protocol, "invocationMode": "stream" if kind == "llm" else "sync", "maxFileBytes": 1024,
            "targets": [{"targetId": "primary", "name": "主要目标", "baseUrl": "http://127.0.0.1:8010",
                         "invokePath": invoke, "healthPath": health, "upstreamModel": "fixture-model"}]}


def save(client, config=None, version=None):
    config = config or definition()
    return client.put(f"{ROOT}/services/{config['serviceId']}", json={"config": config, "expectedVersion": version})


def payload(response, status=200):
    assert response.status_code == status, response.get_json()
    return response.get_json()["data"]


def check(client, entry):
    return client.post(f"{ROOT}/services/{entry['serviceId']}/targets/primary/test",
                       json={"expectedVersion": entry["version"], "revision": entry["draftRevision"]})


def activate(client, entry, enabled=True):
    return client.post(f"{ROOT}/services/{entry['serviceId']}/activation",
                       json={"expectedVersion": entry["version"], "revision": entry["draftRevision"], "enabled": enabled})


@pytest.mark.parametrize("kind", ["asr", "vision", "llm"])
def test_all_kinds_saved_with_explicit_missing_capabilities(catalog_case, kind):
    app, client, _, _ = catalog_case
    entry = payload(save(client, definition(kind)))
    assert entry["version"] == 1 and entry["executionReady"] is False and entry["enabled"] is False
    assert entry["config"]["modelVersion"] is None
    codes = {issue["code"] for issue in entry["issues"]}
    assert {"EXECUTION_NOT_CONNECTED", "STATUS_QUERY_UNSUPPORTED", "PROGRESS_UNSUPPORTED", "ACTUAL_DEVICE_UNSUPPORTED"} <= codes
    assert entry["config"]["targets"][0]["deviceKind"] == "unknown"
    assert app.extensions["database"].engine is not None


def test_checks_do_not_claim_inference_or_change_version_and_get_is_readonly(catalog_case):
    app, client, _, _ = catalog_case
    entry = payload(save(client))
    result = payload(check(client, entry))
    assert result["actualDevice"] is None and result["inferenceVerified"] is False
    app.extensions["service_catalog"].probe = lambda *args: pytest.fail("GET 不得执行健康 GET")
    fresh = payload(client.get(f"{ROOT}/services/asr-main"))
    assert fresh["version"] == 1 and fresh["checks"][0]["revision"] == entry["draftRevision"]
    assert payload(client.get(f"{ROOT}/services"))["total"] == 1


def test_activation_requires_recent_matching_health_and_only_updates_catalog(catalog_case):
    app, client, _, _ = catalog_case
    entry = payload(save(client))
    assert activate(client, entry).get_json()["error"]["code"] == "CATALOG_HEALTH_REQUIRED"
    payload(check(client, entry))
    active = payload(activate(client, entry))
    assert active["enabled"] is True and active["activeRevision"] == entry["draftRevision"]
    assert active["executionReady"] is False
    disabled = payload(activate(client, active, False))
    assert disabled["enabled"] is False and disabled["activeRevision"] == entry["draftRevision"]
    with app.extensions["database"].transaction() as session:
        for name in ("tasks", "remote_asr_calls", "asr_call_permits", "online_requests"):
            assert session.scalar(text(f"SELECT COUNT(*) FROM {name}")) == 0


def test_revision_is_immutable_and_old_active_config_is_pinned(catalog_case):
    app, client, _, _ = catalog_case
    entry = payload(save(client))
    payload(check(client, entry)); active = payload(activate(client, entry))
    changed = definition(); changed["targets"][0]["baseUrl"] = "http://127.0.0.1:8020"
    new = payload(save(client, changed, active["version"]))
    assert new["draftRevision"] != active["activeRevision"]
    assert new["activeConfig"]["targets"][0]["baseUrl"].endswith(":8010")
    assert new["config"]["targets"][0]["baseUrl"].endswith(":8020")
    assert activate(client, new).status_code == 409
    with app.extensions["database"].transaction() as session:
        old = session.get(InferenceServiceRevision, entry["draftRevision"])
        assert old.config["targets"][0]["baseUrl"].endswith(":8010")
        assert session.scalar(select(func.count()).select_from(InferenceServiceRevision)) == 2


def test_concurrent_saves_have_one_winner(catalog_case):
    app, client, _, _ = catalog_case
    payload(save(client))
    def update(index):
        with app.test_client() as other:
            config = definition(); config["name"] = str(index)
            return save(other, config, 1).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, range(2))) == [200, 409]


def test_event_failure_rolls_back_configuration(catalog_case, monkeypatch):
    app, client, _, _ = catalog_case
    def fail(*args):
        raise RuntimeError("injected event write failure")
    monkeypatch.setattr(app.extensions["service_catalog"].repository, "event", fail)
    assert save(client).status_code == 500
    with app.extensions["database"].transaction() as session:
        assert session.scalar(select(func.count()).select_from(InferenceService)) == 0
        assert session.scalar(select(func.count()).select_from(InferenceServiceRevision)) == 0


def test_async_missing_completion_contract_is_recorded_not_enabled(catalog_case):
    _, client, _, _ = catalog_case
    config = definition(); config["invocationMode"] = "async"
    entry = payload(save(client, config))
    assert any(issue["code"] == "COMPLETION_UNAVAILABLE" for issue in entry["issues"])
    payload(check(client, entry))
    assert activate(client, entry).get_json()["error"]["code"] == "CATALOG_INTEGRATION_INCOMPLETE"


def test_optional_monitoring_is_a_declaration_and_not_a_background_query(catalog_case):
    _, client, _, _ = catalog_case
    config = definition()
    config["targets"][0]["monitoring"] = {"protocol": "job-json-v1", "statusPath": "/jobs/{requestId}",
        "resultPath": "/jobs/{requestId}/result", "reportsProgress": True, "reportsDevice": True}
    entry = payload(save(client, config))
    assert not any(issue["code"] == "PROGRESS_UNSUPPORTED" for issue in entry["issues"])
    assert entry["checks"] == [] and entry["executionReady"] is False


def test_expanded_capacity_needs_explicit_evidence_but_can_be_saved(catalog_case):
    _, client, _, _ = catalog_case
    config = definition(); config["targets"][0]["concurrencyLimit"] = 2
    entry = payload(save(client, config)); payload(check(client, entry))
    assert activate(client, entry).get_json()["error"]["code"] == "CATALOG_CAPACITY_EVIDENCE_REQUIRED"
    config["targets"][0]["capacityEvidence"] = "service-owner approved slots: 2 (fixture)"
    entry = payload(save(client, config, entry["version"])); payload(check(client, entry))
    assert payload(activate(client, entry))["executionReady"] is False


def test_gpu_policy_does_not_accept_unknown_device_as_available(catalog_case):
    _, client, _, _ = catalog_case
    config = definition(); config["policy"] = "gpu_preferred"
    entry = payload(save(client, config)); payload(check(client, entry))
    assert any(issue["code"] == "DEVICE_POLICY_UNVERIFIED" for issue in entry["issues"])
    assert activate(client, entry).status_code == 409


def test_config_tampering_is_reported_without_overwriting(catalog_case):
    app, client, _, _ = catalog_case
    entry = payload(save(client))
    with app.extensions["database"].transaction(immediate=True) as session:
        row = session.get(InferenceServiceRevision, entry["draftRevision"])
        row.config = {**row.config, "name": "tampered"}
    response = client.get(f"{ROOT}/services/asr-main")
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "CATALOG_REVISION_INVALID"


@pytest.mark.parametrize("mutation", [
    {"baseUrl": "http://user:secret@localhost"}, {"invokePath": "https://other.example/infer"},
    {"healthPath": "//other.example/health"}, {"concurrencyLimit": True},
    {"deviceKind": "unknown", "deviceId": "cuda:0"},
    {"baseUrl": "https://localhost", "insecureTlsOrigin": "https://other.example"},
    {"baseUrl": "https://localhost", "insecureTlsOrigin": "https://localhost", "caFile": "ca.pem"},
    {"monitoring": {"reportsProgress": True}},
    {"monitoring": {"protocol": "job-json-v1", "statusPath": "/jobs/{other}"}},
    {"monitoring": {"protocol": "job-json-v1", "statusPath": "/jobs/{requestId}/{requestId}"}},
    {"apiKey": "must-not-be-persisted"},
])
def test_invalid_targets_rejected_without_persistence(catalog_case, mutation):
    _, client, _, _ = catalog_case
    config = definition(); config["targets"][0].update(mutation)
    assert save(client, config).status_code == 422
    assert payload(client.get(f"{ROOT}/services"))["total"] == 0


def test_group_refs_and_revision_conflicts(catalog_case):
    _, client, _, _ = catalog_case
    config = definition(); config["targets"][0]["resourceGroupIds"] = ["gpu0"]
    assert save(client, config).get_json()["error"]["code"] == "CATALOG_GROUP_NOT_FOUND"
    group = {"groupId": "gpu0", "name": "实验 GPU", "concurrencyLimit": 1, "capacityEvidence": "service-owner quota"}
    payload(client.put(f"{ROOT}/resource-groups/gpu0", json={"config": group, "expectedVersion": None}))
    assert payload(save(client, config))["config"]["targets"][0]["resourceGroupIds"] == ["gpu0"]
    assert client.put(f"{ROOT}/resource-groups/gpu0", json={"config": group, "expectedVersion": None}).status_code == 409
    assert payload(client.get(f"{ROOT}/resource-groups"))["items"][0]["version"] == 1


def test_health_from_old_draft_or_old_time_cannot_enable(catalog_case):
    app, client, _, _ = catalog_case
    entry = payload(save(client)); payload(check(client, entry))
    with app.extensions["database"].transaction(immediate=True) as session:
        row = session.scalar(select(InferenceTargetCheck))
        row.checked_at -= timedelta(minutes=6)
    assert activate(client, entry).get_json()["error"]["code"] == "CATALOG_HEALTH_REQUIRED"


def test_late_health_response_cannot_update_new_draft(catalog_case):
    app, client, _, _ = catalog_case
    entry = payload(save(client))
    def racing_probe(config, target):
        changed = definition(); changed["name"] = "new"
        with app.test_client() as other:
            payload(save(other, changed, 1))
        return {"success": True, "errorCode": None, "durationMs": 1}
    app.extensions["service_catalog"].probe = racing_probe
    assert check(client, entry).status_code == 409
    with app.extensions["database"].transaction() as session:
        assert session.scalar(select(func.count()).select_from(InferenceTargetCheck)) == 0


def test_import_is_explicit_idempotent_and_preserves_source_selection(catalog_case):
    _, client, _, settings = catalog_case
    store = ServiceConfigStore(settings.database_path)
    config = AsrServiceConfig(service_id="saved-asr", name="既有配置", base_url="http://127.0.0.1:8010", model_id="sensevoice")
    revision = service_revision(config)
    store.write({"version": 1, "items": {config.service_id: revision},
                 "revisions": {revision: config.model_dump(mode="json", by_alias=True)}, "selectedRevision": revision})
    source = settings.database_path.with_name(settings.database_path.name + ".asr-services.json")
    before = sha256(source.read_bytes()).hexdigest()
    assert payload(client.get(f"{ROOT}/services"))["total"] == 0
    result = payload(client.post(f"{ROOT}/import-asr", json={}))
    assert result["imported"] == ["saved-asr"]
    assert payload(client.post(f"{ROOT}/import-asr", json={}))["skipped"] == ["saved-asr"]
    entry = payload(client.get(f"{ROOT}/services/saved-asr"))
    assert entry["enabled"] is False and entry["source"] == "asr-v2.13"
    assert entry["config"]["targets"][0]["deviceKind"] == "unknown"
    assert sha256(source.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("kwargs", [
    {"environ_overrides": {"REMOTE_ADDR": "10.0.0.2"}},
    {"headers": {"Origin": "https://evil.example"}},
    {"headers": {"Host": "evil.example"}},
    {"headers": {"Sec-Fetch-Site": "cross-site"}},
])
def test_management_routes_remain_local_until_auth_module(catalog_case, kwargs):
    _, client, _, _ = catalog_case
    assert client.get(f"{ROOT}/services", **kwargs).status_code == 403


def test_pagination_and_no_implicit_mutations(catalog_case):
    _, client, _, _ = catalog_case
    for index in range(3):
        config = definition(); config["serviceId"] = f"service-{index}"
        payload(save(client, config))
    page = payload(client.get(f"{ROOT}/services?page=2&pageSize=2"))
    assert page["total"] == 3 and len(page["items"]) == 1
    assert client.get(f"{ROOT}/services?pageSize=101").status_code == 422
    assert client.get(f"{ROOT}/services?unknown=true").status_code == 422
    assert client.get(f"{ROOT}/services?page=1&page=2").status_code == 422
    assert client.put(f"{ROOT}/services/asr-main", data="{}", content_type="text/plain").status_code == 415


def test_migration_is_repeatable_metadata_matches_and_populated_downgrade_is_refused(catalog_case):
    app, client, migration, _ = catalog_case
    command.upgrade(migration, "head")
    command.check(migration)
    payload(save(client))
    with pytest.raises(RuntimeError, match="公共目录已有资料"):
        command.downgrade(migration, "20261020_0014")
    assert payload(client.get(f"{ROOT}/services"))["total"] == 1
    assert "tasks" in inspect(app.extensions["database"].engine).get_table_names()


@pytest.mark.parametrize("kind,payload_body", [
    ("asr", {"success": True, "data": {"status": "ok", "model_loaded": True}}),
    ("vision", {"status": "ok", "modelLoaded": True, "model": "fixture-model"}),
    ("llm", {"data": [{"id": "fixture-model"}]}),
])
def test_health_protocols_are_get_only_bounded_and_do_not_claim_gpu(kind, payload_body):
    config = ServiceDefinition.model_validate(definition(kind))
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload_body)
    result = probe_target(config, config.targets[0], transport=httpx.MockTransport(handler))
    assert result["success"] is True and result["actualDevice"] is None and result["inferenceVerified"] is False
    assert len(calls) == 1 and calls[0].method == "GET" and calls[0].content == b""


@pytest.mark.parametrize("code,body,content_type,error", [
    (302, b"", "application/json", "HEALTH_HTTP_ERROR"),
    (200, b"x" * 65_537, "application/json", "HEALTH_RESPONSE_LIMIT"),
    (200, b"not-json", "application/json", "HEALTH_PROTOCOL_INVALID"),
    (200, b"{}", "text/html", "HEALTH_PROTOCOL_INVALID"),
    (200, b'{"status":"ok"}', "application/json", "HEALTH_PROTOCOL_INVALID"),
], ids=["redirect", "oversize", "invalid-json", "wrong-content-type", "missing-contract"])
def test_invalid_health_is_not_readiness(code, body, content_type, error):
    config = ServiceDefinition.model_validate(definition())
    response = lambda request: httpx.Response(code, content=body, headers={"content-type": content_type, "location": "http://other.invalid"})
    result = probe_target(config, config.targets[0], transport=httpx.MockTransport(response))
    assert result["success"] is False and result["errorCode"] == error
