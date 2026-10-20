"""测试资源均放临时目录；每个应用使用独立数据库和日志。"""

import pytest

from backend.config.settings.app_settings import AppSettings
from backend.modules.api.app import close_app, create_app


@pytest.fixture
def app_factory(tmp_path):
    apps = []

    def factory(**overrides):
        folder = tmp_path / str(len(apps))
        values = {"database_path": folder / "db.sqlite3", "log_dir": folder / "logs"}
        values.update(overrides)
        app = create_app(AppSettings(**values))
        apps.append(app)
        return app

    yield factory
    for app in reversed(apps):
        close_app(app)


@pytest.fixture
def app(app_factory):
    return app_factory()
