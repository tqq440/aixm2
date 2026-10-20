"""会话替身只测试控制和契约；真实YOLO验收独立执行。"""

import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
from PIL import Image
import pytest

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.config.vision.model_catalog import MODEL_FILENAME, MODEL_SHA256
from backend.schema.vision.detection_schema import DetectionRequest
from backend.service.vision import detection_service
from backend.service.vision.detection_service import DetectionService
from backend.utils.image.image_utils import (
    ImageDecodeError, ImageFileTooLargeError, ImageFormatError, ImageNotFoundError, ImagePixelLimitError,
)


@pytest.fixture
def settings(tmp_path):
    return AppSettings(vision_model_dir=tmp_path / "model")


@pytest.fixture
def image_path(tmp_path):
    path = tmp_path / "example.png"
    with Image.new("RGB", (200, 100), (128, 0, 255)) as image:
        image.save(path)
    return path


@pytest.fixture
def session_factory(monkeypatch):
    monkeypatch.setattr(detection_service, "inspect_model", Mock())
    session = Mock()
    session.get_providers.return_value = ["CPUExecutionProvider"]
    session.get_inputs.return_value = [SimpleNamespace(name="images", type="tensor(float16)", shape=[1, 3, 640, 640])]
    session.get_outputs.return_value = [SimpleNamespace(name="output0", type="tensor(float16)", shape=[1, 25200, 85])]
    raw = np.zeros((1, 25200, 85), dtype=np.float16)
    raw[0, 0, :5] = [320, 320, 320, 160, 1]
    raw[0, 0, 10] = 0.9  # class 5 = bus
    session.run.return_value = [raw]
    return Mock(return_value=session)


def test_lazily_loads_cpu_session_and_returns_original_pixel_contract(settings, image_path, session_factory):
    service = DetectionService(settings, session_factory=session_factory)
    session_factory.assert_not_called()
    result, prepared = service.run(DetectionRequest(image_path=image_path))
    call = session_factory.call_args
    assert call.args == (str(settings.vision_model_dir / MODEL_FILENAME),)
    assert call.kwargs["providers"] == ["CPUExecutionProvider"]
    assert call.kwargs["sess_options"].intra_op_num_threads == settings.vision_cpu_threads
    assert call.kwargs["sess_options"].inter_op_num_threads == 1
    call = session_factory.return_value.run.call_args
    assert call.args[0] == ["output0"]
    tensor = call.args[1]["images"]
    assert tensor.dtype == np.float16 and tensor.shape == (1, 3, 640, 640)
    assert (result.image_width, result.image_height) == (200, 100)
    assert prepared.rgb.shape == (100, 200, 3)
    assert result.model_sha256 == MODEL_SHA256
    assert result.device == "cpu" and result.input_type == "float16"
    assert result.coordinate_system == "original-pixels-exif-normalized"
    assert result.runtime_version == "1.31.0"
    assert len(result.detections) == 1
    detected = result.detections[0]
    assert detected.class_name == "bus" and detected.class_id == 5
    assert detected.confidence == pytest.approx(0.9, abs=0.001)
    assert detected.box.model_dump() == {"x_min": 50, "y_min": 25, "x_max": 150, "y_max": 75}
    assert result.total_seconds >= result.inference_seconds
    assert "imageWidth" in result.model_dump(by_alias=True)


def test_session_reuse_has_zero_second_load_time(settings, image_path, session_factory):
    service = DetectionService(settings, session_factory=session_factory)
    service.prepare_model()
    result, _ = service.run(DetectionRequest(image_path=image_path))
    assert result.load_seconds == 0
    assert service.prepare_model() == 0
    session_factory.assert_called_once()
    detection_service.inspect_model.assert_called_once_with(settings)


def test_explicit_worker_warmup_runs_blank_tensor_and_reuses_loaded_session(settings, image_path, session_factory):
    service = DetectionService(settings, session_factory=session_factory)
    service.prepare_model()
    session_factory.return_value.run.assert_not_called()
    assert service.warmup() >= 0
    tensor = session_factory.return_value.run.call_args.args[1]["images"]
    assert tensor.shape == (1, 3, 640, 640) and tensor.dtype == np.float16
    assert not tensor.any()
    session_factory.assert_called_once()
    service.run(DetectionRequest(image_path=image_path))
    assert session_factory.return_value.run.call_count == 2


@pytest.mark.parametrize("failure", ["runtime", "shape", "nonfinite", "missing"])
def test_warmup_failure_never_reports_success(settings, session_factory, failure):
    session = session_factory.return_value
    if failure == "runtime":
        session.run.side_effect = RuntimeError("native failure")
    elif failure == "shape":
        session.run.return_value = [np.zeros((1, 1, 85), dtype=np.float16)]
    elif failure == "nonfinite":
        session.run.return_value[0][0, 0, 0] = np.nan
    else:
        session.run.return_value = []
    with pytest.raises(AppError) as error:
        DetectionService(settings, session_factory=session_factory).warmup()
    assert error.value.code == "VISION_MODEL_WARMUP_FAILED"


def test_empty_detection_is_success(settings, image_path, session_factory):
    session_factory.return_value.run.return_value[0][:] = 0
    result, _ = DetectionService(settings, session_factory=session_factory).run(DetectionRequest(image_path=image_path))
    assert result.detections == []


@pytest.mark.parametrize("exception,code,status", [
    (ImageNotFoundError, "IMAGE_NOT_FOUND", 404),
    (ImageFileTooLargeError, "IMAGE_TOO_LARGE", 413),
    (ImagePixelLimitError, "IMAGE_TOO_MANY_PIXELS", 413),
    (ImageFormatError, "IMAGE_FORMAT_UNSUPPORTED", 400),
    (ImageDecodeError, "IMAGE_DECODE_FAILED", 400),
    (ImportError, "VISION_DEPENDENCY_MISSING", 503),
])
def test_image_errors_precede_model_inspection(settings, image_path, session_factory, monkeypatch, exception, code, status):
    monkeypatch.setattr(detection_service, "decode_image_file", Mock(side_effect=exception("failed")))
    with pytest.raises(AppError) as error:
        DetectionService(settings, session_factory=session_factory).run(DetectionRequest(image_path=image_path))
    assert (error.value.code, error.value.status_code) == (code, status)
    session_factory.assert_not_called()
    detection_service.inspect_model.assert_not_called()


def test_real_corrupted_picture_rejected_before_model(settings, image_path, session_factory):
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n broken")
    with pytest.raises(AppError) as error:
        DetectionService(settings, session_factory=session_factory).run(DetectionRequest(image_path=image_path))
    assert error.value.code == "IMAGE_DECODE_FAILED"
    session_factory.assert_not_called()


def test_missing_model_error_is_preserved(settings, image_path, session_factory, monkeypatch):
    monkeypatch.setattr(detection_service, "inspect_model", Mock(side_effect=AppError("VISION_MODEL_NOT_READY", "missing", 503)))
    with pytest.raises(AppError) as error:
        DetectionService(settings, session_factory=session_factory).run(DetectionRequest(image_path=image_path))
    assert error.value.code == "VISION_MODEL_NOT_READY"
    session_factory.assert_not_called()


def test_load_failure_is_classified_and_not_cached(settings, session_factory):
    service = DetectionService(settings, session_factory=session_factory)
    session_factory.side_effect = RuntimeError("failed load")
    with pytest.raises(AppError) as error:
        service.prepare_model()
    assert error.value.code == "VISION_MODEL_LOAD_FAILED"
    session_factory.side_effect = None
    service.prepare_model()
    assert session_factory.call_count == 2


@pytest.mark.parametrize("change", ["provider", "input_type", "input_name", "input_shape", "output_type", "output_shape", "output_count"])
def test_wrong_model_contract_rejected(settings, session_factory, change):
    session = session_factory.return_value
    if change == "provider":
        session.get_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    elif change == "input_type":
        session.get_inputs.return_value[0].type = "tensor(float)"
    elif change == "input_name":
        session.get_inputs.return_value[0].name = "other"
    elif change == "input_shape":
        session.get_inputs.return_value[0].shape = ["batch", 3, 640, 640]
    elif change == "output_type":
        session.get_outputs.return_value[0].type = "tensor(float)"
    elif change == "output_shape":
        session.get_outputs.return_value[0].shape = [1, 300, 6]
    else:
        session.get_outputs.return_value = []
    with pytest.raises(AppError) as error:
        DetectionService(settings, session_factory=session_factory).prepare_model()
    assert error.value.code == "VISION_MODEL_CONTRACT_INVALID"
    session.run.assert_not_called()


def test_inference_runtime_failure_is_distinct(settings, image_path, session_factory):
    session_factory.return_value.run.side_effect = RuntimeError("runtime failure")
    with pytest.raises(AppError) as error:
        DetectionService(settings, session_factory=session_factory).run(DetectionRequest(image_path=image_path))
    assert error.value.code == "DETECTION_FAILED"


@pytest.mark.parametrize("output", [[], [None], [np.zeros((1, 25200, 85), dtype=np.float32)], [np.zeros((1, 300, 6), dtype=np.float16)]])
def test_malformed_output_never_becomes_success(settings, image_path, session_factory, output):
    session_factory.return_value.run.return_value = output
    with pytest.raises(AppError) as error:
        DetectionService(settings, session_factory=session_factory).run(DetectionRequest(image_path=image_path))
    assert error.value.code == "DETECTION_OUTPUT_INVALID"


def test_nonfinite_output_never_becomes_success(settings, image_path, session_factory):
    session_factory.return_value.run.return_value[0][0, 100, 10] = np.nan
    with pytest.raises(AppError) as error:
        DetectionService(settings, session_factory=session_factory).run(DetectionRequest(image_path=image_path))
    assert error.value.code == "DETECTION_OUTPUT_INVALID"


def test_unsafe_constructed_request_is_revalidated(settings, image_path, session_factory):
    request = DetectionRequest.model_construct(image_path=image_path, confidence_threshold=float("nan"), iou_threshold=0.45, max_detections=100)
    with pytest.raises(AppError) as error:
        DetectionService(settings, session_factory=session_factory).run(request)
    assert error.value.code == "DETECTION_REQUEST_INVALID"
    session_factory.assert_not_called()


def test_inference_time_measures_native_call_only(settings, image_path, session_factory, monkeypatch):
    counter = [0.0]
    monkeypatch.setattr(detection_service, "perf_counter", lambda: counter[0])
    output = session_factory.return_value.run.return_value

    def run(*args, **kwargs):
        counter[0] += 2.5
        return output

    session_factory.return_value.run.side_effect = run
    result, _ = DetectionService(settings, session_factory=session_factory).run(DetectionRequest(image_path=image_path))
    assert result.inference_seconds == 2.5 and result.total_seconds == 2.5


def test_import_does_not_load_vision_libraries_or_create_files(tmp_path):
    environment = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-c", "import sys; from backend.service.vision.detection_service import DetectionService; assert not ({'numpy', 'PIL', 'onnxruntime'} & set(sys.modules))"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []
