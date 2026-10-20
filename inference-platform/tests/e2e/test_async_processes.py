"""真实 API/投递器/Worker 进程链路；推理子进程明确使用测试替身。

只替换临时源码副本，不加载模型，也不把模拟文字作为真实转写证据。
"""

from datetime import datetime, timedelta, timezone
import ctypes
from ctypes import wintypes
import io
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave

from alembic import command
from alembic.config import Config
import pytest

from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT


TEST_CHILD_SOURCE = '''"""测试替身：延时返回模拟结果，不导入神经网络运行库。"""
import json
import os
import signal
import time

from backend.common.process.managed_process import MAX_MESSAGE_BYTES
from backend.schema.asr.asr_schema import TranscriptionResult, TranscriptionSegment
from backend.schema.worker.worker_schema import COMMAND_ADAPTER, ReadyEvent, ResultEvent, RunCommand, StartCommand, StopCommand


def inference_child(connection):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    try:
        start = COMMAND_ADAPTER.validate_json(connection.recv_bytes(MAX_MESSAGE_BYTES))
        assert isinstance(start, StartCommand)
        settings = start.settings
        trace_path = settings.log_dir / "test-child-events.jsonl"
        connection.send_bytes(ReadyEvent(model_load_seconds=0).model_dump_json().encode("utf-8"))
        while True:
            command = COMMAND_ADAPTER.validate_json(connection.recv_bytes(MAX_MESSAGE_BYTES))
            if isinstance(command, StopCommand):
                return
            assert isinstance(command, RunCommand)
            with trace_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps({"kind": "start", "taskId": command.input.task_id, "pid": os.getpid(), "monotonic": time.monotonic()}) + "\\n")
            time.sleep(3.2)
            text = "模拟结果，仅用于进程链路测试"
            result = TranscriptionResult(
                text=text, segments=[TranscriptionSegment(start=0, end=0.125, text=text)],
                language="zh", language_requested=command.input.language, language_probability=1,
                audio_duration_seconds=0.125, model_repo=settings.asr_model_repo,
                model_revision=settings.asr_model_revision, device="cpu", compute_type="test-double",
                cpu_threads=1, model_load_seconds=0, transcription_seconds=3.2, total_seconds=3.2,
                faster_whisper_version="test-double", ctranslate2_version="test-double",
            )
            with trace_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps({"kind": "end", "taskId": command.input.task_id, "pid": os.getpid(), "monotonic": time.monotonic()}) + "\\n")
            connection.send_bytes(ResultEvent(task_id=command.input.task_id, result=result).model_dump_json().encode("utf-8"))
    finally:
        connection.close()
'''


@pytest.mark.e2e
@pytest.mark.skipif(os.name != "nt", reason="第四部分受管 Worker 使用 Windows Job Object")
def test_real_async_processes_renew_lease_serve_api_and_preserve_results(tmp_path):
    isolated_project = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / "backend", isolated_project / "backend", ignore=shutil.ignore_patterns("__pycache__"))
    (isolated_project / "backend/service/worker/inference_child.py").write_text(TEST_CHILD_SOURCE, encoding="utf-8")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    settings = AppSettings(
        port=port,
        database_path=tmp_path / "db/async.sqlite3",
        log_dir=tmp_path / "logs",
        upload_dir=tmp_path / "uploads",
        asr_model_dir=tmp_path / "model-must-not-exist",
        lease_seconds=2,
        renew_interval_seconds=0.3,
        notification_visibility_seconds=2,
        runtime_heartbeat_seconds=0.2,
        runtime_stale_seconds=2,
        poll_interval_seconds=0.1,
        worker_startup_timeout_seconds=15,
    )
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("INFERENCE_")}
    environment.update({
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(isolated_project),
        "INFERENCE_PORT": str(port),
        "INFERENCE_DATABASE_PATH": str(settings.database_path),
        "INFERENCE_LOG_DIR": str(settings.log_dir),
        "INFERENCE_UPLOAD_DIR": str(settings.upload_dir),
        "INFERENCE_ASR_MODEL_DIR": str(settings.asr_model_dir),
        "INFERENCE_LEASE_SECONDS": "2",
        "INFERENCE_RENEW_INTERVAL_SECONDS": "0.3",
        "INFERENCE_NOTIFICATION_VISIBILITY_SECONDS": "2",
        "INFERENCE_RUNTIME_HEARTBEAT_SECONDS": "0.2",
        "INFERENCE_RUNTIME_STALE_SECONDS": "2",
        "INFERENCE_POLL_INTERVAL_SECONDS": "0.1",
        "INFERENCE_WORKER_STARTUP_TIMEOUT_SECONDS": "15",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base_url = f"http://127.0.0.1:{port}"
    report = {
        "testDouble": True, "realModelLoaded": False, "startedAt": datetime.now(timezone.utc).isoformat(),
        "baseUrl": base_url, "processes": [], "healthWhileRunningSeconds": [],
    }
    processes = []
    outputs = []
    child_handle = None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    def request(path, *, data=None, headers=None):
        http_headers = {"X-Request-ID": "part4-process-test"}
        http_headers.update(headers or {})
        try:
            response = opener.open(urllib.request.Request(base_url + path, data=data, headers=http_headers), timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            payload = json.loads(response.read())
            assert response.headers["X-Request-ID"] == payload["requestId"]
            return response.status, payload

    def start_process(name, module):
        output_path = tmp_path / f"{name}-process.log"
        output = output_path.open("w", encoding="utf-8")
        outputs.append(output)
        process = subprocess.Popen(
            [sys.executable, "-m", module], cwd=isolated_project, env=environment,
            stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        processes.append((name, process, output_path))
        report["processes"].append({"name": name, "pid": process.pid})
        return process

    def check_processes():
        for name, process, output_path in processes:
            expected_stopped = any(item.get("exitCode") == 0 and item["pid"] == process.pid for item in report["processes"])
            if process.poll() is not None and not expected_stopped:
                pytest.fail(f"{name} exited unexpectedly: " + output_path.read_text(encoding="utf-8", errors="replace"))

    def wait_for_api():
        deadline = time.monotonic() + 20
        while True:
            check_processes()
            try:
                status, payload = request("/api/health")
                assert status == 200 and payload["data"]["status"] == "ok"
                return
            except (urllib.error.URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

    def stop_process(process):
        assert process.poll() is None
        process.send_signal(signal.CTRL_BREAK_EVENT)
        exit_code = process.wait(timeout=15)
        for item in report["processes"]:
            if item["pid"] == process.pid:
                item["exitCode"] = exit_code
        assert exit_code == 0

    audio_buffer = io.BytesIO()
    with wave.open(audio_buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 2000)
    boundary = "part4-test-double-boundary"
    upload_body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="language"\r\n\r\nzh\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="test-double.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode("utf-8") + audio_buffer.getvalue() + f"\r\n--{boundary}--\r\n".encode("utf-8")
    try:
        api = start_process("api", "backend.modules.api.run")
        wait_for_api()
        dispatcher = start_process("dispatcher", "backend.modules.dispatcher.task_dispatcher")
        worker = start_process("worker", "backend.modules.worker.asr_worker")
        deadline = time.monotonic() + 20
        while True:
            check_processes()
            status, runtime = request("/api/runtime")
            assert status == 200
            if runtime["data"]["worker"]["ready"] and runtime["data"]["dispatcher"]["ready"]:
                report["readyRuntime"] = runtime["data"]
                # 保留进程句柄，停止后检查同一个子进程已结束，避免 PID 复用误判。
                child_handle = kernel.OpenProcess(0x00100000, False, runtime["data"]["worker"]["childPid"])
                assert child_handle, ctypes.get_last_error()
                break
            assert time.monotonic() < deadline, runtime
            time.sleep(0.1)
        task_ids = []
        for number in range(2):
            status, receipt = request("/api/tasks/asr", data=upload_body, headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}", "Idempotency-Key": f"process-task-{number}",
            })
            assert status == 201, receipt
            task_ids.append(receipt["data"]["task"]["taskId"])
        saw_running = set()
        finished = {}
        deadline = time.monotonic() + 30
        while len(finished) != 2:
            check_processes()
            for task_id in task_ids:
                status, detail = request(f"/api/tasks/{task_id}")
                assert status == 200
                task = detail["data"]
                assert task["status"] != "failed", task
                if task["status"] == "running":
                    saw_running.add(task_id)
                    started = time.monotonic()
                    health_status, health = request("/api/health")
                    elapsed = time.monotonic() - started
                    assert health_status == 200 and health["data"]["status"] == "ok"
                    assert elapsed < 2, "推理期间 API 应继续独立响应"
                    report["healthWhileRunningSeconds"].append(elapsed)
                elif task["status"] == "succeeded":
                    finished[task_id] = task
            assert time.monotonic() < deadline, {"tasks": finished, "runtime": request("/api/runtime")[1]}
            time.sleep(0.1)
        assert saw_running == set(task_ids)
        assert all(task["attempt"] == 1 for task in finished.values())
        results = {}
        for task_id in task_ids:
            status, payload = request(f"/api/tasks/{task_id}/result")
            assert status == 200
            assert payload["data"]["output"]["text"] == "模拟结果，仅用于进程链路测试"
            assert payload["data"]["output"]["fasterWhisperVersion"] == "test-double"
            results[task_id] = payload["data"]
        # 读取最终数据库快照验证续租发生过，以及串行执行和确认次序。
        with sqlite3.connect(settings.database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(
                "SELECT task_id, started_at, finished_at, lease_expires_at, attempt FROM tasks ORDER BY started_at"
            )]
            for row in rows:
                started = datetime.fromisoformat(row["started_at"])
                expires = datetime.fromisoformat(row["lease_expires_at"])
                assert expires > started + timedelta(seconds=settings.lease_seconds + 0.5)
            assert datetime.fromisoformat(rows[0]["finished_at"]) <= datetime.fromisoformat(rows[1]["started_at"])
            ack_deadline = time.monotonic() + 5
            while True:
                notifications = [dict(row) for row in connection.execute(
                    "SELECT n.status, n.deliveries, n.acked_at, t.finished_at FROM notification_records n JOIN tasks t USING(task_id)"
                )]
                if len(notifications) == 2 and all(item["status"] == "acked" for item in notifications):
                    break
                assert time.monotonic() < ack_deadline, notifications
                time.sleep(0.1)
            assert len(notifications) == 2
            for notification in notifications:
                assert notification["status"] == "acked" and notification["deliveries"] == 1
                assert datetime.fromisoformat(notification["acked_at"]) >= datetime.fromisoformat(notification["finished_at"])
        trace = [json.loads(line) for line in (settings.log_dir / "test-child-events.jsonl").read_text(encoding="utf-8").splitlines()]
        assert [event["kind"] for event in trace] == ["start", "end", "start", "end"]
        assert len({event["pid"] for event in trace}) == 1
        assert [event["taskId"] for event in trace if event["kind"] == "start"] == [row["task_id"] for row in rows]
        report.update({"tasks": list(finished.values()), "executionRows": rows, "notifications": notifications, "childTrace": trace})
        stop_process(worker)
        assert kernel.WaitForSingleObject(child_handle, 5000) == 0
        report["childStopConfirmedByHandle"] = True
        stop_process(dispatcher)
        stop_process(api)
        restarted = start_process("api-restarted", "backend.modules.api.run")
        wait_for_api()
        for task_id, original in results.items():
            status, payload = request(f"/api/tasks/{task_id}/result")
            assert status == 200 and payload["data"] == original
        status, runtime = request("/api/runtime")
        assert status == 200
        assert runtime["data"]["worker"]["availability"] == "stopped"
        assert runtime["data"]["dispatcher"]["availability"] == "stopped"
        report["restartResultsPersisted"] = True
        report["stoppedRuntime"] = runtime["data"]
        stop_process(restarted)
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", port)) != 0
        assert not settings.asr_model_dir.exists()
        report["portClosed"] = True
    finally:
        for _, process, _ in reversed(processes):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        for output in outputs:
            output.close()
        if child_handle:
            kernel.CloseHandle(child_handle)
        report["finishedAt"] = datetime.now(timezone.utc).isoformat()
        (tmp_path / "part4-async-process-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
