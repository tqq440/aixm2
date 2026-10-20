"""第六部分真实 Windows 进程故障注入；临时源码明确使用模型替身。"""

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


CHILD_SOURCE = '''"""测试替身，不导入 Whisper，不把模拟文字作为真实结果。"""
import json, os, signal, time
from backend.common.process.managed_process import MAX_MESSAGE_BYTES
from backend.schema.asr.asr_schema import TranscriptionResult
from backend.schema.worker.worker_schema import COMMAND_ADAPTER, ReadyEvent, ResultEvent

def inference_child(connection):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    try:
        settings = COMMAND_ADAPTER.validate_json(connection.recv_bytes(MAX_MESSAGE_BYTES)).settings
        connection.send_bytes(ReadyEvent(model_load_seconds=0).model_dump_json().encode())
        while True:
            command = COMMAND_ADAPTER.validate_json(connection.recv_bytes(MAX_MESSAGE_BYTES))
            with (settings.log_dir / "test-executions.jsonl").open("a", encoding="utf-8") as trace:
                trace.write(json.dumps({"taskId": command.input.task_id, "pid": os.getpid()}) + "\\n")
            time.sleep(3.2)
            result = TranscriptionResult(
                text="故障恢复模型替身", segments=[], language="zh", language_requested="zh",
                language_probability=1, audio_duration_seconds=0.125,
                model_repo=settings.asr_model_repo, model_revision=settings.asr_model_revision,
                device="cpu", compute_type="test-double", cpu_threads=1,
                model_load_seconds=0, transcription_seconds=3.2, total_seconds=3.2,
                faster_whisper_version="test-double", ctranslate2_version="test-double")
            connection.send_bytes(ResultEvent(task_id=command.input.task_id, result=result).model_dump_json().encode())
    finally:
        connection.close()
'''


@pytest.mark.e2e
@pytest.mark.skipif(os.name != "nt", reason="首版受管 Worker 使用 Windows Job Object")
def test_real_process_cancel_child_crash_and_worker_force_exit_recovery(tmp_path):
    isolated = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / "backend", isolated / "backend", ignore=shutil.ignore_patterns("__pycache__"))
    (isolated / "backend/service/worker/inference_child.py").write_text(CHILD_SOURCE, encoding="utf-8")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    settings = AppSettings(
        port=port, database_path=tmp_path / "db/recovery.sqlite3", log_dir=tmp_path / "logs",
        upload_dir=tmp_path / "uploads", asr_model_dir=tmp_path / "model-must-not-exist",
        lease_seconds=2, renew_interval_seconds=0.3, notification_visibility_seconds=2,
        runtime_heartbeat_seconds=0.2, runtime_stale_seconds=2, poll_interval_seconds=0.1,
        worker_startup_timeout_seconds=15, retry_wait_seconds=0.3,
        queue_timeout_seconds=60, total_timeout_seconds=90,
    )
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("INFERENCE_")}
    environment.update({"PYTHONUNBUFFERED": "1", "PYTHONPATH": str(isolated)})
    for field in (
        "port", "database_path", "log_dir", "upload_dir", "asr_model_dir", "lease_seconds",
        "renew_interval_seconds", "notification_visibility_seconds", "runtime_heartbeat_seconds",
        "runtime_stale_seconds", "poll_interval_seconds", "worker_startup_timeout_seconds",
        "retry_wait_seconds", "queue_timeout_seconds", "total_timeout_seconds",
    ):
        environment[f"INFERENCE_{field.upper()}"] = str(getattr(settings, field))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base_url = f"http://127.0.0.1:{port}"
    processes, outputs, handles = [], [], []
    report = {"testDouble": True, "realModelLoaded": False, "processes": [], "tasks": {}}
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def request(path, *, data=None, headers=None):
        try:
            response = opener.open(urllib.request.Request(base_url + path, data=data, headers=headers or {}), timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def start_process(name, module):
        output_path = tmp_path / f"{name}.log"
        output = output_path.open("w", encoding="utf-8")
        outputs.append(output)
        process = subprocess.Popen(
            [sys.executable, "-m", module], cwd=isolated, env=environment,
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        processes.append((process, output_path))
        report["processes"].append({"name": name, "pid": process.pid})
        return process

    def wait_for(path, predicate, *, timeout=25):
        deadline = time.monotonic() + timeout
        while True:
            for process, output_path in processes:
                if process.poll() is not None and not any(
                    item.get("exitCode") is not None and item["pid"] == process.pid for item in report["processes"]
                ):
                    pytest.fail(output_path.read_text(encoding="utf-8", errors="replace"))
            try:
                status, payload = request(path)
                if status == 200 and predicate(payload["data"]):
                    return payload["data"]
            except (urllib.error.URLError, TimeoutError):
                payload = {"waiting": path}
            assert time.monotonic() < deadline, payload
            time.sleep(0.05)

    def record_exit(process, *, forced=False):
        if forced:
            process.kill()
        else:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        exit_code = process.wait(timeout=15)
        for item in report["processes"]:
            if item["pid"] == process.pid:
                item.update(exitCode=exit_code, forced=forced)
        if not forced:
            assert exit_code == 0

    def child_handle():
        runtime = wait_for("/api/runtime", lambda data: data["worker"]["availability"] == "online" and data["worker"]["childPid"] is not None)
        handle = kernel.OpenProcess(0x00100000 | 0x0001, False, runtime["worker"]["childPid"])
        assert handle, ctypes.get_last_error()
        handles.append(handle)
        assert kernel.WaitForSingleObject(handle, 0) == 258
        return handle

    def wait_for_compute(task_id, count=1):
        deadline = time.monotonic() + 5
        path = settings.log_dir / "test-executions.jsonl"
        while True:
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
            if sum(event["taskId"] == task_id for event in events) >= count:
                return
            assert time.monotonic() < deadline, events
            time.sleep(0.02)

    audio_buffer = io.BytesIO()
    with wave.open(audio_buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 2000)
    boundary = "part6-process-fault-test"
    upload = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="language"\r\n\r\nzh\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="test-double.wav"\r\n'
        'Content-Type: audio/wav\r\n\r\n'
    ).encode() + audio_buffer.getvalue() + f"\r\n--{boundary}--\r\n".encode()

    def submit(name):
        status, payload = request("/api/tasks/asr", data=upload, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}", "Idempotency-Key": name,
        })
        assert status == 201, payload
        return payload["data"]["task"]["taskId"]

    try:
        api = start_process("api", "backend.modules.api.run")
        wait_for("/api/health", lambda data: data["status"] == "ok")
        dispatcher = start_process("dispatcher", "backend.modules.dispatcher.task_dispatcher")
        queued = submit("queued-cancel")
        wait_for(f"/api/tasks/{queued}", lambda task: task["status"] == "queued")
        status, receipt = request(f"/api/tasks/{queued}/cancel", data=b"")
        assert status == 200, receipt
        task = wait_for(f"/api/tasks/{queued}", lambda task: task["status"] == "cancelled")
        assert task["attempt"] == 0
        report["tasks"]["queuedCancel"] = task

        worker = start_process("worker", "backend.modules.worker.asr_worker")
        wait_for("/api/runtime", lambda data: data["worker"]["ready"])
        running = submit("running-cancel")
        wait_for(f"/api/tasks/{running}", lambda task: task["status"] == "running")
        wait_for_compute(running)
        old_child = child_handle()
        status, receipt = request(f"/api/tasks/{running}/cancel", data=b"")
        assert status == 200, receipt
        task = wait_for(f"/api/tasks/{running}", lambda task: task["status"] == "cancelled")
        assert task["attempt"] == 1
        assert kernel.WaitForSingleObject(old_child, 5000) == 0
        report["tasks"]["runningCancel"] = task
        report["cancelledChildStoppedByHandle"] = True
        wait_for("/api/runtime", lambda data: data["worker"]["ready"])

        child_crash = submit("child-crash")
        wait_for(f"/api/tasks/{child_crash}", lambda task: task["status"] == "running")
        wait_for_compute(child_crash)
        old_child = child_handle()
        assert kernel.TerminateProcess(old_child, 17)
        assert kernel.WaitForSingleObject(old_child, 5000) == 0
        task = wait_for(f"/api/tasks/{child_crash}", lambda task: task["status"] in {"failed", "succeeded"})
        assert task["status"] == "succeeded" and task["attempt"] == 2, task
        assert worker.poll() is None
        report["tasks"]["childCrashRecovered"] = task

        parent_crash = submit("worker-force-exit")
        wait_for(f"/api/tasks/{parent_crash}", lambda task: task["status"] == "running")
        wait_for_compute(parent_crash)
        old_child = child_handle()
        record_exit(worker, forced=True)
        assert kernel.WaitForSingleObject(old_child, 5000) == 0
        report["forcedParentChildStoppedByJob"] = True
        task = wait_for(f"/api/tasks/{parent_crash}", lambda task: task["status"] == "queued" and task["attempt"] == 1)
        restarted = start_process("worker-restarted", "backend.modules.worker.asr_worker")
        task = wait_for(f"/api/tasks/{parent_crash}", lambda task: task["status"] in {"failed", "succeeded"})
        assert task["status"] == "succeeded" and task["attempt"] == 2, task
        report["tasks"]["workerCrashRecovered"] = task
        status, result = request(f"/api/tasks/{parent_crash}/result")
        assert status == 200 and result["data"]["output"]["computeType"] == "test-double"
        assert not settings.asr_model_dir.exists()
        with sqlite3.connect(settings.database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(
                "SELECT task_id,status,attempt,dispatch_version,created_at,queue_deadline_at,total_deadline_at FROM tasks ORDER BY created_at"
            )]
            assert [row["dispatch_version"] for row in rows] == [1, 1, 2, 2]
        trace = [json.loads(line) for line in (settings.log_dir / "test-executions.jsonl").read_text(encoding="utf-8").splitlines()]
        assert [event["taskId"] for event in trace].count(queued) == 0
        assert [event["taskId"] for event in trace].count(running) == 1
        assert [event["taskId"] for event in trace].count(child_crash) == 2
        assert [event["taskId"] for event in trace].count(parent_crash) == 2
        report.update(executionRows=rows, childTrace=trace)
        final_child = child_handle()
        record_exit(restarted)
        assert kernel.WaitForSingleObject(final_child, 5000) == 0
        record_exit(dispatcher)
        record_exit(api)
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
        report["portClosed"] = True
    finally:
        for process, _ in reversed(processes):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        for output in outputs:
            output.close()
        for handle in handles:
            if kernel.WaitForSingleObject(handle, 0) == 258:
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 5000)
            kernel.CloseHandle(handle)
        (tmp_path / "part6-recovery-process-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
