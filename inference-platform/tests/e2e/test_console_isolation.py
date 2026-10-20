"""真实 Windows 控制台组信号与 Job/Pipe 测试；不加载任何模型。"""

import json
import os
import subprocess
import sys

import pytest

from backend.config.settings.app_settings import PROJECT_ROOT


@pytest.mark.skipif(os.name != "nt", reason="真实控制台隔离只针对 Windows")
def test_detached_model_survives_native_control_break_handler_in_parent_group(tmp_path):
    script = tmp_path / "console_group.py"
    script.write_text('''import ctypes
from ctypes import wintypes
import json
import time
from backend.common.process.console_isolation import detach_console
from backend.common.process.managed_process import ManagedJsonProcess

HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

def native_handler(events):
    def on_control(event):
        events.append(event)
        return True
    callback = HANDLER(on_control)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.SetConsoleCtrlHandler.argtypes = [HANDLER, wintypes.BOOL]
    kernel.SetConsoleCtrlHandler.restype = wintypes.BOOL
    if not kernel.SetConsoleCtrlHandler(callback, True):
        raise ctypes.WinError(ctypes.get_last_error())
    return callback

def child(connection):
    detached = connection.recv_bytes(1024) == b"detach"
    if detached:
        detach_console()
        detach_console()  # 无 console 时重复操作也须成功。
    events = []
    callback = native_handler(events)  # 模拟模型库后注册的原生 handler。
    connection.send_bytes(b"ready")
    while True:
        connection.recv_bytes(1024)
        connection.send_bytes(json.dumps(events).encode())

def receive(runner, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        payload = runner.poll()
        if payload is not None:
            return payload
        time.sleep(.02)
    raise TimeoutError("test child did not answer")

if __name__ == "__main__":
    events = []
    callback = native_handler(events)
    runners = []
    try:
        for mode in (b"attached", b"detach"):
            runner = ManagedJsonProcess(child)
            runners.append(runner)
            runner.start()
            runner.send(mode)
            assert receive(runner) == b"ready"
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
        # 只广播本测试新建的隐藏 console，不影响 pytest/用户控制台。
        assert kernel.GenerateConsoleCtrlEvent(1, 0), ctypes.get_last_error()
        attached_events = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (1 not in attached_events or 1 not in events):
            runners[0].send(b"check")
            attached_events = json.loads(receive(runners[0]))
            time.sleep(.02)
        runners[1].send(b"check")
        detached_events = json.loads(receive(runners[1]))
        assert 1 in events and 1 in attached_events, (events, attached_events)
        assert detached_events == [] and runners[1].is_alive(), detached_events
        stopped = [runner.stop(5) for runner in runners]
        assert stopped == [True, True]
        assert all(not runner.is_alive() for runner in runners)
        print(json.dumps({"parent_break_received": True, "attached_child_break_received": True,
            "detached_child_break_received": False, "pipe_after_break": True,
            "managed_stop_confirmed": True}))
    finally:
        for runner in runners:
            runner.stop(5)
''', encoding="utf-8")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    result = subprocess.run([sys.executable, str(script)], cwd=PROJECT_ROOT,
        env=dict(os.environ, PYTHONPATH=str(PROJECT_ROOT), PYTHONUTF8="1"),
        creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup,
        capture_output=True, text=True, encoding="utf-8", timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip()) == {
        "parent_break_received": True, "attached_child_break_received": True,
        "detached_child_break_received": False, "pipe_after_break": True,
        "managed_stop_confirmed": True,
    }
