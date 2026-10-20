"""真实数据库中的状态、结果和运行报告 HTTP 契约；推理输出为明确的测试替身。"""

from datetime import timedelta
import io
import wave

from alembic import command
from alembic.config import Config
import pytest

from backend.config.asr.model_catalog import QUEUE_KEY
from backend.config.settings.app_settings import PROJECT_ROOT
from backend.data.broker.broker_repository import BrokerRepository
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.schema.asr.asr_schema import TranscriptionResult, TranscriptionSegment
from backend.schema.execution.execution_schema import FailureInfo
from backend.service.dispatch.dispatch_service import DispatchService
from backend.service.dispatch.dispatcher_runtime import DispatcherRuntime
from backend.service.execution.execution_service import ExecutionService


@pytest.fixture
def execution_case(app_factory, tmp_path):
    app = app_factory(upload_dir=tmp_path / "uploads")
    settings = app.config["APP_SETTINGS"]
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 1600)
    content = buffer.getvalue()
    client = app.test_client()

    def submit(key="execution-api"):
        return client.post("/api/tasks/asr", headers={"Idempotency-Key": key},
                           data={"file": (io.BytesIO(content), "test.wav")})

    database = app.extensions["database"]
    broker = BrokerRepository(database)
    executor = ExecutionService(database, settings)
    dispatcher = DispatchService(database, settings, app.extensions["app_logger"])
    return app, client, submit, dispatcher, broker, executor


def fake_result(settings):
    # 仅验证结果持久化与 API，不能当作真实 ASR 输出。
    return TranscriptionResult(
        text="测试替身输出", segments=[TranscriptionSegment(start=0, end=0.1, text="测试替身输出")],
        language_requested="zh", language="zh", language_probability=1,
        audio_duration_seconds=0.1, model_repo=settings.asr_model_repo,
        model_revision=settings.asr_model_revision, device="cpu", compute_type="int8_float32",
        cpu_threads=4, model_load_seconds=0, transcription_seconds=0.1, total_seconds=0.1,
        faster_whisper_version="test-double", ctranslate2_version="test-double",
    )


def test_result_endpoint_follows_created_queued_running_succeeded_and_persists(execution_case):
    app, client, submit, dispatcher, broker, executor = execution_case
    task = submit().get_json()["data"]["task"]
    path = "/api/tasks/" + task["taskId"]
    assert task["attempt"] == 0 and task["resultAvailable"] is False
    pending = client.get(path + "/result")
    assert pending.status_code == 409
    assert pending.get_json()["error"]["code"] == "RESULT_NOT_READY"
    assert dispatcher.tick() == 1
    assert client.get(path).get_json()["data"]["status"] == "queued"
    delivery = broker.receive(worker_id="test-worker", queue_key=QUEUE_KEY, visibility_seconds=30)
    decision = executor.acquire(delivery)
    assert decision.decision == "acquired"
    running = client.get(path).get_json()["data"]
    assert running["status"] == "running" and running["attempt"] == 1
    assert running["startedAt"].endswith("Z")
    assert "leaseToken" not in running and "inputRef" not in running
    result = fake_result(app.config["APP_SETTINGS"])
    assert executor.commit_result(decision.lease, result)
    assert broker.acknowledge(delivery)
    succeeded = client.get(path).get_json()["data"]
    assert succeeded["status"] == "succeeded" and succeeded["resultAvailable"]
    assert succeeded["finishedAt"].endswith("Z") and succeeded["error"] is None
    output = client.get(path + "/result")
    assert output.status_code == 200
    assert output.get_json()["data"] == {
        "taskId": task["taskId"], "modelId": task["modelId"], "modelVersion": task["modelVersion"],
        "schemaVersion": "1", "output": result.model_dump(by_alias=True, mode="json"),
    }
    app.extensions["database"].dispose()
    assert client.get(path + "/result").get_json()["data"]["output"]["text"] == "测试替身输出"
    replay = submit()
    assert replay.status_code == 200
    assert replay.get_json()["data"]["task"] == succeeded
    assert client.get("/api/tasks").get_json()["data"]["items"][0] == succeeded


def test_failed_execution_is_queryable_without_a_success_result(execution_case):
    app, client, submit, dispatcher, broker, executor = execution_case
    task_id = submit().get_json()["data"]["task"]["taskId"]
    dispatcher.tick()
    delivery = broker.receive(worker_id="test-worker", queue_key=QUEUE_KEY, visibility_seconds=30)
    decision = executor.acquire(delivery)
    failure = FailureInfo(code="TRANSCRIPTION_FAILED", message="测试注入的执行失败。")
    assert executor.commit_failure(decision.lease, failure)
    assert broker.acknowledge(delivery)
    response = client.get(f"/api/tasks/{task_id}")
    assert response.get_json()["data"]["status"] == "failed"
    assert response.get_json()["data"]["error"] == failure.model_dump(by_alias=True)
    result = client.get(f"/api/tasks/{task_id}/result")
    assert result.status_code == 409
    assert result.get_json()["error"]["code"] == "TASK_FAILED"


@pytest.mark.parametrize("path,status,code", [
    ("/api/tasks/" + "0" * 32 + "/result", 404, "TASK_NOT_FOUND"),
    ("/api/tasks/invalid/result", 422, "VALIDATION_ERROR"),
    ("/api/tasks/" + "0" * 32 + "/result?extra=1", 422, "VALIDATION_ERROR"),
    ("/api/runtime?extra=1", 422, "VALIDATION_ERROR"),
])
def test_new_endpoints_validate_paths_and_query(execution_case, path, status, code):
    response = execution_case[1].get(path)
    assert response.status_code == status
    assert response.get_json()["error"]["code"] == code


def test_runtime_does_not_claim_model_readiness_without_reports(execution_case):
    response = execution_case[1].get("/api/runtime")
    assert response.status_code == 200
    state = response.get_json()["data"]
    for name in ("worker", "visionWorker", "dispatcher"):
        assert state[name]["availability"] == "not_started"
        assert state[name]["ready"] is False
        assert state[name]["pid"] is None
    assert state["executionConcurrency"] == 2
    assert state["automaticExecutionRetry"] is True


@pytest.mark.parametrize("queued", [False, True])
def test_cancel_waiting_task_is_idempotent_and_never_infers(execution_case, queued):
    _, client, submit, dispatcher, broker, executor = execution_case
    task = submit().get_json()["data"]["task"]
    path = f"/api/tasks/{task['taskId']}"
    if queued:
        assert dispatcher.tick() == 1
    response = client.post(path + "/cancel")
    assert response.status_code == 200
    receipt = response.get_json()["data"]
    assert receipt["decision"] == "cancelled"
    assert receipt["task"]["status"] == "cancelled" and receipt["task"]["attempt"] == 0
    again = client.post(path + "/cancel").get_json()["data"]
    assert again["decision"] in ("cancelled", "already_finished")
    assert again["task"] == receipt["task"]
    assert client.get(path + "/result").get_json()["error"]["code"] == "TASK_CANCELLED"
    assert dispatcher.tick() == 0
    delivery = broker.receive(worker_id="test", queue_key=QUEUE_KEY, visibility_seconds=30)
    if delivery:
        assert executor.acquire(delivery).decision == "terminal_or_stale"


def test_running_cancel_rejects_result_until_worker_confirms_stop(execution_case):
    app, client, submit, dispatcher, broker, executor = execution_case
    task_id = submit().get_json()["data"]["task"]["taskId"]
    dispatcher.tick()
    delivery = broker.receive(worker_id="worker", queue_key=QUEUE_KEY, visibility_seconds=30)
    lease = executor.acquire(delivery).lease
    path = f"/api/tasks/{task_id}"
    receipt = client.post(path + "/cancel").get_json()["data"]
    assert receipt["decision"] == "cancel_requested"
    assert receipt["task"]["status"] == "cancel_requested"
    assert not executor.commit_result(lease, fake_result(app.config["APP_SETTINGS"]))
    assert executor.control(lease) == "cancel"
    # 模拟 Worker 已确认子进程结束的后续调用；这里没有启动模型。
    assert executor.finish_cancel(lease)
    assert executor.finish_cancel(lease)
    assert client.get(path).get_json()["data"]["status"] == "cancelled"


def test_cancel_cannot_overwrite_success(execution_case):
    app, client, submit, dispatcher, broker, executor = execution_case
    task_id = submit().get_json()["data"]["task"]["taskId"]
    dispatcher.tick()
    delivery = broker.receive(worker_id="worker", queue_key=QUEUE_KEY, visibility_seconds=30)
    lease = executor.acquire(delivery).lease
    assert executor.commit_result(lease, fake_result(app.config["APP_SETTINGS"]))
    response = client.post(f"/api/tasks/{task_id}/cancel").get_json()["data"]
    assert response["decision"] == "already_finished"
    assert response["task"]["status"] == "succeeded" and response["task"]["resultAvailable"]


@pytest.mark.parametrize("suffix,body,content_type,status", [
    ("/cancel", None, None, 404),
    ("/cancel?extra=1", None, None, 422),
    ("/cancel", '{"force":true}', "application/json", 422),
    ("/cancel", 'null', "application/json", 422),
    ("/cancel", 'force=true', "text/plain", 415),
])
def test_cancel_validates_input_and_unknown_task(execution_case, suffix, body, content_type, status):
    response = execution_case[1].post("/api/tasks/" + "0" * 32 + suffix, data=body, content_type=content_type)
    assert response.status_code == status


def test_cancel_is_scoped_to_fixed_local_caller(execution_case):
    from sqlalchemy import text
    app, client, submit, *_ = execution_case
    task_id = submit().get_json()["data"]["task"]["taskId"]
    with app.extensions["database"].engine.begin() as connection:
        connection.execute(text("UPDATE tasks SET caller_id='another' WHERE task_id=:id"), {"id": task_id})
    assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 404


def test_runtime_reports_actual_idle_busy_stale_and_stopped_states(execution_case, monkeypatch):
    app, client, *_ = execution_case
    repository = RuntimeRepository(app.extensions["database"])
    kwargs = dict(component_id="worker", session_id="a" * 32, pid=1234, child_pid=5678)
    repository.report(**kwargs, state="model_loading", model_ready=False)
    loading = client.get("/api/runtime").get_json()["data"]["worker"]
    assert loading["availability"] == "online" and not loading["ready"]
    repository.report(**kwargs, state="idle", model_ready=True)
    ready = client.get("/api/runtime").get_json()["data"]["worker"]
    assert ready["ready"] and ready["modelReady"]
    assert (ready["pid"], ready["childPid"]) == (1234, 5678)
    repository.report(**kwargs, state="busy", model_ready=True, active_task_id="b" * 32)
    busy = client.get("/api/runtime").get_json()["data"]["worker"]
    assert busy["modelReady"] and not busy["ready"] and busy["activeTaskId"] == "b" * 32
    old_now = repository.now()
    with monkeypatch.context() as scoped:
        scoped.setattr(app.extensions["runtime_service"].repository, "now", lambda: old_now + timedelta(seconds=20))
        stale = client.get("/api/runtime").get_json()["data"]["worker"]
        assert stale["availability"] == "stale" and not stale["ready"] and not stale["modelReady"]
    repository.report(**kwargs, state="stopped")
    stopped = client.get("/api/runtime").get_json()["data"]["worker"]
    assert stopped["availability"] == "stopped" and not stopped["ready"]


def test_unmigrated_runtime_and_result_fail_clearly(app_factory, tmp_path):
    app = app_factory(upload_dir=tmp_path / "uploads")
    assert app.test_client().get("/api/runtime").status_code == 503
    assert app.test_client().get("/api/tasks/" + "0" * 32 + "/result").status_code == 503


def test_dispatch_failure_is_visible_during_backoff_and_clears_after_recovery(execution_case, monkeypatch):
    app, client, submit, *_ = execution_case
    settings = app.config["APP_SETTINGS"]
    submit()
    runtime = DispatcherRuntime(settings, app.extensions["app_logger"])
    try:
        runtime.start()
        def fail_publish(*args, **kwargs):
            raise OSError("injected disk failure")
        with monkeypatch.context() as scoped:
            scoped.setattr(runtime.dispatcher.broker, "publish", fail_publish)
            runtime._last_report = 0
            assert runtime.tick() == 0
        failed = client.get("/api/runtime").get_json()["data"]["dispatcher"]
        assert failed["availability"] == "error" and not failed["ready"]
        assert failed["lastError"]
        # 未到补投时间，仍能看见已登记的失败，而不是回到 idle。
        runtime._last_report = 0
        runtime.tick()
        assert client.get("/api/runtime").get_json()["data"]["dispatcher"]["availability"] == "error"
        from sqlalchemy import update
        from backend.data.tasks.task_models import DispatchEvent
        with app.extensions["database"].transaction() as session:
            session.execute(update(DispatchEvent).values(available_at=RuntimeRepository(app.extensions["database"]).now()))
        assert runtime.tick() == 1
        restored = client.get("/api/runtime").get_json()["data"]["dispatcher"]
        assert restored["availability"] == "online" and restored["ready"] and restored["lastError"] is None
    finally:
        runtime.close()
