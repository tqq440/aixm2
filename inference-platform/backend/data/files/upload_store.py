"""M06 本地受管音频和图片持久化；外部文件名不参与任何磁盘路径。"""

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import BinaryIO
from uuid import uuid4

from filelock import FileLock


class UploadTooLargeError(ValueError):
    """流实际读取的字节数超过限制。"""


class EmptyUploadError(ValueError):
    """上传流没有内容。"""


class UnsafeUploadPathError(ValueError):
    """受管路径含有链接或非普通文件，不能安全访问。"""


@dataclass(frozen=True)
class StoredUpload:
    input_ref: str
    path: Path
    size_bytes: int
    sha256: str


class LocalUploadStore:
    """仅管理 UUID 命名的文件；调用者持锁协调事务补偿和孤立对象清理。"""

    CHUNK_SIZE = 64 * 1024
    LOCK_TIMEOUT_SECONDS = 5
    _REFERENCE = re.compile(r"[0-9a-f]{32}")
    _MEDIA_SUFFIXES = {"audio": ".audio", "image": ".image"}

    def __init__(self, root: Path, *, media_type: str = "audio"):
        if not isinstance(media_type, str) or media_type not in self._MEDIA_SUFFIXES:
            raise ValueError("media_type 必须为 audio 或 image")
        # 不 resolve：保留路径中的链接信息，以便访问时拒绝链接/联接点。
        # 构造或导入均不创建目录。
        self.root = Path(os.path.abspath(root))
        self.media_type = media_type

    @contextmanager
    def lock(self):
        """上传事务与孤儿清理共用进程间锁；业务层决定完整持锁范围。"""
        self._reject_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._reject_links(self.root)
        lock_path = self.root / ".maintenance.lock"
        self._reject_links(lock_path)
        with FileLock(lock_path, timeout=self.LOCK_TIMEOUT_SECONDS):
            yield

    def save(self, stream: BinaryIO, *, max_bytes: int) -> StoredUpload:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes 必须为正整数")
        self._prepare_directories()
        input_ref = uuid4().hex
        temporary = self.root / "staging" / f"{input_ref}.part"
        destination = self.resolve_path(input_ref)
        self._assert_reference_unused(input_ref)

        identity: tuple[int, int] | None = None
        digest = hashlib.sha256()
        size = 0
        try:
            # 排他创建：文件名冲突时绝不覆盖既有上传。
            with temporary.open("xb") as target:
                info = os.fstat(target.fileno())
                identity = (info.st_dev, info.st_ino)
                while True:
                    chunk = stream.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadTooLargeError("上传文件超过字节数限制")
                    target.write(chunk)
                    digest.update(chunk)
                if size == 0:
                    raise EmptyUploadError("上传文件不能为空")
                target.flush()
                os.fsync(target.fileno())
            self._reject_links(temporary)
            self._reject_links(destination)
            self._assert_reference_unused(input_ref)
            os.replace(temporary, destination)
            return StoredUpload(input_ref, destination, size, digest.hexdigest())
        except BaseException:
            # replace 极少数异常情况下可能已成功；只删除身份与本次创建相同的文件。
            # 清理失败保留原异常，由离线孤儿清理处理遗留文件。
            if identity is not None:
                self._remove_owned(temporary, identity)
                self._remove_owned(destination, identity)
            raise

    def resolve_path(self, input_ref: str) -> Path:
        self._validate_reference(input_ref)
        path = self.root / "objects" / f"{input_ref}{self._MEDIA_SUFFIXES[self.media_type]}"
        self._reject_links(path)
        if path.exists() and not path.is_file():
            raise UnsafeUploadPathError("受管文件必须为普通文件")
        return path

    def delete(self, input_ref: str) -> None:
        self.resolve_path(input_ref).unlink(missing_ok=True)

    def iter_orphan_candidates(
        self, referenced_refs: set[str], older_than: datetime,
    ) -> list[Path]:
        """只枚举阈值之前的受管普通文件；调用者须持锁查询引用并完成删除。"""
        if older_than.tzinfo is None or older_than.utcoffset() is None:
            raise ValueError("清理时间必须带时区")
        for input_ref in referenced_refs:
            self._validate_reference(input_ref)
        self._reject_links(self.root)
        cutoff = older_than.timestamp()
        candidates: list[Path] = []
        for directory_name, suffixes in (("objects", set(self._MEDIA_SUFFIXES.values())), ("staging", {".part"})):
            directory = self.root / directory_name
            self._reject_links(directory)
            if not directory.exists():
                continue
            for path in directory.iterdir():
                if path.suffix not in suffixes or not self._REFERENCE.fullmatch(path.stem):
                    continue
                self._reject_links(path)
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                if directory_name == "objects" and path.stem in referenced_refs:
                    continue
                if info.st_mtime < cutoff:
                    candidates.append(path)
        return sorted(candidates)

    def delete_candidate(self, path: Path) -> None:
        """删除枚举出的受管对象/临时文件；不会递归、不会删除任意目录。"""
        candidate = Path(os.path.abspath(path))
        allowed = {self.root / "objects": set(self._MEDIA_SUFFIXES.values()), self.root / "staging": {".part"}}
        if candidate.suffix not in allowed.get(candidate.parent, set()):
            raise ValueError("清理路径不属于受管文件目录")
        self._validate_reference(candidate.stem)
        self._reject_links(candidate)
        if candidate.exists() and not candidate.is_file():
            raise ValueError("只能清理受管普通文件")
        candidate.unlink(missing_ok=True)

    def _prepare_directories(self) -> None:
        for path in (self.root, self.root / "staging", self.root / "objects"):
            self._reject_links(path)
            path.mkdir(parents=True, exist_ok=True)
            self._reject_links(path)

    def _assert_reference_unused(self, input_ref: str) -> None:
        # input_ref 在所有媒体中共享唯一命名空间；禁止相同引用跨后缀重用。
        for suffix in self._MEDIA_SUFFIXES.values():
            path = self.root / "objects" / f"{input_ref}{suffix}"
            self._reject_links(path)
            if path.exists():
                raise FileExistsError("受管文件引用已存在")

    @classmethod
    def _validate_reference(cls, input_ref: str) -> None:
        if not isinstance(input_ref, str) or cls._REFERENCE.fullmatch(input_ref) is None:
            raise ValueError("内部文件引用必须为 32 位小写十六进制字符")

    @staticmethod
    def _reject_links(path: Path) -> None:
        for component in (*reversed(path.parents), path):
            try:
                info = component.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise UnsafeUploadPathError("受管路径不得包含符号链接或目录联接点")

    def _remove_owned(self, path: Path, identity: tuple[int, int]) -> None:
        try:
            self._reject_links(path)
            info = path.stat()
            if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == identity:
                path.unlink()
        except (OSError, ValueError):
            pass
