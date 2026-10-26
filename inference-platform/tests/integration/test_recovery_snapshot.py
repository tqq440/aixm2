"""成套快照/隔离恢复；全部资料和配置在独立测试目录。"""

import json
from pathlib import Path
import sqlite3
import shutil
from filelock import FileLock, Timeout
import pytest
from backend.common.exceptions.app_error import StartupError
from backend.config.deployment.linux_settings import deployment_settings
from backend.data.database.database import Database
from backend.data.host_node.node_database import NodeDatabase
from backend.schema.deployment.linux_schema import ValidationWorkspace
from backend.schema.deployment.recovery_schema import RestoreRequest, SnapshotRequest
from backend.service.deployment.validation_service import create_validation_workspace
from backend.service.deployment.recovery_service import create_snapshot, inspect_snapshot, restore_snapshot
from backend.utils.files.recovery_files import readonly_database
from tests.integration.recovery_support import populate_pair
from backend.config.settings.app_settings import PROJECT_ROOT
from backend.modules.api.app import create_app
from backend.modules.host_node.app import create_node_app
from backend.utils.files.source_archive import canonical_json, sha256


@pytest.fixture
def workspace(tmp_path):
    root=tmp_path/"source"
    create_validation_workspace(ValidationWorkspace(root=root))
    return root


def backup(root,output):
    return create_snapshot(SnapshotRequest(root=root,output=output,controllers_stopped_confirmed=True))


def restore(snapshot,output):
    return restore_snapshot(RestoreRequest(snapshot=snapshot,output=output))


def test_empty_workspace_round_trip_stays_quarantined(workspace,tmp_path):
    snapshot=tmp_path/"snapshot";target=tmp_path/"restored"
    result=backup(workspace,snapshot)
    assert result["quarantined"] and not result["redisAccessed"]
    facts=inspect_snapshot(snapshot)
    assert facts["databases"]["center"]["tasks"]==0
    restored=restore(snapshot,target)
    assert restored["originalStatesPreserved"] and not restored["queueReplayed"]
    assert not (target/"READY").exists() and not (target/"config").exists()
    assert (target/"recovery/config/center.private.json").is_file()
    center,_=deployment_settings("center",workspace/"config/center.env")
    node,_=deployment_settings("node",workspace/"config/node.env")
    for root in (snapshot,target):
        for cls,settings,name in ((Database,center,"center"),(NodeDatabase,node,"node")):
            relocated=settings.model_copy(update={"database_path":root/f"db/{name}.sqlite3"})
            with pytest.raises(StartupError,match="RESTORE_QUARANTINED"):cls(relocated)
    # No migrations or startup touched the source database.
    with sqlite3.connect(workspace/"db/center.sqlite3") as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]=="20261022_0021"


def test_populated_round_trip_preserves_every_row_result_and_unknown_occupancy(workspace,tmp_path):
    details=populate_pair(workspace)
    assert details.calls==2
    before={}
    for kind in ("center","node"):
        with readonly_database(workspace/f"db/{kind}.sqlite3") as connection:before[kind]=tuple(connection.iterdump())
    snapshot=tmp_path/"snapshot";target=tmp_path/"restored"
    backup(workspace,snapshot)
    status=inspect_snapshot(snapshot)
    assert status["databases"]["center"]=={"head":"20261022_0021","tasks":3,"inferenceHeld":1,"queryHeld":1}
    assert status["databases"]["node"]["unresolved"]==1
    restore(snapshot,target)
    for root in (workspace,snapshot,target):
        for kind in ("center","node"):
            with readonly_database(root/f"db/{kind}.sqlite3") as connection:
                assert tuple(connection.iterdump())==before[kind]
        assert len(list((root/"uploads/objects").iterdir()))==3
    with readonly_database(target/"db/center.sqlite3") as connection:
        assert connection.execute("SELECT status FROM tasks WHERE task_id=?",(details.tasks[0],)).fetchone()[0]=="succeeded"
        assert connection.execute("SELECT permit_state FROM inference_invocations WHERE invocation_id=?",(details.unknown,)).fetchone()[0]=="uncertain"
        assert connection.execute("SELECT state FROM monitoring_permits WHERE poll_id=?",(details.poll_id,)).fetchone()[0]=="held"
        assert connection.execute("SELECT count(*) FROM gateway_api_keys WHERE key_id=?",(details.key_id,)).fetchone()[0]==1
        assert connection.execute("SELECT count(*) FROM inference_queue_outboxes").fetchone()[0]==3


@pytest.mark.parametrize("suffix",[".linux-api.lock",".node-executor.lock",".host-node.lock"])
def test_running_control_lock_blocks_snapshot_without_creating_output(workspace,tmp_path,suffix):
    database=workspace/("db/node.sqlite3" if suffix==".host-node.lock" else "db/center.sqlite3")
    with FileLock(str(database)+suffix,timeout=0):
        with pytest.raises(Timeout):backup(workspace,tmp_path/"snapshot")
    assert not (tmp_path/"snapshot").exists()
    with FileLock(str(workspace/"db/center.sqlite3")+".linux-api.lock",timeout=0):pass


def test_uncooperative_sqlite_writer_also_blocks_snapshot(workspace,tmp_path):
    with sqlite3.connect(workspace/"db/node.sqlite3") as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError):backup(workspace,tmp_path/"snapshot")
    assert not (tmp_path/"snapshot").exists()


def test_committed_wal_is_included_without_copying_wal_or_replaying_queue(workspace,tmp_path):
    connection=sqlite3.connect(workspace/"db/center.sqlite3")
    try:
        connection.execute("PRAGMA journal_mode=WAL");connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("UPDATE inference_queue_states SET availability='unavailable',last_error_code='SYNTHETIC_REDIS_DOWN'")
        connection.commit()
        assert (workspace/"db/center.sqlite3-wal").is_file()
        backup(workspace,tmp_path/"snapshot")
        assert not list((tmp_path/"snapshot/db").glob("*-wal"))
        with readonly_database(tmp_path/"snapshot/db/center.sqlite3") as restored:
            assert restored.execute("SELECT availability,last_error_code FROM inference_queue_states").fetchone()==("unavailable","SYNTHETIC_REDIS_DOWN")
    finally:connection.close()


def test_upload_missing_or_replaced_cannot_publish_complete_backup(workspace,tmp_path):
    populate_pair(workspace)
    file=next((workspace/"uploads/objects").iterdir());file.write_bytes(b"WRONG_CONTENT")
    with pytest.raises(ValueError,match="UPLOAD"):backup(workspace,tmp_path/"snapshot")
    assert not (tmp_path/"snapshot").exists()


def test_model_files_and_temporary_files_are_excluded_and_logs_remain_private(workspace,tmp_path):
    (workspace/"models/model.bin").write_bytes(b"SYNTHETIC_MODEL_DO_NOT_COPY")
    (workspace/"tmp/upload.part").write_bytes(b"SYNTHETIC_TEMP_DO_NOT_COPY")
    (workspace/"logs/center/example.log").write_text('{"message":"SYNTHETIC_LOG"}\n',encoding="utf-8")
    backup(workspace,tmp_path/"snapshot")
    manifest=json.loads((tmp_path/"snapshot/manifest.json").read_text(encoding="utf-8"))
    assert not any(entry["path"].startswith(("models/","tmp/")) for entry in manifest["files"])
    restore(tmp_path/"snapshot",tmp_path/"restored")
    assert (tmp_path/"restored/logs/center/example.log").read_bytes()==(workspace/"logs/center/example.log").read_bytes()


@pytest.mark.parametrize("destination",["source","source/nested",".",".pending-snapshot-user",".PENDING-RESTORE-user"])
def test_output_overlap_is_rejected(workspace,tmp_path,destination):
    with pytest.raises(ValueError):backup(workspace,tmp_path/destination)


def test_snapshot_and_restore_never_overwrite_existing_directories(workspace,tmp_path):
    snapshot=tmp_path/"snapshot";backup(workspace,snapshot)
    before=(snapshot/"manifest.json").read_bytes()
    with pytest.raises(ValueError):backup(workspace,snapshot)
    assert (snapshot/"manifest.json").read_bytes()==before
    target=tmp_path/"restored";target.mkdir();(target/"keep.txt").write_text("keep",encoding="utf-8")
    with pytest.raises(ValueError):restore(snapshot,target)
    assert (target/"keep.txt").read_text()=="keep"


def test_existing_daily_root_and_source_tree_outputs_are_not_accepted(workspace):
    with pytest.raises((ValueError,FileNotFoundError)):backup(PROJECT_ROOT/"runtime",PROJECT_ROOT/"runtime/deployment-validation/refuse-daily")
    with pytest.raises(ValueError):backup(workspace,PROJECT_ROOT/"docs/private-backup")


@pytest.fixture(scope="module")
def valid_snapshot(tmp_path_factory):
    root=tmp_path_factory.mktemp("recovery-immutable")
    source=root/"source";create_validation_workspace(ValidationWorkspace(root=source))
    snapshot=root/"snapshot";backup(source,snapshot)
    return snapshot


@pytest.mark.parametrize("failure",["missing-complete","changed-manifest","changed-db","extra-file","missing-db","private-path","traversal","duplicate","fence","schema-version","forged-bad-db"])
def test_corrupt_or_unsafe_snapshot_is_rejected_before_restore_publication(valid_snapshot,tmp_path,failure):
    snapshot=tmp_path/"snapshot";shutil.copytree(valid_snapshot,snapshot)
    if failure=="missing-complete":(snapshot/"COMPLETE").unlink()
    elif failure=="changed-manifest":(snapshot/"manifest.json").write_bytes(b"{}")
    elif failure=="changed-db":(snapshot/"db/center.sqlite3").write_bytes(b"invalid")
    elif failure=="extra-file":(snapshot/"unlisted.env").write_bytes(b"SYNTHETIC_PRIVATE")
    elif failure=="missing-db":(snapshot/"db/node.sqlite3").unlink()
    else:
        value=json.loads((snapshot/"manifest.json").read_text(encoding="utf-8"))
        if failure=="private-path":value["files"][0]["path"]="config/daily.env"
        elif failure=="traversal":value["files"][0]["path"]="../outside"
        elif failure=="duplicate":value["files"].append(value["files"][0])
        elif failure=="schema-version":value["schemaVersion"]=True
        else:
            name="db/center.sqlite3.restore-quarantine.json" if failure=="fence" else "db/center.sqlite3"
            content=b'{"snapshotId":"' + b"0"*32 + b'","state":"quarantined","automaticExecution":false}' if failure=="fence" else b"not a database"
            (snapshot/name).write_bytes(content)
            for entry in value["files"]:
                if entry["path"]==name:entry.update(size=len(content),sha256=sha256(content))
        content=canonical_json(value);(snapshot/"manifest.json").write_bytes(content)
        (snapshot/"COMPLETE").write_text(sha256(content)+"\n",encoding="ascii")
    with pytest.raises((ValueError,FileNotFoundError,sqlite3.Error)):
        restore(snapshot,tmp_path/"restored")
    assert not (tmp_path/"restored").exists()


@pytest.mark.parametrize("scope",["log","sqlite-wal"])
def test_hardlinks_are_rejected_without_touching_external_file(workspace,tmp_path,scope):
    external=tmp_path/"external";external.write_bytes(b"SYNTHETIC_UNTOUCHED")
    target=workspace/("logs/center/linked.log" if scope=="log" else "db/center.sqlite3-wal")
    target.hardlink_to(external)
    with pytest.raises(ValueError):backup(workspace,tmp_path/"snapshot")
    assert external.read_bytes()==b"SYNTHETIC_UNTOUCHED" and not (tmp_path/"snapshot").exists()


def test_restored_application_factories_fail_before_log_or_control_side_effects(workspace,tmp_path):
    snapshot=tmp_path/"snapshot";backup(workspace,snapshot);target=tmp_path/"restored";restore(snapshot,target)
    center,_=deployment_settings("center",workspace/"config/center.env")
    node,_=deployment_settings("node",workspace/"config/node.env")
    log=tmp_path/"must-not-create"
    for factory,settings,kind in ((create_app,center,"center"),(create_node_app,node,"node")):
        relocated=settings.model_copy(update={"database_path":target/f"db/{kind}.sqlite3","log_dir":log})
        with pytest.raises(StartupError,match="RESTORE_QUARANTINED"):factory(relocated)
    assert not log.exists()


def test_config_pair_mismatch_is_rejected_before_snapshot(workspace,tmp_path):
    env=workspace/"config/node.env"
    lines=[line for line in env.read_text(encoding="utf-8").splitlines() if not line.startswith("NODE_MONITOR_TOKEN=")]
    env.write_text("\n".join(lines)+'\nNODE_MONITOR_TOKEN="TEST_ONLY_MISMATCH_'+"x"*40+'"\n',encoding="utf-8")
    with pytest.raises(ValueError,match="PAIR_CREDENTIAL"):backup(workspace,tmp_path/"snapshot")
    assert not (tmp_path/"snapshot").exists()


def test_nonempty_upload_staging_is_not_a_complete_offline_snapshot(workspace,tmp_path):
    staging=workspace/"uploads/staging";staging.mkdir()
    (staging/"pending.part").write_bytes(b"SYNTHETIC_UNCOMMITTED")
    with pytest.raises(ValueError,match="STAGING"):backup(workspace,tmp_path/"snapshot")


def test_files_changing_during_backup_abort_publication(workspace,tmp_path,monkeypatch):
    import backend.service.deployment.recovery_service as service
    file=workspace/"logs/center/changing.log";file.write_bytes(b"before")
    actual=service.copy_verified
    def change(source,target,expected=None):
        result=actual(source,target,expected)
        if source==file:file.write_bytes(b"after")
        return result
    monkeypatch.setattr(service,"copy_verified",change)
    with pytest.raises(ValueError,match="SOURCE_CHANGED"):backup(workspace,tmp_path/"snapshot")
    assert not (tmp_path/"snapshot").exists()


def test_publish_failure_keeps_unpublished_evidence_and_never_returns_success(workspace,tmp_path,monkeypatch):
    import backend.service.deployment.recovery_service as service
    def fail(source,target):raise OSError("SYNTHETIC_PUBLISH_FAILURE")
    monkeypatch.setattr(service,"rename_directory_no_replace",fail)
    with pytest.raises(OSError):backup(workspace,tmp_path/"snapshot")
    assert not (tmp_path/"snapshot").exists()
    pending=list(tmp_path.glob(".pending-snapshot-*"));assert len(pending)==1
    with pytest.raises(ValueError,match="NOT_PUBLISHED"):inspect_snapshot(pending[0])


def test_manifest_size_limit_also_applies_before_snapshot_publication(workspace,tmp_path,monkeypatch):
    import backend.service.deployment.recovery_service as service
    monkeypatch.setattr(service,"MAX_MANIFEST_BYTES",1)
    with pytest.raises(ValueError,match="MANIFEST_TOO_LARGE"):backup(workspace,tmp_path/"snapshot")
    assert not (tmp_path/"snapshot").exists()
