"""子进程业务边界使用假 ASR；不执行神经网络。"""

from io import BytesIO

import pytest

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.config.vision.model_catalog import MODEL_SHA256, MODEL_VERSION
from backend.data.files.upload_store import LocalUploadStore
from backend.schema.asr.asr_schema import TranscriptionResult
from backend.schema.execution.execution_schema import ExecutionInput, VisionExecutionInput
from backend.schema.vision.detection_schema import DetectionResult
from backend.schema.worker.worker_schema import EVENT_ADAPTER, RunCommand, StartCommand, StopCommand
from backend.service.worker.inference_child import inference_child


class Connection:
    def __init__(self, commands):
        self.commands = [item.model_dump_json().encode() for item in commands]
        self.events = []
        self.closed = False

    def recv_bytes(self, max_size):
        return self.commands.pop(0)

    def send_bytes(self, payload):
        self.events.append(EVENT_ADAPTER.validate_json(payload))

    def close(self):
        self.closed = True


@pytest.fixture
def child_case(tmp_path, monkeypatch):
    settings = AppSettings(database_path=tmp_path / "data.sqlite3", upload_dir=tmp_path / "uploads", log_dir=tmp_path / "logs")
    saved = LocalUploadStore(settings.upload_dir).save(BytesIO(b"test audio"), max_bytes=100)
    task_input = ExecutionInput(task_id="a" * 32, input_ref=saved.input_ref, input_sha256=saved.sha256, input_size_bytes=saved.size_bytes, model_id="whisper-small", model_version=settings.asr_model_revision, language="zh")
    result = TranscriptionResult(text="测试", segments=[], language="zh", language_requested="zh", language_probability=.9, audio_duration_seconds=1, model_repo=settings.asr_model_repo, model_revision=settings.asr_model_revision, device="cpu", compute_type="int8", cpu_threads=4, model_load_seconds=0, transcription_seconds=1, total_seconds=1, faster_whisper_version="test", ctranslate2_version="test")
    events = []
    class FakeAsr:
        def __init__(self, settings):
            pass

        def prepare_model(self):
            events.append("load")
            return 0.1

        def transcribe(self, request):
            events.append("transcribe")
            assert request.audio_path == saved.path
            return result
    # 仅在测试中替换首个插件的实现；child 的真实目录选择/身份核验继续执行。
    monkeypatch.setattr("backend.service.asr.asr_service.AsrService", FakeAsr)
    monkeypatch.setattr("backend.service.worker.inference_child.signal.signal", lambda *_: None)
    # inference_child 在本单测进程中直接调用，不能解除 pytest 自己的控制台。
    monkeypatch.setattr("backend.service.worker.inference_child.detach_console", lambda: None)
    return settings, saved, task_input, events, FakeAsr


def test_child_loads_once_and_reuses_model_for_two_commands(child_case):
    settings, _, task_input, events, _ = child_case
    connection = Connection([StartCommand(settings=settings), RunCommand(input=task_input), RunCommand(input=task_input), StopCommand()])
    inference_child(connection)
    assert events == ["load", "transcribe", "transcribe"]
    assert [event.type for event in connection.events] == ["ready", "result", "result"]
    assert connection.closed


@pytest.mark.parametrize("corruption", ["size", "hash", "missing", "model_version"])
def test_child_refuses_changed_inputs_before_inference(child_case, corruption):
    settings, saved, task_input, events, _ = child_case
    if corruption == "size":
        saved.path.write_bytes(b"different size")
    elif corruption == "hash":
        saved.path.write_bytes(b"other data")
    elif corruption == "missing":
        saved.path.unlink()
    else:
        task_input = task_input.model_copy(update={"model_version": "0" * 40})
    connection = Connection([StartCommand(settings=settings), RunCommand(input=task_input), StopCommand()])
    inference_child(connection)
    assert events == ["load"]
    assert [event.type for event in connection.events] == ["ready", "failed"]
    assert connection.events[1].failure.code in {"INPUT_INTEGRITY_FAILED", "INPUT_UNAVAILABLE", "MODEL_VERSION_MISMATCH"}


def test_child_load_failure_is_fatal_and_never_ready(child_case, monkeypatch):
    settings, _, task_input, events, factory = child_case
    def missing(self):
        raise AppError("MODEL_MISSING", "模型文件缺失。")
    monkeypatch.setattr(factory, "prepare_model", missing)
    connection = Connection([StartCommand(settings=settings), RunCommand(input=task_input)])
    inference_child(connection)
    assert [event.type for event in connection.events] == ["fatal"]
    assert connection.events[0].failure.code == "MODEL_MISSING"
    assert events == []


def test_child_validates_first_command_before_model_load(child_case):
    _, _, task_input, events, _ = child_case
    connection = Connection([RunCommand(input=task_input)])
    inference_child(connection)
    assert events == []
    assert [event.type for event in connection.events] == ["fatal"]


def test_asr_child_detaches_before_model_load_and_reuses_pipe(child_case, monkeypatch):
    settings, _, task_input, events, _ = child_case
    monkeypatch.setattr("backend.service.worker.inference_child.detach_console", lambda: events.append("detach"))
    connection = Connection([StartCommand(settings=settings), RunCommand(input=task_input), StopCommand()])
    inference_child(connection)
    assert events == ["detach", "load", "transcribe"]
    assert [event.type for event in connection.events] == ["ready", "result"]


def test_asr_console_isolation_failure_never_loads_model(child_case, monkeypatch):
    settings, _, _, events, _ = child_case

    def fail_detach():
        raise OSError("test-only FreeConsole failure")

    monkeypatch.setattr("backend.service.worker.inference_child.detach_console", fail_detach)
    connection = Connection([StartCommand(settings=settings)])
    inference_child(connection)
    assert events == []
    assert [event.type for event in connection.events] == ["fatal"]
    assert connection.events[0].failure.code == "ASR_CONSOLE_DETACH_FAILED"


@pytest.fixture
def vision_child_case(tmp_path, monkeypatch):
    settings = AppSettings(database_path=tmp_path / "vision.sqlite3", upload_dir=tmp_path / "uploads", log_dir=tmp_path / "logs")
    saved = LocalUploadStore(settings.upload_dir, media_type="image").save(BytesIO(b"test picture"), max_bytes=100)
    task_input = VisionExecutionInput(task_id="a" * 32, input_ref=saved.input_ref, input_sha256=saved.sha256, input_size_bytes=saved.size_bytes, model_id="yolo-general", model_version=MODEL_VERSION, image_width=200, image_height=100, confidence_threshold=.5, iou_threshold=.6, max_detections=12)
    result = DetectionResult(model_sha256=MODEL_SHA256, image_width=200, image_height=100, detections=[], runtime_version="test", load_seconds=0, inference_seconds=.1, total_seconds=.2)
    events = []
    class FakeDetection:
        def __init__(self, settings):
            pass

        def prepare_model(self):
            events.append("load")
            return .1

        def warmup(self):
            events.append("warmup")
            return .2

        def run(self, request):
            events.append("detect")
            assert request.image_path == saved.path
            assert (request.confidence_threshold, request.iou_threshold, request.max_detections) == (.5, .6, 12)
            # prepared_image 故意不可序列化，验证 IPC 只传检测 JSON。
            return result, object()

    monkeypatch.setattr("backend.service.worker.inference_child.DetectionService", FakeDetection)
    monkeypatch.setattr("backend.service.worker.inference_child.signal.signal", lambda *_: None)
    return settings, saved, task_input, events, FakeDetection


def test_vision_child_warms_once_before_ready_and_reuses_session_for_empty_results(vision_child_case):
    settings, _, task_input, events, _ = vision_child_case
    connection = Connection([StartCommand(settings=settings, task_type="vision"), RunCommand(input=task_input), RunCommand(input=task_input), StopCommand()])
    inference_child(connection)
    assert events == ["load", "warmup", "detect", "detect"]
    assert [event.type for event in connection.events] == ["ready", "result", "result"]
    assert connection.events[0].warmup_seconds == .2
    assert connection.events[1].result.detections == []
    assert connection.closed


def test_vision_warmup_failure_is_fatal_and_never_claims(vision_child_case, monkeypatch):
    settings, _, task_input, events, factory = vision_child_case
    def failed(_):
        raise AppError("VISION_MODEL_WARMUP_FAILED", "预热失败。", 503)
    monkeypatch.setattr(factory, "warmup", failed)
    connection = Connection([StartCommand(settings=settings, task_type="vision"), RunCommand(input=task_input)])
    inference_child(connection)
    assert events == ["load"]
    assert [event.type for event in connection.events] == ["fatal"]
    assert connection.events[0].failure.code == "VISION_MODEL_WARMUP_FAILED"


@pytest.mark.parametrize("changed,code", [("size", "INPUT_INTEGRITY_FAILED"), ("dimensions", "INPUT_METADATA_MISMATCH"), ("model", "MODEL_VERSION_MISMATCH"), ("type", "MODEL_VERSION_MISMATCH")])
def test_vision_child_validates_snapshot_and_profile(vision_child_case, changed, code):
    settings, saved, task_input, events, _ = vision_child_case
    if changed == "size":
        saved.path.write_bytes(b"changed")
    elif changed == "dimensions":
        task_input = task_input.model_copy(update={"image_width": 201})
    elif changed == "model":
        task_input = task_input.model_copy(update={"model_version": "wrong"})
    else:
        task_input = ExecutionInput(task_id=task_input.task_id, input_ref=saved.input_ref, input_sha256=saved.sha256, input_size_bytes=saved.size_bytes, model_id="whisper-small", model_version=settings.asr_model_revision, language="zh")
    connection = Connection([StartCommand(settings=settings, task_type="vision"), RunCommand(input=task_input), StopCommand()])
    inference_child(connection)
    assert [event.type for event in connection.events] == ["ready", "failed"]
    assert connection.events[1].failure.code == code
    assert events == ["load", "warmup"] + (["detect"] if changed == "dimensions" else [])


def test_remote_child_routes_fixed_profile_and_correlates_request_without_loading_whisper(child_case, monkeypatch):
    settings, saved, task_input, events, _ = child_case
    settings = settings.model_copy(update={"remote_asr_base_url": "https://isolated.invalid:8003",
        "remote_asr_model_id": "test-sensevoice", "remote_asr_model_version": "fixture-v1"})
    task_input = task_input.model_copy(update={"model_id": "test-sensevoice", "model_version": "fixture-v1", "language": None})
    class FakeRemote:
        def __init__(self, _settings):
            assert _settings.remote_asr_model_id == "test-sensevoice"

        def prepare_model(self):
            events.append("remote-health")
            return .1

        def transcribe(self, request):
            assert request.request_id == task_input.task_id
            assert request.audio_path == saved.path and request.language is None
            events.append("remote-call")
            return TranscriptionResult(text="隔离响应", segments=[], audio_duration_seconds=1,
                engine_metadata={"adapter_key": "remote-v2-sensevoice"})
    monkeypatch.setattr("backend.service.asr.remote_v2_adapter.RemoteV2AsrAdapter", FakeRemote)
    connection = Connection([StartCommand(settings=settings, asr_profile="remote"),
                             RunCommand(input=task_input), StopCommand()])
    inference_child(connection)
    assert events == ["remote-health", "remote-call"]
    assert [event.type for event in connection.events] == ["ready", "result"]


def test_start_command_disallows_remote_profile_for_vision(child_case):
    settings, *_ = child_case
    with pytest.raises(ValueError, match="只能用于 ASR"):
        StartCommand(settings=settings, task_type="vision", asr_profile="remote")
