"""本机管理器状态与控制请求持久化；从不按记录的 PID 杀进程。"""

import json
from pathlib import Path
from time import sleep

from filelock import FileLock, Timeout

from backend.utils.file.file_utils import atomic_write_text


WINDOWS_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16, 0.32)


class ControlFiles:
    def __init__(self, directory: Path):
        self.directory = directory
        self.state_path = directory / "state.json"
        self.stop_path = directory / "stop-request.json"
        self.lock_path = directory / "manager.lock"
        self.client_lock_path = directory / "client.lock"

    def prepare(self):
        self.directory.mkdir(parents=True, exist_ok=True)

    def read(self, path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def write(self, path: Path, value: dict):
        self.prepare()
        content = json.dumps(value, ensure_ascii=False, indent=2)
        # Windows短暂的读句柄/扫描器占用可能拒绝replace。每次仍写完整临时文件
        # 并原子替换；只为明确Windows共享/访问码重试，总等待0.63秒。
        # 持续ACL问题与其他I/O故障仍上抛，由管理器按既有流程清理所属服务。
        for attempt in range(len(WINDOWS_REPLACE_RETRY_DELAYS) + 1):
            try:
                atomic_write_text(path, content)
                return
            except PermissionError as error:
                if (getattr(error, "winerror", None) not in {5, 32, 33}
                        or attempt == len(WINDOWS_REPLACE_RETRY_DELAYS)):
                    raise
                sleep(WINDOWS_REPLACE_RETRY_DELAYS[attempt])

    def manager_locked(self) -> bool:
        self.prepare()
        lock = FileLock(self.lock_path, timeout=0)
        try:
            with lock:
                return False
        except Timeout:
            return True
