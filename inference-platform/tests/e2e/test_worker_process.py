"""真实 Windows spawn/Job Object 生命周期测试；不加载或下载神经网络。"""

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from backend.common.process.managed_process import ManagedJsonProcess
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.config.vision.model_catalog import MODEL_VERSION
from backend.schema.execution.execution_schema import AcquireDecision, Delivery, ExecutionInput, ExecutionLease, VisionExecutionInput
from backend.schema.worker.worker_schema import COMMAND_ADAPTER, ReadyEvent, RunCommand, StartCommand
from backend.service.worker.worker_service import WorkerService

pytestmark = pytest.mark.skipif(os.name != "nt", reason="首版 Worker 进程保护针对 Windows")


def fake_inference_child(connection):
    command = connection.recv_bytes(1024)
    if command != b'{"type":"start"}':
        raise ValueError("invalid test command")
    connection.send_bytes(b'{"type":"ready"}')
    while True:
        connection.recv_bytes(1024)


def blocking_profile_child(connection):
    """真实 spawn/JSON 通道；用长阻塞代替模型计算来检查取消隔离。"""
    start = COMMAND_ADAPTER.validate_json(connection.recv_bytes(8 * 1024 * 1024))
    assert isinstance(start, StartCommand)
    connection.send_bytes(ReadyEvent(model_load_seconds=0, warmup_seconds=0).model_dump_json().encode())
    run = COMMAND_ADAPTER.validate_json(connection.recv_bytes(8 * 1024 * 1024))
    assert isinstance(run, RunCommand) and run.input.task_type == start.task_type
    while True:
        time.sleep(1)


def test_cancel_one_profile_confirms_child_exit_without_stopping_other_profile(tmp_path):
    settings = AppSettings(database_path=tmp_path / "isolated.sqlite3", log_dir=tmp_path / "logs", upload_dir=tmp_path / "uploads")
    workers = []
    try:
        for task_type in ("asr", "vision"):
            broker, execution, runtime = Mock(), Mock(), Mock()
            broker.receive.return_value = None
            execution.control.return_value = "continue"
            execution.renew.return_value = True
            execution.finish_cancel.return_value = True
            execution.commit_failure.return_value = True
            worker = WorkerService(None, settings, logging.getLogger("test-real-process"), task_type=task_type, runner=ManagedJsonProcess(blocking_profile_child), broker=broker, execution=execution, runtime=runtime)
            workers.append(worker)
            worker.start()
        deadline = time.monotonic() + 15
        while any(worker.state != "ready" for worker in workers) and time.monotonic() < deadline:
            for worker in workers:
                worker.tick()
            time.sleep(.05)
        assert all(worker.state == "ready" for worker in workers)
        assert workers[0].runner.pid != workers[1].runner.pid
        for number, worker in enumerate(workers):
            task_id = str(number + 1) * 32
            delivery = Delivery(notification_id="a" * 32, task_id=task_id, worker_id=worker.session_id, delivery_token="token", dispatch_version=1, execution_plan_version=1, queue_key=worker.queue_key)
            lease = ExecutionLease(task_id=task_id, worker_id=worker.session_id, lease_token="lease", attempt=1, dispatch_version=1, execution_plan_version=1, lease_expires_at=datetime.now(timezone.utc))
            common = dict(task_id=task_id, input_ref="b" * 32, input_sha256="c" * 64, input_size_bytes=1)
            task_input = (ExecutionInput(**common, model_id="whisper-small", model_version=settings.asr_model_revision, language=None) if number == 0 else VisionExecutionInput(**common, model_id="yolo-general", model_version=MODEL_VERSION, image_width=20, image_height=10, confidence_threshold=.25, iou_threshold=.45, max_detections=100))
            worker.broker.receive.return_value = delivery
            worker.execution.acquire.return_value = AcquireDecision(decision="acquired", input=task_input, lease=lease)
            worker.tick()
            assert worker.state == "running"
            worker.broker.receive.return_value = None
        asr, vision = workers
        vision_pid = vision.runner.pid
        asr.execution.control.return_value = "cancel"
        asr.tick()
        assert not asr.runner.is_alive()
        asr.execution.finish_cancel.assert_not_called()
        assert vision.runner.is_alive() and vision.runner.pid == vision_pid
        assert vision.state == "running" and vision._lock.is_locked
        asr.tick()
        asr.execution.finish_cancel.assert_called_once()
        assert asr.state == "starting"
        assert vision.runner.is_alive() and vision.runner.pid == vision_pid
        assert asr.close()
        assert vision.runner.is_alive() and vision.runner.pid == vision_pid
    finally:
        for worker in workers:
            assert worker.close()
    assert all(not worker.runner.is_alive() for worker in workers)


def test_spawn_child_waits_for_start_and_stop_confirms_exit():
    runner = ManagedJsonProcess(fake_inference_child)
    try:
        runner.start()
        assert runner.is_alive()
        assert runner.poll() is None
        runner.send(b'{"type":"start"}')
        deadline = time.monotonic() + 10
        payload = None
        while time.monotonic() < deadline and payload is None:
            payload = runner.poll()
            time.sleep(0.05)
        assert payload == b'{"type":"ready"}'
        assert runner.stop(5)
        assert not runner.is_alive()
        # stop 已观察到旧进程结束并关闭句柄，才允许同一管理器创建新子进程。
        assert runner.pid is None
        runner.start()
        assert runner.is_alive()
        runner.send(b'{"type":"start"}')
        deadline = time.monotonic() + 10
        payload = None
        while time.monotonic() < deadline and payload is None:
            payload = runner.poll()
            time.sleep(0.05)
        assert payload == b'{"type":"ready"}'
        assert runner.stop(5)
    finally:
        runner.stop(5)


def test_parent_force_exit_kills_bound_inference_child(tmp_path):
    script = tmp_path / "parent.py"
    identity = tmp_path / "child.json"
    script.write_text('''from backend.common.process.managed_process import ManagedJsonProcess
import json, pathlib, sys, time

def child(connection):
    connection.recv_bytes(1024)
    connection.send_bytes(b'{"type":"ready"}')
    while True:
        time.sleep(1)

if __name__ == "__main__":
    runner = ManagedJsonProcess(child)
    try:
        runner.start()
        runner.send(b'{"type":"start"}')
        while runner.poll() is None:
            time.sleep(.05)
        pathlib.Path(sys.argv[1]).write_text(json.dumps({"pid": runner.pid}))
        while True:
            time.sleep(1)
    finally:
        runner.stop(5)
''', encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    parent = subprocess.Popen([sys.executable, str(script), str(identity)], env=env, cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    handle = None
    try:
        deadline = time.monotonic() + 15
        while not identity.exists() and time.monotonic() < deadline and parent.poll() is None:
            time.sleep(0.05)
        assert identity.exists(), parent.communicate(timeout=2)
        pid = json.loads(identity.read_text())["pid"]
        # 打开进程句柄后再结束父进程；不依赖可复用的 PID 判断旧进程是否退出。
        handle = kernel.OpenProcess(0x00100000 | 0x0001, False, pid)
        assert handle
        assert kernel.WaitForSingleObject(handle, 0) == 258
        parent.kill()
        parent.wait(timeout=5)
        assert kernel.WaitForSingleObject(handle, 10000) == 0
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if handle:
            if kernel.WaitForSingleObject(handle, 0) == 258:
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 5000)
            kernel.CloseHandle(handle)
        parent.communicate(timeout=5)
