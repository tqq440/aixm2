"""合成旧库升级、损坏/中断和不执行守卫；不触碰日常库或外部服务。"""

import json
import shutil
import sqlite3
from pathlib import Path
from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from backend.common.exceptions.app_error import StartupError
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.database.database import Database, build_database_url
from backend.data.deployment.migration_fixture import seed_legacy_fixture
from backend.data.deployment.migration_repository import database_evidence, LEGACY_REVISION, NEW_TABLES
from backend.data.invocations.invocation_repository import InvocationRepository
from backend.modules.api.app import create_app
from backend.schema.deployment.migration_schema import MigrationRehearsalRequest
from backend.service.deployment import migration_service
from backend.utils.files.source_archive import canonical_json, sha256


@pytest.fixture(scope="module")
def rehearsal(tmp_path_factory):
    output = tmp_path_factory.mktemp("legacy-fixture") / "rehearsal"
    summary = migration_service.run_migration_rehearsal(MigrationRehearsalRequest(output=output))
    assert summary["tasks"] == 8 and summary["legacyInferenceOccupied"] == 2
    return output


@pytest.fixture
def copied(rehearsal, tmp_path):
    destination = tmp_path / "copy"
    shutil.copytree(rehearsal, destination)
    return destination


def reseal(root):
    """重算外层摘要，让测试到达更深的历史/语义核对。"""
    path = root / "migration-evidence.json"
    evidence = json.loads(path.read_text(encoding="utf-8"))
    for entry in evidence["files"]:
        file = root / entry["path"]
        entry.update(size=file.stat().st_size, sha256=sha256(file.read_bytes()))
    raw = canonical_json(evidence)
    path.write_bytes(raw)
    (root / "COMPLETE").write_bytes((sha256(raw) + "\n").encode("ascii"))


def test_history_and_unknown_occupancy_survive_and_new_chain_is_empty(rehearsal):
    source = rehearsal / "legacy/db/center.sqlite3"
    target = rehearsal / "upgraded/db/center.sqlite3"
    old, new = database_evidence(source), database_evidence(target)
    assert old["head"] == LEGACY_REVISION and new["head"] == "20261022_0021"
    assert len(old["tables"]) == 26
    assert {name: new["tables"][name] for name in old["tables"]} == old["tables"]
    assert set(new["tables"]) - set(old["tables"]) == NEW_TABLES
    engine = create_engine(build_database_url(AppSettings(database_path=target)))
    try:
        with Session(engine) as session:
            session.connection().exec_driver_sql("PRAGMA query_only=ON")
            assert InvocationRepository.legacy_occupied(session)
    finally:
        engine.dispose()
    with sqlite3.connect(target.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT state,count(*) FROM asr_call_permits GROUP BY state ORDER BY state").fetchall() == [("held", 1), ("released", 1), ("uncertain", 1)]
        assert connection.execute("SELECT count(*) FROM tasks WHERE result_json IS NOT NULL AND result_json!='null'").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM inference_invocations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM inference_queue_outboxes").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM inference_task_events").fetchone()[0] == 0
    assert migration_service.inspect_migration_rehearsal(rehearsal)["productionMigration"] == "not_performed"
    assert not list(rehearsal.rglob("*.env")) and not (rehearsal / "READY").exists()


def test_upgrade_recheck_is_idempotent_and_metadata_matches(copied):
    path = copied / "upgraded/db/center.sqlite3"
    before = database_evidence(path)
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.attributes["settings"] = AppSettings(database_path=path)
    command.upgrade(config, "head")
    command.check(config)
    assert database_evidence(path) == before


def test_fixture_generator_refuses_existing_history_or_new_version(copied):
    for kind in ("legacy", "upgraded"):
        path = copied / kind / "db/center.sqlite3"
        before = database_evidence(path)
        engine = create_engine(build_database_url(AppSettings(database_path=path)))
        try:
            with pytest.raises(ValueError, match="REQUIRES_EMPTY_0014"):
                seed_legacy_fixture(engine)
        finally:
            engine.dispose()
        assert database_evidence(path) == before


@pytest.mark.parametrize("kind", ["legacy", "upgraded"])
@pytest.mark.parametrize("factory", [Database, create_app])
def test_both_databases_refuse_runtime_startup(copied, kind, factory):
    settings = AppSettings(database_path=copied / kind / "db/center.sqlite3", log_dir=copied / "must-not-exist")
    with pytest.raises(StartupError, match="RESTORE_QUARANTINED"):
        factory(settings)
    assert not settings.log_dir.exists()


@pytest.mark.parametrize("kind", ["legacy", "upgraded"])
@pytest.mark.parametrize("change", ["result", "fingerprint", "permit", "lease", "route", "queue"])
def test_resigned_history_mutations_are_rejected(copied, kind, change):
    statements = {
        "result": "UPDATE tasks SET result_json='{}' WHERE status='succeeded'",
        "fingerprint": "UPDATE remote_asr_calls SET config_fingerprint='" + "f" * 64 + "'",
        "permit": "UPDATE asr_call_permits SET state='released'",
        "lease": "UPDATE tasks SET lease_expires_at=NULL",
        "route": "UPDATE asr_task_routes SET decision_snapshot='{}'",
        "queue": "DELETE FROM notification_records",
    }
    with sqlite3.connect(copied / kind / "db/center.sqlite3") as connection:
        connection.execute(statements[change])
    reseal(copied)
    with pytest.raises(ValueError, match="MIGRATION_(BASELINE|HISTORY)_CHANGED"):
        migration_service.inspect_migration_rehearsal(copied)


@pytest.mark.parametrize("relative", ["upgraded/uploads/00000000000000000000000000000064.audio", "legacy/db/center.sqlite3.asr-services.json", "RESTORE_QUARANTINED.json"])
def test_corrupt_media_config_or_guard_is_rejected(copied, relative):
    path = copied / relative
    path.write_bytes(path.read_bytes() + b"BROKEN")
    with pytest.raises(ValueError, match="MIGRATION_FILE_CHANGED"):
        migration_service.inspect_migration_rehearsal(copied)


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "traversal", "complete", "new-record"])
def test_manifest_and_new_execution_changes_are_rejected(copied, change):
    path = copied / "migration-evidence.json"
    evidence = json.loads(path.read_text(encoding="utf-8"))
    if change == "missing":
        (copied / evidence["files"][0]["path"]).unlink()
    elif change == "extra":
        (copied / "center.env").write_text("MUST_NOT_START", encoding="ascii")
    elif change in ("duplicate", "traversal"):
        evidence["files"][0]["path"] = evidence["files"][1]["path"] if change == "duplicate" else "../outside"
        raw = canonical_json(evidence)
        path.write_bytes(raw)
        (copied / "COMPLETE").write_bytes((sha256(raw) + "\n").encode("ascii"))
    elif change == "complete":
        (copied / "COMPLETE").write_text("0" * 64 + "\n", encoding="ascii")
    else:
        with sqlite3.connect(copied / "upgraded/db/center.sqlite3") as connection:
            connection.execute("INSERT INTO inference_scheduler_cursors(cursor_id) VALUES('must-not-create')")
        reseal(copied)
    with pytest.raises((ValueError, OSError)):
        migration_service.inspect_migration_rehearsal(copied)


def test_existing_output_and_source_tree_are_never_overwritten(rehearsal, tmp_path):
    before = (rehearsal / "migration-evidence.json").read_bytes()
    for path in (rehearsal, PROJECT_ROOT / "backend/new-rehearsal", tmp_path / ".pending-restore-rejected"):
        with pytest.raises(ValueError):
            migration_service.run_migration_rehearsal(MigrationRehearsalRequest(output=path))
    assert (rehearsal / "migration-evidence.json").read_bytes() == before


def test_interrupted_migration_keeps_source_and_fences_partial_output(tmp_path, monkeypatch):
    original = migration_service._upgrade
    source_facts = []
    def fail_target(path, revision):
        if path.parent.parent.name == "upgraded":
            original(path, "20261021_0016")
            source_facts.append(database_evidence(path.parents[2] / "legacy/db/center.sqlite3"))
            raise RuntimeError("SYNTHETIC_INTERRUPTION")
        return original(path, revision)
    monkeypatch.setattr(migration_service, "_upgrade", fail_target)
    output = tmp_path / "interrupted"
    with pytest.raises(RuntimeError, match="SYNTHETIC_INTERRUPTION"):
        migration_service.run_migration_rehearsal(MigrationRehearsalRequest(output=output))
    assert not output.exists()
    pending, = tmp_path.glob(".pending-restore-legacy-*")
    assert database_evidence(pending / "legacy/db/center.sqlite3") == source_facts[0]
    assert database_evidence(pending / "upgraded/db/center.sqlite3")["head"] == "20261021_0016"
    assert not (pending / "COMPLETE").exists()
    with pytest.raises(ValueError, match="PUBLISHED_DIRECTORY"):
        migration_service.inspect_migration_rehearsal(pending)
    with pytest.raises(StartupError, match="RESTORE_QUARANTINED"):
        Database(AppSettings(database_path=pending / "upgraded/db/center.sqlite3"))
