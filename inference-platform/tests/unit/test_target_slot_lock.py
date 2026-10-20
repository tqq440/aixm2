"""目标槽位的进程互斥与身份隔离，不访问日常数据库或远端服务。"""

import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from backend.service.asr.target_binding import TargetSlotLock


def test_same_slot_is_exclusive_and_release_can_be_reacquired(tmp_path):
    owner = TargetSlotLock(tmp_path / "platform.sqlite3", "remote-default", 0)
    contender = TargetSlotLock(tmp_path / "platform.sqlite3", "remote-default", 0)
    try:
        assert owner.acquire()
        assert owner.acquire()  # 重复取得不需要两次释放。
        assert not contender.acquire()
        contender.release()  # 未取得者不能释放其他对象的锁。
        assert not contender.acquire()
        owner.release()
        assert contender.acquire()
        owner.release()
        assert not TargetSlotLock(tmp_path / "platform.sqlite3", "remote-default", 0).acquire()
    finally:
        owner.release()
        contender.release()
    try:
        assert owner.acquire()
    finally:
        owner.release()


def test_same_slot_is_exclusive_across_processes(tmp_path):
    owner = TargetSlotLock(tmp_path / "platform.sqlite3", "remote-default", 0)
    code = """
import sys
from pathlib import Path
from backend.service.asr.target_binding import TargetSlotLock
lock = TargetSlotLock(Path(sys.argv[1]), 'remote-default', 0)
acquired = lock.acquire()
lock.release()
raise SystemExit(7 if acquired else 0)
"""
    try:
        assert owner.acquire()
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "platform.sqlite3")],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True,
                                timeout=20, check=False,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    finally:
        owner.release()


def test_targets_and_slots_have_distinct_stable_identities(tmp_path):
    database = tmp_path / "platform.sqlite3"
    locks = [TargetSlotLock(database, target, slot) for target, slot in (
        ("remote-default", 0), ("remote-default", 1), ("alias-second", 0), ("alias-second", 1),
    )]
    assert len({lock.path for lock in locks}) == 4
    assert len({lock.worker_key for lock in locks}) == 4
    repeated = TargetSlotLock(database, "remote-default", 0)
    assert repeated.worker_key == locks[0].worker_key
    assert repeated.path == locks[0].path
    assert all(len(lock.worker_key) <= 64 for lock in locks)
    try:
        assert all(lock.acquire() for lock in locks)
    finally:
        for lock in locks:
            lock.release()


def test_database_paths_canonicalize_but_separate_databases_do_not_share_lock(tmp_path):
    first = TargetSlotLock(tmp_path / "platform.sqlite3", "remote-default", 0)
    alias = TargetSlotLock(tmp_path / "unused" / ".." / "platform.sqlite3", "remote-default", 0)
    other = TargetSlotLock(tmp_path / "isolated.sqlite3", "remote-default", 0)
    assert first.path == alias.path
    assert first.worker_key == alias.worker_key == other.worker_key
    assert first.path != other.path
    try:
        assert first.acquire()
        assert not alias.acquire()
        assert other.acquire()
    finally:
        first.release()
        alias.release()
        other.release()


def test_constructing_lock_has_no_filesystem_side_effect_and_uses_hash_only(tmp_path):
    folder = tmp_path / "not-created"
    lock = TargetSlotLock(folder / "platform.sqlite3", "named-target.with-dashes_1", 1023)
    assert not folder.exists()
    assert lock.path.parent == folder.resolve()
    assert "named-target" not in lock.path.name
    digest = lock.path.name.removeprefix("platform.sqlite3.target-slot-").removesuffix(".lock")
    assert len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


@pytest.mark.parametrize("target_id", ["", "../escape", "..\\escape", "/absolute", "C:\\absolute", "target/child", "target:stream", "target\x00tail", " leading", "trailing ", "x" * 65])
def test_invalid_target_cannot_choose_lock_path(tmp_path, target_id):
    with pytest.raises(ValidationError):
        TargetSlotLock(tmp_path / "platform.sqlite3", target_id, 0)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("slot", [-1, 1024, True, 0.0, "0", None])
def test_slot_requires_controlled_integer(tmp_path, slot):
    with pytest.raises(ValidationError):
        TargetSlotLock(tmp_path / "platform.sqlite3", "remote-default", slot)
    assert not list(tmp_path.iterdir())


def test_io_error_is_not_reported_as_an_occupied_slot(tmp_path, monkeypatch):
    lock = TargetSlotLock(tmp_path / "platform.sqlite3", "remote-default", 0)

    def fail(**kwargs):
        raise PermissionError("test permission denied")

    monkeypatch.setattr(lock._lock, "acquire", fail)
    with pytest.raises(PermissionError, match="test permission denied"):
        lock.acquire()
