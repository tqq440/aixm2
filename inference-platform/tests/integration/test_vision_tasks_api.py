"""图片任务 HTTP 契约与受管文件边界；结果提交使用测试夹具，不运行模型。"""

from io import BytesIO
import wave
from uuid import uuid4

from alembic import command
from alembic.config import Config
import numpy as np
from PIL import Image
import pytest
from sqlalchemy import text
from werkzeug.datastructures import MultiDict

from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.config.vision.model_catalog import MODEL_ID, MODEL_VERSION, MODEL_SHA256, QUEUE_KEY, REGISTRY_SPEC
from backend.data.broker.broker_repository import BrokerRepository
from backend.data.files.upload_store import LocalUploadStore
from backend.modules.api.app import create_app, close_app
from backend.schema.models.model_schema import ModelRegistration
from backend.schema.vision.detection_schema import BoundingBox, Detection, DetectionResult
from backend.service.dispatch.dispatch_service import DispatchService
from backend.service.execution.execution_service import ExecutionService
from backend.service.models.model_registry_service import ModelRegistryService


@pytest.fixture
def vision_app_factory(tmp_path):
    apps = []

    def factory(**overrides):
        root = tmp_path / str(len(apps))
        settings = AppSettings(database_path=root / "db.sqlite3", log_dir=root / "logs",
                               upload_dir=root / "uploads", **overrides)
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.attributes["settings"] = settings
        command.upgrade(config, "head")
        app = create_app(settings)
        apps.append(app)
        registry = ModelRegistryService(app.extensions["database"], settings)
        registry.register(ModelRegistration.model_validate(REGISTRY_SPEC))
        registry.enable(MODEL_ID, MODEL_VERSION)
        registry.set_default(MODEL_ID, MODEL_VERSION)
        return app

    yield factory
    for app in reversed(apps):
        close_app(app)


@pytest.fixture
def vision_app(vision_app_factory):
    return vision_app_factory()


def image_bytes(*, image_format="PNG", size=(32, 24), mode="RGB", color=(17, 34, 51), exif=None):
    with Image.new(mode, size, color) as image, BytesIO() as output:
        image.save(output, format=image_format, **({"exif": exif} if exif is not None else {}))
        return output.getvalue()


def audio_bytes():
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 1600)
    return output.getvalue()


def submit(client, payload=None, *, key=None, form=None, task_type="vision", filename="fixture.png"):
    data = dict(form or {})
    data["file"] = (BytesIO(image_bytes() if payload is None else payload), filename)
    return client.post(f"/api/tasks/{task_type}", data=data,
                       headers={"Idempotency-Key": key or uuid4().hex}, content_type="multipart/form-data")


def database_counts(app):
    with app.extensions["database"].engine.connect() as connection:
        return {table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                for table in ("tasks", "idempotency_records", "dispatch_events")}


def finish_test_detection(app, task_id, *, with_box=False):
    settings = app.config["APP_SETTINGS"]
    database = app.extensions["database"]
    DispatchService(database, settings, app.extensions["app_logger"]).tick()
    delivery = BrokerRepository(database).receive(worker_id="test-vision", queue_key=QUEUE_KEY, visibility_seconds=30)
    assert delivery.task_id == task_id
    service = ExecutionService(database, settings, task_type="vision")
    decision = service.acquire(delivery)
    assert decision.decision == "acquired"
    detections = [Detection(class_id=0, class_name="person", confidence=0.9,
                            box=BoundingBox(x_min=1, y_min=1, x_max=12, y_max=15))] if with_box else []
    result = DetectionResult(model_sha256=MODEL_SHA256, image_width=decision.input.image_width,
                             image_height=decision.input.image_height, detections=detections,
                             runtime_version="test-only", load_seconds=0, inference_seconds=0.01, total_seconds=0.02)
    assert service.commit_result(decision.lease, result)
    return result


@pytest.mark.parametrize("image_format", ["PNG", "JPEG"])
def test_image_upload_creates_three_records_and_safe_original_input(vision_app, image_format):
    client = vision_app.test_client()
    payload = image_bytes(image_format=image_format)
    response = submit(client, payload, filename="../../输入.png")
    assert response.status_code == 201, response.json
    task = response.json["data"]["task"]
    assert task["taskType"] == "vision" and task["status"] == "created"
    assert task["modelId"] == MODEL_ID and task["modelVersion"] == MODEL_VERSION
    assert task["parameters"] == {"confidenceThreshold": 0.25, "iouThreshold": 0.45, "maxDetections": 100}
    assert task["input"]["fileName"] == "输入.png"
    assert task["input"]["mediaType"] == "image"
    assert (task["input"]["width"], task["input"]["height"]) == (32, 24)
    assert "durationSeconds" not in task["input"]
    assert response.headers["Location"] == f"/api/tasks/{task['taskId']}"
    assert database_counts(vision_app) == {"tasks": 1, "idempotency_records": 1, "dispatch_events": 1}
    root = vision_app.config["APP_SETTINGS"].upload_dir
    assert [path.read_bytes() for path in (root / "objects").iterdir()] == [payload]
    assert next((root / "objects").iterdir()).suffix == ".image"
    with vision_app.extensions["database"].engine.connect() as connection:
        assert connection.execute(text("SELECT queue_key, input_media_type, audio_duration_seconds FROM tasks")).one() == (QUEUE_KEY, "image", None)
        assert connection.execute(text("SELECT queue_key FROM dispatch_events")).scalar_one() == QUEUE_KEY


def test_exif_oriented_input_and_download_use_same_normalized_coordinates(vision_app):
    exif = Image.Exif()
    exif[274] = 6
    response = submit(vision_app.test_client(), image_bytes(size=(16, 8), exif=exif))
    task = response.json["data"]["task"]
    assert (task["input"]["width"], task["input"]["height"]) == (8, 16)
    image = vision_app.test_client().get(f"/api/tasks/{task['taskId']}/image")
    with Image.open(BytesIO(image.data)) as decoded:
        assert decoded.size == (8, 16)
        assert decoded.getexif().get(274) is None


def test_transparent_image_download_is_rgb_on_white(vision_app):
    client = vision_app.test_client()
    response = submit(client, image_bytes(mode="RGBA", color=(1, 2, 3, 0)))
    task_id = response.json["data"]["task"]["taskId"]
    download = client.get(f"/api/tasks/{task_id}/image?variant=original")
    assert download.status_code == 200 and download.mimetype == "image/png"
    assert download.headers["X-Content-Type-Options"] == "nosniff"
    with Image.open(BytesIO(download.data)) as decoded:
        assert decoded.mode == "RGB" and decoded.getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize("kind,expected", [("empty", "IMAGE_EMPTY"), ("corrupt", "IMAGE_DECODE_FAILED"),
                                          ("gif", "IMAGE_FORMAT_UNSUPPORTED"), ("animated", "IMAGE_FORMAT_UNSUPPORTED")])
def test_invalid_image_is_not_accepted_or_retained(vision_app, kind, expected):
    if kind == "empty":
        payload = b""
    elif kind == "corrupt":
        payload = image_bytes()[:-12]
    elif kind == "gif":
        payload = image_bytes(image_format="GIF")
    else:
        with Image.new("RGB", (8, 6), "red") as first, Image.new("RGB", (8, 6), "blue") as second, BytesIO() as target:
            first.save(target, format="PNG", save_all=True, append_images=[second], duration=50)
            payload = target.getvalue()
    response = submit(vision_app.test_client(), payload)
    assert response.status_code in (400, 422)
    assert response.json["error"]["code"] == expected
    assert database_counts(vision_app) == {"tasks": 0, "idempotency_records": 0, "dispatch_events": 0}
    assert list(vision_app.config["APP_SETTINGS"].upload_dir.glob("*/*")) == []


@pytest.mark.parametrize("settings,expected", [({"vision_max_file_bytes": 40}, "IMAGE_TOO_LARGE"),
                                            ({"vision_max_pixels": 100}, "IMAGE_TOO_MANY_PIXELS")])
def test_image_enforces_its_own_byte_and_pixel_limits(vision_app_factory, settings, expected):
    app = vision_app_factory(**settings)
    response = submit(app.test_client())
    assert response.status_code == 413 and response.json["error"]["code"] == expected
    assert database_counts(app)["tasks"] == 0
    assert list(app.config["APP_SETTINGS"].upload_dir.glob("*/*")) == []


@pytest.mark.parametrize("field,value", [("confidenceThreshold", "nan"), ("confidenceThreshold", "0"),
    ("iouThreshold", "1.1"), ("maxDetections", "301"), ("maxDetections", "1.5"),
    ("language", "zh"), ("imagePath", "C:/private.png"), ("width", "32")])
def test_vision_form_rejects_invalid_or_untrusted_fields(vision_app, field, value):
    response = submit(vision_app.test_client(), form={field: value})
    assert response.status_code == 422 and response.json["error"]["code"] == "VALIDATION_ERROR"
    assert database_counts(vision_app)["tasks"] == 0


def test_duplicate_form_values_and_multiple_files_are_rejected(vision_app):
    client = vision_app.test_client()
    repeated = MultiDict([("confidenceThreshold", "0.25"), ("confidenceThreshold", "0.4"),
                          ("file", (BytesIO(image_bytes()), "image.png"))])
    response = client.post("/api/tasks/vision", data=repeated, headers={"Idempotency-Key": "repeat"})
    assert response.status_code == 422 and response.json["error"]["code"] == "VALIDATION_ERROR"
    files = MultiDict([("file", (BytesIO(image_bytes()), "one.png")), ("file", (BytesIO(image_bytes()), "two.png"))])
    response = client.post("/api/tasks/vision", data=files, headers={"Idempotency-Key": "multiple"})
    assert response.status_code == 422 and response.json["error"]["code"] == "UPLOAD_FILE_REQUIRED"
    assert database_counts(vision_app)["tasks"] == 0


@pytest.mark.parametrize("route,model_id", [("vision", "whisper-small"), ("asr", MODEL_ID)])
def test_cross_type_model_is_rejected_before_file_acceptance(vision_app, route, model_id):
    response = submit(vision_app.test_client(), task_type=route, form={"modelId": model_id})
    assert response.status_code == 422 and response.json["error"]["code"] == "MODEL_TASK_TYPE_MISMATCH"
    assert database_counts(vision_app)["tasks"] == 0


def test_model_and_task_type_filters_preserve_default_asr_and_correct_pagination(vision_app):
    client = vision_app.test_client()
    assert [item["taskType"] for item in client.get("/api/models").json["data"]["items"]] == ["asr"]
    assert [item["taskType"] for item in client.get("/api/models?taskType=vision").json["data"]["items"]] == ["vision"]
    assert {item["taskType"] for item in client.get("/api/models?taskType=all").json["data"]["items"]} == {"asr", "vision"}
    assert submit(client, audio_bytes(), task_type="asr", filename="audio.wav").status_code == 201
    ids = [submit(client).json["data"]["task"]["taskId"] for _ in range(3)]
    default = client.get("/api/tasks").json["data"]
    assert default["total"] == 1 and default["items"][0]["taskType"] == "asr"
    first = client.get("/api/tasks?taskType=vision&pageSize=2").json["data"]
    second = client.get("/api/tasks?taskType=vision&pageSize=2&page=2").json["data"]
    assert first["total"] == second["total"] == 3
    assert {item["taskId"] for item in first["items"] + second["items"]} == set(ids)
    assert client.get("/api/tasks?taskType=all").json["data"]["total"] == 4
    for endpoint in ("models", "tasks"):
        assert client.get(f"/api/{endpoint}?taskType=vision&taskType=asr").status_code == 422
    assert client.get("/api/models?taskType=llm").json["data"]["items"] == []
    assert client.get("/api/tasks?taskType=llm").status_code == 422


@pytest.mark.parametrize("first_type", ["asr", "vision"])
def test_idempotency_key_is_shared_and_cannot_cross_task_types(vision_app, first_type):
    client = vision_app.test_client()
    second_type = "vision" if first_type == "asr" else "asr"
    first = submit(client, audio_bytes() if first_type == "asr" else image_bytes(), task_type=first_type, key="shared")
    assert first.status_code == 201
    response = submit(client, audio_bytes() if second_type == "asr" else image_bytes(), task_type=second_type, key="shared")
    assert response.status_code == 409 and response.json["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert database_counts(vision_app) == {"tasks": 1, "idempotency_records": 1, "dispatch_events": 1}
    assert len(list(vision_app.config["APP_SETTINGS"].upload_dir.glob("objects/*"))) == 1


def test_vision_replay_survives_disable_and_changed_default_without_new_file(vision_app, monkeypatch):
    client = vision_app.test_client()
    original = submit(client, key="repeat").json["data"]["task"]
    registry = vision_app.extensions["model_service"].registry
    future = ModelRegistration.model_validate(REGISTRY_SPEC).model_copy(update={"model_version": "test-future"})
    # 只为验证未来默认切换的契约允许此测试规范；不执行或声明真实未来模型。
    monkeypatch.setattr(registry, "execution_supported", lambda spec: True)
    registry.register(future)
    registry.enable(MODEL_ID, future.model_version)
    registry.set_default(MODEL_ID, future.model_version)
    registry.disable(MODEL_ID, MODEL_VERSION)
    replay = submit(client, key="repeat")
    assert replay.status_code == 200 and replay.json["data"]["replayed"]
    assert replay.json["data"]["task"]["taskId"] == original["taskId"]
    assert replay.json["data"]["task"]["modelVersion"] == MODEL_VERSION
    assert submit(client, key="repeat", form={"modelVersion": MODEL_VERSION}).status_code == 200
    assert submit(client, key="repeat", form={"modelVersion": "test-future"}).status_code == 409
    assert submit(client, key="repeat", form={"confidenceThreshold": "0.5"}).status_code == 409
    assert submit(client, image_bytes(color=(99, 34, 51)), key="repeat").status_code == 409
    assert database_counts(vision_app)["tasks"] == 1
    assert len(list(vision_app.config["APP_SETTINGS"].upload_dir.glob("objects/*"))) == 1


def test_disabled_vision_model_rejects_new_but_replays_existing_request(vision_app):
    client = vision_app.test_client()
    assert submit(client, key="old").status_code == 201
    vision_app.extensions["model_service"].registry.disable(MODEL_ID, MODEL_VERSION)
    response = submit(client, key="new")
    assert response.status_code == 422 and response.json["error"]["code"] == "MODEL_DISABLED"
    assert submit(client, key="old").status_code == 200
    assert client.get("/api/models?taskType=vision").json["data"]["items"] == []


def test_failed_three_table_submission_rolls_back_and_cleans_uploaded_image(vision_app, monkeypatch):
    service = vision_app.extensions["task_service"]
    create = service.repository.create_with_event

    def break_event(**kwargs):
        kwargs["event"].task_id = uuid4().hex
        create(**kwargs)

    monkeypatch.setattr(service.repository, "create_with_event", break_event)
    response = submit(vision_app.test_client())
    assert response.status_code == 503 and response.json["error"]["code"] == "TASK_STORAGE_UNAVAILABLE"
    assert database_counts(vision_app) == {"tasks": 0, "idempotency_records": 0, "dispatch_events": 0}
    assert list(vision_app.config["APP_SETTINGS"].upload_dir.glob("*/*")) == []


def test_capacity_limit_of_twenty_is_global_and_replays_do_not_consume_capacity(vision_app):
    client = vision_app.test_client()
    for number in range(20):
        kind = "asr" if number % 2 == 0 else "vision"
        response = submit(client, audio_bytes() if kind == "asr" else image_bytes(), task_type=kind, key=f"accepted-{number}")
        assert response.status_code == 201, response.json
    for kind in ("asr", "vision"):
        rejected = submit(client, audio_bytes() if kind == "asr" else image_bytes(), task_type=kind)
        assert rejected.status_code == 429 and rejected.json["error"]["code"] == "TASK_CAPACITY_EXCEEDED"
    replay = submit(client, key="accepted-1")
    assert replay.status_code == 200 and replay.json["data"]["replayed"]
    assert database_counts(vision_app) == {"tasks": 20, "idempotency_records": 20, "dispatch_events": 20}
    assert len(list(vision_app.config["APP_SETTINGS"].upload_dir.glob("objects/*"))) == 20


def test_committed_json_and_generated_png_remain_accessible_after_model_disable(vision_app):
    client = vision_app.test_client()
    task_id = submit(client).json["data"]["task"]["taskId"]
    assert client.get(f"/api/tasks/{task_id}/image?variant=annotated").json["error"]["code"] == "RESULT_NOT_READY"
    result = finish_test_detection(vision_app, task_id, with_box=True)
    vision_app.extensions["model_service"].registry.disable(MODEL_ID, MODEL_VERSION)
    response = client.get(f"/api/tasks/{task_id}/result")
    assert response.status_code == 200
    assert response.json["data"]["output"] == result.model_dump(mode="json", by_alias=True)
    original = client.get(f"/api/tasks/{task_id}/image?variant=original")
    annotated = client.get(f"/api/tasks/{task_id}/image?variant=annotated")
    assert original.status_code == annotated.status_code == 200
    with Image.open(BytesIO(original.data)) as first, Image.open(BytesIO(annotated.data)) as second:
        assert first.size == second.size == (32, 24)
        assert not np.array_equal(np.array(first), np.array(second))
    assert len(list(vision_app.config["APP_SETTINGS"].upload_dir.glob("objects/*"))) == 1


def test_empty_detection_succeeds_and_produces_unmodified_png(vision_app):
    client = vision_app.test_client()
    task_id = submit(client).json["data"]["task"]["taskId"]
    finish_test_detection(vision_app, task_id)
    assert client.get(f"/api/tasks/{task_id}/result").json["data"]["output"]["detections"] == []
    assert client.get(f"/api/tasks/{task_id}/image").data == client.get(f"/api/tasks/{task_id}/image?variant=annotated").data


def test_image_endpoint_rejects_asr_unknown_paths_and_untrusted_query(vision_app):
    client = vision_app.test_client()
    asr_id = submit(client, audio_bytes(), task_type="asr").json["data"]["task"]["taskId"]
    assert client.get(f"/api/tasks/{asr_id}/image").json["error"]["code"] == "TASK_TYPE_MISMATCH"
    assert client.get(f"/api/tasks/{'a' * 32}/image").status_code == 404
    image_id = submit(client).json["data"]["task"]["taskId"]
    for query in ("path=C:/private.png", "variant=raw", "variant=original&variant=annotated"):
        assert client.get(f"/api/tasks/{image_id}/image?{query}").status_code == 422


@pytest.mark.parametrize("tamper", ["change_size", "same_size", "missing"])
def test_image_download_rejects_tampered_or_missing_managed_input(vision_app, tamper):
    client = vision_app.test_client()
    task_id = submit(client).json["data"]["task"]["taskId"]
    repository = vision_app.extensions["task_service"].repository
    record = repository.get_task("local-user", task_id)
    path = LocalUploadStore(vision_app.config["APP_SETTINGS"].upload_dir, media_type="image").resolve_path(record.input_ref)
    if tamper == "missing":
        path.unlink()
    elif tamper == "change_size":
        path.write_bytes(b"changed")
    else:
        path.write_bytes(b"x" * record.input_size_bytes)
    response = client.get(f"/api/tasks/{task_id}/image")
    assert response.status_code == (503 if tamper == "missing" else 409)
    assert response.json["error"]["code"] == ("INPUT_UNAVAILABLE" if tamper == "missing" else "INPUT_INTEGRITY_FAILED")
