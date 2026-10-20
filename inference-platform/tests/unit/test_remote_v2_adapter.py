"""使用隔离 transport 验证协议，不代表任何真实 Remote-V2 模型已接通。"""

import asyncio
from io import BytesIO
import ssl
import wave

import httpx2 as httpx
import pytest

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.schema.asr.asr_schema import TranscriptionRequest
from backend.service.asr.adapter_catalog import configured_asr_plugin
from backend.service.asr.remote_v2_adapter import RemoteV2AsrAdapter, _ssl_context, probe_remote_asr


@pytest.fixture
def settings():
    return AppSettings(remote_asr_base_url="https://isolated.test:8003", remote_asr_model_id="fixture-asr",
                       remote_asr_model_version="fixture-deployment-1")


@pytest.fixture
def transcription_request(tmp_path):
    # 与受管上传一致使用 .audio 扩展名，文件实际内容为可解码 WAV。
    path = tmp_path / "input.audio"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\0\0" * 32_000)
    return TranscriptionRequest(audio_path=path, language=None, request_id="fixture-task-id")


def result_body(**changes):
    return {"success": True, "engine": "sensevoice", "data": {"confidence": 9.06},
            "details": {"request_id": "fixture-task-id", "segments": [
                {"start": 0.3, "end": 2.8, "text": "隔离协议测试。", "user": "unknown"}]}, **changes}


def test_unconfigured_health_never_makes_request():
    def unexpected(transcription_request):
        pytest.fail("未配置时不得访问网络")
    health = probe_remote_asr(AppSettings(), transport=httpx.MockTransport(unexpected))
    assert not health.configured and not health.ready
    assert health.error_code == "REMOTE_ASR_NOT_CONFIGURED"


@pytest.mark.parametrize("data", [
    {"status": "loading", "model_loaded": True}, {"status": "ok", "model_loaded": False},
    {"status": "ok", "model_loaded": "true"}, {"status": "ok"},
])
def test_health_requires_explicit_readiness(settings, data):
    adapter = RemoteV2AsrAdapter(settings, transport=httpx.MockTransport(
        lambda transcription_request: httpx.Response(200, json={"success": True, "data": data})))
    with pytest.raises(AppError) as caught:
        adapter.prepare_model()
    assert caught.value.code == "REMOTE_ASR_UNAVAILABLE"


def test_health_and_transcription_send_only_fixed_protocol_and_keep_unknown_facts_absent(settings, transcription_request):
    seen = []
    def handler(http_request):
        seen.append(http_request)
        if http_request.url.path.endswith("/health"):
            return httpx.Response(200, json={"success": True, "data": {"status": "ok", "model_loaded": True, "version": "14.2"}})
        return httpx.Response(200, json=result_body())
    adapter = RemoteV2AsrAdapter(settings, transport=httpx.MockTransport(handler))
    assert adapter.prepare_model() >= 0
    result = adapter.transcribe(transcription_request)
    assert [item.url.path for item in seen] == ["/api/v1/asr/health", "/api/v1/asr/transcripts"]
    upload = seen[-1].content
    for value in (b'name="audio"; filename="audio.wav"', b'name="outputs"', b"transcript", b"sensevoice", b"fixture-task-id"):
        assert value in upload
    assert b'name="language"' not in upload and b'name="device_id"' not in upload
    wav_start = upload.index(b"RIFF")
    wave_size = int.from_bytes(upload[wav_start + 4:wav_start + 8], "little") + 8
    with wave.open(BytesIO(upload[wav_start:wav_start + wave_size]), "rb") as audio:
        assert audio.getframerate() == 16000 and audio.getnchannels() == 1 and audio.getsampwidth() == 2
        assert audio.getnframes() == 64_000
    assert result.audio_duration_seconds == 4 and result.text == "隔离协议测试。"
    assert result.language is None and result.language_probability is None and result.language_requested is None
    assert result.model_revision is None and result.device is None and result.faster_whisper_version is None
    assert result.engine_metadata.runtime_versions == {"remote-v2-api": "14.2"}
    assert result.engine_metadata.model_revision is None
    assert configured_asr_plugin(settings, "remote").result_matches(settings, result)


def test_valid_empty_segments_is_an_empty_transcript(settings, transcription_request):
    response = result_body(details={"segments": []})
    result = RemoteV2AsrAdapter(settings, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))).transcribe(transcription_request)
    assert result.text == "" and result.segments == [] and result.audio_duration_seconds == 4


@pytest.mark.parametrize("change", [
    {"request_id": None}, {"language": "zh"}, {"audio_path": "missing-file"},
])
def test_local_validation_failure_proves_post_not_sent(settings, transcription_request, change):
    def unexpected(_):
        pytest.fail("本地校验失败不得发送请求")
    adapter = RemoteV2AsrAdapter(settings, transport=httpx.MockTransport(unexpected))
    with pytest.raises(AppError) as caught:
        adapter.transcribe(transcription_request.model_copy(update=change))
    assert caught.value.code == "REMOTE_ASR_NOT_SENT"


@pytest.mark.parametrize("status,body,expected", [
    (400, {"detail": "private upstream error"}, "REMOTE_ASR_REJECTED"),
    (413, {"detail": "private upstream error"}, "REMOTE_ASR_REJECTED"),
    (503, {"detail": "Service not ready"}, "REMOTE_ASR_REJECTED"),
    (503, {"detail": "gateway lost backend"}, "REMOTE_ASR_UNKNOWN"),
    (500, {"detail": "private upstream error"}, "REMOTE_ASR_UNKNOWN"),
    (400, {"error": "bad format"}, "REMOTE_ASR_UNKNOWN"),
    (302, {}, "REMOTE_ASR_UNKNOWN"),
    (202, {"success": True}, "REMOTE_ASR_UNKNOWN"),
])
def test_error_body_is_not_exposed_and_no_retry_or_redirect(settings, transcription_request, status, body, expected):
    calls = []
    def handler(_):
        calls.append(1)
        return httpx.Response(status, json=body, headers={"Location": "https://other.test"})
    with pytest.raises(AppError) as caught:
        RemoteV2AsrAdapter(settings, transport=httpx.MockTransport(handler)).transcribe(transcription_request)
    assert caught.value.code == expected and calls == [1]
    assert "private" not in caught.value.message and "other.test" not in caught.value.message


@pytest.mark.parametrize("body", [
    result_body(success=False), result_body(success=1), result_body(engine="paraformer"),
    result_body(details={"request_id": "different", "segments": []}),
    result_body(details={"segments": [{"start": 2, "end": 1, "text": "invalid"}]}),
    result_body(details={"segments": [{"start": 0, "end": 5, "text": "past duration"}]}),
    result_body(details={"segments": [{"start": 0, "end": 1, "text": 123}]}),
    result_body(details={"segments": [{"start": 0, "end": 1}]}),
    result_body(details={"segments": [{"start": 1, "end": 2, "text": "1"}, {"start": 0, "end": 1, "text": "2"}]}),
])
def test_malformed_result_cannot_become_success_or_release_unknown_work(settings, transcription_request, body):
    with pytest.raises(AppError) as caught:
        RemoteV2AsrAdapter(settings, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))).transcribe(transcription_request)
    assert caught.value.code == "REMOTE_ASR_UNKNOWN"


@pytest.mark.parametrize("exception,expected", [
    (httpx.ConnectError, "REMOTE_ASR_NOT_SENT"), (httpx.ConnectTimeout, "REMOTE_ASR_NOT_SENT"),
    (httpx.ReadTimeout, "REMOTE_ASR_UNKNOWN"), (httpx.ReadError, "REMOTE_ASR_UNKNOWN"),
    (httpx.WriteError, "REMOTE_ASR_UNKNOWN"),
])
def test_transport_failure_classification_does_not_expose_private_detail(settings, transcription_request, exception, expected):
    def handler(_):
        raise exception("private-endpoint-or-certificate")
    with pytest.raises(AppError) as caught:
        RemoteV2AsrAdapter(settings, transport=httpx.MockTransport(handler)).transcribe(transcription_request)
    assert caught.value.code == expected and "private" not in caught.value.message


def test_true_total_timeout_cancels_and_closes_transport(settings, transcription_request):
    closed = []
    class SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, http_request):
            await asyncio.sleep(1)
            return httpx.Response(200, json=result_body())
        async def aclose(self):
            closed.append(True)
    settings = settings.model_copy(update={"remote_asr_total_timeout_seconds": 0.1})
    with pytest.raises(AppError) as caught:
        RemoteV2AsrAdapter(settings, transport=SlowTransport()).transcribe(transcription_request)
    assert caught.value.code == "REMOTE_ASR_UNKNOWN" and closed == [True]


def test_response_limit_closes_transport_and_does_not_parse_unbounded_body(settings, transcription_request):
    with pytest.raises(AppError) as caught:
        RemoteV2AsrAdapter(settings, transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1)))).transcribe(transcription_request)
    assert caught.value.code == "REMOTE_ASR_UNKNOWN"


def test_default_ssl_context_verifies_certificate_and_hostname(settings):
    context = _ssl_context(settings)
    assert context.check_hostname is True and context.verify_mode == ssl.CERT_REQUIRED


def test_explicit_origin_exception_applies_to_precheck_health_and_post(settings, transcription_request, monkeypatch):
    settings = AppSettings(**{**settings.model_dump(),
                              "remote_asr_insecure_tls_origin": settings.remote_asr_base_url})
    calls = []
    original_client = httpx.AsyncClient
    def capture_client(**kwargs):
        context = kwargs["verify"]
        assert context.verify_mode == ssl.CERT_NONE and context.check_hostname is False
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        calls.append(context)
        return original_client(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", capture_client)
    def respond(request):
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"success": True, "data": {"status": "ok", "model_loaded": True}})
        return httpx.Response(200, json=result_body())
    adapter = RemoteV2AsrAdapter(settings, transport=httpx.MockTransport(respond))
    adapter.prepare_model()
    assert adapter.transcribe(transcription_request).text == "隔离协议测试。"
    assert len(calls) == 2


@pytest.mark.parametrize("changes", [
    {"remote_asr_base_url": "https://other.test:8003"}, {"remote_asr_model_id": None},
    {"remote_asr_ca_file": "not-a-real-ca.pem"},
])
def test_internal_settings_copy_cannot_reuse_tls_exception_for_another_policy(settings, transcription_request, changes):
    settings = settings.model_copy(update={"remote_asr_insecure_tls_origin": settings.remote_asr_base_url, **changes})
    def unexpected(_):
        pytest.fail("例外失配必须在发送前失败")
    with pytest.raises(ValueError):
        _ssl_context(settings)
    adapter = RemoteV2AsrAdapter(settings, transport=httpx.MockTransport(unexpected))
    with pytest.raises(AppError) as caught:
        adapter.transcribe(transcription_request)
    assert caught.value.code == "REMOTE_ASR_NOT_SENT"
    assert not probe_remote_asr(settings, transport=httpx.MockTransport(unexpected)).ready


def test_tls_exception_does_not_follow_redirect_or_retry(settings, transcription_request):
    settings = settings.model_copy(update={"remote_asr_insecure_tls_origin": settings.remote_asr_base_url})
    calls = []
    def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://another.test/api/v1/asr/transcripts"})
    with pytest.raises(AppError) as caught:
        RemoteV2AsrAdapter(settings, transport=httpx.MockTransport(redirect)).transcribe(transcription_request)
    assert caught.value.code == "REMOTE_ASR_UNKNOWN"
    assert calls == ["https://isolated.test:8003/api/v1/asr/transcripts"]


@pytest.mark.parametrize("skip_tls", [False, True])
def test_unknown_upstream_version_does_not_block_health_or_transcription(settings, transcription_request, skip_tls):
    settings = AppSettings(**{**settings.model_dump(), "remote_asr_model_version": None,
        "remote_asr_insecure_tls_origin": settings.remote_asr_base_url if skip_tls else None})
    seen = []
    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"success": True, "data": {
                "status": "ok", "model_loaded": True, "version": "14.2"}})
        return httpx.Response(200, json=result_body())
    adapter = RemoteV2AsrAdapter(settings, transport=httpx.MockTransport(handler))
    adapter.prepare_model()
    result = adapter.transcribe(transcription_request)
    assert result.text == "隔离协议测试。"
    assert result.engine_metadata.runtime_versions == {"remote-v2-api": "14.2"}
    assert result.engine_metadata.model_revision is None
    assert configured_asr_plugin(settings, "remote").result_matches(settings, result)
    assert seen == ["/api/v1/asr/health", "/api/v1/asr/transcripts"]
