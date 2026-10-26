"""从合成0014资料演练升级；所有输出保留启动隔离，不接受日常源库。"""

from pathlib import Path
from uuid import uuid4
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from backend.config.deployment.linux_settings import CENTRAL_HEAD
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.database.database import build_database_url
from backend.data.deployment.migration_fixture import fixture_uploads, seed_legacy_fixture
from backend.data.deployment.migration_repository import LEGACY_REVISION, database_evidence, upgraded_facts
from backend.schema.deployment.migration_schema import MigrationEvidence, MigrationRehearsalRequest
from backend.schema.deployment.recovery_schema import RecoveryFence
from backend.utils.files.deployment_files import new_directory
from backend.utils.files.recovery_files import file_fact, sqlite_snapshot, sync_directory, sync_tree
from backend.utils.files.source_archive import (
    MAX_MANIFEST_BYTES, MAX_TOTAL_BYTES, canonical_json, plain_path, read_bytes, regular_files,
    rename_directory_no_replace, sha256, strict_json, write_exclusive,
)


def _upgrade(path, revision):
    settings = AppSettings(database_path=path, log_dir=path.parent / "unused-logs")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.attributes["settings"] = settings
    command.upgrade(config, revision)


def _new_output(path):
    path = plain_path(path, allow_missing=True)
    if any(part.casefold().startswith(".pending-") for part in path.parts):
        raise ValueError("MIGRATION_RESERVED_OUTPUT")
    if path.exists() or path == PROJECT_ROOT.parent or path in PROJECT_ROOT.parents:
        raise ValueError("MIGRATION_NEW_OUTPUT_REQUIRED")
    if path.is_relative_to(PROJECT_ROOT.parent) and not path.is_relative_to(PROJECT_ROOT / "runtime/deployment-validation"):
        raise ValueError("MIGRATION_OUTPUT_NOT_ISOLATED")
    if not path.parent.is_dir():
        raise ValueError("MIGRATION_OUTPUT_PARENT_REQUIRED")
    return path


def _verify(root):
    raw = read_bytes(root / "migration-evidence.json", limit=MAX_MANIFEST_BYTES)
    if read_bytes(root / "COMPLETE", limit=128) != (sha256(raw) + "\n").encode():
        raise ValueError("MIGRATION_EVIDENCE_INCOMPLETE")
    evidence = MigrationEvidence.model_validate(strict_json(raw))
    files = {item.path: item for item in evidence.files}
    if len(files) != len(evidence.files) or sum(item.size for item in evidence.files) > MAX_TOTAL_BYTES:
        raise ValueError("MIGRATION_FILE_LIST_INVALID")
    actual = {p.relative_to(root).as_posix() for p in regular_files(root)}
    if actual != set(files) | {"migration-evidence.json", "COMPLETE"}:
        raise ValueError("MIGRATION_FILE_SET_CHANGED")
    for name, entry in files.items():
        if file_fact(root / name, name) != entry.model_dump():
            raise ValueError("MIGRATION_FILE_CHANGED")
    fence = canonical_json(RecoveryFence(snapshot_id=evidence.rehearsal_id).model_dump(by_alias=True))
    for name in ("RESTORE_QUARANTINED.json", "legacy/db/center.sqlite3.restore-quarantine.json", "upgraded/db/center.sqlite3.restore-quarantine.json"):
        if read_bytes(root / name, limit=4096) != fence:
            raise ValueError("MIGRATION_FENCE_INVALID")
    old = database_evidence(root / "legacy/db/center.sqlite3")
    current = database_evidence(root / "upgraded/db/center.sqlite3")
    expected = {name: fact.model_dump(by_alias=True) for name, fact in evidence.tables.items()}
    if old["head"] != LEGACY_REVISION or current["head"] != CENTRAL_HEAD or old["tables"] != expected:
        raise ValueError("MIGRATION_BASELINE_CHANGED")
    if any(current["tables"].get(name) != fact for name, fact in expected.items()):
        raise ValueError("MIGRATION_HISTORY_CHANGED")
    for name, content in fixture_uploads().items():
        for kind in ("legacy", "upgraded"):
            if read_bytes(root / kind / "uploads" / name, limit=4096) != content:
                raise ValueError("MIGRATION_INPUT_CHANGED")
    for kind in ("legacy", "upgraded"):
        if read_bytes(root / kind / "db/center.sqlite3.asr-services.json", limit=4096) != canonical_json({"syntheticOnly": True, "configFingerprint": "b" * 64}):
            raise ValueError("MIGRATION_CONFIG_CHANGED")
    facts = upgraded_facts(root / "upgraded/db/center.sqlite3", expected)
    if (facts["tasks"], facts["onlineRequests"], facts["legacyInferenceOccupied"], facts["legacyOnlineOccupied"]) != (8, 2, 2, 1):
        raise ValueError("MIGRATION_FIXTURE_COVERAGE_CHANGED")
    return {"rehearsalId": evidence.rehearsal_id, "sourceRevision": LEGACY_REVISION, "targetRevision": CENTRAL_HEAD,
            "preservedTables": len(expected), "tasks": facts["tasks"], "uploads": 8,
            "onlineRequests": facts["onlineRequests"], "legacyInferenceOccupied": facts["legacyInferenceOccupied"],
            "legacyOnlineOccupied": facts["legacyOnlineOccupied"], "state": "quarantined",
            "syntheticOnly": True, "productionMigration": "not_performed", "newExecutionEnabled": False}


def inspect_migration_rehearsal(root):
    root = Path(root)
    if not root.is_absolute() or any(part.casefold().startswith(".pending-") for part in root.parts):
        raise ValueError("MIGRATION_PUBLISHED_DIRECTORY_REQUIRED")
    return _verify(plain_path(root))


def run_migration_rehearsal(request):
    request = MigrationRehearsalRequest.model_validate(request.model_dump())
    output = _new_output(request.output)
    identifier = uuid4().hex
    # 沿用恢复守卫认识的暂存前缀；从首个数据库产生前就隔离。
    pending = output.parent / (".pending-restore-legacy-" + identifier)
    new_directory(pending)
    fence = canonical_json(RecoveryFence(snapshot_id=identifier).model_dump(by_alias=True))
    write_exclusive(pending / "RESTORE_QUARANTINED.json", fence)
    for kind in ("legacy", "upgraded"):
        new_directory(pending / kind)
        for directory in ("db", "uploads"):
            new_directory(pending / kind / directory)
        write_exclusive(pending / kind / "db/center.sqlite3.restore-quarantine.json", fence)
        write_exclusive(pending / kind / "db/center.sqlite3.asr-services.json", canonical_json({"syntheticOnly": True, "configFingerprint": "b" * 64}))
        for name, content in fixture_uploads().items():
            write_exclusive(pending / kind / "uploads" / name, content)
    legacy = pending / "legacy/db/center.sqlite3"
    upgraded = pending / "upgraded/db/center.sqlite3"
    write_exclusive(legacy, b"")
    _upgrade(legacy, LEGACY_REVISION)
    # 不使用Database或应用启动入口，避免触发旧Worker恢复逻辑。
    engine = create_engine(build_database_url(AppSettings(database_path=legacy)))
    def foreign_keys(connection, _):
        cursor = connection.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    event.listen(engine, "connect", foreign_keys)
    try:
        seed_legacy_fixture(engine)
    finally:
        engine.dispose()
    before = database_evidence(legacy)
    sqlite_snapshot(legacy, upgraded)
    _upgrade(upgraded, CENTRAL_HEAD)
    first = database_evidence(upgraded)
    _upgrade(upgraded, CENTRAL_HEAD)
    if database_evidence(upgraded) != first or database_evidence(legacy) != before:
        raise ValueError("MIGRATION_REPEAT_OR_SOURCE_CHANGED")
    entries = [file_fact(path, path.relative_to(pending).as_posix()) for path in sorted(regular_files(pending))]
    evidence = MigrationEvidence(rehearsal_id=identifier, tables=before["tables"], files=entries)
    raw = canonical_json(evidence.model_dump(by_alias=True))
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("MIGRATION_EVIDENCE_TOO_LARGE")
    write_exclusive(pending / "migration-evidence.json", raw)
    write_exclusive(pending / "COMPLETE", (sha256(raw) + "\n").encode())
    result = _verify(pending)
    sync_tree(pending)
    rename_directory_no_replace(pending, output)
    sync_directory(output.parent)
    return result
