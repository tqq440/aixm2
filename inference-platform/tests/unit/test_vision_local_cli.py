"""视觉本地 CLI、配置与输出编排；推理替身不代表真实模型验收。"""

from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib import request as urllib_request

import numpy as np
from PIL import Image
from pydantic import ValidationError
import pytest

from backend.common.cli import execution
from backend.common.exceptions.app_error import AppError, ConfigurationError
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT, load_settings
from backend.data.vision import model_store
from backend.modules.vision import detect_image, prepare_model
from backend.schema.vision.detection_schema import DetectionOptions, DetectionRequest, DetectionResult
from backend.service.vision import detection_service, local_detection_service, model_preparation_service
from backend.utils.image.image_utils import PreparedImage


@pytest.fixture
def cli_settings(tmp_path, monkeypatch):
    settings = AppSettings(database_path=tmp_path / "db.sqlite3", log_dir=tmp_path / "logs",
                           vision_model_dir=tmp_path / "model")
    monkeypatch.setattr(execution, "load_settings", lambda: settings)
    return settings


@pytest.fixture
def flow_result():
    return DetectionResult(model_sha256="0" * 64, image_width=8, image_height=6,
                           detections=[], runtime_version="test-only", load_seconds=0,
                           inference_seconds=0.01, total_seconds=0.02)


@pytest.fixture
def prepared_image():
    return PreparedImage(rgb=np.zeros((6, 8, 3), dtype=np.uint8), width=8, height=6)


def test_cli_validates_and_forwards_paths_and_detection_parameters(tmp_path, monkeypatch, capsys, cli_settings, flow_result):
    monkeypatch.chdir(tmp_path)
    operation = Mock(return_value=flow_result)
    monkeypatch.setattr(detect_image, "detect_to_directory", operation)
    assert detect_image.main(["图片.png", "--output-dir", "检测结果", "--confidence-threshold", "0.4",
                              "--iou-threshold", "0.6", "--max-detections", "10"]) == 0
    settings, request, output = operation.call_args.args
    assert settings is cli_settings
    assert type(request) is DetectionRequest
    assert request.image_path == tmp_path / "图片.png"
    assert (request.confidence_threshold, request.iou_threshold, request.max_detections) == (0.4, 0.6, 10)
    assert output == tmp_path / "检测结果"
    assert json.loads(capsys.readouterr().out)["detections"] == []
    assert "检测结果" in (cli_settings.log_dir / "vision.log").read_text(encoding="utf-8")


def test_cli_default_output_is_new_project_runtime_directory(monkeypatch, capsys, cli_settings, flow_result):
    operation = Mock(return_value=flow_result)
    monkeypatch.setattr(detect_image, "detect_to_directory", operation)
    assert detect_image.main(["image.png"]) == 0
    output = operation.call_args.args[2]
    assert output.parent == PROJECT_ROOT / "runtime/results"
    assert output.name.startswith("vision-")
    assert len(output.name) == len("vision-") + 32
    capsys.readouterr()


@pytest.mark.parametrize("arguments", [
    ["image.png", "--confidence-threshold", "0"],
    ["image.png", "--confidence-threshold", "nan"],
    ["image.png", "--iou-threshold", "1.1"],
    ["image.png", "--max-detections", "301"],
    ["image.png", "--max-detections", "1.5"],
    ["image.png", "--output-dir", " "],
    [" "],
])
def test_invalid_cli_parameters_fail_before_service(arguments, monkeypatch, capsys, cli_settings):
    operation = Mock()
    monkeypatch.setattr(detect_image, "detect_to_directory", operation)
    assert detect_image.main(arguments) == 2
    operation.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err[captured.err.index('{\n  "error"'):])
    assert error["error"]["code"] == "VALIDATION_ERROR"
    assert error["error"]["details"]
    assert len(error["requestId"]) == 32


@pytest.mark.parametrize("invalid", [
    {"imagePath": "image.png", "unknown": 1},
    {"imagePath": "image.png", "outputDir": ""},
    {"imagePath": 123},
    {"imagePath": "image.png", "confidenceThreshold": float("inf")},
])
def test_options_reject_unknown_keys_empty_paths_and_nonfinite_numbers(invalid):
    with pytest.raises(ValidationError):
        DetectionOptions.model_validate(invalid)


@pytest.mark.parametrize("check", [True, False])
def test_prepare_cli_selects_explicit_prepare_or_read_only_check(monkeypatch, capsys, cli_settings, check):
    manifest = model_preparation_service._expected_manifest()
    inspect = Mock(return_value=manifest)
    download = Mock(return_value=manifest)
    monkeypatch.setattr(prepare_model, "inspect_model", inspect)
    monkeypatch.setattr(prepare_model, "prepare_model", download)
    assert prepare_model.main(["--check"] if check else []) == 0
    expected, unused = (inspect, download) if check else (download, inspect)
    expected.assert_called_once_with(cli_settings)
    unused.assert_not_called()
    assert json.loads(capsys.readouterr().out)["modelVersion"] == manifest.model_version


def test_missing_model_check_cli_returns_structured_error_without_network(monkeypatch, capsys, cli_settings):
    network = Mock(side_effect=AssertionError("禁止联网"))
    monkeypatch.setattr(model_store, "urlopen", network)
    assert prepare_model.main(["--check"]) == 2
    captured = capsys.readouterr()
    error = json.loads(captured.err[captured.err.index('{\n  "error"'):])
    assert error["error"]["code"] == "VISION_MODEL_NOT_READY"
    assert not cli_settings.vision_model_dir.exists()
    network.assert_not_called()


def test_existing_output_is_rejected_before_model_construction(tmp_path, monkeypatch, cli_settings):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "old.txt").write_text("keep", encoding="utf-8")
    service = Mock()
    monkeypatch.setattr(local_detection_service, "DetectionService", service)
    with pytest.raises(AppError) as caught:
        local_detection_service.detect_to_directory(cli_settings, DetectionRequest(image_path="missing.png"), output)
    assert caught.value.code == "RESULT_EXISTS"
    assert (output / "old.txt").read_text(encoding="utf-8") == "keep"
    service.assert_not_called()


def test_local_flow_preserves_concurrent_result(tmp_path, monkeypatch, cli_settings, flow_result, prepared_image):
    output = tmp_path / "result"

    def run(_request):
        output.mkdir()
        (output / "winner.txt").write_text("keep", encoding="utf-8")
        return flow_result, prepared_image

    service = Mock()
    service.return_value.run.side_effect = run
    monkeypatch.setattr(local_detection_service, "DetectionService", service)
    with pytest.raises(AppError) as caught:
        local_detection_service.detect_to_directory(cli_settings, DetectionRequest(image_path="input.png"), output)
    assert caught.value.code == "RESULT_EXISTS"
    assert (output / "winner.txt").read_text(encoding="utf-8") == "keep"
    assert sorted(path.name for path in output.iterdir()) == ["winner.txt"]


def test_local_storage_failure_is_reported_as_failure(tmp_path, monkeypatch, cli_settings, flow_result, prepared_image):
    service = Mock()
    service.return_value.run.return_value = flow_result, prepared_image
    monkeypatch.setattr(local_detection_service, "DetectionService", service)
    monkeypatch.setattr(local_detection_service, "write_detection_result", Mock(side_effect=OSError("test disk full")))
    with pytest.raises(AppError) as caught:
        local_detection_service.detect_to_directory(cli_settings, DetectionRequest(image_path="input.png"), tmp_path / "result")
    assert caught.value.code == "RESULT_WRITE_FAILED"


def test_detection_cli_uses_verified_local_files_without_network(tmp_path, monkeypatch, capsys, cli_settings):
    weights = b"test-only-model"
    digest = sha256(weights).hexdigest()
    monkeypatch.setattr(model_preparation_service, "MODEL_SIZE_BYTES", len(weights))
    monkeypatch.setattr(model_preparation_service, "MODEL_SHA256", digest)
    monkeypatch.setattr(detection_service, "MODEL_SHA256", digest)
    cli_settings.vision_model_dir.mkdir()
    (cli_settings.vision_model_dir / model_preparation_service.MODEL_FILENAME).write_bytes(weights)
    (cli_settings.vision_model_dir / "manifest.json").write_text(
        model_preparation_service._expected_manifest().model_dump_json(by_alias=True), encoding="utf-8")
    input_image = tmp_path / "input.png"
    with Image.new("RGB", (8, 6), (17, 34, 51)) as image:
        image.save(input_image)
    network = Mock(side_effect=AssertionError("检测路径不允许联网"))
    monkeypatch.setattr(model_store, "urlopen", network)
    monkeypatch.setattr(urllib_request, "urlopen", network)
    session = Mock()
    session.get_providers.return_value = ["CPUExecutionProvider"]
    session.get_inputs.return_value = [SimpleNamespace(name="images", type="tensor(float16)", shape=[1, 3, 640, 640])]
    session.get_outputs.return_value = [SimpleNamespace(name="output0", type="tensor(float16)", shape=[1, 25200, 85])]
    session.run.return_value = [np.zeros((1, 25200, 85), dtype=np.float16)]
    factory = Mock(return_value=session)
    monkeypatch.setattr(local_detection_service, "DetectionService",
                        lambda settings: detection_service.DetectionService(settings, session_factory=factory))
    output = tmp_path / "result"
    assert detect_image.main([str(input_image), "--output-dir", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["detections"] == []
    assert result["modelSha256"] == digest
    assert result["imageWidth"] == 8 and result["imageHeight"] == 6
    assert json.loads((output / "result.json").read_text(encoding="utf-8")) == result
    with Image.open(output / "annotated.png") as image:
        assert image.size == (8, 6)
        assert image.getpixel((0, 0)) == (17, 34, 51)
    network.assert_not_called()
    session.run.assert_called_once()


def test_vision_defaults_and_relative_paths_are_project_local_without_creation(tmp_path):
    settings = load_settings(None, environ={})
    assert settings.vision_model_dir == PROJECT_ROOT / "runtime/models/yolov5n-onnx"
    assert (settings.vision_cpu_threads, settings.vision_max_file_bytes, settings.vision_max_pixels) == (4, 10 * 1024 * 1024, 20_000_000)
    settings = load_settings(None, environ={"INFERENCE_VISION_MODEL_DIR": str(tmp_path / "new-model")})
    assert settings.vision_model_dir == tmp_path / "new-model"
    assert not settings.vision_model_dir.exists()
    relative = load_settings(None, environ={"INFERENCE_VISION_MODEL_DIR": "runtime/models/test-not-created"})
    assert relative.vision_model_dir == PROJECT_ROOT / "runtime/models/test-not-created"


@pytest.mark.parametrize("key,value", [
    ("INFERENCE_VISION_CPU_THREADS", "0"),
    ("INFERENCE_VISION_CPU_THREADS", "33"),
    ("INFERENCE_VISION_MAX_FILE_BYTES", "0"),
    ("INFERENCE_VISION_MAX_FILE_BYTES", "10485761"),
    ("INFERENCE_VISION_MAX_PIXELS", "0"),
    ("INFERENCE_VISION_MAX_PIXELS", "20000001"),
    ("INFERENCE_VISION_MODEL_DIR", " "),
    ("INFERENCE_VISION_DEVICE", "cuda"),
])
def test_invalid_or_unknown_vision_configuration_is_rejected(key, value):
    with pytest.raises(ConfigurationError, match=key):
        load_settings(None, environ={key: value})


def test_vision_model_directory_cannot_be_a_file(tmp_path):
    path = tmp_path / "file"
    path.write_text("keep", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="INFERENCE_VISION_MODEL_DIR"):
        load_settings(None, environ={"INFERENCE_VISION_MODEL_DIR": str(path)})


def test_vision_environment_takes_precedence_without_changing_asr_limits(tmp_path):
    env_file = tmp_path / "settings.env"
    env_file.write_text("INFERENCE_VISION_MAX_PIXELS=1000\n", encoding="utf-8")
    settings = load_settings(env_file, environ={"INFERENCE_VISION_MAX_PIXELS": "2000"})
    assert settings.vision_max_pixels == 2000
    assert (settings.asr_max_file_bytes, settings.asr_max_audio_seconds) == (20 * 1024 * 1024, 300)
