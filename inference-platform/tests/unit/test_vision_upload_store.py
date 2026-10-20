"""同一 M06 根目录中的图片/音频隔离、锁和全类型清理。"""

from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import os
from pathlib import Path
from types import SimpleNamespace

from filelock import Timeout
import pytest

from backend.data.files.upload_store import EmptyUploadError, LocalUploadStore, UnsafeUploadPathError, UploadTooLargeError


def test_image_upload_uses_managed_image_suffix_and_preserves_original_bytes(tmp_path):
    store = LocalUploadStore(tmp_path / "uploads", media_type="image")
    assert not store.root.exists()
    content = b"test image bytes" * 10000
    with store.lock():
        saved = store.save(BytesIO(content), max_bytes=len(content))
    assert saved.path == store.root / "objects" / f"{saved.input_ref}.image"
    assert saved.path.read_bytes() == content
    assert saved.size_bytes == len(content)
    assert saved.sha256 == hashlib.sha256(content).hexdigest()
    assert store.resolve_path(saved.input_ref) == saved.path
    assert list((store.root / "staging").iterdir()) == []
    store.delete(saved.input_ref)
    store.delete(saved.input_ref)
    assert not saved.path.exists()


@pytest.mark.parametrize("media_type", [None, "", "video", ".image", "../objects", "image/../audio", ["image"]])
def test_media_type_is_an_explicit_allowlist_before_filesystem_access(tmp_path, media_type):
    with pytest.raises(ValueError, match="media_type"):
        LocalUploadStore(tmp_path / "uploads", media_type=media_type)
    assert list(tmp_path.iterdir()) == []


def test_cross_media_resolve_and_delete_do_not_fall_back_to_other_file(tmp_path):
    audio_store = LocalUploadStore(tmp_path / "uploads")
    image_store = LocalUploadStore(tmp_path / "uploads", media_type="image")
    audio = audio_store.save(BytesIO(b"old audio"), max_bytes=100)
    image = image_store.save(BytesIO(b"new image"), max_bytes=100)
    assert audio.path.suffix == ".audio" and image.path.suffix == ".image"
    assert not image_store.resolve_path(audio.input_ref).exists()
    assert not audio_store.resolve_path(image.input_ref).exists()
    image_store.delete(audio.input_ref)
    audio_store.delete(image.input_ref)
    assert audio.path.read_bytes() == b"old audio"
    assert image.path.read_bytes() == b"new image"


@pytest.mark.parametrize("reference", ["../secret", "a" * 32 + ".audio", "a" * 32 + ".image", "A" * 32, "a" * 31, None])
def test_image_references_cannot_smuggle_a_path_or_another_suffix(tmp_path, reference):
    store = LocalUploadStore(tmp_path / "uploads", media_type="image")
    with pytest.raises(ValueError):
        store.resolve_path(reference)
    with pytest.raises(ValueError):
        store.delete(reference)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("first_type,second_type", [("audio", "image"), ("image", "audio")])
def test_input_reference_cannot_be_reused_across_media(tmp_path, monkeypatch, first_type, second_type):
    reference = "a" * 32
    monkeypatch.setattr("backend.data.files.upload_store.uuid4", lambda: SimpleNamespace(hex=reference))
    first = LocalUploadStore(tmp_path / "uploads", media_type=first_type)
    saved = first.save(BytesIO(b"previous"), max_bytes=100)
    other = LocalUploadStore(tmp_path / "uploads", media_type=second_type)
    with pytest.raises(FileExistsError):
        other.save(BytesIO(b"replacement"), max_bytes=100)
    assert saved.path.read_bytes() == b"previous"
    assert list((first.root / "objects").iterdir()) == [saved.path]
    assert list((first.root / "staging").iterdir()) == []


@pytest.mark.parametrize("content,limit,error", [(b"", 10, EmptyUploadError), (b"abcd", 3, UploadTooLargeError)])
def test_failed_image_upload_does_not_leave_partial_or_final_object(tmp_path, content, limit, error):
    store = LocalUploadStore(tmp_path / "uploads", media_type="image")
    with pytest.raises(error):
        store.save(BytesIO(content), max_bytes=limit)
    assert list(store.root.glob("*/*")) == []


def test_audio_and_image_submission_share_one_maintenance_lock(tmp_path, monkeypatch):
    audio = LocalUploadStore(tmp_path / "uploads")
    image = LocalUploadStore(tmp_path / "uploads", media_type="image")
    monkeypatch.setattr(LocalUploadStore, "LOCK_TIMEOUT_SECONDS", 0.02)
    with audio.lock():
        with pytest.raises(Timeout):
            with image.lock():
                pytest.fail("图片上传不能绕过音频与孤儿维护的共同锁")
    with image.lock():
        assert (image.root / ".maintenance.lock").exists()


@pytest.mark.parametrize("maintenance_type", ["audio", "image"])
def test_cleanup_sees_both_media_but_keeps_all_referenced_and_recent_objects(tmp_path, maintenance_type):
    audio = LocalUploadStore(tmp_path / "uploads")
    image = LocalUploadStore(tmp_path / "uploads", media_type="image")
    referenced = [audio.save(BytesIO(b"keep audio"), max_bytes=100), image.save(BytesIO(b"keep image"), max_bytes=100)]
    orphans = [audio.save(BytesIO(b"old audio"), max_bytes=100), image.save(BytesIO(b"old image"), max_bytes=100)]
    recent = image.save(BytesIO(b"recent"), max_bytes=100)
    unmanaged = audio.root / "objects" / "notes.png"
    unmanaged.write_bytes(b"unmanaged")
    staging = audio.root / "staging" / f"{'b' * 32}.part"
    staging.write_bytes(b"partial")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    for path in [upload.path for upload in referenced + orphans] + [unmanaged, staging]:
        os.utime(path, (old, old))
    maintenance = LocalUploadStore(audio.root, media_type=maintenance_type)
    with maintenance.lock():
        candidates = maintenance.iter_orphan_candidates({upload.input_ref for upload in referenced}, datetime.now(timezone.utc) - timedelta(days=1))
        assert set(candidates) == {upload.path for upload in orphans} | {staging}
        for path in candidates:
            maintenance.delete_candidate(path)
    assert all(upload.path.exists() for upload in referenced)
    assert recent.path.exists() and unmanaged.exists()
    assert not any(upload.path.exists() for upload in orphans)
    assert not staging.exists()


@pytest.mark.parametrize("relative", ["objects/note.image", "objects/sub/" + "a" * 32 + ".image", "staging/" + "a" * 32 + ".image"])
def test_cleanup_rejects_nonmanaged_image_paths(tmp_path, relative):
    store = LocalUploadStore(tmp_path / "uploads")
    path = store.root / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"keep")
    with pytest.raises(ValueError):
        store.delete_candidate(path)
    assert path.read_bytes() == b"keep"


@pytest.mark.skipif(os.name != "nt", reason="Windows 目录联接点测试")
def test_default_audio_maintenance_and_image_access_reject_image_junction(tmp_path):
    import _winapi

    root = tmp_path / "uploads"
    (root / "objects").mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    reference = "a" * 32
    junction = root / "objects" / f"{reference}.image"
    try:
        _winapi.CreateJunction(str(target), str(junction))
    except PermissionError as exc:
        pytest.skip(f"当前沙箱不能创建目录联接点：{exc}")
    try:
        audio = LocalUploadStore(root)
        image = LocalUploadStore(root, media_type="image")
        with pytest.raises(UnsafeUploadPathError):
            image.resolve_path(reference)
        with pytest.raises(UnsafeUploadPathError):
            image.delete(reference)
        with pytest.raises(UnsafeUploadPathError):
            audio.iter_orphan_candidates(set(), datetime.now(timezone.utc))
        with pytest.raises(UnsafeUploadPathError):
            audio.delete_candidate(junction)
        assert marker.read_text(encoding="utf-8") == "keep"
    finally:
        junction.rmdir()
