"""受控目录命令与旧命令共存；使用隔离库，不运行模型或网络请求。"""

import json

import pytest

from backend.common.exceptions.app_error import AppError
from backend.common.cli import execution
from backend.modules.asr import targets_cli
from tests.integration.test_asr_target_admission import target_case


@pytest.fixture
def invoke(target_case, monkeypatch):
    def execute(operation, **_kwargs):
        return operation(target_case.settings, None)
    monkeypatch.setattr(targets_cli, "execute", execute)
    return targets_cli.main


def test_catalog_cli_register_query_observe_and_legacy_commands(invoke, tmp_path):
    original = invoke(["register-current"]).root
    assert original
    config = tmp_path / "target.json"
    config.write_text(json.dumps({"targetId": "candidate", "connection": {
        "baseUrl": "https://cli-catalog.invalid", "modelId": "candidate-model"},
        "evidenceKind": "isolated"}), encoding="utf-8")
    first = invoke(["register-config", str(config)])
    second = invoke(["register-config", str(config)])
    assert first.created and not second.created
    assert first.catalog_revision_id == second.catalog_revision_id
    document = tmp_path / "observation.json"
    document.write_text(json.dumps({"catalogRevisionId": first.catalog_revision_id,
        "source": "fixture", "evidenceKind": "isolated", "evidence": "isolated report only",
        "observedAt": "2020-01-01T00:00:00Z", "ttlSeconds": 15}), encoding="utf-8")
    assert invoke(["record-observation", str(document)]).created
    assert not invoke(["record-observation", str(document)]).created
    page = invoke(["catalog", "--limit", "1"])
    assert page.total == 2 and len(page.items) == 1
    assert page.items[0].observations[0].freshness == "stale"
    assert not page.items[0].routing_enabled
    assert invoke(["list"]).root["targets"]


@pytest.mark.parametrize("content", ['{"targetId":"a","targetId":"b"}', '{"a": NaN}', '{', ' ' * 65537],
                         ids=["duplicate-key", "nonfinite", "malformed", "oversize"])
def test_catalog_cli_rejects_invalid_local_json(invoke, tmp_path, content):
    path = tmp_path / "invalid.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(AppError) as error:
        invoke(["register-config", str(path)])
    assert error.value.code == "ASR_CATALOG_INVALID_JSON"
    assert invoke(["catalog"]).total == 0


def test_catalog_cli_rejects_remote_file_paths(invoke):
    with pytest.raises(AppError) as error:
        invoke(["register-config", r"\\server\share\target.json"])
    assert error.value.code == "ASR_CATALOG_INVALID_JSON"


@pytest.mark.parametrize("field,value", [
    ("insecureTlsOrigin", "https://user:PRIVATE_TOKEN_SENTINEL@fixture.invalid"),
    ("caFile", "MISSING_PRIVATE_PATH_SENTINEL.pem"),
    ("baseUrl", "https://user:PRIVATE_TOKEN_SENTINEL@fixture.invalid"),
])
def test_real_cli_logging_redacts_invalid_connection(target_case, tmp_path, monkeypatch, capsys, field, value):
    monkeypatch.setattr(execution, "load_settings", lambda: target_case.settings)
    connection = {"baseUrl": "https://fixture.invalid", "modelId": "fixture-model", field: value}
    path = tmp_path / "invalid-connection.json"
    path.write_text(json.dumps({"targetId": "candidate", "connection": connection}), encoding="utf-8")
    assert targets_cli.main(["register-config", str(path)]) == 2
    captured = capsys.readouterr()
    log = (target_case.settings.log_dir / "asr-targets-maintenance.log").read_text(encoding="utf-8")
    combined = captured.out + captured.err + log
    assert "ASR_CATALOG_INVALID_CONNECTION" in combined
    assert "PRIVATE_TOKEN_SENTINEL" not in combined
    assert "PRIVATE_PATH_SENTINEL" not in combined


def test_real_cli_logging_redacts_file_read_error(target_case, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(execution, "load_settings", lambda: target_case.settings)
    assert targets_cli.main(["register-config", str(tmp_path / "PRIVATE_FILE_SENTINEL.json")]) == 2
    captured = capsys.readouterr()
    log = (target_case.settings.log_dir / "asr-targets-maintenance.log").read_text(encoding="utf-8")
    assert "ASR_CATALOG_INVALID_JSON" in captured.err + log
    assert "PRIVATE_FILE_SENTINEL" not in captured.out + captured.err + log
