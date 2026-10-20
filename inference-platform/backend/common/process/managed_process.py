"""受管 spawn 进程和有大小限制的 JSON 字节通道；不含任务规则。"""

from collections.abc import Callable
import multiprocessing
from multiprocessing.connection import Connection
import os

from backend.common.process.windows_job import WindowsJob

MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class ManagedJsonProcess:
    def __init__(self, target: Callable[[Connection], None]) -> None:
        self._target = target
        self._process = None
        self._connection = None
        self._job = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("受管进程已启动")
        # 本版本验收边界为 Windows，不能在缺少父进程死亡保护时接单。
        if os.name != "nt":
            raise OSError("本版本 Worker 的子进程保护仅支持 Windows")
        self._job = WindowsJob()
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        self._connection = parent
        self._process = context.Process(target=self._target, args=(child,), daemon=True)
        try:
            self._process.start()
            self._job.assign(self._process.pid)
        except BaseException:
            self.stop(2)
            raise
        finally:
            child.close()

    def send(self, payload: bytes) -> None:
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError("进程消息超过大小限制")
        if self._connection is None:
            raise RuntimeError("受管进程尚未启动")
        self._connection.send_bytes(payload)

    def poll(self) -> bytes | None:
        if self._connection is not None and self._connection.poll():
            return self._connection.recv_bytes(MAX_MESSAGE_BYTES)
        return None

    def is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def stop(self, timeout: float) -> bool:
        """等待进程句柄确认结束；不以发送终止请求代替已经停止。"""
        process = self._process
        if process is not None and process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=max(0, timeout / 2))
            if process.is_alive() and self._job is not None:
                self._job.close()
            if process.is_alive():
                process.kill()
                process.join(timeout=max(0, timeout / 2))
            if process.is_alive():
                return False
        if self._job is not None:
            self._job.close()
            self._job = None
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if process is not None:
            process.close()
            self._process = None
        return True
