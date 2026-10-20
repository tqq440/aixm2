"""仅管理内存中真实 Popen 句柄及 Job 的进程组；不从 PID 档案接管进程。"""

import ctypes
from ctypes import wintypes
from pathlib import Path
import signal
import subprocess
import time

from backend.common.process.windows_job import WindowsJob


class ProcessTreeJob(WindowsJob):
    def active_count(self) -> int:
        class Accounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD),
            ]
        query = self._kernel.QueryInformationJobObject
        query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        query.restype = wintypes.BOOL
        accounting = Accounting()
        if not query(self._handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return accounting.ActiveProcesses

    def terminate(self):
        terminate = self._kernel.TerminateJobObject
        terminate.argtypes = [wintypes.HANDLE, wintypes.UINT]
        terminate.restype = wintypes.BOOL
        if not terminate(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def wait_empty(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.active_count():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True


class OwnedProcess:
    """child_entry 收到 GO 前不执行服务；先加入 Job，防启动竞态泄漏孤儿。"""

    def __init__(self, name: str, command: list[str], *, cwd: Path, env: dict,
                 log_path: Path, job: ProcessTreeJob):
        self.name = name
        self._log = log_path.open("ab", buffering=0)
        self.process = None
        try:
            self.process = subprocess.Popen(
                command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                stdout=self._log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            job.assign(self.process.pid)
            self.process.stdin.write(b"GO\n")
            self.process.stdin.flush()
            self.process.stdin.close()
        except BaseException:
            if self.process is not None and self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            self._log.close()
            raise

    @property
    def pid(self):
        return self.process.pid

    def poll(self):
        return self.process.poll()

    def stop(self, timeout: float) -> bool:
        if self.process.poll() is None:
            try:
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            except OSError:
                if self.process.poll() is None:
                    return False
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return False
        return True

    def close_log(self):
        self._log.close()
