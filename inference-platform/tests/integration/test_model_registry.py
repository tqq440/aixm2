"""目录持久化与维护命令测试；不下载模型，不把测试注册的模型称为可真实执行。"""

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import json
import sqlite3
from threading import Barrier

from alembic import command
from alembic.config import Config
import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, text

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.database.database import Database
from backend.data.models.model_models import ModelRecord, ModelVersionRecord
from backend.modules.models.model_registry import main
from backend.schema.models.model_schema import ModelRegistration
from backend.service.models.model_registry_service import ModelRegistryService
from backend.service.asr.adapter_catalog import configured_asr_plugin


@pytest.fixture
def registry(tmp_path):
    settings = AppSettings(database_path=tmp_path / "registry.sqlite3", log_dir=tmp_path / "logs")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    database = Database(settings)
    try:
        yield ModelRegistryService(database, settings), config
    finally:
        database.dispose()


def future_spec(**overrides):
    payload = {
        "modelId": "future-vision", "modelVersion": "v1", "taskType": "vision",
        "executionMode": "offline", "parameterSchema": {},
        "inputConstraints": {"maxFileBytes": 1000}, "outputCapabilities": ["detections"],
        "adapterKey": "future-vision", "resourceRequirements": {"device": "cpu"},
    }
    payload.update(overrides)
    return ModelRegistration.model_validate(payload)


def test_seed_is_supported_enabled_default_and_matches_orm(registry):
    service, config = registry
    available = service.list_available()
    assert len(available) == 1
    spec = available[0]
    assert spec.model_id == "whisper-small"
    assert spec.model_version == service.settings.asr_model_revision
    assert len(spec.parameter_schema["languages"]) == 99
    assert spec.input_constraints == {"maxFileBytes": 20971520, "maxAudioSeconds": 300.0}
    assert spec.resource_requirements == {"device": "cpu", "computeType": "int8", "maxConcurrency": 1}
    assert service.resolve("whisper-small") == spec
    item = service.list_versions().items[0]
    assert item.enabled and item.is_default and item.execution_supported
    with service.database.session_factory() as session:
        model = session.get(ModelRecord, "whisper-small")
        version = session.get(ModelVersionRecord, (spec.model_id, spec.model_version))
        assert model.to_dict()["default_version"] == spec.model_version
        assert version.to_dict()["spec_json"]["modelVersion"] == spec.model_version
    command.check(config)


def test_registration_is_disabled_idempotent_and_cannot_overwrite_version(registry):
    service, _ = registry
    spec = future_spec()
    first = service.register(spec)
    assert first.created and not first.item.enabled and not first.item.is_default
    assert not first.item.execution_supported
    second = service.register(spec)
    assert not second.created
    with pytest.raises(AppError, match="不可覆盖") as failure:
        service.register(future_spec(inputConstraints={"maxFileBytes": 2000}))
    assert failure.value.code == "MODEL_VERSION_IMMUTABLE"
    assert failure.value.status_code == 409
    assert service.resolve("future-vision", "v1", require_enabled=False) == spec
    assert len(service.list_available()) == 1


def test_new_version_remains_disabled_without_changing_existing_default(registry):
    service, _ = registry
    original = service.resolve("whisper-small")
    pending = original.model_copy(update={"model_version": "pending-version"})
    result = service.register(pending)
    assert not result.item.enabled and not result.item.is_default
    assert service.resolve("whisper-small") == original
    assert service.list_versions().total == 2


@pytest.mark.parametrize("field,value", [("adapter_key", "future-asr"), ("model_version", "unimplemented-version"),
    ("resource_requirements", {"device": "cuda", "maxConcurrency": 8})])
def test_persisted_asr_registration_cannot_create_an_unimplemented_plugin(registry, field, value):
    service, _ = registry
    values = configured_asr_plugin(service.settings).model_spec().model_dump()
    values.update(model_id="registered-but-unimplemented", **{field: value})
    spec = ModelRegistration.model_validate(values)
    receipt = service.register(spec)
    assert receipt.created and not receipt.item.enabled and not receipt.item.execution_supported
    with pytest.raises(AppError) as caught:
        service.enable(spec.model_id, spec.model_version)
    assert caught.value.code == "MODEL_EXECUTION_UNSUPPORTED"
    assert [item.model_id for item in service.list_available()] == ["whisper-small"]


def test_register_existing_seed_is_idempotent_and_does_not_reset_mutable_state(registry):
    service, _ = registry
    spec = service.resolve("whisper-small")
    service.disable(spec.model_id, spec.model_version)
    result = service.register(spec)
    assert not result.created and not result.item.enabled and result.item.is_default
    assert service.list_available() == []


def test_same_version_boolean_cannot_replace_numeric_resource_requirement(registry):
    service, _ = registry
    spec = service.resolve("whisper-small")
    # PowerShell 等 JSON 工具可能把 300.0 写成 300；数值不变仍可幂等登记。
    integer_limits = spec.model_copy(update={
        "input_constraints": {"maxAudioSeconds": 300, "maxFileBytes": 20971520},
    })
    assert not service.register(integer_limits).created
    changed = spec.model_copy(update={
        "resource_requirements": {"device": "cpu", "computeType": "int8", "maxConcurrency": True},
    })
    with pytest.raises(AppError) as failure:
        service.register(changed)
    assert failure.value.code == "MODEL_VERSION_IMMUTABLE"
    assert type(service.resolve("whisper-small").resource_requirements["maxConcurrency"]) is int


def test_execution_capability_distinguishes_boolean_from_numeric_spec(registry):
    service, _ = registry
    spec = service.resolve("whisper-small")
    equivalent = spec.model_copy(update={
        "resource_requirements": {"maxConcurrency": 1.0, "computeType": "int8", "device": "cpu"},
    })
    assert service.execution_supported(equivalent)
    invalid = spec.model_copy(update={
        "resource_requirements": {"device": "cpu", "computeType": "int8", "maxConcurrency": True},
    })
    assert not service.execution_supported(invalid)
    reversed_outputs = spec.model_copy(update={"output_capabilities": list(reversed(spec.output_capabilities))})
    assert not service.execution_supported(reversed_outputs)


def test_disable_default_persists_and_reenable_restores_only_that_version(registry):
    service, config = registry
    spec = service.resolve("whisper-small")
    disabled = service.disable(spec.model_id, spec.model_version)
    assert not disabled.enabled and disabled.is_default
    assert service.list_available() == []
    for version in (None, spec.model_version):
        with pytest.raises(AppError) as failure:
            service.resolve(spec.model_id, version)
        assert failure.value.code == "MODEL_DISABLED"
    assert service.resolve(spec.model_id, spec.model_version, require_enabled=False) == spec
    service.database.dispose()
    command.upgrade(config, "head")
    database = Database(service.settings)
    try:
        restarted = ModelRegistryService(database, service.settings)
        assert restarted.list_available() == []
        assert restarted.list_versions().items[0].is_default
        with pytest.raises(AppError) as failure:
            restarted.set_default(spec.model_id, spec.model_version)
        assert failure.value.code == "MODEL_DISABLED"
        assert restarted.enable(spec.model_id, spec.model_version).enabled
        assert restarted.set_default(spec.model_id, spec.model_version).is_default
        assert restarted.list_available() == [spec]
    finally:
        database.dispose()


def test_future_adapter_cannot_enable_or_become_default(registry):
    service, _ = registry
    spec = future_spec()
    service.register(spec)
    with pytest.raises(AppError) as failure:
        service.enable(spec.model_id, spec.model_version)
    assert failure.value.code == "MODEL_EXECUTION_UNSUPPORTED"
    assert failure.value.status_code == 422
    with pytest.raises(AppError) as failure:
        service.set_default(spec.model_id, spec.model_version)
    assert failure.value.code == "MODEL_DISABLED"
    assert not service.list_versions().items[0].enabled
    with pytest.raises(AppError) as failure:
        service.resolve(spec.model_id)
    assert failure.value.code == "MODEL_DEFAULT_UNSET"


@pytest.mark.parametrize("field,value", [
    ("model_version", "different-revision"), ("adapter_key", "new-adapter"),
    ("model_repo", "other/model"), ("input_constraints", {"maxFileBytes": 999, "maxAudioSeconds": 300}),
    ("parameter_schema", {"languages": ["zh"]}), ("output_capabilities", ["text"]),
    ("resource_requirements", {"device": "cuda", "computeType": "int8", "maxConcurrency": 1}),
])
def test_declared_capability_must_match_real_worker_exactly(registry, field, value):
    service, _ = registry
    spec = service.resolve("whisper-small")
    assert not service.execution_supported(spec.model_copy(update={field: value}))


def test_lower_deployment_limits_do_not_rewrite_immutable_spec(registry):
    service, _ = registry
    settings = service.settings.model_copy(update={"asr_max_file_bytes": 1000, "asr_max_audio_seconds": 1})
    reduced = ModelRegistryService(service.database, settings)
    assert reduced.resolve("whisper-small").input_constraints["maxFileBytes"] == 20971520
    assert len(reduced.list_available()) == 1


def test_unknown_identity_and_version_have_management_errors(registry):
    service, _ = registry
    for model_id, version, code in [("missing", None, "MODEL_NOT_FOUND"),
                                    ("whisper-small", "missing", "MODEL_VERSION_NOT_FOUND")]:
        with pytest.raises(AppError) as failure:
            service.resolve(model_id, version)
        assert failure.value.code == code and failure.value.status_code == 404


def test_listing_is_stable_paginated_and_includes_unavailable_entries(registry):
    service, _ = registry
    for number in range(3):
        service.register(future_spec(modelVersion=f"v{number}"))
    first = service.list_versions(page=1, page_size=2)
    second = service.list_versions(page=2, page_size=2)
    assert first.total == second.total == 4
    identities = [(item.spec.model_id, item.spec.model_version) for item in first.items + second.items]
    assert identities == sorted(set(identities))
    assert service.list_versions(page=3, page_size=2).items == []
    with pytest.raises(ValidationError):
        service.list_versions(page=0)
    with pytest.raises(ValidationError):
        service.list_versions(page_size=101)


def test_default_switch_uses_explicit_version_without_implicit_latest(registry, monkeypatch):
    service, _ = registry
    original = service.resolve("whisper-small")
    future = original.model_copy(update={"model_version": "contract-only-v2"})
    service.register(future)
    # 仅测试默认指针事务契约；未加载或宣称第二个真实执行模型。
    monkeypatch.setattr(service, "execution_supported", lambda spec: True)
    service.enable(future.model_id, future.model_version)
    assert service.resolve("whisper-small") == original
    service.set_default(future.model_id, future.model_version)
    assert service.resolve("whisper-small") == future
    assert service.resolve(original.model_id, original.model_version) == original
    service.disable(future.model_id, future.model_version)
    assert service.list_available() == []
    service.set_default(original.model_id, original.model_version)
    assert service.list_available() == [original]


def test_admission_uses_selected_version_and_rechecks_in_caller_transaction(registry):
    service, _ = registry
    spec = service.resolve("whisper-small")
    with service.database.transaction(immediate=True) as session:
        assert service.validate_admission(session, spec.model_id, spec.model_version) == spec
    service.disable(spec.model_id, spec.model_version)
    with service.database.transaction(immediate=True) as session:
        with pytest.raises(AppError) as failure:
            service.validate_admission(session, spec.model_id, spec.model_version)
        assert failure.value.code == "MODEL_DISABLED"


def test_concurrent_same_version_register_has_one_creator_and_no_lost_update(registry):
    service, _ = registry
    barrier = Barrier(2)

    def register_one():
        barrier.wait(timeout=3)
        return service.register(future_spec()).created

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: register_one(), range(2)))
    assert sorted(results) == [False, True]
    assert service.list_versions().total == 2


def test_registration_file_bounds_encoding_and_invalid_json(registry, tmp_path):
    service, _ = registry
    target = tmp_path / "model.json"
    target.write_text(future_spec().model_dump_json(by_alias=True), encoding="utf-8-sig")
    assert service.register_file(target).created
    target.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(AppError) as failure:
        service.register_file(target)
    assert failure.value.code == "MODEL_SPEC_FILE_TOO_LARGE"
    target.write_bytes(b"\xff\xfe")
    with pytest.raises(AppError) as failure:
        service.register_file(target)
    assert failure.value.code == "MODEL_SPEC_FILE_INVALID"
    target.write_text("{invalid}", encoding="utf-8")
    with pytest.raises(ValidationError):
        service.register_file(target)
    with pytest.raises(AppError) as failure:
        service.register_file(tmp_path / "missing")
    assert failure.value.code == "MODEL_SPEC_FILE_INVALID"


@pytest.mark.parametrize("overrides", [
    {"unexpected": "field"}, {"taskType": "llm"}, {"executionMode": "online"},
    {"outputCapabilities": [""]}, {"outputCapabilities": ["text", "text"]},
    {"parameterSchema": {"nested": {"value": float("nan")}}},
    {"resourceRequirements": {"memory": float("inf")}}, {"modelVersion": "../v1"},
])
def test_external_registration_rejects_inconsistent_or_unsafe_schema(overrides):
    with pytest.raises(ValidationError):
        future_spec(**overrides)


def test_unmigrated_registry_reports_storage_error_and_never_creates_tables(tmp_path):
    settings = AppSettings(database_path=tmp_path / "unmigrated.sqlite3")
    database = Database(settings)
    try:
        with pytest.raises(AppError) as failure:
            ModelRegistryService(database, settings).list_available()
        assert failure.value.code == "MODEL_REGISTRY_UNAVAILABLE"
        assert failure.value.status_code == 503
        assert inspect(database.engine).get_table_names() == []
    finally:
        database.dispose()


def test_cli_register_list_and_status_changes_use_persisted_service(registry, tmp_path, monkeypatch, capsys):
    service, _ = registry
    monkeypatch.setattr("backend.common.cli.execution.load_settings", lambda: service.settings)
    target = tmp_path / "model.json"
    target.write_text(future_spec().model_dump_json(by_alias=True), encoding="utf-8")
    assert main(["register", "--file", str(target)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] and not payload["item"]["enabled"]
    assert main(["list", "--page", "1", "--page-size", "1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 2 and len(payload["items"]) == 1
    spec = service.resolve("whisper-small")
    selector = ["--model-id", spec.model_id, "--model-version", spec.model_version]
    for action, expected in [("disable", False), ("enable", True), ("set-default", True)]:
        assert main([action, *selector]) == 0
        assert json.loads(capsys.readouterr().out)["enabled"] is expected
    assert main(["enable", "--model-id", "future-vision", "--model-version", "v1"]) == 2
    assert '"code": "MODEL_EXECUTION_UNSUPPORTED"' in capsys.readouterr().err
    assert main(["list", "--page", "0"]) == 2
    assert '"code": "VALIDATION_ERROR"' in capsys.readouterr().err


def test_additive_migration_preserves_all_existing_task_and_idempotency_values(tmp_path):
    settings = AppSettings(database_path=tmp_path / "upgrade.sqlite3")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "20261012_0004")
    database = Database(settings)
    try:
        with database.engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO tasks (task_id, caller_id, model_id, model_version, parameters,
                    input_ref, input_filename, input_sha256, input_size_bytes, audio_duration_seconds,
                    queue_key, status, result_json, result_fingerprint)
                VALUES (:id, 'local-user', 'whisper-small', 'historical-version', '{"language":"zh"}',
                    :ref, '原始音频.wav', :sha, 100, 0.1, 'asr.whisper-small', 'succeeded',
                    '{"text":"保留既有结果"}', :fingerprint)
            """), {"id": "a" * 32, "ref": "b" * 32, "sha": "c" * 64, "fingerprint": "d" * 64})
            connection.execute(text("""
                INSERT INTO idempotency_records (id, caller_id, idempotency_key, request_fingerprint, task_id)
                VALUES (:id, 'local-user', 'old-key', :fingerprint, :task)
            """), {"id": "e" * 32, "fingerprint": "f" * 64, "task": "a" * 32})
            before = {table: connection.execute(text(f"SELECT * FROM {table}")).all()
                      for table in ("tasks", "idempotency_records")}
            original_columns = {
                table: ", ".join(f'"{row[1]}"' for row in connection.execute(text(f"PRAGMA table_info({table})")))
                for table in before
            }
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        command.check(config)
        with database.engine.connect() as connection:
            for table, rows in before.items():
                assert connection.execute(text(f"SELECT {original_columns[table]} FROM {table}")).all() == rows
            assert connection.execute(text("SELECT count(*) FROM model_versions")).scalar_one() == 1
        command.downgrade(config, "20261012_0004")
        with database.engine.connect() as connection:
            for table, rows in before.items():
                assert connection.execute(text(f"SELECT * FROM {table}")).all() == rows
    finally:
        database.dispose()


def test_registry_migration_can_render_offline_sql_without_database_access(tmp_path):
    settings = AppSettings(database_path=tmp_path / "must-not-be-created.sqlite3")
    output = StringIO()
    config = Config(str(PROJECT_ROOT / "alembic.ini"), output_buffer=output)
    config.attributes["settings"] = settings
    command.upgrade(config, "20261012_0004:20261013_0005", sql=True)
    assert not settings.database_path.exists()
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version VALUES ('20261012_0004')")
        connection.executescript(output.getvalue())
        stored, enabled = connection.execute("SELECT spec_json, enabled FROM model_versions").fetchone()
        spec = ModelRegistration.model_validate_json(stored)
        assert spec.model_id == "whisper-small" and enabled == 1
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261013_0005"
