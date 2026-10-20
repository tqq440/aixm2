"""固定视觉模型文件操作；不决定模型版本或业务准入。"""

from contextlib import contextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from backend.utils.file.file_utils import atomic_write_text, sha256_file

MANIFEST_FILENAME = "manifest.json"


class VisionModelStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def read_manifest(self) -> str:
        return (self.directory / MANIFEST_FILENAME).read_text(encoding="utf-8")

    def has_entries(self) -> bool:
        return self.directory.exists() and any(self.directory.iterdir())

    def file_info(self, filename: str) -> tuple[int, str]:
        path = self.directory / filename
        return path.stat().st_size, sha256_file(path)

    def download(self, url: str, filename: str, max_bytes: int) -> None:
        with urlopen(url, timeout=60) as response, (self.directory / filename).open("xb") as output:
            total = 0
            while chunk := response.read(min(1024 * 1024, max_bytes + 1 - total)):
                total += len(chunk)
                if total > max_bytes:
                    raise OSError("下载文件超过预期大小")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())

    def write_manifest(self, content: str) -> None:
        atomic_write_text(self.directory / MANIFEST_FILENAME, content)

    @contextmanager
    def stage(self):
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=f".{self.directory.name}-", dir=self.directory.parent) as directory:
            yield VisionModelStore(Path(directory))

    def publish(self, staged: "VisionModelStore") -> None:
        # 只允许替换空目录；rmdir 不递归删除，遇到其他操作写入则失败。
        if self.directory.exists():
            self.directory.rmdir()
        # Windows rename 对已存在的目标失败，保留原模型和并发发布结果。
        staged.directory.rename(self.directory)
