"""模型使用替身，仅验证调用控制、错误边界和结果契约。"""

import os
import subprocess
import sys
import wave
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import PROJECT_ROOT, AppSettings
from backend.schema.asr.asr_schema import TranscriptionRequest
from backend.service.asr import asr_service
from backend.service.asr.asr_service import AsrService


@pytest.fixture
def audio_path(tmp_path):
    path = tmp_path / "test.wav"
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(b"\x00\x00" * 16000)
    return path


@pytest.fixture
def settings(tmp_path):
    return AppSettings(asr_model_dir=tmp_path / "model")


@pytest.fixture
def model_factory(monkeypatch):
    monkeypatch.setattr(asr_service, "inspect_model", Mock())
    model = Mock()
    model.model = SimpleNamespace(device="cpu", compute_type="int8_float32")
    model.transcribe.side_effect = lambda *args, **kwargs: (
        iter([SimpleNamespace(start=0.0, end=0.5, text=" 你好"), SimpleNamespace(start=0.5, end=1.0, text="世界。")]),
        SimpleNamespace(language="zh", language_probability=1.0),
    )
    return Mock(return_value=model)


def test_model_load_is_lazy_and_transcription_has_actual_engine_metadata(settings, model_factory, audio_path):
    service = AsrService(settings, model_factory=model_factory)
    model_factory.assert_not_called()
    result = service.transcribe(TranscriptionRequest(audio_path=audio_path))
    model_factory.assert_called_once_with(
        str(settings.asr_model_dir), device="cpu", compute_type="int8", cpu_threads=4,
        num_workers=1, local_files_only=True,
    )
    call = model_factory.return_value.transcribe.call_args
    assert call.args[0].shape == (16000,)
    assert call.kwargs["language"] == "zh"
    assert call.kwargs["task"] == "transcribe"
    assert call.kwargs["vad_filter"] is False
    assert result.text == "你好世界。"
    assert result.language_requested == "zh"
    assert [(segment.start, segment.end) for segment in result.segments] == [(0, 0.5), (0.5, 1)]
    assert result.device == "cpu"
    assert result.compute_type == "int8_float32"
    assert result.audio_duration_seconds == 1
    assert result.model_revision == settings.asr_model_revision
    assert result.faster_whisper_version == "1.2.1"
    assert result.ctranslate2_version == "4.8.2"
    assert result.engine_metadata.adapter_key == "faster-whisper"
    assert result.engine_metadata.runtime_versions == {"faster-whisper": "1.2.1", "ctranslate2": "4.8.2"}
    assert result.engine_metadata.device == result.device
    assert result.engine_metadata.compute_type == result.compute_type
    assert result.engine_metadata.total_seconds == result.total_seconds
    assert result.total_seconds >= result.transcription_seconds
    assert "audioDurationSeconds" in result.model_dump(by_alias=True)


def test_reuses_loaded_model_with_zero_second_call_load_time(settings, model_factory, audio_path):
    service = AsrService(settings, model_factory=model_factory)
    service.transcribe(TranscriptionRequest(audio_path=audio_path))
    second = service.transcribe(TranscriptionRequest(audio_path=audio_path, language=None))
    assert model_factory.call_count == 1
    assert second.model_load_seconds == 0
    assert second.language_requested is None
    assert model_factory.return_value.transcribe.call_args.kwargs["language"] is None


def test_missing_model_error_preserved_before_factory(settings, model_factory, audio_path, monkeypatch):
    monkeypatch.setattr(asr_service, "inspect_model", Mock(side_effect=AppError("MODEL_NOT_READY", "模型缺失")))
    with pytest.raises(AppError) as error:
        AsrService(settings, model_factory=model_factory).transcribe(TranscriptionRequest(audio_path=audio_path))
    assert error.value.code == "MODEL_NOT_READY"
    model_factory.assert_not_called()


@pytest.mark.parametrize("file_kind", ["missing", "directory"])
def test_audio_must_be_regular_existing_file(settings, model_factory, tmp_path, file_kind):
    path = tmp_path / "audio"
    if file_kind == "directory":
        path.mkdir()
    with pytest.raises(AppError) as error:
        AsrService(settings, model_factory=model_factory).transcribe(TranscriptionRequest(audio_path=path))
    assert error.value.code == "AUDIO_NOT_FOUND"
    model_factory.assert_not_called()


def test_file_size_limit_precedes_decoding_and_model_load(settings, model_factory, audio_path, monkeypatch):
    settings = settings.model_copy(update={"asr_max_file_bytes": audio_path.stat().st_size - 1})
    decoder = Mock()
    monkeypatch.setattr(asr_service, "decode_audio_file", decoder)
    with pytest.raises(AppError) as error:
        AsrService(settings, model_factory=model_factory).transcribe(TranscriptionRequest(audio_path=audio_path))
    assert error.value.code == "AUDIO_TOO_LARGE"
    assert error.value.status_code == 413
    decoder.assert_not_called()
    model_factory.assert_not_called()


def test_exact_file_size_and_duration_limits_are_allowed(settings, model_factory, audio_path):
    settings = settings.model_copy(update={"asr_max_file_bytes": audio_path.stat().st_size, "asr_max_audio_seconds": 1})
    result = AsrService(settings, model_factory=model_factory).transcribe(TranscriptionRequest(audio_path=audio_path))
    assert result.audio_duration_seconds == 1


def test_duration_limit_uses_real_decoded_samples(settings, model_factory, audio_path):
    settings = settings.model_copy(update={"asr_max_audio_seconds": 0.5})
    with pytest.raises(AppError) as error:
        AsrService(settings, model_factory=model_factory).transcribe(TranscriptionRequest(audio_path=audio_path))
    assert error.value.code == "AUDIO_TOO_LONG"
    model_factory.assert_not_called()


def test_invalid_audio_rejected_before_model_load(settings, model_factory, audio_path):
    audio_path.write_bytes(b"not audio")
    with pytest.raises(AppError) as error:
        AsrService(settings, model_factory=model_factory).transcribe(TranscriptionRequest(audio_path=audio_path))
    assert error.value.code == "AUDIO_DECODE_FAILED"
    model_factory.assert_not_called()


@pytest.mark.parametrize("language", ["xxx", "yue"])
def test_unknown_language_rejected_before_decode_and_model(settings, model_factory, audio_path, monkeypatch, language):
    decoder = Mock()
    monkeypatch.setattr(asr_service, "decode_audio_file", decoder)
    with pytest.raises(AppError) as error:
        AsrService(settings, model_factory=model_factory).transcribe(TranscriptionRequest(audio_path=audio_path, language=language))
    assert error.value.code == "LANGUAGE_UNSUPPORTED"
    decoder.assert_not_called()
    model_factory.assert_not_called()


def test_model_load_failure_is_clear(settings, model_factory, audio_path):
    model_factory.side_effect = RuntimeError("bad model")
    with pytest.raises(AppError) as error:
        AsrService(settings, model_factory=model_factory).transcribe(TranscriptionRequest(audio_path=audio_path))
    assert error.value.code == "MODEL_LOAD_FAILED"


def test_lazy_generator_failure_never_returns_partial_success(settings, model_factory, audio_path):
    def segments():
        yield SimpleNamespace(start=0.0, end=0.5, text="部分结果")
        raise RuntimeError("failed while executing generator")

    model_factory.return_value.transcribe.side_effect = lambda *args, **kwargs: (
        segments(), SimpleNamespace(language="zh", language_probability=1.0),
    )
    with pytest.raises(AppError) as error:
        AsrService(settings, model_factory=model_factory).transcribe(TranscriptionRequest(audio_path=audio_path))
    assert error.value.code == "TRANSCRIPTION_FAILED"


def test_transcription_timer_includes_generator_execution(settings, model_factory, audio_path, monkeypatch):
    time_value = [0.0]
    monkeypatch.setattr(asr_service, "perf_counter", lambda: time_value[0])

    def segments():
        time_value[0] += 3.5
        yield SimpleNamespace(start=0.0, end=0.5, text="完成")
        time_value[0] += 2.5

    model_factory.return_value.transcribe.side_effect = lambda *args, **kwargs: (
        segments(), SimpleNamespace(language="zh", language_probability=1.0),
    )
    result = AsrService(settings, model_factory=model_factory).transcribe(TranscriptionRequest(audio_path=audio_path))
    assert result.transcription_seconds == 6.0
    assert result.total_seconds == 6.0


def test_invalid_model_segments_cannot_escape_as_success(settings, model_factory, audio_path):
    model_factory.return_value.transcribe.side_effect = lambda *args, **kwargs: (
        iter([SimpleNamespace(start=1.0, end=0.0, text="坏时间戳")]),
        SimpleNamespace(language="zh", language_probability=1.0),
    )
    with pytest.raises(AppError) as error:
        AsrService(settings, model_factory=model_factory).transcribe(TranscriptionRequest(audio_path=audio_path))
    assert error.value.code == "TRANSCRIPTION_FAILED"


def test_service_import_does_not_load_native_inference_or_audio_libraries(tmp_path):
    environment = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from backend.service.asr.asr_service import AsrService; "
            "assert not ({'av', 'faster_whisper', 'ctranslate2'} & set(sys.modules))",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
