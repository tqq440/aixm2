"""独立 HTTP 服务只用模型替身；验证上传、并发、清理和有界解码。"""

from io import BytesIO
from threading import Event, Thread
from unittest.mock import Mock
import wave

import pytest
from werkzeug.datastructures import MultiDict

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.modules.asr.http_server import create_app, main
from backend.schema.asr.asr_schema import TranscriptionResult, TranscriptionSegment
from backend.service.asr.asr_service import AsrService


@pytest.fixture
def adapter():
    fake = Mock()
    fake.transcribe.return_value = TranscriptionResult(
        text="你好", segments=[TranscriptionSegment(start=0, end=0.5, text="你好")],
        audio_duration_seconds=1, language="zh")
    return fake


@pytest.fixture
def app(tmp_path, adapter):
    application = create_app(AppSettings(database_path=tmp_path / "isolated.sqlite3"), adapter=adapter)
    application.config["TESTING"] = True
    return application


def test_construct_and_health_never_load_model(app, adapter):
    client = app.test_client()
    assert client.get("/health").status_code == 503
    assert client.get("/v1/models").status_code == 503
    adapter.prepare_model.assert_not_called()
    app.extensions["asr_http_service"].prepare_model()
    assert client.get("/health").json == {
        "status": "ok", "model_loaded": True, "model": "whisper-small", "busy": False}
    assert client.get("/v1/models").json["data"][0]["id"] == "whisper-small"
    adapter.prepare_model.assert_called_once()


def test_transcribe_round_trip_ignores_untrusted_filename_and_cleans(app, adapter):
    service = app.extensions["asr_http_service"]
    service.prepare_model()
    seen = []

    def transcribe(command):
        assert command.audio_path.read_bytes() == b"audio"
        assert command.language is None
        seen.append(command.audio_path)
        return TranscriptionResult(text="你好", segments=[], audio_duration_seconds=1)

    adapter.transcribe.side_effect = transcribe
    response = app.test_client().post("/v1/audio/transcriptions", data={
        "model": "whisper-small", "response_format": "verbose_json", "language": "auto",
        "file": (BytesIO(b"audio"), "../../outside.wav"),
    })
    assert response.status_code == 200
    assert response.json["text"] == "你好"
    assert seen and not seen[0].exists()
    assert list(service.upload_root.iterdir()) == []


@pytest.mark.parametrize("changes", [
    {"model": "another-model"}, {"model": ""}, {"response_format": "text"},
    {"language": "xxx"}, {"prompt": "not supported"}, {"language": "zh_CN"},
])
def test_invalid_form_rejected_before_inference(app, adapter, changes):
    app.extensions["asr_http_service"].prepare_model()
    data = {"model": "whisper-small", "file": (BytesIO(b"audio"), "a.wav"), **changes}
    assert app.test_client().post("/v1/audio/transcriptions", data=data).status_code == 422
    adapter.transcribe.assert_not_called()


@pytest.mark.parametrize("data", [
    {"model": "whisper-small"},
    MultiDict([("model", "whisper-small"), ("model", "whisper-small"), ("file", (BytesIO(b"a"), "a.wav"))]),
    MultiDict([("model", "whisper-small"), ("file", (BytesIO(b"a"), "a.wav")), ("file", (BytesIO(b"b"), "b.wav"))]),
])
def test_missing_or_duplicate_fields_rejected(app, adapter, data):
    app.extensions["asr_http_service"].prepare_model()
    response = app.test_client().post("/v1/audio/transcriptions", data=data, content_type="multipart/form-data")
    assert response.status_code == 422
    adapter.transcribe.assert_not_called()


@pytest.mark.parametrize("body, expected", [(b"", 422), (b"12345", 413), (b"1234", 200)])
def test_stream_size_bound_and_empty_cleanup(tmp_path, adapter, body, expected):
    app = create_app(AppSettings(database_path=tmp_path / "db.sqlite3", asr_max_file_bytes=4), adapter=adapter)
    service = app.extensions["asr_http_service"]
    service.prepare_model()
    response = app.test_client().post("/v1/audio/transcriptions", data={
        "model": "whisper-small", "file": (BytesIO(body), "a.wav")})
    assert response.status_code == expected
    assert list(service.upload_root.iterdir()) == []
    assert adapter.transcribe.call_count == (1 if expected == 200 else 0)


def test_total_body_limit_rejects_before_inference(app, adapter):
    service = app.extensions["asr_http_service"]
    service.prepare_model()
    app.config["MAX_CONTENT_LENGTH"] = 20
    response = app.test_client().post("/v1/audio/transcriptions", data={
        "model": "whisper-small", "file": (BytesIO(b"audio"), "a.wav")})
    assert response.status_code == 413
    assert not service.upload_root.exists()
    adapter.transcribe.assert_not_called()


def test_multipart_payload_larger_than_parser_buffer_stays_supported_and_cleans(app, adapter):
    service = app.extensions["asr_http_service"]
    service.prepare_model()
    response = app.test_client().post("/v1/audio/transcriptions", data={
        "model": "whisper-small", "file": (BytesIO(b"x" * 1_000_000), "a.wav")})
    assert response.status_code == 200
    assert list(service.upload_root.iterdir()) == []


def test_wrong_content_type_and_not_ready_never_parse_audio(app, adapter):
    response = app.test_client().post("/v1/audio/transcriptions", data=b"malformed")
    assert response.status_code == 503
    app.extensions["asr_http_service"].prepare_model()
    response = app.test_client().post("/v1/audio/transcriptions", json={"model": "whisper-small"})
    assert response.status_code == 415
    adapter.transcribe.assert_not_called()


@pytest.mark.parametrize("failure, expected", [
    (AppError("AUDIO_TOO_LONG", "超过 300 秒", 413), 413),
    (RuntimeError("private path must not be returned"), 500),
])
def test_failure_cleans_upload_and_releases_slot(app, adapter, failure, expected):
    service = app.extensions["asr_http_service"]
    service.prepare_model()
    adapter.transcribe.side_effect = failure
    response = app.test_client().post("/v1/audio/transcriptions", data={
        "model": "whisper-small", "file": (BytesIO(b"audio"), "a.wav")})
    assert response.status_code == expected
    assert "private path" not in response.get_data(as_text=True)
    assert list(service.upload_root.iterdir()) == []
    assert service.health()["busy"] is False


def test_concurrent_request_rejected_without_parsing_or_inference(app, adapter):
    service = app.extensions["asr_http_service"]
    service.prepare_model()
    entered, release = Event(), Event()
    result = adapter.transcribe.return_value
    results = []

    def blocked(command):
        entered.set()
        assert release.wait(5)
        return result

    def send_first():
        with app.test_client() as client:
            results.append(client.post("/v1/audio/transcriptions", data={
                "model": "whisper-small", "file": (BytesIO(b"audio"), "a.wav")}).status_code)

    adapter.transcribe.side_effect = blocked
    thread = Thread(target=send_first)
    thread.start()
    try:
        assert entered.wait(5)
        response = app.test_client().post("/v1/audio/transcriptions", data=b"malformed")
        assert response.status_code == 503
        assert response.json["error"]["code"] == "ASR_SERVICE_BUSY"
        assert adapter.transcribe.call_count == 1
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert results == [200]
    assert list(service.upload_root.iterdir()) == []


def test_real_audio_duration_limit_with_fake_model(tmp_path, monkeypatch):
    settings = AppSettings(database_path=tmp_path / "db.sqlite3", asr_max_audio_seconds=1)
    monkeypatch.setattr("backend.service.asr.asr_service.inspect_model", Mock())
    fake_model = Mock()
    factory = Mock(return_value=fake_model)
    app = create_app(settings, adapter=AsrService(settings, model_factory=factory))
    service = app.extensions["asr_http_service"]
    service.prepare_model()
    audio = BytesIO()
    with wave.open(audio, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(b"\0\0" * 16001)
    audio.seek(0)
    response = app.test_client().post("/v1/audio/transcriptions", data={
        "model": "whisper-small", "file": (audio, "a.wav")})
    assert response.status_code == 413
    assert response.json["error"]["code"] == "AUDIO_TOO_LONG"
    fake_model.transcribe.assert_not_called()
    assert list(service.upload_root.iterdir()) == []


def test_cli_pins_loopback_and_prepares_only_on_explicit_start(tmp_path, monkeypatch):
    fake_app = Mock()
    fake_service = Mock()
    fake_app.extensions = {"asr_http_service": fake_service}
    create = Mock(return_value=fake_app)
    monkeypatch.setattr("backend.modules.asr.http_server.create_app", create)
    settings = AppSettings(database_path=tmp_path / "db.sqlite3")

    def execute_stub(operation, **kwargs):
        operation(settings, Mock())
        return 0

    monkeypatch.setattr("backend.modules.asr.http_server.execute", execute_stub)
    assert main(["--port", "8007"]) == 0
    fake_service.prepare_model.assert_called_once()
    fake_app.run.assert_called_once_with(
        host="127.0.0.1", port=8007, debug=False, use_reloader=False, threaded=True)
