"""文件边界、数据库关闭、SQLite屏障及确认契约。"""

import json
from pathlib import Path
import sqlite3
import pytest
from pydantic import ValidationError
from backend.schema.deployment.recovery_schema import SnapshotFile, SnapshotRequest
from backend.utils.files.recovery_files import copy_verified, readonly_database, sqlite_snapshot, sqlite_write_barrier
from backend.utils.files.source_archive import sha256


@pytest.mark.parametrize("confirmation",[False,1,"true",None])
def test_offline_confirmation_must_be_explicit_json_true(tmp_path,confirmation):
    with pytest.raises(ValidationError):SnapshotRequest(root=tmp_path,output=tmp_path/"snapshot",controllers_stopped_confirmed=confirmation)


@pytest.mark.parametrize("path",["../outside","db/../../outside","/absolute","config/secret.env","db/center.sqlite3-wal","logs/center/CON","uploads/a:stream"])
def test_manifest_path_cannot_escape_or_restore_private_startup_files(path):
    with pytest.raises(ValidationError):SnapshotFile(path=path,size=0,sha256="a"*64)


def test_copy_is_exclusive_and_checks_the_expected_digest(tmp_path):
    source=tmp_path/"source";source.write_bytes(b"fixture")
    expected=SnapshotFile(path="logs/center/test.log",size=7,sha256=sha256(b"fixture"))
    copy_verified(source,tmp_path/"target",expected)
    with pytest.raises(FileExistsError):copy_verified(source,tmp_path/"target",expected)
    source.write_bytes(b"changed")
    with pytest.raises(ValueError,match="CHANGED"):copy_verified(source,tmp_path/"other",expected)
    assert not (tmp_path/"other").exists()


def test_database_snapshot_closes_every_handle_and_write_barrier_is_released(tmp_path):
    source=tmp_path/"source.sqlite3"
    connection=sqlite3.connect(source)
    connection.execute("CREATE TABLE sample(value TEXT)");connection.execute("INSERT INTO sample VALUES ('fixture')");connection.commit();connection.close()
    with sqlite_write_barrier(source):
        other=sqlite3.connect(source,timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError):other.execute("BEGIN IMMEDIATE")
        finally:other.close()
        sqlite_snapshot(source,tmp_path/"snapshot.sqlite3")
    (tmp_path/"snapshot.sqlite3").rename(tmp_path/"renamed.sqlite3")
    with readonly_database(tmp_path/"renamed.sqlite3") as restored:
        assert restored.execute("SELECT value FROM sample").fetchone()[0]=="fixture"
        with pytest.raises(sqlite3.OperationalError):restored.execute("DELETE FROM sample")
    with sqlite_write_barrier(source):pass


@pytest.mark.parametrize("folder",[".pending-snapshot-fixture",".pending-restore-fixture"])
def test_unpublished_directories_are_fenced_even_before_marker_is_written(tmp_path,folder):
    from backend.common.exceptions.app_error import StartupError
    from backend.config.settings.app_settings import AppSettings
    from backend.data.database.database import Database
    path=tmp_path/folder/"db/center.sqlite3"
    with pytest.raises(StartupError,match="RESTORE_QUARANTINED"):Database(AppSettings(database_path=path))
    assert not path.parent.exists()


def test_root_quarantine_marker_blocks_accidentally_missing_database_marker(tmp_path):
    from backend.common.storage.recovery_guard import require_active_database
    from backend.common.exceptions.app_error import StartupError
    (tmp_path/"RESTORE_QUARANTINED.json").write_bytes(b"even malformed markers must fail closed")
    with pytest.raises(StartupError,match="RESTORE_QUARANTINED"):require_active_database(tmp_path/"db/center.sqlite3")
