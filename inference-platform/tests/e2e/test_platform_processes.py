"""真实 Windows 控制台/Job 生命周期，不加载 Whisper；与真实模型验收分开。"""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from backend.common.process.platform_process import OwnedProcess, ProcessTreeJob
from backend.config.settings.app_settings import PROJECT_ROOT

pytestmark = pytest.mark.skipif(os.name != "nt", reason="平台启动脚本仅支持 Windows")


def test_gated_owned_process_break_confirms_exit_and_job_empty(tmp_path):
    code = "import sys,signal,time\nassert sys.stdin.buffer.readline(16)==b'GO\\n'\nsignal.signal(signal.SIGBREAK,lambda *_:sys.exit(0))\nprint('READY',flush=True)\nwhile True: time.sleep(.05)"
    job = ProcessTreeJob()
    owned = OwnedProcess("test", [sys.executable, "-u", "-c", code], cwd=tmp_path,
                         env=os.environ.copy(), log_path=tmp_path / "service.log", job=job)
    try:
        deadline = time.monotonic() + 10
        while "READY" not in (tmp_path / "service.log").read_text() and time.monotonic() < deadline:
            time.sleep(.05)
        assert owned.poll() is None
        assert job.active_count() == 1
        assert owned.stop(5)
        assert owned.poll() == 0
        assert job.wait_empty(5)
    finally:
        if owned.poll() is None:
            job.terminate()
            owned.process.wait(timeout=5)
        owned.close_log()
        job.close()


def test_manager_forced_exit_kills_owned_tree_but_not_unmanaged_process(tmp_path):
    script = tmp_path / "manager.py"
    identity = tmp_path / "child.json"
    child_code = (
        "import sys,subprocess,time,json,pathlib; assert sys.stdin.buffer.readline(16)==b'GO\\n'; "
        "from backend.common.process.console_isolation import detach_console; detach_console(); "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'child':__import__('os').getpid(),'grandchild':p.pid})); time.sleep(60)"
    )
    script.write_text(
        "import sys,time,pathlib,os\n"
        "from backend.common.process.platform_process import ProcessTreeJob,OwnedProcess\n"
        "job=ProcessTreeJob()\n"
        f"p=OwnedProcess('test',[sys.executable,'-c',{child_code!r},sys.argv[1]],cwd=pathlib.Path.cwd(),env=os.environ.copy(),log_path=pathlib.Path(sys.argv[1]+'.log'),job=job)\n"
        "time.sleep(60)\n", encoding="utf-8",
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL)
    parent = subprocess.Popen([sys.executable, str(script), str(identity)], cwd=tmp_path,
                              env=dict(os.environ, PYTHONPATH=str(PROJECT_ROOT)), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    handles = []
    try:
        deadline = time.monotonic() + 15
        while not identity.exists() and time.monotonic() < deadline and parent.poll() is None:
            time.sleep(.05)
        assert identity.exists(), parent.communicate(timeout=2)
        for pid in json.loads(identity.read_text()).values():
            handle = kernel.OpenProcess(0x00100000, False, pid)
            assert handle
            handles.append(handle)
            assert kernel.WaitForSingleObject(handle, 0) == 258
        parent.kill()
        parent.wait(timeout=5)
        for handle in handles:
            assert kernel.WaitForSingleObject(handle, 10000) == 0
        assert unrelated.poll() is None
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        parent.communicate(timeout=5)
        unrelated.kill()
        unrelated.wait(timeout=5)
        for handle in handles:
            kernel.CloseHandle(handle)


def test_platform_break_drains_detached_work_then_confirms_owned_job_empty(tmp_path):
    """真实隐藏控制台/Job/Pipe，推理由可控测试进程代替，不是模型验收。"""
    agent_script = tmp_path / "draining_agent.py"
    controller_script = tmp_path / "console_manager.py"
    ready = tmp_path / "working.json"
    finished = tmp_path / "finished.json"
    evidence = tmp_path / "evidence.json"
    agent_script.write_text('''import ctypes
from ctypes import wintypes
import json
import multiprocessing
from pathlib import Path
import signal
import sys
import time
from backend.common.process.console_isolation import detach_console
from backend.common.process.managed_process import ManagedJsonProcess

def inference(connection):
    detach_console()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    handler = handler_type(lambda event: kernel.ExitProcess(91) or True)
    kernel.SetConsoleCtrlHandler.argtypes = [handler_type, wintypes.BOOL]
    assert kernel.SetConsoleCtrlHandler(handler, True)
    connection.send_bytes(b'READY')
    assert connection.recv_bytes() == b'WORK'
    connection.send_bytes(b'BUSY')
    time.sleep(1)
    connection.send_bytes(b'DONE')
    time.sleep(30)

if __name__ == '__main__':
    assert sys.stdin.buffer.readline(16) == b'GO\\n'
    stopping = []
    signal.signal(signal.SIGBREAK, lambda *_: stopping.append(True))
    process = ManagedJsonProcess(inference)
    process.start()
    try:
        deadline = time.monotonic() + 15
        while process.poll() != b'READY':
            assert time.monotonic() < deadline
            time.sleep(.01)
        process.send(b'WORK')
        while process.poll() != b'BUSY':
            assert time.monotonic() < deadline
            time.sleep(.01)
        Path(sys.argv[1]).write_text(json.dumps({'child_pid': process.pid}))
        while process.poll() != b'DONE':
            assert process.is_alive() and time.monotonic() < deadline
            time.sleep(.01)
        assert stopping, 'parent must receive the platform shutdown signal'
        assert process.stop(5)
        Path(sys.argv[2]).write_text(json.dumps({'stop_received': True, 'work_completed': True, 'child_exit_confirmed': True}))
    finally:
        process.stop(5)
''', encoding="utf-8")
    controller_script.write_text('''import json
import os
from pathlib import Path
import sys
import time
from backend.common.process.platform_process import OwnedProcess, ProcessTreeJob

agent, ready, finished, evidence = map(Path, sys.argv[1:])
job = ProcessTreeJob()
owned = OwnedProcess('test-agent', [sys.executable, str(agent), str(ready), str(finished)],
                     cwd=Path.cwd(), env=os.environ.copy(), log_path=ready.with_suffix('.log'), job=job)
try:
    deadline = time.monotonic() + 20
    while not ready.exists():
        assert owned.poll() is None and time.monotonic() < deadline
        time.sleep(.01)
    count_before = job.active_count()
    assert count_before == 2, count_before
    assert not owned.stop(0)
    assert owned.poll() is None
    count_during = job.active_count()
    assert count_during == 2, count_during
    while owned.poll() is None:
        assert time.monotonic() < deadline
        time.sleep(.01)
    assert owned.poll() == 0
    assert job.wait_empty(5)
    outcome = json.loads(finished.read_text())
    assert all(outcome.values())
    evidence.write_text(json.dumps(dict(outcome, job_before=count_before, job_during=count_during, job_after=job.active_count())))
finally:
    if owned.poll() is None or job.active_count():
        job.terminate()
    owned.process.wait(timeout=5)
    owned.close_log()
    job.close()
''', encoding="utf-8")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    completed = subprocess.run(
        [sys.executable, str(controller_script), str(agent_script), str(ready), str(finished), str(evidence)],
        cwd=PROJECT_ROOT, env=dict(os.environ, PYTHONPATH=str(PROJECT_ROOT), PYTHONUTF8="1"),
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=45,
        creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup,
    )
    assert completed.returncode == 0, completed.stderr + (ready.with_suffix(".log").read_text(encoding="utf-8") if ready.with_suffix(".log").exists() else "")
    outcome = json.loads(evidence.read_text())
    assert outcome == {"stop_received": True, "work_completed": True, "child_exit_confirmed": True,
                       "job_before": 2, "job_during": 2, "job_after": 0}
