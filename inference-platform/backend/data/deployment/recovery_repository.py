"""只读检查快照数据库，不创建会话、迁移、执行恢复或改动状态。"""

from backend.config.deployment.linux_settings import CENTRAL_HEAD, NODE_HEAD
from backend.utils.files.recovery_files import readonly_database
from backend.utils.files.source_archive import strict_json
from backend.utils.json.json_utils import canonical_json_sha256


def inspect_snapshot_databases(center, node):
    facts={};bindings={}
    for kind,path,table,expected in (("center",center,"alembic_version",CENTRAL_HEAD),
                                     ("node",node,"host_node_alembic_versions",NODE_HEAD)):
        with readonly_database(path) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall()!=[("ok",)]:
                raise ValueError("RECOVERY_DATABASE_INTEGRITY")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("RECOVERY_DATABASE_FOREIGN_KEYS")
            if connection.execute("SELECT version_num FROM "+table).fetchall()!=[(expected,)]:
                raise ValueError("RECOVERY_DATABASE_REVISION")
            facts[kind]={"head":expected}
            if kind=="center":
                facts[kind].update(tasks=connection.execute("SELECT count(*) FROM tasks").fetchone()[0],
                    inferenceHeld=connection.execute("SELECT count(*) FROM inference_invocations WHERE permit_state IN ('held','uncertain')").fetchone()[0],
                    queryHeld=connection.execute("SELECT count(*) FROM monitoring_permits WHERE state='held'").fetchone()[0])
                facts["dispatchNodes"]=[row[0] for row in connection.execute(
                    "SELECT DISTINCT json_extract(command_json,'$.nodeId') FROM inference_node_dispatches LIMIT 2")]
                for identifier,digest,command in connection.execute("SELECT invocation_id,request_hash,command_json FROM inference_node_dispatches"):
                    if canonical_json_sha256(strict_json(command.encode("utf-8")))!=digest:
                        raise ValueError("RECOVERY_DISPATCH_IDENTITY_INVALID")
                    bindings[identifier]=digest
            else:
                facts[kind].update(executions=connection.execute("SELECT count(*) FROM host_node_executions").fetchone()[0],
                    unresolved=connection.execute("SELECT count(*) FROM host_node_executions WHERE state IN ('sending','unknown')").fetchone()[0])
                facts["nodeIdentities"]=[row[0] for row in connection.execute("SELECT node_id FROM host_node_identities")]
                for identifier,digest,command in connection.execute("SELECT invocation_id,request_hash,command_json FROM host_node_executions"):
                    if bindings.get(identifier)!=digest or canonical_json_sha256(strict_json(command.encode("utf-8")))!=digest:
                        raise ValueError("RECOVERY_NODE_CALL_MISMATCH")
    return facts


def task_inputs(center):
    with readonly_database(center) as connection:
        yield from connection.execute("SELECT input_ref,input_media_type,input_sha256,input_size_bytes FROM tasks")
