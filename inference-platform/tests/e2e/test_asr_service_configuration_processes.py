"""手工配置→真实回环 HTTP/Windows 子进程→结果；上游是固定替身，不是模型验收。"""

from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from email.policy import default
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
import threading
from types import SimpleNamespace
from uuid import uuid4
import wave

from alembic import command
from alembic.config import Config
from filelock import FileLock
import pytest
from sqlalchemy import select

from backend.config.settings.app_settings import PROJECT_ROOT, load_settings
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.asr.target_models import AsrCallPermit, AsrTargetRevision, AsrTargetWorkerSession
from backend.data.tasks.task_models import TaskRecord
from backend.service.dispatch.dispatch_service import DispatchService
from test_remote_asr_pool_processes import pool_case

pytestmark = pytest.mark.skipif(os.name != "nt", reason="真实受管子进程使用 Windows Job Object")

FIXTURE_TEXT = "手工接口配置的回环隔离结果。"
FIXTURE_MODEL = "fixture/openai-audio"
FIXTURE_AUTH_ENV = "ASR_ISOLATED_CHILD_TEST_KEY"
FIXTURE_AUTH_VALUE = "isolated-test-token-not-a-real-secret"


@pytest.fixture
def http_openai_asr(monkeypatch):
    """仅监听 127.0.0.1；执行真实 HTTP，但不加载或调用任何真实模型。"""
    monkeypatch.setenv(FIXTURE_AUTH_ENV, FIXTURE_AUTH_VALUE)
    control = SimpleNamespace(posts=[], gets=[], bad_auth=0, mode="ok",
                              entered=threading.Event(), release=threading.Event(),
                              release_by_request={})
    control.release.set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def answer(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authenticated(self):
            if self.headers.get("Authorization") == f"Bearer {FIXTURE_AUTH_VALUE}":
                return True
            control.bad_auth += 1
            self.answer({"error": {"message": "fixture authentication failed"}}, 401)
            return False

        def do_GET(self):
            if not self.authenticated():
                return
            control.gets.append(self.path)
            if self.path != "/custom/v1/models":
                self.answer({"error": {"message": "fixture path not found"}}, 404)
                return
            self.answer({"object": "list", "data": [{"id": FIXTURE_MODEL}]})

        def do_POST(self):
            if not self.authenticated():
                return
            if self.path != "/custom/v1/audio/transcriptions":
                self.answer({"error": {"message": "fixture path not found"}}, 404)
                return
            content_length = int(self.headers["Content-Length"])
            if not 0 < content_length < 128 * 1024:
                self.answer({"error": {"message": "fixture upload limit"}}, 413)
                return
            body = self.rfile.read(content_length)
            message = BytesParser(policy=default).parsebytes(
                ("Content-Type: " + self.headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
            fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                      for part in message.iter_parts()}
            control.posts.append(fields)
            control.entered.set()
            if control.mode == "hold":
                control.release.wait(20)
            try:
                # json 响应没有分段，平台必须保持 segments=[]，不能编造时间戳。
                self.answer({"text": FIXTURE_TEXT})
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    control.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield control
    finally:
        control.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.fixture
def remote_process_case(app_factory, tmp_path, http_openai_asr):
    """覆盖旧 fixture 的配置准备；继续复用原 pool_case 的实际子进程/排空设施。"""
    initial = app_factory(upload_dir=tmp_path / "uploads",
                          runtime_heartbeat_seconds=.1, poll_interval_seconds=.05)
    base_settings = initial.config["APP_SETTINGS"]
    migration = Config(str(PROJECT_ROOT / "alembic.ini"))
    migration.attributes["settings"] = base_settings
    command.upgrade(migration, "head")
    initial_client = initial.test_client()
    config = {
        "serviceId": "manual-loopback", "name": "隔离回环 HTTP 服务", "protocol": "openai-audio",
        "baseUrl": http_openai_asr.url, "modelId": "manual-loopback-asr",
        "upstreamModel": FIXTURE_MODEL, "apiKeyEnv": FIXTURE_AUTH_ENV,
        "transcribePath": "/custom/v1/audio/transcriptions", "healthPath": "/custom/v1/models",
        "connectTimeoutSeconds": 5, "totalTimeoutSeconds": 12,
    }
    assert initial_client.get("/api/asr/services").json["data"]["items"] == []
    assert http_openai_asr.gets == http_openai_asr.posts == []
    saved = initial_client.put("/api/asr/services/manual-loopback", json={"config": config})
    assert saved.status_code == 200, saved.json
    revision = saved.json["data"]["items"][0]["revision"]
    health = initial_client.post("/api/asr/services/test", json={"config": config})
    assert health.status_code == 200 and health.json["data"]["success"], health.json
    assert http_openai_asr.posts == []
    chosen = initial_client.post("/api/asr/services/selection",
        json={"serviceId": "manual-loopback", "expectedRevision": revision})
    assert chosen.status_code == 200 and chosen.json["data"]["restartRequired"], chosen.json
    environment = {
        "INFERENCE_DATABASE_PATH": str(base_settings.database_path),
        "INFERENCE_LOG_DIR": str(base_settings.log_dir),
        "INFERENCE_UPLOAD_DIR": str(base_settings.upload_dir),
        "INFERENCE_RUNTIME_HEARTBEAT_SECONDS": ".1", "INFERENCE_POLL_INTERVAL_SECONDS": ".05",
    }
    selected = load_settings(env_file=None, environ=environment)
    assert selected.asr_service_revision == revision
    # 正式 app 工厂的 register_running 完成登记；此 fixture 不直接登记模型。
    app = app_factory(**selected.model_dump())
    client, database = app.test_client(), app.extensions["database"]
    state = client.get("/api/asr/services").json["data"]
    assert state["runningServiceId"] == "manual-loopback" and not state["restartRequired"]
    model = state["runningModel"]
    assert model["modelVersion"].startswith("integration-")
    audio = BytesIO()
    with wave.open(audio, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 1600)

    def submit(key):
        return client.post("/api/tasks/asr", headers={"Idempotency-Key": key}, data={
            "modelId": model["modelId"], "modelVersion": model["modelVersion"], "language": "auto",
            "file": (BytesIO(audio.getvalue()), "isolated-openai.wav"),
        })

    return SimpleNamespace(client=client, database=database, settings=selected, server=http_openai_asr,
        dispatcher=DispatchService(database, selected, app.extensions["app_logger"]),
        submit=submit, task=lambda task_id: client.get("/api/tasks/" + task_id).json["data"],
        revision=revision, model=model, initial_client=initial_client, environment=environment,
        app_factory=app_factory, health=health.json["data"])


def test_selected_openai_config_runs_real_http_child_and_preserves_history(pool_case):
    case = pool_case
    pool = case.start_pool()
    child_pids = [worker.runner.pid for worker in pool.workers]
    assert all(pid is not None and pid != os.getpid() for pid in child_pids)
    case.server.mode = "hold"
    case.server.release.clear()
    submitted = case.submit("manual-http-child")
    assert submitted.status_code == 201, submitted.json
    task_id = submitted.json["data"]["task"]["taskId"]
    assert case.dispatcher.tick() == 1
    case.pump_pool(lambda: case.server.entered.is_set())
    assert case.task(task_id)["status"] == "running"
    with case.database.session_factory() as session:
        permit = session.get(AsrCallPermit, task_id)
        assert permit is not None and permit.state == "held"
        assert permit.worker_session_id in {worker.session_id for worker in pool.workers}
        target = session.get(AsrTargetRevision, permit.revision_id)
        assert target.config_snapshot["protocol"] == "openai-audio"
        assert target.device_kind == "unknown"
    assert len(case.server.posts) == 1 and case.server.bad_auth == 0
    uploaded = case.server.posts[0]
    assert set(uploaded) == {"file", "model", "response_format"}
    assert uploaded["file"].startswith(b"RIFF") and uploaded["model"] == FIXTURE_MODEL.encode()
    assert uploaded["response_format"] == b"json"
    case.server.release.set()
    case.pump_pool(lambda: case.task(task_id)["status"] == "succeeded")
    result = case.client.get(f"/api/tasks/{task_id}/result").json["data"]
    assert result["output"]["text"] == FIXTURE_TEXT and result["output"]["segments"] == []
    assert result["output"]["engineMetadata"]["runtimeVersions"] == {}
    assert result["versionInfo"] == {"versionBasis": "integration_revision", "upstreamVersion": None}
    assert case.task(task_id)["attempt"] == 1
    with case.database.session_factory() as session:
        assert session.get(AsrCallPermit, task_id).state == "released"
        assert session.get(RemoteAsrCall, task_id).state == "released"
        assert len(session.scalars(select(TaskRecord)).all()) == 1
    pool.request_drain()
    case.pump_pool(lambda: pool.drained)
    case.pump_pool(pool.close)
    assert all(not worker.runner.is_alive() for worker in pool.workers)
    with FileLock(pool._lock.lock_file, timeout=0):
        pass
    with case.database.session_factory() as session:
        assert all(session.get(AsrTargetWorkerSession, worker.session_id).active_slot_key is None
                   for worker in pool.workers)
    # 重建 API 后只读历史及幂等查询；不启动调用池，不再提交给上游。
    restored = case.app_factory(**load_settings(env_file=None, environ=case.environment).model_dump())
    restored_client = restored.test_client()
    assert restored_client.get(f"/api/tasks/{task_id}/result").json["data"] == result
    assert restored_client.get(f"/api/tasks/{task_id}").json["data"]["status"] == "succeeded"
    assert case.submit("manual-http-child").status_code == 200
    assert len(case.server.posts) == 1
    # 页面 TXT/JSON 下载取自此 DTO；保存相同内容用于交付证据，不声称浏览器下载事件。
    evidence_dir = PROJECT_ROOT / "runtime/verification/unified-asr-config"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    run_id = "openai-http-child-" + uuid4().hex
    text_path, json_path = evidence_dir / f"{run_id}.txt", evidence_dir / f"{run_id}-result.json"
    text_path.write_text(result["output"]["text"], encoding="utf-8")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    assert text_path.read_text(encoding="utf-8") == FIXTURE_TEXT
    assert json.loads(json_path.read_text(encoding="utf-8")) == result
    evidence = {
        "observedAt": datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).isoformat(),
        "scope": "isolated-loopback-http-and-real-windows-children; fixed fixture response, no real model",
        "configuration": {"serviceId": "manual-loopback", "revision": case.revision,
                          "protocol": "openai-audio", "bootstrapRegistered": True},
        "healthSucceeded": case.health["success"], "healthDoesNotProveInference": True,
        "childPids": child_pids, "childrenExited": True, "poolLockReleased": True,
        "upstreamPosts": len(case.server.posts), "badAuthRequests": case.server.bad_auth,
        "taskId": task_id, "attempt": 1, "status": "succeeded",
        "permitState": "released", "callState": "released", "workerSlotsReleased": True,
        "restartResultPreserved": True, "replayedWithoutResend": True, "deviceKind": "unknown",
        "exports": {"txt": text_path.name, "json": json_path.name,
                    "txtSha256": sha256(text_path.read_bytes()).hexdigest(),
                    "jsonSha256": sha256(json_path.read_bytes()).hexdigest()},
        "browserDownloadEventVerified": False,
    }
    (evidence_dir / f"{run_id}-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
