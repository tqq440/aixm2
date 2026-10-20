"""标准库引导接收器：仅首次源码安装，不装依赖、不运行源码。"""

from contextlib import contextmanager
import getpass
import os
from pathlib import Path
import platform
import re
import shutil
import sys
from uuid import uuid4
import zipfile
import zlib

from backend.service.deployment.source_bundle_service import source_path_allowed
from backend.utils.files.source_archive import (
    MAX_ARCHIVE_BYTES, MAX_MANIFEST_BYTES, canonical_json, digest_id,
    manifest_entries, plain_path, read_bytes, regular_files, rename_directory_no_replace, sha256, strict_json,
    verified_archive, write_exclusive,
)


_OWNER = {"schemaVersion": 1, "tool": "aixm-source-receiver"}
_RESERVE_BYTES = 16 * 1024 * 1024


class Receiver:
    def __init__(self, root: Path):
        # 保存参数而已；导入和构造都不会创建目录或读取配置。
        self.root = Path(os.path.abspath(root))

    def check(self) -> dict:
        plain_path(self.root, allow_missing=True)
        if self.root.exists() and not self.root.is_dir():
            raise ValueError("Receiver root must be a directory")
        target = self.root / "inference-platform"
        plain_path(target, allow_missing=True)
        if target.exists() and not target.is_dir():
            raise ValueError("Source destination must be a directory")
        probe = self.root
        while not probe.exists():
            probe = probe.parent
        state = "absent" if not target.exists() else ("empty" if not any(target.iterdir()) else "present")
        return {"root": str(self.root), "rootExists": self.root.exists(), "targetPath": str(target), "targetState": state, "freeBytes": shutil.disk_usage(probe).free, "pythonVersion": sys.version.split()[0], "pythonSupported": sys.version_info >= (3, 10), "pythonExecutable": sys.executable, "operatingSystem": platform.system(), "platformRelease": platform.release(), "machine": platform.machine(), "user": getpass.getuser(), "uid": os.getuid() if hasattr(os, "getuid") else None, "rootReadable": os.access(probe, os.R_OK), "rootWritable": os.access(probe, os.W_OK), "nodeExecutable": shutil.which("node"), "npmExecutable": shutil.which("npm")}

    def prepare(self, bundle_id: str) -> dict:
        bundle_id = digest_id(bundle_id)
        checked = self.check()
        if not checked["pythonSupported"]:
            raise ValueError("Python 3.10 or newer is required")
        if checked["freeBytes"] < _RESERVE_BYTES:
            raise ValueError("Insufficient free disk space")
        if not self.root.exists():
            plain_path(self.root.parent)
            self.root.mkdir(mode=0o700)
        base = self.root / ".aixm-incoming"
        if not base.exists():
            base.mkdir(mode=0o700)
            write_exclusive(base / ".receiver-owner.json", canonical_json(_OWNER))
        self._owned_base()
        directory = base / bundle_id
        plain_path(directory, allow_missing=True)
        marker = {"schemaVersion": 1, "bundleId": bundle_id}
        if not directory.exists():
            directory.mkdir(mode=0o700)
            write_exclusive(directory / ".receiver-bundle.json", canonical_json(marker))
        if strict_json(read_bytes(directory / ".receiver-bundle.json", 1024)) != marker:
            raise ValueError("Incoming directory has no matching ownership marker")
        for item in directory.iterdir():
            plain_path(item)
            if item.name in {".receiver-bundle.json", "source.zip", "manifest.json"}:
                if not item.is_file():
                    raise ValueError("Incoming transfer paths must be ordinary files")
            elif re.fullmatch(r"\.extract-[0-9a-f]{32}", item.name) is None or not item.is_dir():
                raise ValueError("Unknown incoming content; refusing transfer into this directory")
        return {"bundleId": bundle_id, "incomingPath": str(directory), "archivePath": str(directory / "source.zip"), "manifestPath": str(directory / "manifest.json"), "status": "prepared"}

    def install(self, bundle_id: str) -> dict:
        directory, entries = self._validated_bundle(bundle_id)
        with self._operation_lock():
            target = self.root / "inference-platform"
            plain_path(target, allow_missing=True)
            if target.exists():
                self._verify_tree(entries)
                return self._result(bundle_id, entries, "already_installed")
            needed = sum(entry["size"] for entry in entries) + _RESERVE_BYTES
            if shutil.disk_usage(self.root).free < needed:
                raise ValueError("Insufficient disk space for extraction")
            staging = directory / (".extract-" + uuid4().hex)
            staging.mkdir(mode=0o700)
            try:
                with zipfile.ZipFile(directory / "source.zip") as source:
                    for entry in entries:
                        path = staging.joinpath(*entry["path"].split("/"))
                        plain_path(path.parent, allow_missing=True)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with source.open(entry["path"]) as stream:
                            content = stream.read(entry["size"] + 1)
                        if len(content) != entry["size"] or sha256(content) != entry["sha256"]:
                            raise ValueError("Archive changed during installation")
                        write_exclusive(path, content)
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError, zlib.error, EOFError, KeyError) as exc:
                raise ValueError("Source ZIP changed or became unreadable during installation") from exc
            self._verify_tree(entries, root=staging)
            plain_path(target, allow_missing=True)
            if target.exists():
                raise ValueError("Destination appeared during installation")
            rename_directory_no_replace(staging / "inference-platform", target)
            staging.rmdir()
            self._verify_tree(entries)
            return self._result(bundle_id, entries, "installed")

    def verify(self, bundle_id: str) -> dict:
        _, entries = self._validated_bundle(bundle_id)
        with self._operation_lock():
            self._verify_tree(entries)
        return self._result(bundle_id, entries, "verified")

    def _owned_base(self) -> Path:
        base = plain_path(self.root / ".aixm-incoming")
        if not base.is_dir() or strict_json(read_bytes(base / ".receiver-owner.json", 1024)) != _OWNER:
            raise ValueError("Incoming base is not owned by this receiver")
        return base

    def _validated_bundle(self, bundle_id: str) -> tuple[Path, list[dict]]:
        bundle_id = digest_id(bundle_id)
        directory = plain_path(self._owned_base() / bundle_id)
        if strict_json(read_bytes(directory / ".receiver-bundle.json", 1024)) != {"schemaVersion": 1, "bundleId": bundle_id}:
            raise ValueError("Incoming ownership marker does not match")
        entries = manifest_entries(strict_json(read_bytes(directory / "manifest.json", MAX_MANIFEST_BYTES)))
        if any(not source_path_allowed(entry["path"]) for entry in entries):
            raise ValueError("Manifest includes a non-source or secret path")
        if sha256(read_bytes(directory / "source.zip", MAX_ARCHIVE_BYTES)) != bundle_id:
            raise ValueError("Archive checksum does not match bundle identifier")
        try:
            verified_archive(directory / "source.zip", entries)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, zlib.error, EOFError) as exc:
            raise ValueError("Invalid source ZIP") from exc
        return directory, entries

    @contextmanager
    def _operation_lock(self):
        # 无自动 stale 接管；异常退出留下锁时须核对，不据时间猜测退出。
        lock = self._owned_base() / ".operation-lock"
        plain_path(lock, allow_missing=True)
        try:
            lock.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ValueError("Another receiver operation needs completion or review") from exc
        try:
            yield
        finally:
            plain_path(lock)
            lock.rmdir()

    def _verify_tree(self, entries: list[dict], *, root: Path | None = None) -> None:
        root = self.root if root is None else root
        target = plain_path(root / "inference-platform")
        expected = {entry["path"]: entry for entry in entries}
        actual = {path.relative_to(root).as_posix(): path for path in regular_files(target)}
        if set(actual) != set(expected):
            raise ValueError("Destination has missing or unknown files; refusing overwrite")
        for name, path in actual.items():
            entry = expected[name]
            content = read_bytes(path, entry["size"])
            if len(content) != entry["size"] or sha256(content) != entry["sha256"]:
                raise ValueError("Destination file differs from manifest")

    @staticmethod
    def _result(bundle_id: str, entries: list[dict], status: str) -> dict:
        return {"bundleId": bundle_id, "status": status, "fileCount": len(entries), "totalBytes": sum(entry["size"] for entry in entries), "servicesStarted": False, "dependenciesInstalled": False}
