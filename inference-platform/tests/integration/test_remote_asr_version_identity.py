"""上游版本缺失时的登记身份和历史投影；SQLite/HTTP 真实，转写结果为隔离夹具。"""

from io import BytesIO
from types import SimpleNamespace
import wave

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import event

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import PROJECT_ROOT
from backend.data.broker.broker_repository import BrokerRepository
from backend.data.models.model_models import ModelVersionRecord
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.data.tasks.task_models import TaskRecord
from backend.schema.asr.asr_schema import AsrEngineMetadata, TranscriptionResult
from backend.schema.models.model_schema import ModelRegistration
from backend.service.asr.adapter_catalog import remote_asr_model_spec
from backend.service.asr.remote_service import REMOTE_QUEUE, RemoteAsrService
from backend.service.dispatch.dispatch_service import DispatchService
from backend.service.execution.execution_service import ExecutionService
from backend.service.models.model_registry_service import ModelRegistryService


VERSION_INFO = {"versionBasis": "integration_revision", "upstreamVersion": None}


@pytest.fixture
def unknown_version_case(app_factory, tmp_path, monkeypatch):
    values = {"upload_dir": tmp_path / "uploads", "remote_asr_base_url": "https://isolated.test:8003",
              "remote_asr_model_id": "fixture-asr", "remote_asr_model_version": None}
    app = app_factory(**values)
    settings = app.config["APP_SETTINGS"]
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    database = app.extensions["database"]
    registry = ModelRegistryService(database, settings)
    spec = remote_asr_model_spec(settings)
    registry.register(spec)
    registry.enable(spec.model_id, spec.model_version)
    registry.set_default(spec.model_id, spec.model_version)
    worker_id = "1" * 32
    runtime = RuntimeRepository(database)
    RemoteAsrService(database, settings).begin_worker(worker_id)
    runtime.report(component_id="remote_asr_worker", session_id=worker_id, pid=123,
                   state="ready", model_ready=True)
    monkeypatch.setattr("backend.service.asr.remote_service.probe_remote_asr", lambda _: SimpleNamespace(ready=True))
    buffer = BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 1600)
    content = buffer.getvalue()
    client = app.test_client()
    executor = ExecutionService(database, settings, asr_profile="remote")
    dispatcher = DispatchService(database, settings, app.extensions["app_logger"])
    broker = BrokerRepository(database)

    def submit(key="fixture-request", target=None, **fields):
        return (target or client).post("/api/tasks/asr", headers={"Idempotency-Key": key},
            data={"modelId": spec.model_id, "file": (BytesIO(content), "fixture.wav"), **fields})

    def succeed():
        response = submit()
        assert response.status_code == 201, response.get_json()
        task = response.get_json()["data"]["task"]
        dispatcher.tick()
        delivery = broker.receive(worker_id=worker_id, queue_key=REMOTE_QUEUE, visibility_seconds=30)
        acquired = executor.acquire(delivery)
        assert acquired.decision == "acquired"
        result = TranscriptionResult(text="隔离版本测试", segments=[], audio_duration_seconds=0.1,
            engine_metadata=AsrEngineMetadata(adapter_key="remote-v2-sensevoice", runtime_versions={"remote-v2-api": "14.2"}))
        assert executor.commit_result(acquired.lease, result)
        return task

    return SimpleNamespace(app=app, client=client, settings=settings, db=database, registry=registry,
        spec=spec, worker_id=worker_id, runtime=runtime, executor=executor, dispatcher=dispatcher,
        broker=broker, submit=submit, succeed=succeed, values=values, factory=app_factory)


def test_optional_upstream_version_is_consistent_through_catalog_and_completed_result(unknown_version_case):
    case = unknown_version_case
    assert case.spec.model_version.startswith("integration-") and len(case.spec.model_version) == 60
    available = case.client.get("/api/models").get_json()["data"]["items"]
    model = next(item for item in available if item["modelId"] == case.spec.model_id)
    assert model["versionInfo"] == VERSION_INFO
    assert model["modelVersion"] == case.spec.model_version
    registered = next(item for item in case.registry.list_versions().model_dump(by_alias=True)["items"]
                      if item["spec"]["modelId"] == case.spec.model_id)
    assert registered["spec"]["versionInfo"] == VERSION_INFO
    status = case.client.get("/api/asr/external/status").get_json()["data"]
    assert status["state"] == "ready" and status["versionInfo"] == VERSION_INFO
    task = case.succeed()
    assert task["versionInfo"] == VERSION_INFO
    task_id = task["taskId"]
    detail = case.client.get(f"/api/tasks/{task_id}").get_json()["data"]
    listed = case.client.get("/api/tasks").get_json()["data"]["items"][0]
    result = case.client.get(f"/api/tasks/{task_id}/result").get_json()["data"]
    for payload in (detail, listed, result):
        assert payload["modelVersion"] == case.spec.model_version
        assert payload["versionInfo"] == VERSION_INFO
    assert result["output"]["text"] == "隔离版本测试"
    assert result["output"]["engineMetadata"]["runtimeVersions"] == {"remote-v2-api": "14.2"}
    assert result["output"].get("modelRevision") is None
    assert result["output"]["engineMetadata"].get("modelRevision") is None
    assert "versionInfo" not in result["output"]
    with case.db.session_factory() as session:
        stored = session.get(TaskRecord, task_id)
        assert "versionInfo" not in stored.result_json
        assert stored.model_version == case.spec.model_version


def test_known_version_restart_and_disable_preserve_unknown_history_and_replay(unknown_version_case):
    case = unknown_version_case
    task = case.succeed()
    task_id = task["taskId"]
    before = case.client.get(f"/api/tasks/{task_id}/result").get_json()["data"]
    case.registry.disable(case.spec.model_id, case.spec.model_version)
    restarted = case.factory(**{**case.values, "database_path": case.settings.database_path,
                              "remote_asr_model_version": "provider-release-2"})
    settings = restarted.config["APP_SETTINGS"]
    registry = ModelRegistryService(restarted.extensions["database"], settings)
    known = remote_asr_model_spec(settings)
    registry.register(known)
    registry.enable(known.model_id, known.model_version)
    registry.set_default(known.model_id, known.model_version)
    client = restarted.test_client()
    assert client.get(f"/api/tasks/{task_id}/result").get_json()["data"] == before
    assert client.get(f"/api/tasks/{task_id}").get_json()["data"]["versionInfo"] == VERSION_INFO
    replay = case.submit(target=client)
    assert replay.status_code == 200, replay.get_json()
    receipt = replay.get_json()["data"]
    assert receipt["replayed"] and receipt["task"]["taskId"] == task_id
    assert receipt["task"]["versionInfo"] == VERSION_INFO
    assert receipt["task"]["modelVersion"] == case.spec.model_version
    # 新版本须由真实绑定到该修订的槽报告就绪，不能复用旧版本的ready。
    new_worker = "2" * 32
    RemoteAsrService(restarted.extensions["database"], settings).begin_worker(new_worker)
    RuntimeRepository(restarted.extensions["database"]).report(
        component_id="remote_asr_worker", session_id=new_worker, pid=124,
        state="ready", model_ready=True)
    fresh = case.submit("new-version-request", target=client)
    assert fresh.status_code == 201, fresh.get_json()
    fresh_task = fresh.get_json()["data"]["task"]
    assert fresh_task["modelVersion"] == "provider-release-2" and "versionInfo" not in fresh_task
    models = client.get("/api/models").get_json()["data"]["items"]
    current = next(item for item in models if item["modelId"] == known.model_id)
    assert "versionInfo" not in current
    status = client.get("/api/asr/external/status").get_json()["data"]
    assert "versionInfo" not in status


def test_missing_version_metadata_cannot_overwrite_same_registered_identity(unknown_version_case):
    case = unknown_version_case
    original = case.spec.model_dump(mode="json", by_alias=True)
    falsely_known = {key: value for key, value in original.items() if key != "versionInfo"}
    with pytest.raises(AppError) as raised:
        case.registry.register(ModelRegistration.model_validate(falsely_known))
    assert raised.value.code == "MODEL_VERSION_IMMUTABLE"
    assert case.registry.register(case.spec).created is False
    with case.db.session_factory() as session:
        stored = session.get(ModelVersionRecord, (case.spec.model_id, case.spec.model_version))
        assert stored.spec_json == original


def test_version_metadata_does_not_create_one_query_per_listed_task(unknown_version_case):
    case = unknown_version_case
    for index in range(6):
        assert case.submit(f"batch-{index}").status_code == 201
    assert case.submit("local", modelId="whisper-small").status_code == 201
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT") and "model_versions" in statement:
            statements.append(statement)

    event.listen(case.db.engine, "before_cursor_execute", capture)
    try:
        response = case.client.get("/api/tasks?pageSize=20")
    finally:
        event.remove(case.db.engine, "before_cursor_execute", capture)
    assert response.status_code == 200
    items = response.get_json()["data"]["items"]
    remote = [item for item in items if item["modelId"] == case.spec.model_id]
    assert len(remote) == 6 and all(item["versionInfo"] == VERSION_INFO for item in remote)
    assert "versionInfo" not in next(item for item in items if item["modelId"] == "whisper-small")
    assert len(statements) == 1


def test_known_config_cannot_claim_task_pinned_to_unknown_revision(unknown_version_case):
    case = unknown_version_case
    response = case.submit()
    assert response.status_code == 201
    task_id = response.get_json()["data"]["task"]["taskId"]
    case.dispatcher.tick()
    delivery = case.broker.receive(worker_id=case.worker_id, queue_key=REMOTE_QUEUE, visibility_seconds=30)
    changed = case.settings.model_copy(update={"remote_asr_model_version": "provider-release-2"})
    decision = ExecutionService(case.db, changed, asr_profile="remote").acquire(delivery)
    assert decision.decision == "busy"
    detail = case.client.get(f"/api/tasks/{task_id}").get_json()["data"]
    assert detail["attempt"] == 0 and detail["status"] == "queued"
    assert detail["versionInfo"] == VERSION_INFO
