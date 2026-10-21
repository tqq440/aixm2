"""固定 Docker API 与业务 HTTP 契约替身，不连接 Docker。"""

import json
from uuid import uuid4

import httpx2 as httpx
import pytest

from backend.common.containers.docker_engine import DockerEngine, DockerFailure
from backend.schema.host_node.control_schema import RuntimeConfig
from backend.service.host_node.business_control_client import BusinessControlClient
from backend.service.host_node.control_client import NodeControlClient
from tests.integration.host_control_support import FIRST, runtime_config
from tests.integration.host_node_support import REPORT, node_settings


class Response:
    def __init__(self, status, body): self.status = status; self.body = body
    def read(self, maximum): return self.body[:maximum]


class Connection:
    def __init__(self, responses): self.responses = iter(responses); self.calls = []; self.closed = 0
    def request(self, method, path, body, headers): self.calls.append((method, path, body, headers))
    def getresponse(self): return next(self.responses)
    def close(self): self.closed += 1


def test_docker_driver_has_only_fixed_inspect_start_and_sigterm_paths(tmp_path):
    raw = {"Id": FIRST, "Image": "sha256:" + "a" * 64, "State": {"Status": "running", "Running": True},
           "Config": {"Labels": {"inference.node_id": "node", "private": "PRIVATE_LABEL"}, "Env": ["TOKEN=DO_NOT_EXPORT"]},
           "HostConfig": {"RestartPolicy": {"Name": "no"}, "AutoRemove": False}}
    connection = Connection([Response(200, json.dumps(raw).encode()), Response(204, b""), Response(204, b"")])
    driver = DockerEngine(node_settings(tmp_path, docker_enabled=True), connection_factory=lambda *_: connection)
    result = driver.inspect(FIRST); driver.start(FIRST); driver.terminate(FIRST)
    assert [(method, path) for method, path, *_ in connection.calls] == [
        ("GET", f"/v1.47/containers/{FIRST}/json"), ("POST", f"/v1.47/containers/{FIRST}/start"),
        ("POST", f"/v1.47/containers/{FIRST}/kill?signal=SIGTERM")]
    assert "DO_NOT_EXPORT" not in repr(result) and "PRIVATE_LABEL" not in repr(result)
    assert connection.closed == 3


@pytest.mark.parametrize("response,expected", [
    (Response(404, b"private-error"), "DOCKER_NOT_FOUND"),
    (Response(500, b"private-error"), "DOCKER_API_ERROR"),
    (Response(200, b"x" * 65537), "DOCKER_RESPONSE_TOO_LARGE"),
    (Response(200, b"bad-json-private-error"), "DOCKER_QUERY_FAILED"),
    (Response(200, b"{}"), "DOCKER_RESPONSE_INVALID"),
])
def test_docker_error_bodies_are_not_returned(tmp_path, response, expected):
    connection = Connection([response])
    driver = DockerEngine(node_settings(tmp_path, docker_enabled=True), connection_factory=lambda *_: connection)
    with pytest.raises(DockerFailure) as caught: driver.inspect(FIRST)
    assert caught.value.code == expected and "private-error" not in str(caught.value)


def test_docker_is_disabled_by_default_and_cannot_accept_arbitrary_id_paths(tmp_path):
    driver = DockerEngine(node_settings(tmp_path))
    with pytest.raises(DockerFailure, match="DOCKER_DISABLED"): driver.start(FIRST)
    driver = DockerEngine(node_settings(tmp_path, docker_enabled=True), connection_factory=lambda *_: None)
    with pytest.raises(DockerFailure, match="DOCKER_ID_INVALID"): driver.start("../other")


@pytest.mark.parametrize("mutation", [
    {"versions": [{"versionId": "latest", "containerId": "asr; rm x", "imageId": "asr:latest", "compatibilityEvidence": "x"}]},
    {"drainPath": "http://other/service"}, {"drainPath": "/safe/../bad"},
    {"drainPath": None}, {"rollbackCompatibilityEvidence": None}, {"shellCommand": "start-anything"},
])
def test_runtime_input_rejects_dynamic_commands_and_unsafe_control_paths(mutation):
    with pytest.raises(ValueError): RuntimeConfig.model_validate(runtime_config(**mutation))


@pytest.mark.parametrize("mode", ["ok", "redirect", "wrong-id", "large", "invalid", "compressed"])
def test_business_ack_is_bounded_and_matches_exact_operation_without_redirects(tmp_path, mode):
    settings = node_settings(tmp_path)
    command = {"operationId": str(uuid4()), "instanceId": str(uuid4()), "configRevision": "a" * 64, "action": "drain"}
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer " + REPORT
        assert request.url.path == "/internal/drain"
        if mode == "redirect": return httpx.Response(302, headers={"Location": "http://other/private"})
        if mode == "large": return httpx.Response(200, content=b"x" * 16385)
        if mode == "invalid": return httpx.Response(200, content=b"private-invalid-response")
        if mode == "compressed": return httpx.Response(200, content=b"raw", headers={"content-encoding": "unknown"})
        return httpx.Response(200, json={**command, "operationId": str(uuid4()) if mode == "wrong-id" else command["operationId"], "accepted": True})
    client = BusinessControlClient(settings, transport=httpx.MockTransport(handler))
    config = {"internalBaseUrl": "http://127.0.0.1:1234", "reportTokenEnv": "NODE_REPORT_TOKEN_ASR"}
    if mode == "ok": client.send(config, runtime_config(), "drain", command)
    else:
        with pytest.raises(DockerFailure) as caught: client.send(config, runtime_config(), "drain", command)
        assert REPORT not in str(caught.value) and "private-invalid-response" not in str(caught.value)
    assert len(calls) == 1


def test_business_control_obeys_total_deadline_even_when_bytes_keep_arriving(tmp_path, monkeypatch):
    clock = iter([0, 10])
    monkeypatch.setattr("backend.service.host_node.business_control_client.monotonic", lambda: next(clock))
    command = {"operationId": str(uuid4()), "instanceId": str(uuid4()), "configRevision": "a" * 64, "action": "drain"}
    client = BusinessControlClient(node_settings(tmp_path), transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, json={**command, "accepted": True})))
    with pytest.raises(DockerFailure, match="BUSINESS_CONTROL_UNKNOWN"):
        client.send({"internalBaseUrl": "http://127.0.0.1:1234", "reportTokenEnv": "NODE_REPORT_TOKEN_ASR"},
                    runtime_config(), "drain", command)


@pytest.mark.parametrize("url", ["http://external-node", "http://user:secret@127.0.0.1", "https://node/path", "https://node?token=x"])
def test_control_client_rejects_plaintext_remote_or_credential_urls(tmp_path, url):
    from backend.common.exceptions.app_error import AppError
    with pytest.raises(AppError): NodeControlClient(node_settings(tmp_path), url)
