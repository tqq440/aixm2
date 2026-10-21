"""真实文件/SQLite/HTTP隔离验证；不访问校园服务、不加载模型。"""

from types import SimpleNamespace

import pytest

from backend.config.asr.service_config import apply_service_selection, read_service_document, settings_for_service
from backend.config.settings.app_settings import AppSettings, load_settings
from backend.data.asr.service_config_store import ServiceConfigStore
from backend.modules.api.app import close_app, create_app
from backend.schema.asr.service_config_schema import AsrServiceConfig
from backend.service.asr.remote_catalog import config_fingerprint
from tests.integration.test_remote_asr_version_identity import unknown_version_case


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Configuration tests must not access the network")
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.getaddrinfo", denied)


def configuration(**overrides):
    value = dict(serviceId="manual-asr", name="实验室转写", protocol="remote-v2",
        baseUrl="http://127.0.0.1:8010", transcribePath="/api/v1/asr/transcripts",
        healthPath="/api/v1/asr/health", modelId="manual-sensevoice")
    value.update(overrides)
    return value


def save(case, config=None, expected=None):
    config = config or configuration()
    response = case.client.put("/api/asr/services/" + config["serviceId"],
        json={"config": config, "expectedRevision": expected})
    assert response.status_code == 200, response.json
    return response.json["data"]


def choose(case, state):
    item = state["items"][0]
    return case.client.post("/api/asr/services/selection",
        json={"serviceId": item["serviceId"], "expectedRevision": item["revision"]})


def test_list_and_save_do_not_mutate_running_settings_or_tasks(unknown_version_case):
    case = unknown_version_case
    before = config_fingerprint(case.settings)
    assert case.client.get("/api/asr/services").json["data"]["items"] == []
    state = save(case)
    assert state["selectedRevision"] is None and not state["restartRequired"]
    assert state["runningModel"]["modelId"] == case.spec.model_id
    assert config_fingerprint(case.settings) == before
    assert read_service_document(case.settings).items["manual-asr"] == state["items"][0]["revision"]
    assert '"apiKey":' not in ServiceConfigStore(case.settings.database_path).path.read_text(encoding="utf-8")


def test_stale_edit_conflict_and_idempotent_retry(unknown_version_case):
    case = unknown_version_case
    first = save(case)
    assert save(case) == first
    response = case.client.put("/api/asr/services/manual-asr",
        json={"config": configuration(name="changed"), "expectedRevision": None})
    assert response.status_code == 409
    second = save(case, configuration(name="changed"), first["items"][0]["revision"])
    assert second["items"][0]["revision"] != first["items"][0]["revision"]


def test_select_requires_restart_and_registers_exact_model_on_next_start(unknown_version_case):
    case = unknown_version_case
    response = choose(case, save(case))
    assert response.status_code == 200
    assert response.json["data"]["restartRequired"]
    selected = apply_service_selection(case.settings)
    assert selected.asr_service_id == "manual-asr"
    assert selected.remote_asr_base_url == "http://127.0.0.1:8010"
    app = create_app(selected)
    try:
        state = app.test_client().get("/api/asr/services").json["data"]
        assert not state["restartRequired"] and state["runningServiceId"] == "manual-asr"
        assert state["runningModel"]["modelVersion"].startswith("integration-")
        assert any(model.model_id == "manual-sensevoice"
                   for model in app.extensions["model_service"].registry.list_available())
    finally:
        close_app(app)


def test_old_queued_task_blocks_service_switch_but_not_draft_save(unknown_version_case):
    case = unknown_version_case
    task = case.submit()
    assert task.status_code == 201
    response = choose(case, save(case))
    assert response.status_code == 409
    assert response.json["error"]["code"] == "ASR_CONFIG_PENDING_TASKS"
    assert read_service_document(case.settings).selected_revision is None


def test_selection_blocks_new_work_in_old_process(unknown_version_case):
    case = unknown_version_case
    assert choose(case, save(case)).status_code == 200
    response = case.submit()
    assert response.status_code in (409, 503)
    assert response.json["error"]["code"] in {"ASR_CONFIG_RESTART_REQUIRED", "REMOTE_ASR_UNAVAILABLE"}


def test_selected_revision_is_frozen_when_draft_changes(unknown_version_case):
    case = unknown_version_case
    first = save(case)
    choose(case, first)
    second = save(case, configuration(baseUrl="http://127.0.0.1:8020"), first["items"][0]["revision"])
    assert second["selectedRevision"] == first["items"][0]["revision"]
    assert apply_service_selection(case.settings).remote_asr_base_url.endswith(":8010")
    pinned = apply_service_selection(case.settings)
    assert choose(case, second).status_code == 200
    assert apply_service_selection(pinned).remote_asr_base_url.endswith(":8010")
    assert apply_service_selection(case.settings).remote_asr_base_url.endswith(":8020")


def test_health_probe_is_manual_readonly_and_not_inference(unknown_version_case, monkeypatch):
    case = unknown_version_case
    calls = []
    def probe(settings):
        calls.append(settings.remote_asr_base_url)
        return SimpleNamespace(ready=True, error_code=None, error_message=None, api_version="14.2")
    monkeypatch.setattr("backend.service.asr.service_config_service.probe_remote_asr", probe)
    case.client.get("/api/asr/services")
    assert calls == []
    response = case.client.post("/api/asr/services/test", json={"config": configuration()})
    assert response.status_code == 200 and response.json["data"]["success"]
    assert "尚未验证音频转写" in response.json["data"]["message"]
    assert calls == ["http://127.0.0.1:8010"]
    assert not ServiceConfigStore(case.settings.database_path).path.exists()


@pytest.mark.parametrize("headers", [
    {"Origin": "https://attacker.example"},
    {"Host": "attacker.example"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "null"},
])
def test_cross_site_configuration_and_probes_are_denied(unknown_version_case, headers):
    response = unknown_version_case.client.post("/api/asr/services/test",
        json={"config": configuration()}, headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize("config", [
    configuration(baseUrl="http://user:secret@127.0.0.1:8001"),
    configuration(transcribePath="//other-host/upload"),
    configuration(baseUrl="ftp://127.0.0.1:8001"),
    configuration(protocol="openai-audio", upstreamModel=None),
])
def test_invalid_protocol_binding_not_saved(unknown_version_case, config):
    response = unknown_version_case.client.put("/api/asr/services/manual-asr", json={"config": config})
    assert response.status_code == 422
    assert not ServiceConfigStore(unknown_version_case.settings.database_path).path.exists()


def test_active_delete_denied_and_unused_delete_keeps_snapshot(unknown_version_case):
    case = unknown_version_case
    state = save(case)
    revision = state["items"][0]["revision"]
    choose(case, state)
    assert case.client.delete("/api/asr/services/manual-asr",
        json={"expectedRevision": revision}).status_code == 409
    response = case.client.post("/api/asr/services/selection",
        json={"serviceId": None, "expectedRevision": revision})
    assert response.status_code == 200
    assert case.client.delete("/api/asr/services/manual-asr",
        json={"expectedRevision": revision}).status_code == 200
    doc = read_service_document(case.settings)
    assert doc.items == {} and revision in doc.revisions


def test_bad_file_fails_closed_and_is_not_overwritten(unknown_version_case):
    case = unknown_version_case
    store = ServiceConfigStore(case.settings.database_path)
    store.path.write_text('{"version":1,"version":1}', encoding="utf-8")
    assert case.client.get("/api/asr/services").status_code == 503
    response = case.client.put("/api/asr/services/manual-asr", json={"config": configuration()})
    assert response.status_code == 503
    assert store.path.read_text(encoding="utf-8") == '{"version":1,"version":1}'


def test_load_settings_uses_database_specific_store_and_legacy_pin(unknown_version_case):
    case = unknown_version_case
    choose(case, save(case))
    environment = {"INFERENCE_DATABASE_PATH": str(case.settings.database_path)}
    loaded = load_settings(env_file=None, environ=environment)
    assert loaded.asr_service_id == "manual-asr"
    legacy = load_settings(env_file=None, environ=dict(environment, INFERENCE_ASR_SERVICE_REVISION="legacy"))
    assert legacy.remote_asr_base_url is None and legacy.asr_service_revision is None
    other = load_settings(env_file=None, environ={"INFERENCE_DATABASE_PATH": str(case.settings.database_path.with_name("other.db"))})
    assert other.remote_asr_base_url is None


def test_manual_settings_preserve_other_secrets(tmp_path):
    from pydantic import SecretStr
    settings = AppSettings(database_path=tmp_path / "test.db", agent_token=SecretStr("x" * 32))
    changed = settings_for_service(settings, AsrServiceConfig.model_validate(configuration()))
    assert changed.agent_token.get_secret_value() == "x" * 32


def test_restore_environment_restores_exact_model_default(unknown_version_case):
    case = unknown_version_case
    state = save(case, configuration(modelId=case.spec.model_id))
    choose(case, state)
    manual = apply_service_selection(case.settings)
    app = create_app(manual)
    close_app(app)
    assert case.client.post("/api/asr/services/selection", json={
        "serviceId": None, "expectedRevision": state["items"][0]["revision"]}).status_code == 200
    restored = create_app(case.settings)
    try:
        models = restored.extensions["model_service"].registry.list_available()
        assert any(item.model_id == case.spec.model_id and item.model_version == case.spec.model_version
                   for item in models)
    finally:
        close_app(restored)


def test_model_identity_conflict_is_rejected_before_selection(unknown_version_case):
    from backend.config.asr.model_catalog import MODEL_ID
    from backend.service.asr.adapter_catalog import configured_asr_plugin
    from backend.service.models.model_registry_service import ModelRegistryService
    case = unknown_version_case
    legacy = configured_asr_plugin(case.settings).model_spec()
    ModelRegistryService(case.db, case.settings).register(legacy)
    state = save(case, configuration(modelId=MODEL_ID, modelVersion=legacy.model_version))
    response = choose(case, state)
    assert response.status_code == 409 and response.json["error"]["code"] == "ASR_MODEL_ID_RESERVED"
    assert read_service_document(case.settings).selected_revision is None


def test_existing_local_model_id_cannot_be_hijacked_with_new_version(unknown_version_case):
    case = unknown_version_case
    from backend.service.asr.adapter_catalog import configured_asr_plugin
    local = configured_asr_plugin(case.settings).model_spec()
    case.registry.register(local)
    state = save(case, configuration(modelId=local.model_id))
    response = choose(case, state)
    assert response.status_code == 409 and response.json["error"]["code"] == "ASR_MODEL_ID_RESERVED"
