"""部署配置边界，不连接真实模型服务。"""

import pytest
from pydantic import ValidationError

from backend.common.exceptions.app_error import ConfigurationError
from backend.config.llm.model_catalog import configured_spec
from backend.config.settings.app_settings import AppSettings, load_settings

BASE = {"llm_endpoint": "http://127.0.0.1:18080/v1/chat/completions", "llm_model_id": "test-llm",
        "llm_model_version": "fixture-v1", "llm_upstream_model": "fixture-model"}


def test_default_is_explicitly_unconfigured():
    assert configured_spec(AppSettings()) is None


@pytest.mark.parametrize("missing", list(BASE))
def test_connection_group_must_be_complete(missing):
    values = BASE.copy()
    values.pop(missing)
    with pytest.raises(ValidationError):
        AppSettings(**values)
    if missing == "llm_upstream_model":
        with pytest.raises(ValidationError):
            AppSettings(**{**BASE, "llm_upstream_model": "   "})


@pytest.mark.parametrize("url", ["file:///private", "http://name:secret@localhost/v1/chat/completions",
    "http://localhost/v1/chat/completions?key=secret", "http://localhost/v1/chat/completions#secret",
    "http://localhost/v1/models", "http://localhost:invalid/v1/chat/completions", "http://localhost/ v1/chat/completions"])
def test_endpoint_must_not_carry_credentials_or_unsupported_protocol(url):
    with pytest.raises(ValidationError):
        AppSettings(**{**BASE, "llm_endpoint": url})
    with pytest.raises(ValidationError):
        AppSettings(**{**BASE, "llm_endpoint": "http://localhost:0/v1/chat/completions"})


def test_secret_excluded_from_repr_serialization_and_catalog():
    settings = AppSettings(**BASE, llm_api_key="private-test-key")
    assert "private-test-key" not in repr(settings) + settings.model_dump_json() + configured_spec(settings).model_dump_json()
    assert "llm_api_key" not in settings.model_dump()


def test_env_error_never_echoes_secret():
    with pytest.raises(ConfigurationError) as caught:
        load_settings(None, environ={"INFERENCE_LLM_API_KEY": "do-not-echo"})
    assert "do-not-echo" not in str(caught.value)


@pytest.mark.parametrize("key,value", [("llm_admission_wait_seconds", 0), ("llm_max_concurrent_requests", 5),
    ("llm_total_timeout_seconds", float("inf")), ("llm_lease_seconds", 1), ("llm_slow_consumer_seconds", -1)])
def test_runtime_limits_are_bounded(key, value):
    with pytest.raises(ValidationError):
        AppSettings(**BASE, **{key: value})
