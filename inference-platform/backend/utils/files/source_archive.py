"""仅用标准库的路径、JSON、哈希与 ZIP 工具；导入不访问文件系统。"""

from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import zipfile


MAX_FILES = 10000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_RESERVED = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z", re.I)


def archive_path(value: str) -> str:
    """接受跨 Windows/Linux 的唯一相对文件名，不修正含糊输入。"""
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024:
        raise ValueError("Invalid archive path")
    if "\\" in value or ":" in value or any(ord(char) < 32 for char in value):
        raise ValueError("Unsafe archive path")
    parts = value.split("/")
    if any(part in ("", ".", "..") or part.endswith((".", " ")) or _RESERVED.fullmatch(part) for part in parts):
        raise ValueError("Non-canonical archive path")
    if PurePosixPath(value).is_absolute():
        raise ValueError("Absolute archive path")
    return value


def digest_id(value: str) -> str:
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        raise ValueError("Expected a lowercase SHA-256 identifier")
    return value


def plain_path(path: Path, *, allow_missing: bool = False, allow_hardlinks: bool = False) -> Path:
    """逐级 lstat；拒绝符号链接、Windows reparse point 和特殊文件。"""
    result = Path(os.path.abspath(path))
    for item in (*reversed(result.parents), result):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked paths are not accepted")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError("Only ordinary files and directories are accepted")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1 and not allow_hardlinks:
            raise ValueError("Hard-linked files are not accepted")
    return result


def read_bytes(path: Path, limit: int, *, allow_hardlinks: bool = False) -> bytes:
    path = plain_path(path, allow_hardlinks=allow_hardlinks)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or (before.st_nlink != 1 and not allow_hardlinks) or before.st_size > limit:
            raise ValueError("File is not ordinary or exceeds its size limit")
        content = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if len(content) > limit or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("File exceeds its limit or changed during reading")
    return content


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def strict_json(content: bytes) -> object:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Non-finite JSON number")

    try:
        return json.loads(content.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid UTF-8 JSON") from exc


def write_exclusive(path: Path, content: bytes) -> None:
    plain_path(path.parent)
    plain_path(path, allow_missing=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def rename_directory_no_replace(source: Path, destination: Path) -> None:
    """原子提交且不覆盖竞态中新建的空目录；不支持时明确拒绝。"""
    source = plain_path(source)
    destination = plain_path(destination, allow_missing=True)
    if destination.exists():
        raise FileExistsError("Destination already exists")
    if os.name == "nt":
        # Windows rename 即使目标为空目录也不会覆盖。
        os.rename(source, destination)
        return
    if sys.platform.startswith("linux"):
        import ctypes
        library = ctypes.CDLL(None, use_errno=True)
        rename = getattr(library, "renameat2", None)
        if rename is None:
            raise ValueError("Atomic no-replace rename is unavailable")
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
        return
    raise ValueError("Atomic no-replace rename is unsupported on this OS")


def regular_files(root: Path, *, excluded_names: frozenset[str] = frozenset()) -> list[Path]:
    root = plain_path(root)
    if not root.is_dir():
        raise ValueError("Expected a directory")
    result = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            if path.name.casefold() in excluded_names:
                continue
            plain_path(path)
            if path.is_dir():
                pending.append(path)
            else:
                result.append(path)
                if len(result) > MAX_FILES:
                    raise ValueError("Too many files")
    return sorted(result)


def create_archive(files: Mapping[str, Path], destination: Path) -> dict:
    if not files or len(files) > MAX_FILES:
        raise ValueError("Invalid archive file count")
    folded = set()
    total = 0
    entries = []
    plain_path(destination.parent)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for name, path in sorted(files.items()):
            archive_path(name)
            if name.casefold() in folded:
                raise ValueError("Duplicate archive path")
            folded.add(name.casefold())
            content = read_bytes(path, MAX_FILE_BYTES)
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise ValueError("Archive expanded size exceeds limit")
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(info, content)
            entries.append({"path": name, "size": len(content), "sha256": sha256(content)})
    if destination.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("Archive compressed size exceeds limit")
    return {"schemaVersion": 1, "files": entries}


def manifest_entries(value: object) -> list[dict]:
    if not isinstance(value, dict) or set(value) != {"schemaVersion", "files"} or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise ValueError("Unsupported manifest")
    entries = value["files"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
        raise ValueError("Invalid manifest file count")
    seen = set()
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise ValueError("Invalid manifest entry")
        path = archive_path(entry["path"])
        if path.casefold() in seen:
            raise ValueError("Duplicate manifest path")
        seen.add(path.casefold())
        digest_id(entry["sha256"])
        if type(entry["size"]) is not int or not 0 <= entry["size"] <= MAX_FILE_BYTES:
            raise ValueError("Invalid manifest file size")
        total += entry["size"]
    if total > MAX_TOTAL_BYTES:
        raise ValueError("Manifest expanded size exceeds limit")
    return entries


def verified_archive(archive: Path, entries: list[dict]) -> None:
    """逐成员有界解压校验，不使用 extract/extractall。"""
    expected = {entry["path"]: entry for entry in entries}
    seen = set()
    with zipfile.ZipFile(archive) as source:
        infos = source.infolist()
        if len(infos) != len(expected):
            raise ValueError("ZIP and manifest file counts differ")
        for info in infos:
            name = archive_path(info.filename)
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if name.casefold() in seen or name not in expected or info.is_dir() or kind not in (0, stat.S_IFREG) or (info.external_attr & 0x10):
                raise ValueError("ZIP contains duplicate, extra or non-regular entries")
            if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ValueError("Unsupported ZIP encoding")
            seen.add(name.casefold())
            entry = expected[name]
            if info.file_size != entry["size"]:
                raise ValueError("ZIP size differs from manifest")
            with source.open(info) as stream:
                content = stream.read(entry["size"] + 1)
            if len(content) != entry["size"] or sha256(content) != entry["sha256"]:
                raise ValueError("ZIP content differs from manifest")
