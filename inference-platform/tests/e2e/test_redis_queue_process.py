"""真实 Python 入口，隔离源码/库/日志；不代表真实 Redis 联网通过。"""

import os
from pathlib import Path
import shutil
import subprocess
import sys

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text

from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.database.database import Database


@pytest.mark.parametrize("mode", ["disabled", "missing_client"])
def test_queue_cli_requires_explicit_backend_and_dependency_without_touching_daily_data(tmp_path, mode):
    project = tmp_path / "isolated-project"
    shutil.copytree(PROJECT_ROOT / "backend", project / "backend", ignore=shutil.ignore_patterns("__pycache__"))
    settings = AppSettings(database_path=tmp_path / "db.sqlite3", log_dir=tmp_path / "logs")
    migration = Config(str(PROJECT_ROOT / "alembic.ini")); migration.attributes["settings"] = settings
    command.upgrade(migration, "head" if mode == "missing_client" else "20261020_0014")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("INFERENCE_")}
    environment.update(PYTHONPATH=str(project), PYTHONUNBUFFERED="1",
        INFERENCE_DATABASE_PATH=str(settings.database_path), INFERENCE_LOG_DIR=str(settings.log_dir))
    if mode == "missing_client":
        environment.update(INFERENCE_COMMON_EXECUTION_ENABLED="true", INFERENCE_COMMON_QUEUE_BACKEND="redis",
                           INFERENCE_REDIS_URL="redis://user:PRIVATE_QUEUE_PASSWORD@127.0.0.1:6379/0")
    code = "import runpy,sys; sys.modules['redis']=None; sys.argv=['run_queue','--once']; runpy.run_module('backend.modules.queue.run_queue',run_name='__main__')"
    result = subprocess.run([sys.executable, "-c", code], cwd=project, env=environment,
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    expected = "REDIS_CLIENT_MISSING" if mode == "missing_client" else "REDIS_QUEUE_DISABLED"
    assert result.returncode == 2 and expected in result.stderr
    logs = "".join(path.read_text(encoding="utf-8") for path in settings.log_dir.glob("*.log"))
    assert "PRIVATE_QUEUE_PASSWORD" not in result.stdout + result.stderr + logs
    database = Database(settings)
    try:
        with database.transaction() as session:
            assert session.scalar(text("SELECT COUNT(*) FROM tasks")) == 0
            if mode == "disabled":
                assert session.scalar(text("SELECT version_num FROM alembic_version")) == "20261020_0014"
    finally: database.dispose()
