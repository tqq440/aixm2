"""内部执行 HTTP 的边界、凭据与错误脱敏；不连接网络。"""

import httpx2 as httpx
from pydantic import ValidationError
import pytest

from backend.common.exceptions.app_error import AppError
from backend.config.host_node.node_settings import load_node_settings
from backend.schema.catalog.service_schema import NodeTargetBinding
from backend.schema.host_node.execution_schema import ExecutionCommand
from backend.service.host_node.execution_client import NodeExecutionClient
from tests.integration.host_node_support import ADMIN, REPORT, node_settings
from tests.integration.test_node_execution_bridge import EXECUTOR


def binding(**overrides):
    return NodeTargetBinding(node_id="node", service_id="asr", config_revision="a"*64,
        base_url="http://127.0.0.1:8020", executor_token_env="TEST_TOKEN", **overrides)


@pytest.mark.parametrize("status", [301, 404, 429, 500, 503])
def test_non_success_does_not_follow_redirect_retry_or_infer_terminal(status):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location":"http://untrusted.invalid/SECRET"}, text="RAW_SECRET_RESPONSE")
    client = NodeExecutionClient(binding(), environ={"TEST_TOKEN": EXECUTOR}, transport=httpx.MockTransport(handle))
    with pytest.raises(AppError) as error: client.availability()
    assert len(calls) == 1 and error.value.code == "NODE_RESPONSE_UNCONFIRMED"
    assert "RAW_SECRET_RESPONSE" not in str(error.value) and EXECUTOR not in str(error.value)


@pytest.mark.parametrize("response", [httpx.Response(200, content=b"x"*(3*1024*1024+1)),
    httpx.Response(200, text="not json"), httpx.Response(200, headers={"content-encoding":"br"}, content=b"")])
def test_oversize_invalid_and_encoded_response_are_not_credible(response):
    client = NodeExecutionClient(binding(), environ={"TEST_TOKEN": EXECUTOR}, transport=httpx.MockTransport(lambda _: response))
    with pytest.raises(AppError): client.availability()


def test_missing_token_never_opens_connection():
    with pytest.raises(AppError): NodeExecutionClient(binding(), environ={})


@pytest.mark.parametrize("value", [ADMIN, REPORT, "short", "x"*31+" "])
def test_executor_token_is_independent_and_bounded(tmp_path, value):
    with pytest.raises(ValidationError): node_settings(tmp_path, executor_token=value)


def test_executor_env_is_loaded_but_not_serialized():
    settings = load_node_settings(environ={"NODE_ID":"node", "NODE_ADMIN_TOKEN": ADMIN, "NODE_EXECUTOR_TOKEN":EXECUTOR})
    assert settings.executor_token.get_secret_value() == EXECUTOR
    assert EXECUTOR not in settings.model_dump_json() + repr(settings)


def test_unsafe_node_origins_are_rejected():
    for url in ("http://user:pass@host", "http://host/path", "http://host/?key=SECRET", "file:///tmp"):
        with pytest.raises(ValueError): NodeTargetBinding.model_validate({**binding().model_dump(), "base_url":url})
