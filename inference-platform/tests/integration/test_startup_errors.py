"""使用真实文件/目录冲突验证启动失败信息及日志句柄清理。"""

import pytest

from backend.common.exceptions.app_error import StartupError
from backend.config.settings.app_settings import AppSettings
from backend.modules.api.app import create_app


def test_unwritable_log_directory_has_clear_startup_error(tmp_path):
    blocker = tmp_path / "private-log-parent"
    blocker.write_text("A file cannot contain a log directory.", encoding="utf-8")
    settings = AppSettings(database_path=tmp_path / "db.sqlite3", log_dir=blocker / "logs")
    with pytest.raises(StartupError) as caught:
        create_app(settings)
    assert "INFERENCE_LOG_DIR" in str(caught.value)
    assert "日志目录不可写" in str(caught.value)
    assert "private-log-parent" not in str(caught.value)
    assert not settings.database_path.exists()


def test_unwritable_database_directory_has_clear_error_and_closes_log(tmp_path):
    blocker = tmp_path / "private-db-parent"
    blocker.write_text("A file cannot contain a database directory.", encoding="utf-8")
    settings = AppSettings(database_path=blocker / "nested/db.sqlite3", log_dir=tmp_path / "logs")
    with pytest.raises(StartupError) as caught:
        create_app(settings)
    assert "INFERENCE_DATABASE_PATH" in str(caught.value)
    assert "数据库无法连接" in str(caught.value)
    assert "private-db-parent" not in str(caught.value)
    log_file = settings.log_dir / "api.log"
    assert "数据库初始化失败" in log_file.read_text(encoding="utf-8")
    # Windows refuses this operation if the failed startup still owns the file handle.
    log_file.unlink()
    assert not log_file.exists()
