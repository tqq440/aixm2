"""模型可用性与任务受理事务；此处只用短 PCM，不把替身输出当模型验收。"""

from concurrent.futures import ThreadPoolExecutor
import io
from threading import Event
import wave

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text

from backend.config.asr.model_catalog import MODEL_ID, QUEUE_KEY
from backend.config.settings.app_settings import PROJECT_ROOT
from backend.data.broker.broker_repository import BrokerRepository
from backend.service.dispatch.dispatch_service import DispatchService
from backend.service.execution.execution_service import ExecutionService


@pytest.fixture
def catalog_case(app_factory, tmp_path):
    app = app_factory(upload_dir=tmp_path / "uploads")
    settings = app.config["APP_SETTINGS"]
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 1600)
    payload = buffer.getvalue()

    def submit(key="model-admission", **fields):
        content = fields.pop("content", payload)
        return app.test_client().post("/api/tasks/asr", headers={"Idempotency-Key": key},
            data={"file": (io.BytesIO(content), "catalog.wav"), **fields})

    return app, settings, app.extensions["model_service"].registry, submit, payload


def test_explicit_version_and_legacy_omission_resolve_same_seed(catalog_case):
    app, settings, _, submit, _ = catalog_case
    legacy = submit().get_json()["data"]["task"]
    replay = submit(modelVersion=settings.asr_model_revision)
    assert replay.status_code == 200
    assert replay.get_json()["data"] == {"replayed": True, "task": legacy}
    models = app.test_client().get("/api/models").get_json()["data"]["items"]
    assert models[0]["modelVersion"] == legacy["modelVersion"]
    assert models[0]["resourceRequirements"]["device"] == "cpu"


def test_disable_blocks_only_new_requests_and_reenable_restores_catalog(catalog_case):
    app, settings, registry, submit, _ = catalog_case
    task = submit().get_json()["data"]["task"]
    registry.disable(MODEL_ID, settings.asr_model_revision)
    assert app.test_client().get("/api/models").get_json()["data"]["items"] == []
    assert submit("new-task").status_code == 422
    assert submit("explicit-new", modelVersion=settings.asr_model_revision).status_code == 422
    assert submit().get_json()["data"] == {"task": task, "replayed": True}
    assert submit(modelVersion=settings.asr_model_revision).status_code == 200
    assert app.test_client().get(f"/api/tasks/{task['taskId']}").status_code == 200
    registry.enable(MODEL_ID, settings.asr_model_revision)
    assert len(app.test_client().get("/api/models").get_json()["data"]["items"]) == 1
    assert submit("after-reenable").status_code == 201


@pytest.mark.parametrize("changed", [
    {"modelVersion": "other-version"}, {"modelId": "another-model"},
    {"language": "auto"}, {"content": b"different content"},
])
def test_disabled_version_does_not_hide_idempotency_conflicts(catalog_case, changed):
    _, settings, registry, submit, _ = catalog_case
    assert submit().status_code == 201
    registry.disable(MODEL_ID, settings.asr_model_revision)
    conflict = submit(**changed)
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("version", ["unknown", "", "a" * 65])
def test_unknown_or_invalid_explicit_version_never_creates_upload(catalog_case, version):
    app, settings, _, submit, _ = catalog_case
    assert submit(modelVersion=version).status_code == 422
    assert list(settings.upload_dir.rglob("*.audio")) == []
    assert app.test_client().get("/api/tasks").get_json()["data"]["total"] == 0


def test_disable_during_upload_is_rechecked_at_task_commit(catalog_case, monkeypatch):
    app, settings, registry, submit, _ = catalog_case
    service = app.extensions["task_service"]
    save = service.store.save

    def disable_after_save(stream, *, max_bytes):
        result = save(stream, max_bytes=max_bytes)
        registry.disable(MODEL_ID, settings.asr_model_revision)
        return result

    monkeypatch.setattr(service.store, "save", disable_after_save)
    assert submit().status_code == 422
    assert list(settings.upload_dir.rglob("*.audio")) == []
    with app.extensions["database"].engine.connect() as connection:
        for table in ("tasks", "idempotency_records", "dispatch_events"):
            assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0


def test_acceptance_holding_write_lock_precedes_concurrent_disable(catalog_case, monkeypatch):
    app, settings, registry, submit, _ = catalog_case
    checked, disabling = Event(), Event()
    models = app.extensions["model_service"]
    validate = models.validate_admission

    def check_while_locked(session, model_id, version):
        validate(session, model_id, version)
        checked.set()
        assert disabling.wait(5)

    def disable():
        assert checked.wait(5)
        disabling.set()
        registry.disable(MODEL_ID, settings.asr_model_revision)

    monkeypatch.setattr(models, "validate_admission", check_while_locked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(submit)
        stopping = pool.submit(disable)
        response = creating.result(timeout=10)
        stopping.result(timeout=10)
    assert response.status_code == 201
    assert submit("later-new-task").status_code == 422
    assert submit().status_code == 200


def test_disabled_version_still_allows_previously_accepted_worker_lease(catalog_case):
    app, settings, registry, submit, _ = catalog_case
    task = submit().get_json()["data"]["task"]
    registry.disable(MODEL_ID, settings.asr_model_revision)
    database = app.extensions["database"]
    dispatcher = DispatchService(database, settings, app.extensions["app_logger"])
    assert dispatcher.tick() == 1
    broker = BrokerRepository(database)
    delivery = broker.receive(worker_id="catalog-test", queue_key=QUEUE_KEY, visibility_seconds=30)
    acquired = ExecutionService(database, settings).acquire(delivery)
    assert acquired.decision == "acquired"
    assert acquired.input.task_id == task["taskId"]
    assert acquired.input.model_version == settings.asr_model_revision


def test_replay_does_not_resolve_current_default(catalog_case, monkeypatch):
    # 第八部分只有一个可执行版本；通过让默认解析报错验证重放根本不依赖当前默认。
    app, _, _, submit, _ = catalog_case
    task = submit().get_json()["data"]["task"]

    def changed_default(*args, **kwargs):
        raise AssertionError("重放不得重新解析当前默认版本")

    monkeypatch.setattr(app.extensions["model_service"], "resolve", changed_default)
    assert submit().get_json()["data"] == {"task": task, "replayed": True}


def test_model_endpoint_explains_missing_migration(app_factory):
    response = app_factory().test_client().get("/api/models")
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "MODEL_REGISTRY_UNAVAILABLE"
