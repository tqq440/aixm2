"""接口配置文件的有界读取、跨进程锁和原子保存；不存认证秘密。"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from filelock import FileLock

from backend.utils.json.bounded_json import read_bounded_json

MAX_CONFIG_BYTES = 1024 * 1024


class ServiceConfigStore:
    def __init__(self, database_path: Path):
        self.path = database_path.with_name(database_path.name + ".asr-services.json")

    def read(self):
        if not self.path.exists():
            return {}
        return read_bounded_json(self.path, max_bytes=MAX_CONFIG_BYTES)

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path) + ".lock", timeout=2):
            yield

    def write(self, document):
        content = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
        if len(content) > MAX_CONFIG_BYTES:
            raise ValueError("ASR configuration exceeds byte limit")
        temporary = None
        try:
            with NamedTemporaryFile(mode="wb", dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp",
                                    delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
