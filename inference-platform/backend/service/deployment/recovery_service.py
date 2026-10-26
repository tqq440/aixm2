"""成套离线快照与隔离恢复；不提供生产切换或解除隔离操作。"""

from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from filelock import FileLock
from backend.common.storage.recovery_guard import require_active_database
from backend.config.deployment.linux_settings import deployment_settings
from backend.config.settings.app_settings import PROJECT_ROOT
from backend.data.deployment.recovery_repository import inspect_snapshot_databases, task_inputs
from backend.data.files.upload_store import LocalUploadStore
from backend.schema.deployment.recovery_schema import RecoveryFence, RestoreRequest, SnapshotFile, SnapshotManifest, SnapshotRequest
from backend.utils.files.deployment_files import new_directory
from backend.utils.files.recovery_files import copy_verified, file_fact, private_parents, sqlite_snapshot, sqlite_write_barrier, sync_directory, sync_tree
from backend.utils.files.source_archive import (
    MAX_MANIFEST_BYTES, MAX_TOTAL_BYTES, canonical_json, plain_path, read_bytes, regular_files,
    rename_directory_no_replace, sha256, strict_json, write_exclusive,
)


def new_recovery_output(path, source):
    path=plain_path(Path(path),allow_missing=True)
    source=plain_path(Path(source))
    if any(part.casefold().startswith(".pending-") for part in path.parts):raise ValueError("RECOVERY_RESERVED_OUTPUT")
    if path.exists() or path==source or path.is_relative_to(source) or source.is_relative_to(path):
        raise ValueError("RECOVERY_OUTPUT_OVERLAP")
    # 本Git工作区内只允许忽略的隔离运行目录；不把私密快照写进源码树。
    if path.is_relative_to(PROJECT_ROOT.parent) and not path.is_relative_to(PROJECT_ROOT/"runtime/deployment-validation"):
        raise ValueError("RECOVERY_OUTPUT_NOT_ISOLATED")
    if not path.parent.is_dir():raise ValueError("RECOVERY_OUTPUT_PARENT_REQUIRED")
    return path


def _inventory(root, center):
    files={}
    for directory in ("uploads","logs/center","logs/node"):
        path=plain_path(root/directory)
        if not path.is_dir():raise ValueError("RECOVERY_DIRECTORY_REQUIRED")
        for file in regular_files(path):
            if file.name.endswith(".lock"):
                continue
            files[file.relative_to(root).as_posix()]=file
    config=plain_path(center.database_path.with_name(center.database_path.name+".asr-services.json"),allow_missing=True)
    if config.exists():files["db/center.sqlite3.asr-services.json"]=plain_path(config)
    if any(p.relative_to(root/"uploads").parts[0]=="staging" for p in files.values() if p.is_relative_to(root/"uploads")):
        raise ValueError("RECOVERY_UPLOAD_STAGING_NOT_EMPTY")
    facts=[file_fact(path,name) for name,path in sorted(files.items())]
    if sum(item["size"] for item in facts)>MAX_TOTAL_BYTES:raise ValueError("RECOVERY_TOO_LARGE")
    return files,facts


def _validate_pair(root, manifest, facts):
    if set(facts["dispatchNodes"])-{manifest.node_id} or set(facts["nodeIdentities"])-{manifest.node_id}:
        raise ValueError("RECOVERY_NODE_COVERAGE_INCOMPLETE")
    entries={file.path:file for file in manifest.files}
    for reference,media,digest,size in task_inputs(root/"db/center.sqlite3"):
        store=LocalUploadStore(root/"uploads",media_type=media)
        name=store.resolve_path(reference).relative_to(root).as_posix()
        entry=entries.get(name)
        if entry is None or (entry.sha256,entry.size)!=(digest,size):
            raise ValueError("RECOVERY_UPLOAD_MISSING_OR_CHANGED")
    for kind in ("center","node"):
        fence=RecoveryFence.model_validate(strict_json(read_bytes(root/f"db/{kind}.sqlite3.restore-quarantine.json",1024)))
        if fence.snapshot_id!=manifest.snapshot_id:raise ValueError("RECOVERY_FENCE_MISMATCH")


def create_snapshot(value):
    request=SnapshotRequest.model_validate(value)
    root=plain_path(request.root)
    output=new_recovery_output(request.output,root)
    # 不读取日常.env，也不接受未标为6A独立验证用途的目录。
    identity=strict_json(read_bytes(root/"deployment-validation.json",4096))
    if not isinstance(identity,dict) or identity.get("purpose")!="isolated-verification" or identity.get("schemaVersion")!=1:
        raise ValueError("RECOVERY_VALIDATION_WORKSPACE_REQUIRED")
    read_bytes(root/"READY",4096)
    raw={kind:read_bytes(root/f"config/{kind}.env",65536) for kind in ("center","node")}
    center,center_values=deployment_settings("center",root/"config/center.env")
    node,node_values=deployment_settings("node",root/"config/node.env")
    if Path(center_values["DEPLOY_STATE_ROOT"])!=root or Path(node_values["DEPLOY_STATE_ROOT"])!=root:
        raise ValueError("RECOVERY_PAIR_ROOT_MISMATCH")
    for center_key,node_key in (("INFERENCE_INGRESS_TOKEN","NODE_INGRESS_TOKEN"),
                                ("DEPLOY_NODE_EXECUTOR_TOKEN","NODE_EXECUTOR_TOKEN"),
                                ("INFERENCE_MONITOR_TOKEN","NODE_MONITOR_TOKEN")):
        if center_values.get(center_key)!=node_values.get(node_key):raise ValueError("RECOVERY_PAIR_CREDENTIAL_MISMATCH")
    expected_origin=f"http://127.0.0.1:{center.port}"
    if node.central_url!=expected_origin or (node.monitor_central_url and node.monitor_central_url!=expected_origin):
        raise ValueError("RECOVERY_PAIR_CENTER_MISMATCH")
    for settings in (center,node):require_active_database(settings.database_path)
    if center.upload_dir!=root/"uploads" or center.log_dir!=root/"logs/center" or node.log_dir!=root/"logs/node":
        raise ValueError("RECOVERY_PAIR_LAYOUT_REQUIRED")
    # 这些锁须由原控制进程正常释放；拿不到就退出，不停止进程、不删锁。
    locks=[root/".recovery-snapshot.lock",Path(str(center.database_path)+".linux-api.lock"),
           Path(str(center.database_path)+".node-executor.lock"),Path(str(node.database_path)+".host-node.lock"),
           root/"uploads/.maintenance.lock",Path(str(center.database_path)+".asr-services.json.lock")]
    locks.extend(Path(str(center.database_path)+suffix) for suffix in (
        ".worker.lock",".vision-worker.lock",".dispatcher.lock",".agent.lock",".remote-asr-worker.lock"))
    with ExitStack() as stack:
        for lock in locks:
            plain_path(lock,allow_missing=True)
            stack.enter_context(FileLock(str(lock),timeout=0))
        for settings in (center,node):stack.enter_context(sqlite_write_barrier(settings.database_path))
        before,source_facts=_inventory(root,center)
        expected_files={fact["path"]:SnapshotFile.model_validate(fact) for fact in source_facts}
        if any(read_bytes(root/f"config/{kind}.env",65536)!=raw[kind] for kind in raw):
            raise ValueError("RECOVERY_CONFIG_CHANGED")
        staging=new_directory(output.parent/(".pending-snapshot-"+uuid4().hex))
        for directory in ("db","config"):new_directory(staging/directory)
        entries=[];identifier=uuid4().hex
        fence=canonical_json(RecoveryFence(snapshot_id=identifier).model_dump(mode="json",by_alias=True))
        for kind,settings,config in (("center",center,center_values),("node",node,node_values)):
            database=staging/f"db/{kind}.sqlite3"
            sqlite_snapshot(settings.database_path,database)
            write_exclusive(Path(str(database)+".restore-quarantine.json"),fence)
            write_exclusive(staging/f"config/{kind}.private.json",canonical_json(config))
        for name,source in before.items():
            expected=expected_files[name]
            target=staging/name;private_parents(target,staging);copy_verified(source,target,expected)
        # 比较所有文件和目录清单，捕获离线确认不成立或非合作写入。
        _,after_facts=_inventory(root,center)
        if source_facts!=after_facts or any(read_bytes(root/f"config/{kind}.env",65536)!=raw[kind] for kind in raw):
            raise ValueError("RECOVERY_SOURCE_CHANGED")
        for file in regular_files(staging):entries.append(file_fact(file,file.relative_to(staging).as_posix()))
        manifest=SnapshotManifest(snapshot_id=identifier,created_at=datetime.now(timezone.utc),source_name=identity["name"],node_id=node.node_id,files=entries)
        facts=inspect_snapshot_databases(staging/"db/center.sqlite3",staging/"db/node.sqlite3")
        _validate_pair(staging,manifest,facts)
        document=canonical_json(manifest.model_dump(mode="json",by_alias=True))
        if len(document)>MAX_MANIFEST_BYTES:raise ValueError("RECOVERY_MANIFEST_TOO_LARGE")
        write_exclusive(staging/"manifest.json",document)
        write_exclusive(staging/"COMPLETE",(sha256(document)+"\n").encode("ascii"))
        sync_tree(staging);rename_directory_no_replace(staging,output);sync_directory(output.parent)
    return {"snapshotId":identifier,"files":len(entries),"quarantined":True,"modelsStarted":False,"redisAccessed":False}


def verify_snapshot(path):
    root=plain_path(Path(path))
    if root.name.casefold().startswith(".pending-"):raise ValueError("RECOVERY_NOT_PUBLISHED")
    document=read_bytes(root/"manifest.json",MAX_MANIFEST_BYTES)
    if read_bytes(root/"COMPLETE",128)!=(sha256(document)+"\n").encode("ascii"):
        raise ValueError("RECOVERY_MANIFEST_DIGEST")
    manifest=SnapshotManifest.model_validate(strict_json(document))
    expected={entry.path:entry for entry in manifest.files}
    actual={file.relative_to(root).as_posix() for file in regular_files(root)}
    if actual!=set(expected)|{"manifest.json","COMPLETE"}:raise ValueError("RECOVERY_FILE_SET_MISMATCH")
    for name,entry in expected.items():
        fact=file_fact(root/name,name)
        if (fact["size"],fact["sha256"])!=(entry.size,entry.sha256):raise ValueError("RECOVERY_FILE_DIGEST")
    facts=inspect_snapshot_databases(root/"db/center.sqlite3",root/"db/node.sqlite3")
    _validate_pair(root,manifest,facts)
    return manifest,facts


def inspect_snapshot(path):
    manifest,facts=verify_snapshot(path)
    return {"snapshotId":manifest.snapshot_id,"files":len(manifest.files),"quarantined":True,
            "scope":"one-center-one-node-isolated","databases":{"center":facts["center"],"node":facts["node"]},
            "redisBackupIncluded":False,"runtimeActivationAvailable":False}


def restore_snapshot(value):
    request=RestoreRequest.model_validate(value)
    output=new_recovery_output(request.output,request.snapshot)
    manifest,_=verify_snapshot(request.snapshot)
    staging=new_directory(output.parent/(".pending-restore-"+uuid4().hex))
    # 私密启动资料只保留JSON证据，不生成可启动env、不沿用旧主机路径。
    for entry in manifest.files:
        name="recovery/"+entry.path if entry.path.startswith("config/") else entry.path
        target=staging/name;private_parents(target,staging)
        copy_verified(request.snapshot/entry.path,target,entry)
    for directory in ("uploads","logs","logs/center","logs/node"):
        path=staging/directory
        if not path.exists():new_directory(path)
    facts=inspect_snapshot_databases(staging/"db/center.sqlite3",staging/"db/node.sqlite3")
    _validate_pair(staging,manifest,facts)
    # 保存原清单作证据；不会生成READY或重新投递任何通知。
    write_exclusive(staging/"recovery/manifest.json",canonical_json(manifest.model_dump(mode="json",by_alias=True)))
    write_exclusive(staging/"RESTORE_QUARANTINED.json",canonical_json(RecoveryFence(snapshot_id=manifest.snapshot_id).model_dump(mode="json",by_alias=True)))
    sync_tree(staging);rename_directory_no_replace(staging,output);sync_directory(output.parent)
    return {"snapshotId":manifest.snapshot_id,"files":len(manifest.files),"quarantined":True,
            "originalStatesPreserved":True,"modelsStarted":False,"queueReplayed":False}
