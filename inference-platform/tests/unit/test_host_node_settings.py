"""Node 配置不依赖中心平台 .env，秘密不参与公开序列化。"""

from pathlib import Path

from pydantic import ValidationError
import pytest

from backend.common.exceptions.app_error import ConfigurationError
from backend.config.host_node.node_settings import NodeSettings, PROJECT_ROOT, load_node_settings
from tests.integration.host_node_support import ADMIN, OBSERVER, REPORT, node_settings


def test_node_environment_is_explicit_and_never_reads_platform_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("NODE_ID=wrong\nNODE_ADMIN_TOKEN=not-a-token\n", encoding="utf-8")
    settings = load_node_settings(environ={"NODE_ID": "from-process", "NODE_ADMIN_TOKEN": ADMIN,
        "INFERENCE_PORT": "invalid", "NODE_REPORT_TOKEN_ASR": REPORT})
    assert settings.node_id == "from-process" and settings.port == 8020
    assert settings.report_tokens["NODE_REPORT_TOKEN_ASR"].get_secret_value() == REPORT
    assert all(token not in repr(settings) + settings.model_dump_json() for token in (ADMIN, REPORT))


def test_explicit_env_file_does_not_interpolate_secrets_and_process_values_win(tmp_path):
    path = tmp_path / "node.env"
    path.write_text(f"NODE_ID=file-node\nNODE_ADMIN_TOKEN={ADMIN}\nNODE_PORT=18020\n", encoding="utf-8")
    settings = load_node_settings(env_file=path, environ={"NODE_ID": "process-node"})
    assert settings.node_id == "process-node" and settings.port == 18020


@pytest.mark.parametrize("overrides", [
    {"admin_token": "short"}, {"admin_token": "x" * 31 + " "},
    {"observer_token": ADMIN}, {"report_tokens": {"NODE_REPORT_TOKEN_ASR": ADMIN}},
    {"report_tokens": {"NODE_REPORT_TOKEN_ASR": REPORT, "NODE_REPORT_TOKEN_OTHER": REPORT}},
    {"report_tokens": {"WRONG_PREFIX": REPORT}}, {"report_tokens": {"NODE_REPORT_TOKEN_ASR": "short"}},
    {"database_path": PROJECT_ROOT / "runtime/db/platform.sqlite3"},
    {"database_path": Path("//unknown-server/node.sqlite3")},
    {"stale_seconds": 5, "heartbeat_seconds": 5}, {"node_id": "../node"},
])
def test_invalid_settings_fail_without_echoing_secrets(tmp_path, overrides):
    with pytest.raises(ValidationError):
        node_settings(tmp_path, **overrides)


def test_public_configuration_errors_never_include_environment_values(tmp_path):
    secret = "SENSITIVE_INVALID_TOKEN"
    with pytest.raises(ConfigurationError) as caught:
        load_node_settings(environ={"NODE_ID": "node", "NODE_ADMIN_TOKEN": secret})
    assert secret not in str(caught.value)
    with pytest.raises(ConfigurationError):
        load_node_settings(env_file=tmp_path / "missing.env", environ={})
