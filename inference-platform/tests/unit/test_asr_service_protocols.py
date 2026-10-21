"""统一 ASR 接口的隔离协议测试；不访问真实服务或证明模型推理能力。"""

import asyncio
import wave

import httpx2 as httpx
import pytest
from pydantic import ValidationError

from backend.common.exceptions.app_error import AppError, ConfigurationError
from backend.config.settings.app_settings import (
    AppSettings, asr_service_origin, load_settings, remote_asr_origin, resolve_remote_asr_api_key,
)
from backend.schema.asr.asr_schema import TranscriptionRequest
from backend.service.asr.remote_catalog import config_fingerprint, remote_asr_model_version, remote_result_matches
from backend.service.asr.remote_v2_adapter import RemoteV2AsrAdapter, probe_remote_asr
from backend.utils.json.json_utils import canonical_json_sha256


def settings(**changes):
    return AppSettings(**{
        "remote_asr_base_url": "http://127.0.0.1:8013",
        "remote_asr_model_id": "manual-asr",
        "remote_asr_protocol": "openai-audio",
        "remote_asr_upstream_model": "service/model-1",
        "remote_asr_transcribe_path": "/v1/audio/transcriptions",
        "remote_asr_health_path": "/v1/models",
        **changes,
    })


@pytest.fixture
def audio_request(tmp_path):
    path = tmp_path / "isolated.audio"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 16000)
    return TranscriptionRequest(audio_path=path, language=None, request_id="protocol-fixture")


def test_http_and_https_use_same_origin_contract_but_tls_exception_remains_https_only():
    assert asr_service_origin("http://LOCALHOST:80/") == "http://localhost"
    assert asr_service_origin("https://LOCALHOST:443/") == "https://localhost"
    assert asr_service_origin("http://[::1]:8013") == "http://[::1]:8013"
    assert settings().remote_asr_base_url == "http://127.0.0.1:8013"
    with pytest.raises(ValueError):
        remote_asr_origin("http://127.0.0.1:8013")
    with pytest.raises(ValidationError):
        settings(remote_asr_insecure_tls_origin="https://127.0.0.1:8013")


@pytest.mark.parametrize("origin", [
    "ftp://localhost", "http://user:password@localhost", "http://localhost:0",
    "http://localhost:99999", "http://localhost:", "http://localhost/service",
    "http://localhost?", "http://localhost#", "http://*.example.test", "http://local%host",
    "http://localhost\\path", "http://localhost\n",
])
def test_invalid_service_origins_are_rejected(origin):
    with pytest.raises(ValidationError):
        settings(remote_asr_base_url=origin)


@pytest.mark.parametrize("path", [
    "v1/models", "//other.test/v1/models", "https://other.test/v1/models",
    "/v1/models?key=secret", "/v1/models#fragment", "/v1/../models", "/v1/./models",
    "/%2e%2e/models", "/v1\\models", "/v1/models\n",
])
def test_paths_cannot_override_origin_or_smuggle_query_credentials(path):
    with pytest.raises(ValidationError):
        settings(remote_asr_health_path=path)


def test_openai_protocol_requires_upstream_model_and_valid_environment_name():
    for changes in ({"remote_asr_upstream_model": None}, {"remote_asr_upstream_model": " \n"},
                    {"remote_asr_api_key_env": "ASR-SECRET"}, {"remote_asr_api_key_env": "key=secret"}):
        with pytest.raises(ValidationError):
            settings(**changes)
    with pytest.raises(ValidationError):
        AppSettings(remote_asr_api_key_env="ASR_API_KEY")


def test_new_fields_load_from_environment_without_embedding_secret():
    result = load_settings(None, environ={
        "INFERENCE_REMOTE_ASR_BASE_URL": "http://localhost:8000",
        "INFERENCE_REMOTE_ASR_MODEL_ID": "manual-asr",
        "INFERENCE_REMOTE_ASR_PROTOCOL": "openai-audio",
        "INFERENCE_REMOTE_ASR_TRANSCRIBE_PATH": "/v1/audio/transcriptions",
        "INFERENCE_REMOTE_ASR_HEALTH_PATH": "/v1/models",
        "INFERENCE_REMOTE_ASR_UPSTREAM_MODEL": "whisper-1",
        "INFERENCE_REMOTE_ASR_API_KEY_ENV": "ASR_API_KEY",
        "ASR_API_KEY": "not-copied-to-settings",
    })
    assert result.remote_asr_protocol == "openai-audio"
    assert result.remote_asr_upstream_model == "whisper-1"
    assert "not-copied-to-settings" not in result.model_dump_json()


def test_openai_health_and_audio_use_configured_paths_bearer_and_multipart(audio_request, monkeypatch):
    config = settings(remote_asr_api_key_env="ASR_PROTOCOL_TEST_KEY")
    monkeypatch.setenv("ASR_PROTOCOL_TEST_KEY", "fixture-bearer-secret")
    seen = []

    def respond(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer fixture-bearer-secret"
        if request.method == "GET":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "service/model-1"}]})
        return httpx.Response(200, json={"text": "真实字段形状的隔离响应。"})

    adapter = RemoteV2AsrAdapter(config, transport=httpx.MockTransport(respond))
    adapter.prepare_model()
    result = adapter.transcribe(audio_request)
    assert [request.url.path for request in seen] == ["/v1/models", "/v1/audio/transcriptions"]
    assert all(request.url.host == "127.0.0.1" for request in seen)
    for part in (b'name="file"; filename="audio.wav"', b'name="model"', b"service/model-1",
                 b'name="response_format"', b"json"):
        assert part in seen[1].content
    assert b'name="audio"' not in seen[1].content and b'name="request_id"' not in seen[1].content
    assert result.text == "真实字段形状的隔离响应。" and result.segments == []
    assert result.language is None and result.device is None and result.model_revision is None
    assert result.engine_metadata.runtime_versions == {}
    assert remote_result_matches(config, result)
    assert "fixture-bearer-secret" not in result.model_dump_json()


def test_openai_supplied_segments_are_preserved_without_fabricating_language(audio_request):
    body = {"text": "一个片段", "language": "Chinese", "duration": 123,
            "segments": [{"start": 0.1, "end": 0.5, "text": "一个片段", "id": 0}]}
    result = RemoteV2AsrAdapter(settings(), transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=body))).transcribe(audio_request)
    assert [(segment.start, segment.end, segment.text) for segment in result.segments] == [(0.1, 0.5, "一个片段")]
    assert result.audio_duration_seconds == 1 and result.language is None


@pytest.mark.parametrize("body,expected", [
    ({"data": [{"id": "different-model"}]}, "REMOTE_ASR_MODEL_NOT_FOUND"),
    ({"data": []}, "REMOTE_ASR_MODEL_NOT_FOUND"),
    ({"data": [{"id": 3}]}, "REMOTE_ASR_PROTOCOL_INVALID"),
    ({"data": "wrong"}, "REMOTE_ASR_PROTOCOL_INVALID"),
    ({}, "REMOTE_ASR_PROTOCOL_INVALID"),
    ({"data": [{"model": "service/model-1"}]}, "REMOTE_ASR_PROTOCOL_INVALID"),
])
def test_openai_models_probe_requires_the_configured_model(body, expected):
    health = probe_remote_asr(settings(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    assert not health.ready and health.error_code == expected


@pytest.mark.parametrize("status,expected", [
    (401, "REMOTE_ASR_AUTH_FAILED"), (403, "REMOTE_ASR_AUTH_FAILED"),
    (404, "REMOTE_ASR_PATH_NOT_FOUND"), (500, "REMOTE_ASR_HTTP_ERROR"),
    (503, "REMOTE_ASR_HTTP_ERROR"), (302, "REMOTE_ASR_HTTP_ERROR"),
])
def test_probe_http_failure_is_actionable_without_upstream_text_or_redirects(status, expected):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(status, text="private-upstream-secret",
                              headers={"Location": "https://another.test/stolen"})

    health = probe_remote_asr(settings(), transport=httpx.MockTransport(respond))
    assert not health.ready and health.error_code == expected and len(seen) == 1
    assert "private" not in health.model_dump_json() and "another.test" not in health.model_dump_json()


@pytest.mark.parametrize("exception,expected", [
    (httpx.ConnectError, "REMOTE_ASR_CONNECTION_FAILED"),
    (httpx.ConnectTimeout, "REMOTE_ASR_TIMEOUT"), (httpx.ReadTimeout, "REMOTE_ASR_TIMEOUT"),
    (httpx.ReadError, "REMOTE_ASR_NETWORK_ERROR"),
])
def test_probe_network_failure_is_distinguished_without_private_details(exception, expected):
    def respond(_):
        raise exception("private-host-secret")

    health = probe_remote_asr(settings(), transport=httpx.MockTransport(respond))
    assert not health.ready and health.error_code == expected
    assert "private" not in health.model_dump_json()


def test_probe_rejects_non_json_response_as_protocol_mismatch():
    health = probe_remote_asr(settings(), transport=httpx.MockTransport(
        lambda _: httpx.Response(200, text="<html>private-server-error</html>")))
    assert health.error_code == "REMOTE_ASR_PROTOCOL_INVALID" and not health.ready
    assert "private" not in health.model_dump_json()


@pytest.mark.parametrize("secret", [None, "", "  ", "private\nsecret", "private secret", "汉字"])
def test_missing_or_invalid_auth_never_sends_any_request(audio_request, monkeypatch, secret):
    config = settings(remote_asr_api_key_env="ASR_PROTOCOL_TEST_KEY")
    if secret is None:
        monkeypatch.delenv("ASR_PROTOCOL_TEST_KEY", raising=False)
    else:
        monkeypatch.setenv("ASR_PROTOCOL_TEST_KEY", secret)
    with pytest.raises(ConfigurationError):
        resolve_remote_asr_api_key(config)

    def unexpected(_):
        pytest.fail("鉴权不可用时请求不得发送")

    transport = httpx.MockTransport(unexpected)
    health = probe_remote_asr(config, transport=transport)
    assert health.error_code == "REMOTE_ASR_AUTH_NOT_CONFIGURED" and not health.ready
    with pytest.raises(AppError) as caught:
        RemoteV2AsrAdapter(config, transport=transport).transcribe(audio_request)
    assert caught.value.code == "REMOTE_ASR_NOT_SENT"
    assert "private" not in caught.value.message and "private" not in health.error_message


@pytest.mark.parametrize("body", [
    {}, {"text": None}, {"text": 123}, {"text": "ok", "segments": None},
    {"text": "ok", "segments": [{"start": 0, "end": 2, "text": "out-of-range"}]},
    {"text": "ok", "segments": [{"start": 0.4, "end": 0.3, "text": "reversed"}]},
    {"text": "ok", "segments": [{"start": 0.6, "end": 0.9, "text": "late"},
                               {"start": 0.1, "end": 0.2, "text": "early"}]},
    {"text": "x" * 131_073},
])
def test_malformed_openai_success_is_unknown_and_never_retried(audio_request, body):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=body)

    with pytest.raises(AppError) as caught:
        RemoteV2AsrAdapter(settings(), transport=httpx.MockTransport(respond)).transcribe(audio_request)
    assert caught.value.code == "REMOTE_ASR_UNKNOWN" and len(seen) == 1


@pytest.mark.parametrize("status,expected", [
    (400, "REMOTE_ASR_REJECTED"), (401, "REMOTE_ASR_REJECTED"), (403, "REMOTE_ASR_REJECTED"),
    (413, "REMOTE_ASR_REJECTED"), (429, "REMOTE_ASR_REJECTED"),
    (302, "REMOTE_ASR_UNKNOWN"), (307, "REMOTE_ASR_UNKNOWN"),
    (202, "REMOTE_ASR_UNKNOWN"), (500, "REMOTE_ASR_UNKNOWN"), (503, "REMOTE_ASR_UNKNOWN"),
])
def test_openai_errors_are_sanitized_no_redirect_or_retry(audio_request, status, expected):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(status, json={"error": {"message": "private upstream secret"}},
                              headers={"Location": "https://another.test/stolen"})

    with pytest.raises(AppError) as caught:
        RemoteV2AsrAdapter(settings(), transport=httpx.MockTransport(respond)).transcribe(audio_request)
    assert caught.value.code == expected and len(seen) == 1
    assert "private" not in caught.value.message and "another.test" not in caught.value.message


def test_openai_total_timeout_is_unknown_and_transport_is_closed(audio_request):
    closed = []

    class SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await asyncio.sleep(1)
            return httpx.Response(200, json={"text": "late"})

        async def aclose(self):
            closed.append(True)

    with pytest.raises(AppError) as caught:
        RemoteV2AsrAdapter(settings(remote_asr_total_timeout_seconds=0.1),
                           transport=SlowTransport()).transcribe(audio_request)
    assert caught.value.code == "REMOTE_ASR_UNKNOWN" and closed == [True]


@pytest.mark.parametrize("headers,content", [
    ({}, b"x" * (4 * 1024 * 1024 + 1)), ({"Content-Encoding": "gzip"}, b"compressed"),
], ids=["oversized-response", "compressed-response"])
def test_openai_response_size_and_compression_are_bounded(audio_request, headers, content):
    with pytest.raises(AppError) as caught:
        RemoteV2AsrAdapter(settings(), transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=content, headers=headers))).transcribe(audio_request)
    assert caught.value.code == "REMOTE_ASR_UNKNOWN"


def test_remote_v2_custom_http_paths_preserve_its_wire_protocol(audio_request):
    config = settings(remote_asr_protocol="remote-v2", remote_asr_upstream_model=None,
                      remote_asr_health_path="/healthz", remote_asr_transcribe_path="/transcribe")
    seen = []

    def respond(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"success": True, "data": {"status": "ok", "model_loaded": True}})
        return httpx.Response(200, json={"success": True, "engine": "sensevoice",
                                       "details": {"request_id": "protocol-fixture", "segments": []}})

    adapter = RemoteV2AsrAdapter(config, transport=httpx.MockTransport(respond))
    adapter.prepare_model()
    assert adapter.transcribe(audio_request).text == ""
    assert [request.url.path for request in seen] == ["/healthz", "/transcribe"]
    assert b'name="audio"' in seen[1].content and b"sensevoice" in seen[1].content


def test_default_legacy_unknown_and_known_fingerprints_remain_byte_identical():
    config = AppSettings(remote_asr_base_url="https://isolated.test:8003", remote_asr_model_id="fixture")
    binding = {"revisionScheme": "remote-v2-sync-integration-v1", "adapterKey": "remote-v2-sensevoice",
               "baseUrl": "https://isolated.test:8003", "modelId": "fixture",
               "caFile": None, "insecureTlsOrigin": None}
    version = "integration-" + canonical_json_sha256(binding)[:48]
    assert remote_asr_model_version(config) == version
    common = {"profile": "remote-v2-sensevoice", "baseUrl": "https://isolated.test:8003",
              "modelId": "fixture", "caFile": None}
    assert config_fingerprint(config) == canonical_json_sha256({
        **common, "modelVersion": version, "versionBasis": "integration_revision"})
    known = config.model_copy(update={"remote_asr_model_version": "release-1"})
    assert config_fingerprint(known) == canonical_json_sha256({**common, "modelVersion": "release-1"})


@pytest.mark.parametrize("changes", [
    {"remote_asr_protocol": "remote-v2"}, {"remote_asr_transcribe_path": "/transcribe"},
    {"remote_asr_health_path": "/models"}, {"remote_asr_upstream_model": "other-model"},
    {"remote_asr_api_key_env": "ASR_API_KEY"},
])
def test_protocol_and_auth_reference_change_new_identity(changes):
    first, second = settings(), settings(**changes)
    assert config_fingerprint(first) != config_fingerprint(second)
    assert remote_asr_model_version(first) != remote_asr_model_version(second)
    assert config_fingerprint(first.model_copy(update={"remote_asr_model_version": "known"})) != config_fingerprint(
        second.model_copy(update={"remote_asr_model_version": "known"}))


def test_auth_value_and_service_label_do_not_leak_into_or_rewrite_identity(monkeypatch):
    config = settings(remote_asr_api_key_env="ASR_PROTOCOL_TEST_KEY")
    before = config_fingerprint(config)
    monkeypatch.setenv("ASR_PROTOCOL_TEST_KEY", "first-secret")
    assert resolve_remote_asr_api_key(config) == "first-secret"
    monkeypatch.setenv("ASR_PROTOCOL_TEST_KEY", "second-secret")
    assert config_fingerprint(config) == before
    renamed = config.model_copy(update={"asr_service_id": "renamed-label", "asr_service_revision": "f" * 64})
    assert config_fingerprint(renamed) == before
    assert "second-secret" not in config.model_dump_json()
