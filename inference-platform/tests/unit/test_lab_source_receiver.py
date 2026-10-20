"""标准库接收器拒绝越界、覆盖和损坏传输；不启动任何进程。"""

import io
import json
from pathlib import Path
import shutil
import stat
import zipfile

import pytest

from backend.service.deployment.source_receiver import Receiver
from backend.utils.files import source_archive


@pytest.fixture
def staged_bundle(tmp_path):
    receiver = Receiver(tmp_path / "receiver")

    def stage(*, files=None, manifest=None, mode=None, duplicate=False):
        files = {"inference-platform/backend/__init__.py": b"", "inference-platform/README.md": b"source\n"} if files is None else files
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                entry = zipfile.ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = (stat.S_IFREG | 0o644 if mode is None else mode) << 16
                archive.writestr(entry, data)
            if duplicate:
                with pytest.warns(UserWarning, match="Duplicate"):
                    archive.writestr(next(iter(files)), next(iter(files.values())))
        content = output.getvalue()
        bundle_id = source_archive.sha256(content)
        prepared = receiver.prepare(bundle_id)
        Path(prepared["archivePath"]).write_bytes(content)
        if manifest is None:
            manifest = {"schemaVersion": 1, "files": [{"path": name, "size": len(data), "sha256": source_archive.sha256(data)} for name, data in files.items()]}
        Path(prepared["manifestPath"]).write_bytes(source_archive.canonical_json(manifest))
        return receiver, bundle_id, Path(prepared["incomingPath"])

    return stage


def test_atomic_install_verify_and_identical_repeat(staged_bundle):
    receiver, bundle_id, incoming = staged_bundle()
    result = receiver.install(bundle_id)
    assert result["status"] == "installed"
    assert result["servicesStarted"] is False and result["dependenciesInstalled"] is False
    assert receiver.verify(bundle_id)["status"] == "verified"
    assert receiver.install(bundle_id)["status"] == "already_installed"
    assert not list(incoming.glob(".extract-*"))
    assert (receiver.root / "inference-platform/README.md").read_bytes() == b"source\n"


def test_check_and_constructor_do_not_create_files(tmp_path):
    receiver = Receiver(tmp_path / "new-root")
    checked = receiver.check()
    assert checked["rootExists"] is False and checked["targetState"] == "absent"
    assert checked["pythonSupported"] is True
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bundle_id", ["../escape", "A" * 64, "0" * 63])
def test_invalid_identifier_never_creates_root(tmp_path, bundle_id):
    receiver = Receiver(tmp_path / "root")
    with pytest.raises(ValueError):
        receiver.prepare(bundle_id)
    assert not receiver.root.exists()


def test_existing_unknown_code_is_preserved(staged_bundle):
    receiver, bundle_id, _ = staged_bundle()
    target = receiver.root / "inference-platform"
    target.mkdir()
    (target / "user.txt").write_text("keep")
    with pytest.raises(ValueError, match="unknown files"):
        receiver.install(bundle_id)
    assert (target / "user.txt").read_text() == "keep"


def test_existing_empty_user_directory_is_not_removed(staged_bundle):
    receiver, bundle_id, _ = staged_bundle()
    target = receiver.root / "inference-platform"
    target.mkdir()
    before = target.stat().st_ino
    with pytest.raises(ValueError):
        receiver.install(bundle_id)
    assert target.is_dir() and target.stat().st_ino == before


@pytest.mark.parametrize("change", ["edited", "extra", "missing"])
def test_post_install_differences_reject_idempotent_install(staged_bundle, change):
    receiver, bundle_id, _ = staged_bundle()
    receiver.install(bundle_id)
    path = receiver.root / "inference-platform/README.md"
    if change == "edited":
        path.write_text("changed")
    elif change == "missing":
        path.unlink()
    else:
        (path.parent / "unknown.py").write_text("unknown")
    with pytest.raises(ValueError):
        receiver.install(bundle_id)


@pytest.mark.parametrize("path", ["../escape.py", "/absolute.py", "inference-platform/backend/../escape.py", "inference-platform\\backend\\a.py", "inference-platform/backend/C:a.py", "inference-platform/.env", "inference-platform/runtime/db/data.json", "inference-platform/docs/server.key"])
def test_bad_archive_path_is_rejected_before_extraction(staged_bundle, path):
    receiver, bundle_id, _ = staged_bundle(files={path: b"sensitive"})
    with pytest.raises(ValueError):
        receiver.install(bundle_id)
    assert not (receiver.root / "inference-platform").exists()


def test_link_zip_entry_is_rejected(staged_bundle):
    receiver, bundle_id, _ = staged_bundle(files={"inference-platform/backend/linked.py": b"/etc/passwd"}, mode=stat.S_IFLNK | 0o777)
    with pytest.raises(ValueError, match="non-regular"):
        receiver.install(bundle_id)


def test_zip_duplicate_is_rejected(staged_bundle):
    receiver, bundle_id, _ = staged_bundle(duplicate=True)
    with pytest.raises(ValueError, match="file counts"):
        receiver.install(bundle_id)


def test_archive_hash_change_is_rejected(staged_bundle):
    receiver, bundle_id, incoming = staged_bundle()
    with (incoming / "source.zip").open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        receiver.install(bundle_id)


def test_manifest_extra_entry_and_wrong_hash_are_rejected(staged_bundle):
    receiver, bundle_id, incoming = staged_bundle()
    path = incoming / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"][1]["sha256"] = "0" * 64
    path.write_bytes(source_archive.canonical_json(manifest))
    with pytest.raises(ValueError, match="content differs"):
        receiver.install(bundle_id)
    manifest["files"].append({"path": "inference-platform/missing.py", "size": 0, "sha256": "0" * 64})
    path.write_bytes(source_archive.canonical_json(manifest))
    with pytest.raises(ValueError):
        receiver.install(bundle_id)


def test_duplicate_json_and_bool_size_are_not_coerced(staged_bundle):
    receiver, bundle_id, incoming = staged_bundle()
    path = incoming / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"][0]["size"] = False
    path.write_bytes(source_archive.canonical_json(manifest))
    with pytest.raises(ValueError, match="size"):
        receiver.install(bundle_id)
    path.write_text('{"schemaVersion":1,"schemaVersion":1,"files":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate JSON"):
        receiver.install(bundle_id)


def test_manifest_case_collision_is_rejected(staged_bundle):
    files = {"inference-platform/backend/x.py": b"a", "inference-platform/backend/X.py": b"b"}
    receiver, bundle_id, _ = staged_bundle(files=files)
    with pytest.raises(ValueError, match="Duplicate manifest"):
        receiver.install(bundle_id)


def test_disk_full_rejects_before_extraction(staged_bundle, monkeypatch):
    receiver, bundle_id, _ = staged_bundle()
    monkeypatch.setattr(shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(10, 9, 1))
    with pytest.raises(ValueError, match="disk space"):
        receiver.install(bundle_id)
    assert not (receiver.root / "inference-platform").exists()


def test_unknown_incoming_directory_is_not_claimed(tmp_path):
    root = tmp_path / "receiver"
    incoming = root / ".aixm-incoming"
    incoming.mkdir(parents=True)
    (incoming / "user.txt").write_text("keep")
    with pytest.raises((ValueError, OSError)):
        Receiver(root).prepare("a" * 64)
    assert list(incoming.iterdir()) == [incoming / "user.txt"]


def test_operation_lock_is_not_deleted_or_bypassed(staged_bundle):
    receiver, bundle_id, _ = staged_bundle()
    lock = receiver.root / ".aixm-incoming/.operation-lock"
    lock.mkdir()
    with pytest.raises(ValueError, match="Another receiver"):
        receiver.install(bundle_id)
    assert lock.is_dir()


def test_prepare_refuses_preexisting_linked_transfer_file(staged_bundle, tmp_path):
    receiver, bundle_id, incoming = staged_bundle()
    (incoming / "source.zip").unlink()
    outside = tmp_path / "private.txt"
    outside.write_bytes(b"keep")
    try:
        (incoming / "source.zip").hardlink_to(outside)
    except OSError:
        pytest.skip("Temporary filesystem does not support hard links")
    with pytest.raises(ValueError, match="Hard-linked"):
        receiver.prepare(bundle_id)
    assert outside.read_bytes() == b"keep"


def test_prepare_refuses_unknown_incoming_file(staged_bundle):
    receiver, bundle_id, incoming = staged_bundle()
    (incoming / "unknown.txt").write_text("keep")
    with pytest.raises(ValueError, match="Unknown incoming"):
        receiver.prepare(bundle_id)
