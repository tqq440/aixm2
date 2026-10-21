"""可选队列配置不改变默认行为，不暴露 URL/凭据。"""

import sys
from types import ModuleType

from pydantic import ValidationError
import pytest

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings, load_settings
from backend.config.queue.redis_config import create_redis_client


def test_default_and_explicit_queue_settings():
    assert AppSettings().common_queue_backend == "sqlite"
    settings = load_settings(env_file=None, environ={"INFERENCE_COMMON_EXECUTION_ENABLED": "true",
        "INFERENCE_COMMON_QUEUE_BACKEND": "redis", "INFERENCE_REDIS_URL": "rediss://user:PRIVATE@localhost:6379/2"})
    assert settings.common_queue_backend == "redis"
    assert "PRIVATE" not in repr(settings) and "PRIVATE" not in settings.model_dump_json()


@pytest.mark.parametrize("changes", [
    {"common_queue_backend": "redis"},
    {"common_execution_enabled": True, "common_queue_backend": "redis"},
    *[{"redis_url": url} for url in ["http://localhost/0", "redis://localhost:0/0", "redis://localhost/16",
                                    "redis://localhost/0?socket_timeout=0", "rediss://localhost/0#private", "redis://a b/0"]],
    {"redis_namespace": "unsafe:*"}, {"redis_timeout_seconds": 0}, {"redis_republish_seconds": 0},
])
def test_invalid_config_rejected(changes):
    with pytest.raises(ValidationError): AppSettings(**changes)


def test_optional_client_config_disables_implicit_retry_and_keeps_tls_validation(monkeypatch):
    captured = {}
    redis = ModuleType("redis"); backoff = ModuleType("redis.backoff"); retry = ModuleType("redis.retry")
    class Redis:
        @staticmethod
        def from_url(url, **options): captured.update(url=url, **options); return "client"
    class NoBackoff: pass
    class Retry:
        def __init__(self, backoff, retries): self.retries = retries
    redis.Redis, backoff.NoBackoff, retry.Retry = Redis, NoBackoff, Retry
    for name, module in [("redis", redis), ("redis.backoff", backoff), ("redis.retry", retry)]:
        monkeypatch.setitem(sys.modules, name, module)
    settings = AppSettings(common_execution_enabled=True, common_queue_backend="redis", redis_url="rediss://localhost/0")
    assert create_redis_client(settings) == "client"
    assert captured["retry"].retries == 0 and captured["retry_on_timeout"] is False
    assert captured["ssl_cert_reqs"] == "required" and captured["socket_timeout"] <= 3
