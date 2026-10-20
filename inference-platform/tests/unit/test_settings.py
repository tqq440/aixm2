import os

import pytest

from backend.common.exceptions.app_error import ConfigurationError
from backend.config.settings.app_settings import PROJECT_ROOT, load_settings


def test_defaults_are_local_and_relative_to_project(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = load_settings(env_file=None, environ={})
    assert settings.host == "127.0.0.1"
    assert settings.port == 5000
    assert settings.log_level == "INFO"
    assert settings.database_path == PROJECT_ROOT / "runtime/db/platform.sqlite3"
    assert settings.log_dir == PROJECT_ROOT / "runtime/logs"
    assert list(tmp_path.iterdir()) == []


def test_environment_overrides_dotenv_without_changing_process_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "INFERENCE_PORT=5011\nINFERENCE_LOG_LEVEL=WARNING\n"
        "INFERENCE_DATABASE_PATH=runtime/db/测试数据库.sqlite3\n",
        encoding="utf-8-sig",
    )
    monkeypatch.setenv("INFERENCE_PORT", "5077")
    settings = load_settings(env_file, environ={"INFERENCE_PORT": "5022"})
    assert settings.port == 5022
    assert settings.log_level == "WARNING"
    assert settings.database_path == PROJECT_ROOT / "runtime/db/测试数据库.sqlite3"
    # The explicitly supplied mapping is authoritative, not the ambient process.
    assert load_settings(env_file, environ={}).port == 5011
    assert os.environ["INFERENCE_PORT"] == "5077"


def test_dotenv_does_not_interpolate_other_environment_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("INFERENCE_DATABASE_PATH=${UNRELATED_SECRET}/db.sqlite3\n", encoding="utf-8")
    settings = load_settings(env_file, environ={"UNRELATED_SECRET": "private-value"})
    assert "${UNRELATED_SECRET}" in str(settings.database_path)
    assert "private-value" not in str(settings.database_path)


def test_malformed_dotenv_fails_with_line_number_without_echoing_input(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text('# Local settings\nINFERENCE_PORT="8765\n', encoding="utf-8")
    with pytest.raises(ConfigurationError) as caught:
        # An environment override must not hide a malformed configuration file.
        load_settings(env_file, environ={"INFERENCE_PORT": "5001"})
    message = str(caught.value)
    assert ".env" in message
    assert "第 2 行" in message
    assert "语法错误" in message
    assert "8765" not in message
    assert "8765" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("INFERENCE_PORT", "private-invalid-port"),
        ("INFERENCE_PORT", "0"),
        ("INFERENCE_PORT", "65536"),
        ("INFERENCE_HOST", "0.0.0.0"),
        ("INFERENCE_LOG_LEVEL", "private-invalid-level"),
        ("INFERENCE_DATABASE_PATH", " "),
        ("INFERENCE_LOG_DIR", " "),
        ("INFERENCE_UNKNOWN", "private-value"),
    ],
)
def test_invalid_settings_name_problem_without_echoing_values(key, value):
    with pytest.raises(ConfigurationError) as caught:
        load_settings(env_file=None, environ={key: value})
    assert key in str(caught.value)
    if value.strip():
        assert value not in str(caught.value)


def test_database_directory_and_log_file_are_rejected(tmp_path):
    log_file = tmp_path / "not-a-directory"
    log_file.write_text("existing file", encoding="utf-8")
    for key, value in [("INFERENCE_DATABASE_PATH", tmp_path), ("INFERENCE_LOG_DIR", log_file)]:
        with pytest.raises(ConfigurationError, match=key):
            load_settings(env_file=None, environ={key: str(value)})
