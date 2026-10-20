"""本地 CLI 与结果保存边界；替身仅验证流程，不代表真实模型结果。"""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from backend.common.cli import execution
from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.data.asr import result_store
from backend.modules.asr import prepare_model, transcribe_file
from backend.schema.asr.asr_schema import ModelManifest, TranscriptionOptions, TranscriptionRequest, TranscriptionResult
from backend.service.asr import local_transcription_service


@pytest.fixture
def cli_settings(tmp_path, monkeypatch):
    settings = AppSettings(
        database_path=tmp_path / "db.sqlite3",
        log_dir=tmp_path / "logs",
        asr_model_dir=tmp_path / "model",
    )
    monkeypatch.setattr(execution, "load_settings", lambda: settings)
    return settings


@pytest.fixture
def flow_result(cli_settings):
    return TranscriptionResult(
        text="仅用于流程测试", segments=[], language="zh", language_requested=None,
        language_probability=0.9, audio_duration_seconds=1,
        model_repo=cli_settings.asr_model_repo, model_revision=cli_settings.asr_model_revision,
        device="cpu", compute_type="int8_float32", cpu_threads=4,
        model_load_seconds=0.2, transcription_seconds=0.3, total_seconds=0.5,
        faster_whisper_version="test-only", ctranslate2_version="test-only",
    )


@pytest.mark.parametrize(("language_args", "expected_language"), [([], "zh"), (["--language", "auto"], None)])
def test_transcribe_cli_validates_and_forwards_paths_and_language(
    tmp_path, monkeypatch, capsys, cli_settings, flow_result, language_args, expected_language,
):
    monkeypatch.chdir(tmp_path)
    operation = Mock(return_value=flow_result)
    monkeypatch.setattr(transcribe_file, "transcribe_to_file", operation)

    assert transcribe_file.main(["音频.wav", "--output", "结果.json", *language_args]) == 0

    actual_settings, request, output = operation.call_args.args
    assert actual_settings is cli_settings
    assert isinstance(request, TranscriptionRequest)
    assert request.audio_path == tmp_path / "音频.wav"
    assert request.language == expected_language
    assert output == tmp_path / "结果.json"
    captured = capsys.readouterr()
    assert json.loads(captured.out)["text"] == "仅用于流程测试"
    assert "结果已保存" in (cli_settings.log_dir / "asr.log").read_text(encoding="utf-8")


def test_cli_validation_failure_does_not_call_service(tmp_path, monkeypatch, capsys, cli_settings):
    operation = Mock()
    monkeypatch.setattr(transcribe_file, "transcribe_to_file", operation)

    assert transcribe_file.main([str(tmp_path / "input.wav"), "--language", "ZH"]) == 2

    operation.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err[captured.err.index('{\n  "error"'):])
    assert error["error"]["code"] == "VALIDATION_ERROR"
    assert error["error"]["details"][0]["field"] == "language"
    assert len(error["requestId"]) == 32
    logs = (cli_settings.log_dir / "asr.log").read_text(encoding="utf-8")
    assert "VALIDATION_ERROR" in logs
    assert error["requestId"] in logs


def test_cli_app_error_has_nonzero_exit_request_id_and_persistent_cause(
    tmp_path, monkeypatch, capsys, cli_settings,
):
    operation = Mock(side_effect=AppError("RESULT_WRITE_FAILED", "转写完成但结果保存失败。"))
    monkeypatch.setattr(transcribe_file, "transcribe_to_file", operation)

    assert transcribe_file.main([str(tmp_path / "input.wav")]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err[captured.err.index('{\n  "error"'):])
    assert error["error"]["code"] == "RESULT_WRITE_FAILED"
    logs = (cli_settings.log_dir / "asr.log").read_text(encoding="utf-8")
    assert error["requestId"] in logs
    assert "RESULT_WRITE_FAILED" in logs


def test_prepare_check_cli_inspects_without_calling_download(monkeypatch, capsys, cli_settings):
    manifest = ModelManifest(model_repo=cli_settings.asr_model_repo, model_revision=cli_settings.asr_model_revision, files={})
    inspect = Mock(return_value=manifest)
    download = Mock()
    monkeypatch.setattr(prepare_model, "inspect_model", inspect)
    monkeypatch.setattr(prepare_model, "prepare_model", download)

    assert prepare_model.main(["--check"]) == 0

    inspect.assert_called_once_with(cli_settings)
    download.assert_not_called()
    assert json.loads(capsys.readouterr().out)["modelRevision"] == cli_settings.asr_model_revision


@pytest.mark.parametrize("invalid", [
    {"audioPath": " "},
    {"audioPath": "input.wav", "outputPath": " "},
    {"audioPath": "input.wav", "overwrite": True},
])
def test_external_options_reject_empty_paths_and_unknown_switches(invalid):
    with pytest.raises(ValidationError):
        TranscriptionOptions.model_validate(invalid)


def test_existing_result_is_rejected_before_model_construction(tmp_path, monkeypatch, cli_settings):
    output = tmp_path / "result.json"
    output.write_text("previous result", encoding="utf-8")
    model_service = Mock()
    monkeypatch.setattr("backend.service.asr.asr_service.AsrService", model_service)

    with pytest.raises(AppError) as caught:
        local_transcription_service.transcribe_to_file(
            cli_settings, TranscriptionRequest(audio_path=tmp_path / "input.wav"), output,
        )

    assert caught.value.code == "RESULT_EXISTS"
    assert output.read_text(encoding="utf-8") == "previous result"
    model_service.assert_not_called()


def test_storage_failure_never_returns_success(tmp_path, monkeypatch, cli_settings, flow_result):
    model_service = Mock()
    model_service.return_value.transcribe.return_value = flow_result
    monkeypatch.setattr("backend.service.asr.asr_service.AsrService", model_service)
    monkeypatch.setattr(local_transcription_service, "write_result", Mock(side_effect=OSError("test disk full")))

    with pytest.raises(AppError) as caught:
        local_transcription_service.transcribe_to_file(
            cli_settings, TranscriptionRequest(audio_path=tmp_path / "input.wav"), tmp_path / "result.json",
        )

    assert caught.value.code == "RESULT_WRITE_FAILED"
    assert isinstance(caught.value.__cause__, OSError)


def test_racing_result_file_is_preserved_after_transcription(tmp_path, monkeypatch, cli_settings, flow_result):
    output = tmp_path / "result.json"

    def transcribe(_request):
        output.write_text("another operation won", encoding="utf-8")
        return flow_result

    model_service = Mock()
    model_service.return_value.transcribe.side_effect = transcribe
    monkeypatch.setattr("backend.service.asr.asr_service.AsrService", model_service)

    with pytest.raises(AppError) as caught:
        local_transcription_service.transcribe_to_file(
            cli_settings, TranscriptionRequest(audio_path=tmp_path / "input.wav"), output,
        )

    assert caught.value.code == "RESULT_EXISTS"
    assert output.read_text(encoding="utf-8") == "another operation won"


def test_result_store_preserves_existing_file(tmp_path):
    output = tmp_path / "existing.json"
    output.write_text("old result", encoding="utf-8")
    with pytest.raises(FileExistsError):
        result_store.write_result(output, "new result")
    assert output.read_text(encoding="utf-8") == "old result"


def test_result_store_removes_partial_file_even_when_close_also_fails(tmp_path, monkeypatch):
    output = tmp_path / "result.json"
    original_open = Path.open

    class FailingWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def write(self, content):
            self.stream.write(content[:2])
            self.stream.flush()
            raise OSError("test-only disk full")

        def close(self):
            self.stream.close()
            raise OSError("test-only disk full on close")

    def failing_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return FailingWriter(stream) if path == output and args[0] == "x" else stream

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(OSError, match="disk full"):
        result_store.write_result(output, '{"text":"complete"}')
    assert not output.exists(), "保存失败不能留下看似完整的结果文件，否则相同输出名无法再次使用"
