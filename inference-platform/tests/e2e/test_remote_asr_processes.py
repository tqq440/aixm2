"""真实 HTTPS、Windows 子进程与 SQLite 链路；响应为隔离协议替身，不是模型验证。"""

from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import wave

from alembic import command
from alembic.config import Config
import pytest

from backend.config.settings.app_settings import ENV_FIELDS, PROJECT_ROOT
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.tasks.task_models import TaskRecord
from backend.service.asr.adapter_catalog import remote_asr_model_spec
from backend.service.dispatch.dispatch_service import DispatchService
from backend.service.models.model_registry_service import ModelRegistryService
from backend.service.worker.worker_service import WorkerService

pytestmark = pytest.mark.skipif(os.name != "nt", reason="真实受管子进程使用 Windows Job Object")


@pytest.fixture
def https_asr(tmp_path):
    """只监听回环地址；证书、私钥及音频均为每次测试新生成的临时资料。"""
    openssl = Path(sys.prefix) / "Library/bin/openssl.exe"
    if not openssl.is_file():
        pytest.skip("当前 Python 环境没有 OpenSSL 测试证书工具；不自动安装")
    certificate, key, config = tmp_path / "test-ca.pem", tmp_path / "test-key.pem", tmp_path / "openssl.cnf"
    config.write_text("[req]\ndistinguished_name=dn\n[dn]\n", encoding="ascii")
    subprocess.run([str(openssl), "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(certificate), "-days", "1", "-subj", "/CN=127.0.0.1",
        "-addext", "subjectAltName=IP:127.0.0.1", "-config", str(config)],
        capture_output=True, check=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    control = SimpleNamespace(mode="ok", posts=[], entered=threading.Event(), release=threading.Event())
    control.release_by_request, control.post_modes, control.response_texts = {}, {}, {}
    control.release.set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def answer(self, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            assert self.path == "/api/v1/asr/health"
            self.answer({"success": True, "data": {"status": "ok", "model_loaded": True, "version": "14.2"}})

        def do_POST(self):
            assert self.path == "/api/v1/asr/transcripts"
            body = self.rfile.read(int(self.headers["Content-Length"]))
            message = BytesParser(policy=default).parsebytes(
                ("Content-Type: " + self.headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
            fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                      for part in message.iter_parts()}
            control.posts.append(fields)
            request_id = fields["request_id"].decode()
            mode = control.post_modes.get(request_id, control.mode)
            control.entered.set()
            if mode == "hold":
                control.release_by_request.get(request_id, control.release).wait(15)
            if mode == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                self.close_connection = True
                return
            try:
                self.answer({"success": True, "engine": "sensevoice", "details": {
                    "request_id": request_id,
                    "segments": [{"start": 0.0, "end": 0.1,
                        "text": control.response_texts.get(request_id, "隔离协议测试")}]}})
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                pass  # 调用超时测试可先关闭连接；并不据此认为远端取消。

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    control.url = f"https://127.0.0.1:{server.server_port}"
    control.certificate = certificate
    try:
        yield control
    finally:
        control.release.set()
        for release in control.release_by_request.values():
            release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def remote_process_case(app_factory, tmp_path, https_asr):
    app = app_factory(upload_dir=tmp_path / "uploads", remote_asr_base_url=https_asr.url,
        remote_asr_ca_file=https_asr.certificate, remote_asr_model_id="test-sensevoice",
        # 与生产默认健康预算一致；Windows 新进程首次 httpcore2/truststore 导入
        # 包含系统版本探测，实测冷启动约 3.5 秒，不能被夹具的 2 秒提前中断。
        remote_asr_model_version="isolated-fixture-v1", remote_asr_connect_timeout_seconds=5,
        remote_asr_total_timeout_seconds=8, runtime_heartbeat_seconds=.1, poll_interval_seconds=.05)
    settings, database = app.config["APP_SETTINGS"], app.extensions["database"]
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    registry = ModelRegistryService(database, settings)
    spec = remote_asr_model_spec(settings)
    registry.register(spec)
    registry.enable(spec.model_id, spec.model_version)
    registry.set_default(spec.model_id, spec.model_version)
    audio = BytesIO()
    with wave.open(audio, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 1600)
    client, workers = app.test_client(), []
    dispatcher = DispatchService(database, settings, app.extensions["app_logger"])

    class Case:
        def start_worker(self):
            worker = WorkerService(database, settings, app.extensions["app_logger"], asr_profile="remote")
            workers.append(worker)
            worker.start()
            self.pump(lambda: worker.state == "ready")
            return worker

        def pump(self, condition, timeout=25):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                for worker in workers:
                    if worker._owns_lock:
                        worker.tick()
                if condition():
                    return
                time.sleep(.05)
            pytest.fail("隔离进程链路未在测试期限内完成：" + str([worker.state for worker in workers]))

        def submit(self, key):
            return client.post("/api/tasks/asr", headers={"Idempotency-Key": key}, data={
                "modelId": spec.model_id, "modelVersion": spec.model_version, "language": "auto",
                "file": (BytesIO(audio.getvalue()), "isolated.wav")})

        def task(self, task_id):
            return client.get("/api/tasks/" + task_id).get_json()["data"]

    case = Case()
    case.client, case.database, case.settings = client, database, settings
    case.server, case.dispatcher = https_asr, dispatcher
    try:
        yield case
    finally:
        https_asr.release.set()
        for worker in workers:
            assert worker.close(), "隔离测试必须确认本次创建的 HTTP 子进程退出"


def test_https_real_child_drain_preserves_queue_then_restart_finishes_it(remote_process_case):
    case = remote_process_case
    worker = case.start_worker()
    case.server.mode = "hold"
    case.server.release.clear()
    first, second = case.submit("first"), case.submit("second")
    assert first.status_code == second.status_code == 201
    first_id, second_id = [item.get_json()["data"]["task"]["taskId"] for item in (first, second)]
    deadlines = {key: case.task(second_id)[key] for key in ("queueDeadlineAt", "totalDeadlineAt")}
    assert case.dispatcher.tick() == 2
    case.pump(lambda: case.server.entered.is_set())
    assert case.task(first_id)["status"] == "running"
    rejected = case.client.post(f"/api/tasks/{first_id}/cancel")
    assert rejected.status_code == 409
    assert rejected.get_json()["error"]["code"] == "REMOTE_ASR_CANCEL_UNSUPPORTED"
    worker.request_drain()
    worker.tick()
    assert worker.runner.is_alive() and not worker.drained
    draining = case.client.get("/api/asr/external/status").get_json()["data"]
    assert draining["state"] == "stopping" and draining["currentTaskId"] == first_id
    assert case.submit("after-drain").status_code == 503
    case.server.release.set()
    case.pump(lambda: worker.drained)
    assert case.task(first_id)["status"] == "succeeded"
    assert case.task(second_id)["status"] == "queued"
    assert {key: case.task(second_id)[key] for key in deadlines} == deadlines
    assert len(case.server.posts) == 1
    assert worker.close()
    assert not worker.runner.is_alive()
    assert case.client.get(f"/api/tasks/{first_id}/result").get_json()["data"]["output"]["text"] == "隔离协议测试"
    case.server.mode = "ok"
    restarted = case.start_worker()
    case.pump(lambda: case.task(second_id)["status"] == "succeeded")
    assert len(case.server.posts) == 2
    assert all(post["outputs"] == b"transcript" and post["engine"] == b"sensevoice"
               and post["audio"].startswith(b"RIFF") for post in case.server.posts)
    assert case.submit("first").status_code == 200
    assert len(case.server.posts) == 2
    assert restarted.close()


def test_https_disconnect_keeps_unknown_across_worker_restart_without_resending(remote_process_case):
    case = remote_process_case
    worker = case.start_worker()
    case.server.mode = "disconnect"
    first, second = case.submit("unknown"), case.submit("queued")
    assert first.status_code == second.status_code == 201
    first_id, second_id = [item.get_json()["data"]["task"]["taskId"] for item in (first, second)]
    case.dispatcher.tick()
    case.pump(lambda: case.task(first_id)["status"] == "failed")
    task = case.task(first_id)
    assert task["remoteOutcome"] == "unknown" and task["attempt"] == task["maxAttempts"] == 1
    assert len(case.server.posts) == 1
    with case.database.session_factory() as session:
        call = session.get(RemoteAsrCall, first_id)
        assert call.state == "uncertain"
        assert session.get(TaskRecord, second_id).status == "queued"
    assert worker.close()
    case.server.mode = "ok"
    restarted = case.start_worker()
    for _ in range(4):
        restarted.tick()
    state = case.client.get("/api/asr/external/status").get_json()["data"]
    assert state["state"] == "uncertain"
    assert case.submit("blocked").status_code == 503
    assert case.submit("unknown").status_code == 200
    assert len(case.server.posts) == 1
    assert case.task(second_id)["attempt"] == 0
    assert restarted.close()


def test_actual_remote_entry_ctrl_break_drains_http_before_job_exit(remote_process_case, tmp_path):
    """执行正式 child_entry 与 run_worker，真实 CTRL_BREAK 只停止本机领取并排空。"""
    case = remote_process_case
    # 正式入口会读取 PROJECT_ROOT/.env；隔离源码根目录，避免日常令牌或
    # TLS 例外污染带可信测试 CA 的配置。保持真实入口和配置读取逻辑不变。
    isolated_project = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / "backend", isolated_project / "backend",
                    ignore=shutil.ignore_patterns("__pycache__"))
    script = tmp_path / "console_controller.py"
    stop_flag, signalled, evidence = tmp_path / "stop", tmp_path / "signalled", tmp_path / "evidence.json"
    service_log = tmp_path / "remote-entry.log"
    script.write_text('''import json
import os
from pathlib import Path
import sys
import time
from backend.common.process.platform_process import OwnedProcess, ProcessTreeJob

stop, signalled, evidence, service_log = map(Path, sys.argv[1:])
job = ProcessTreeJob()
owned = OwnedProcess('remote_asr_worker', [sys.executable, '-m', 'backend.modules.platform.child_entry', 'remote_asr_worker'],
    cwd=Path.cwd(), env=os.environ.copy(), log_path=service_log, job=job)
try:
    deadline = time.monotonic() + 35
    while not stop.exists():
        assert owned.poll() is None and time.monotonic() < deadline
        time.sleep(.02)
    assert not owned.stop(0)
    signalled.write_text('sent')
    while owned.poll() is None:
        assert time.monotonic() < deadline
        time.sleep(.02)
    assert owned.poll() == 0
    assert job.wait_empty(5)
    evidence.write_text(json.dumps({'exit_code': owned.poll(), 'job_active': job.active_count(), 'pid': owned.pid}))
finally:
    if owned.poll() is None or job.active_count():
        job.terminate()
    owned.process.wait(timeout=5)
    owned.close_log()
    job.close()
''', encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("INFERENCE_")}
    values = case.settings.model_dump(mode="json")
    for env_name, field in ENV_FIELDS.items():
        if values.get(field) is not None:
            env[env_name] = str(values[field])
    env.update(PYTHONPATH=str(isolated_project), PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    controller = subprocess.Popen([sys.executable, str(script), str(stop_flag), str(signalled),
        str(evidence), str(service_log)], cwd=isolated_project, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
    try:
        case.pump(lambda: case.client.get("/api/asr/external/status").get_json()["data"]["state"] == "ready")
        case.server.mode = "hold"
        case.server.release.clear()
        first, second = case.submit("signal-first"), case.submit("signal-second")
        assert first.status_code == second.status_code == 201
        first_id, second_id = [item.get_json()["data"]["task"]["taskId"] for item in (first, second)]
        case.dispatcher.tick()
        case.pump(lambda: case.server.entered.is_set())
        stop_flag.write_text("stop", encoding="ascii")
        case.pump(lambda: signalled.exists() and case.client.get("/api/asr/external/status").get_json()["data"]["state"] == "stopping")
        assert controller.poll() is None
        assert case.task(first_id)["status"] == "running"
        assert case.submit("signal-rejected").status_code == 503
        case.server.release.set()
        stdout, stderr = controller.communicate(timeout=20)
        assert controller.returncode == 0, stderr.decode("utf-8") + service_log.read_text(encoding="utf-8")
        assert json.loads(evidence.read_text())["job_active"] == 0
        assert case.task(first_id)["status"] == "succeeded"
        assert case.task(second_id)["status"] == "queued"
        assert len(case.server.posts) == 1
        runtime = case.client.get("/api/runtime").get_json()["data"]["remoteAsrWorker"]
        assert runtime["reportedState"] == "stopped"
    finally:
        case.server.release.set()
        if controller.poll() is None:
            controller.kill()
            controller.wait(timeout=5)
        controller.communicate(timeout=5)
