from datetime import datetime, timedelta, timezone
import hashlib
import io
import os
from pathlib import Path
from types import SimpleNamespace

from filelock import Timeout
import pytest

from backend.data.files.upload_store import EmptyUploadError, LocalUploadStore, UploadTooLargeError


def test_constructor_does_not_create_directories(tmp_path):
    LocalUploadStore(tmp_path / "uploads")
    assert list(tmp_path.iterdir()) == []


def test_save_reads_chunks_and_hashes_exact_limit(tmp_path):
    payload = b"sample" * 25000

    class ChunkOnlyStream(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= LocalUploadStore.CHUNK_SIZE
            return super().read(size)

    store = LocalUploadStore(tmp_path / "uploads")
    stored = store.save(ChunkOnlyStream(payload), max_bytes=len(payload))
    assert stored.path == store.root / "objects" / f"{stored.input_ref}.audio"
    assert len(stored.input_ref) == 32
    assert stored.path.read_bytes() == payload
    assert stored.size_bytes == len(payload)
    assert stored.sha256 == hashlib.sha256(payload).hexdigest()
    assert store.resolve_path(stored.input_ref) == stored.path
    assert list((store.root / "staging").iterdir()) == []
    store.delete(stored.input_ref)
    store.delete(stored.input_ref)
    assert not stored.path.exists()


@pytest.mark.parametrize("payload,limit,error", [(b"", 10, EmptyUploadError), (b"abcd", 3, UploadTooLargeError)])
def test_invalid_size_leaves_no_file(tmp_path, payload, limit, error):
    store = LocalUploadStore(tmp_path / "uploads")
    with pytest.raises(error):
        store.save(io.BytesIO(payload), max_bytes=limit)
    assert list((store.root / "staging").iterdir()) == []
    assert list((store.root / "objects").iterdir()) == []


def test_interrupted_read_removes_partial_file(tmp_path):
    class BrokenStream:
        calls = 0

        def read(self, size):
            self.calls += 1
            if self.calls == 2:
                raise KeyboardInterrupt()
            return b"partial audio"

    store = LocalUploadStore(tmp_path / "uploads")
    with pytest.raises(KeyboardInterrupt):
        store.save(BrokenStream(), max_bytes=1024)
    assert list(store.root.glob("*/*")) == []


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_write_or_promote_error_removes_own_file(tmp_path, monkeypatch, operation):
    def fail(*args, **kwargs):
        raise OSError("injected disk failure")

    monkeypatch.setattr(f"backend.data.files.upload_store.os.{operation}", fail)
    store = LocalUploadStore(tmp_path / "uploads")
    with pytest.raises(OSError, match="injected"):
        store.save(io.BytesIO(b"audio"), max_bytes=100)
    assert list(store.root.glob("*/*")) == []


def test_replace_completed_then_raised_removes_promoted_object(tmp_path, monkeypatch):
    original_replace = os.replace

    def ambiguous_replace(source, destination):
        original_replace(source, destination)
        raise OSError("failure after promotion")

    monkeypatch.setattr("backend.data.files.upload_store.os.replace", ambiguous_replace)
    store = LocalUploadStore(tmp_path / "uploads")
    with pytest.raises(OSError, match="after promotion"):
        store.save(io.BytesIO(b"audio"), max_bytes=100)
    assert list(store.root.glob("*/*")) == []


def test_failed_promote_never_deletes_unowned_destination(tmp_path, monkeypatch):
    def replaced_by_other_file(source, destination):
        Path(destination).write_bytes(b"another upload")
        raise OSError("collision")

    monkeypatch.setattr("backend.data.files.upload_store.os.replace", replaced_by_other_file)
    store = LocalUploadStore(tmp_path / "uploads")
    with pytest.raises(OSError, match="collision"):
        store.save(io.BytesIO(b"audio"), max_bytes=100)
    assert list((store.root / "staging").iterdir()) == []
    assert [path.read_bytes() for path in (store.root / "objects").iterdir()] == [b"another upload"]


@pytest.mark.parametrize("directory,suffix", [("objects", ".audio"), ("staging", ".part")])
def test_reference_collision_keeps_existing_file(tmp_path, monkeypatch, directory, suffix):
    reference = "a" * 32
    monkeypatch.setattr("backend.data.files.upload_store.uuid4", lambda: SimpleNamespace(hex=reference))
    root = tmp_path / "uploads"
    path = root / directory / f"{reference}{suffix}"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"previous upload")
    store = LocalUploadStore(root)
    with pytest.raises(FileExistsError):
        store.save(io.BytesIO(b"new upload"), max_bytes=100)
    assert path.read_bytes() == b"previous upload"


@pytest.mark.parametrize("reference", ["../secret", "C:\\secret", "/secret", "A" * 32, "a" * 31, "a" * 33, "a" * 32 + ".audio", None])
def test_invalid_references_are_rejected_without_touching_disk(tmp_path, reference):
    store = LocalUploadStore(tmp_path / "uploads")
    with pytest.raises(ValueError):
        store.resolve_path(reference)
    with pytest.raises(ValueError):
        store.delete(reference)
    assert list(tmp_path.iterdir()) == []


def test_orphan_candidates_exclude_referenced_recent_and_unmanaged_files(tmp_path):
    store = LocalUploadStore(tmp_path / "uploads")
    kept = store.save(io.BytesIO(b"referenced"), max_bytes=100)
    orphan = store.save(io.BytesIO(b"orphan"), max_bytes=100)
    recent = store.save(io.BytesIO(b"recent"), max_bytes=100)
    staging = store.root / "staging" / f"{'b' * 32}.part"
    staging.write_bytes(b"interrupted upload")
    unmanaged = store.root / "objects" / "notes.txt"
    unmanaged.write_bytes(b"leave me alone")
    nested = store.root / "objects" / f"{'c' * 32}.audio"
    nested.mkdir()
    old = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    for path in (kept.path, orphan.path, staging, unmanaged, nested):
        os.utime(path, (old, old))
    with store.lock():
        candidates = store.iter_orphan_candidates(
            {kept.input_ref}, datetime.now(timezone.utc) - timedelta(days=1),
        )
        assert set(candidates) == {orphan.path, staging}
        for path in candidates:
            store.delete_candidate(path)
    assert kept.path.exists() and recent.path.exists() and unmanaged.exists() and nested.is_dir()
    assert not orphan.path.exists() and not staging.exists()


@pytest.mark.parametrize("relative", ["outside.audio", "objects/notes.txt", "objects/sub/" + "a" * 32 + ".audio", "staging/" + "a" * 32 + ".audio"])
def test_delete_candidate_rejects_unmanaged_paths(tmp_path, relative):
    store = LocalUploadStore(tmp_path / "uploads")
    candidate = store.root / relative
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"keep")
    with pytest.raises(ValueError):
        store.delete_candidate(candidate)
    assert candidate.read_bytes() == b"keep"


def test_delete_candidate_rejects_path_escape_and_directory(tmp_path):
    store = LocalUploadStore(tmp_path / "uploads")
    outside = tmp_path / f"{'a' * 32}.audio"
    outside.write_bytes(b"keep")
    with pytest.raises(ValueError):
        store.delete_candidate(store.root / "objects" / ".." / ".." / outside.name)
    directory = store.root / "objects" / f"{'b' * 32}.audio"
    directory.mkdir(parents=True)
    with pytest.raises(ValueError):
        store.delete_candidate(directory)
    assert outside.exists() and directory.is_dir()


def test_cleanup_requires_aware_cutoff(tmp_path):
    store = LocalUploadStore(tmp_path / "uploads")
    with pytest.raises(ValueError, match="时区"):
        store.iter_orphan_candidates(set(), datetime.now())


def test_locks_exclude_other_store_instances_and_release_after_exception(tmp_path, monkeypatch):
    first = LocalUploadStore(tmp_path / "uploads")
    second = LocalUploadStore(tmp_path / "uploads")
    monkeypatch.setattr(LocalUploadStore, "LOCK_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(RuntimeError, match="operation interrupted"):
        with first.lock():
            with pytest.raises(Timeout):
                with second.lock():
                    pytest.fail("second lock should not be acquired")
            raise RuntimeError("operation interrupted")
    with second.lock():
        assert second.root.exists()


def test_symlinks_are_rejected_without_deleting_their_target(tmp_path):
    root = tmp_path / "uploads"
    (root / "objects").mkdir(parents=True)
    target = tmp_path / "target.audio"
    target.write_bytes(b"outside managed directory")
    reference = "a" * 32
    link = root / "objects" / f"{reference}.audio"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"当前 Windows 账户不能创建符号链接：{exc}")
    store = LocalUploadStore(root)
    with pytest.raises(ValueError, match="符号链接"):
        store.resolve_path(reference)
    with pytest.raises(ValueError, match="符号链接"):
        store.delete(reference)
    with pytest.raises(ValueError, match="符号链接"):
        store.delete_candidate(link)
    assert target.read_bytes() == b"outside managed directory"


def test_linked_root_is_rejected(tmp_path):
    target = tmp_path / "actual"
    target.mkdir()
    root = tmp_path / "uploads"
    try:
        root.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前 Windows 账户不能创建符号链接：{exc}")
    store = LocalUploadStore(root)
    with pytest.raises(ValueError, match="符号链接"):
        store.save(io.BytesIO(b"audio"), max_bytes=100)
    with pytest.raises(ValueError, match="符号链接"):
        with store.lock():
            pytest.fail("linked root cannot be locked")
    assert list(target.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows 目录联接点测试")
@pytest.mark.parametrize("linked_part", ["root", "objects"])
def test_windows_directory_junction_is_rejected(tmp_path, linked_part):
    import _winapi

    target = tmp_path / "outside"
    target.mkdir()
    root = tmp_path / "uploads"
    junction = root
    if linked_part == "objects":
        root.mkdir()
        junction = root / "objects"
    try:
        _winapi.CreateJunction(str(target), str(junction))
    except PermissionError as exc:
        pytest.skip(f"当前沙箱不能创建目录联接点：{exc}")
    try:
        store = LocalUploadStore(root)
        with pytest.raises(ValueError, match="联接点"):
            store.save(io.BytesIO(b"audio"), max_bytes=100)
        with pytest.raises(ValueError, match="联接点"):
            store.resolve_path("a" * 32)
        with pytest.raises(ValueError, match="联接点"):
            store.iter_orphan_candidates(set(), datetime.now(timezone.utc))
        assert list(target.iterdir()) == []
    finally:
        # 只移除本次创建的联接点，不递归，也不修改其目标目录。
        junction.rmdir()
