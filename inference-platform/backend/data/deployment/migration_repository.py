"""只读迁移前后证据；不进行任务恢复、通知重投或许可释放。"""

import re
from backend.utils.files.recovery_files import readonly_database
from backend.utils.files.source_archive import canonical_json, sha256


LEGACY_REVISION = "20261020_0014"
NEW_TABLES = {
    "inference_services", "inference_service_revisions", "inference_resource_groups",
    "inference_target_checks", "inference_catalog_events", "inference_application_budgets",
    "inference_invocations", "inference_reservation_items", "inference_task_events",
    "inference_scheduler_cursors", "inference_cpu_profiles", "inference_queue_states",
    "inference_queue_outboxes", "inference_node_dispatches", "gateway_api_keys",
    "gateway_node_observations", "monitoring_permits",
}


def database_evidence(path):
    with readonly_database(path) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("MIGRATION_INTEGRITY_FAILED")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("MIGRATION_FOREIGN_KEYS_FAILED")
        head = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        if len(head) != 1:
            raise ValueError("MIGRATION_REVISION_INVALID")
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        if len(names) > 100:
            raise ValueError("MIGRATION_TOO_MANY_TABLES")
        tables = {}
        for name in names:
            if name == "alembic_version":
                continue
            if not re.fullmatch(r"[a-z_]+", name):
                raise ValueError("MIGRATION_TABLE_INVALID")
            rows = connection.execute('SELECT * FROM "' + name + '" LIMIT 10001').fetchall()
            if len(rows) > 10000:
                raise ValueError("MIGRATION_TOO_MANY_ROWS")
            # 有界库只摘要，不输出正文。包括类型，避免BLOB与普通字符串混淆。
            encoded = sorted(canonical_json([[type(v).__name__, v.hex() if isinstance(v, bytes) else v] for v in row]) for row in rows)
            schema = connection.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? ORDER BY type,name", (name,)).fetchall()
            tables[name] = {"rows": len(rows), "columns": [row[1] for row in connection.execute('PRAGMA table_info("' + name + '")')],
                            "rowsSha256": sha256(b"".join(encoded)), "schemaSha256": sha256(canonical_json(schema))}
        return {"head": head[0][0], "tables": tables}


def upgraded_facts(path, old_tables):
    """旧任务不自动归属新应用，也不生成新执行/通知/详细历史事件。"""
    with readonly_database(path) as connection:
        all_tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        new_tables = all_tables - set(old_tables) - {"alembic_version"}
        if new_tables != NEW_TABLES:
            raise ValueError("MIGRATION_TABLE_SET_INVALID")
        for name in sorted(new_tables - {"inference_queue_states"}):
            if not re.fullmatch(r"[a-z_]+", name) or connection.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0]:
                raise ValueError("MIGRATION_UNEXPECTED_NEW_RECORDS")
        queues = connection.execute("SELECT state_id,broker_id,availability FROM inference_queue_states").fetchall()
        if len(queues) != 1 or queues[0][0] != "redis" or not re.fullmatch(r"[a-f0-9]{32}", queues[0][1]) or queues[0][2] != "unknown":
            raise ValueError("MIGRATION_QUEUE_SEED_INVALID")
        return {"tasks": connection.execute("SELECT count(*) FROM tasks").fetchone()[0],
                "onlineRequests": connection.execute("SELECT count(*) FROM online_requests").fetchone()[0],
                "legacyInferenceOccupied": connection.execute("SELECT count(*) FROM asr_call_permits WHERE state IN ('held','uncertain')").fetchone()[0],
                "legacyOnlineOccupied": connection.execute("SELECT count(*) FROM online_requests WHERE permit_state IN ('held','uncertain')").fetchone()[0],
                "newTables": len(new_tables), "brokerId": queues[0][1]}
