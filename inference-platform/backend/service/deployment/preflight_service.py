"""只读部署检查，不探测模型、不迁移、不修复环境。"""

from importlib import metadata
import os
from pathlib import Path
import platform
import shutil
import socket
import sqlite3
import sys
from backend.config.deployment.linux_settings import deployment_settings,CENTRAL_HEAD,NODE_HEAD,GUNICORN_VERSION
from backend.utils.files.source_archive import plain_path


def inspect_deployment(component,env_file,*,require_linux=False,check_port=True):
    settings,values=deployment_settings(component,env_file)
    root=plain_path(Path(values["DEPLOY_STATE_ROOT"]))
    issues=[];facts={"os":platform.system(),"python":platform.python_version(),"component":component,
        "database":str(settings.database_path),"queue":getattr(settings,"common_queue_backend",None),"readOnly":True}
    if require_linux and platform.system()!="Linux":issues.append("LINUX_REQUIRED")
    if sys.version_info[:2]!=(3,11):issues.append("PYTHON_VERSION_UNVERIFIED")
    packages={}
    for name in ("Flask","SQLAlchemy","alembic","filelock","gunicorn","redis"):
        try:packages[name]=metadata.version(name)
        except metadata.PackageNotFoundError:packages[name]=None
    facts["packages"]=packages
    if require_linux and component!="executor" and packages["gunicorn"]!=GUNICORN_VERSION:issues.append("GUNICORN_PIN_REQUIRED")
    if getattr(settings,"common_queue_backend",None)=="redis" and packages["redis"] is None:issues.append("REDIS_CLIENT_MISSING")
    if not (root/"READY").is_file():issues.append("WORKSPACE_NOT_READY")
    # query_only + mode=ro 不会创建丢失的库；不执行迁移或业务SQL。
    try:
        plain_path(settings.database_path)
        with sqlite3.connect(settings.database_path.as_uri()+"?mode=ro",uri=True,timeout=1) as connection:
            connection.execute("PRAGMA query_only=ON")
            table="host_node_alembic_versions" if component=="node" else "alembic_version"
            heads=[row[0] for row in connection.execute("SELECT version_num FROM "+table)]
        expected=NODE_HEAD if component=="node" else CENTRAL_HEAD
        facts["databaseHeads"]=heads
        if heads!=[expected]:issues.append("DATABASE_REVISION_MISMATCH")
    except (OSError,ValueError,sqlite3.Error):issues.append("DATABASE_READ_FAILED")
    for directory in (root,root/"config",root/"db",settings.log_dir):
        try:
            info=plain_path(directory).stat()
            if os.name=="posix" and (info.st_uid!=os.getuid() or info.st_mode & 0o077):issues.append("PRIVATE_DIRECTORY_PERMISSIONS_REQUIRED")
        except (OSError,ValueError):issues.append("DIRECTORY_UNAVAILABLE")
    facts["diskFreeBytes"]=shutil.disk_usage(root).free
    if facts["diskFreeBytes"]<1024**3:issues.append("DISK_BELOW_1GIB")
    if check_port and component!="executor":
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as probe:
            try:probe.bind((settings.host,settings.port));facts["portAvailable"]=True
            except OSError:facts["portAvailable"]=False;issues.append("PORT_UNAVAILABLE")
    facts["tools"]={name:shutil.which(name) is not None for name in ("systemctl","nginx","redis-server","docker")}
    return {"ok":not issues,"issues":sorted(set(issues)),"facts":facts,
            "notVerified":["systemd-user-session-and-boot","gunicorn-serving-on-linux","redis-connectivity-lua-restart","models-and-containers","network-and-tls"]}
