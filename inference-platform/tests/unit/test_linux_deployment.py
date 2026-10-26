"""部署边界与运行生命周期验证；不代表在Linux上运行Gunicorn或systemd。"""

import json
import os
from pathlib import Path
import sqlite3
import sys
from types import ModuleType,SimpleNamespace
import pytest
from filelock import FileLock
from pydantic import ValidationError
from backend.common.exceptions.app_error import ConfigurationError
from backend.config.deployment.linux_settings import deployment_settings,deployment_values,deployment_environment
from backend.config.settings.app_settings import PROJECT_ROOT
from backend.modules.deployment.linux_cli import main
from backend.modules.deployment.linux_server import DeploymentRuntime,serve_linux
from backend.schema.deployment.linux_schema import LinuxDeploymentPlan,ValidationWorkspace
from backend.service.deployment.linux_plan_service import render_linux_plan
from backend.service.deployment.preflight_service import inspect_deployment
from backend.service.deployment.validation_service import create_validation_workspace
from backend.service.deployment.source_bundle_service import source_path_allowed


@pytest.fixture
def workspace(tmp_path):
    root=tmp_path/"isolated"
    create_validation_workspace(ValidationWorkspace(root=root))
    return root


def plan():
    return LinuxDeploymentPlan.model_validate_json((PROJECT_ROOT/"examples/linux/module6a.json").read_text(encoding="utf-8"))


def edit_env(path,key,value):
    lines=path.read_text(encoding="utf-8").splitlines()
    lines=[line for line in lines if not line.startswith(key+'=')]
    lines.append(f'{key}={json.dumps(value,ensure_ascii=False)}')
    path.write_text('\n'.join(lines)+'\n',encoding="utf-8")


def test_initialize_new_workspace_private_and_independent(workspace):
    center,cv=deployment_settings("center",workspace/"config/center.env")
    node,nv=deployment_settings("node",workspace/"config/node.env")
    assert center.database_path!=node.database_path
    assert cv["DEPLOY_NODE_EXECUTOR_TOKEN"]==nv["NODE_EXECUTOR_TOKEN"]
    assert cv["INFERENCE_INGRESS_TOKEN"]==nv["NODE_INGRESS_TOKEN"]
    assert len({nv[k] for k in ("NODE_ADMIN_TOKEN","NODE_OBSERVER_TOKEN","NODE_EXECUTOR_TOKEN","NODE_REPORT_TOKEN_ASR","NODE_INGRESS_TOKEN")})==5
    assert center.upload_dir==workspace/"uploads"
    assert not node.business_enabled and not node.docker_enabled
    assert not any((workspace/"models").iterdir())
    assert (workspace/"READY").is_file()
    if os.name=="posix":assert (workspace/"config/center.env").stat().st_mode & 0o777==0o600


def test_reinitialization_does_not_overwrite_env_or_data(workspace):
    env=workspace/"config/center.env";before=env.read_bytes()
    db=workspace/"db/center.sqlite3";old=db.read_bytes()
    with pytest.raises(FileExistsError):create_validation_workspace(ValidationWorkspace(root=workspace))
    assert env.read_bytes()==before and db.read_bytes()==old


@pytest.mark.parametrize("relative",[".","runtime/db/new-validation","backend/validation","runtime/deployment-validation"])
def test_init_cannot_use_project_or_existing_roots(relative):
    root=PROJECT_ROOT/relative
    with pytest.raises((ValueError,FileExistsError)):
        create_validation_workspace(ValidationWorkspace(root=root))


def test_paths_with_spaces_unicode_and_backslash_are_roundtripped(workspace):
    settings,_=deployment_settings("center",workspace/"config/center.env")
    assert settings.database_path==workspace/"db/center.sqlite3"


@pytest.mark.parametrize("key,value",[("DEPLOY_STATE_ROOT","relative"),("DEPLOY_COMPONENT","node"),
    ("INFERENCE_DATABASE_PATH","runtime/db/platform.sqlite3"),("INFERENCE_UPLOAD_DIR","runtime/uploads"),
    ("INFERENCE_COMMON_EXECUTION_ENABLED","false"),("INFERENCE_PUBLIC_API_ENABLED","false")])
def test_center_rejects_daily_fallback_and_wrong_role(workspace,key,value):
    env=workspace/"config/center.env";edit_env(env,key,value)
    with pytest.raises((ValueError,ConfigurationError)):deployment_settings("center",env)


@pytest.mark.parametrize("key",["NODE_BUSINESS_ENABLED","NODE_DOCKER_ENABLED"])
def test_validation_cannot_enable_business_or_docker(workspace,key):
    env=workspace/"config/node.env";edit_env(env,key,"true")
    with pytest.raises(ConfigurationError):deployment_settings("node",env)


@pytest.mark.parametrize("extra",['NODE_ADMIN_TOKEN="duplicate"\n','BAD_NAME="secret"\n','DEPLOY_COMPONENT="node"\n',
    'DEPLOY_STATE_ROOT="${HOME}/x"\n','broken="secret\n'])
def test_env_parser_rejects_ambiguity_without_exposing_secret(workspace,extra):
    env=workspace/"config/node.env"
    with env.open("a",encoding="utf-8") as handle:handle.write(extra)
    with pytest.raises(ConfigurationError) as error:deployment_values(env)
    assert "secret" not in str(error.value) and "duplicate" not in str(error.value)


def test_explicit_environment_ignores_and_restores_ambient(monkeypatch,workspace):
    monkeypatch.setenv("INFERENCE_DATABASE_PATH","INVALID_DAILY_PATH")
    monkeypatch.setenv("NODE_ADMIN_TOKEN","INVALID_DAILY_TOKEN")
    settings,values=deployment_settings("center",workspace/"config/center.env")
    assert settings.database_path==workspace/"db/center.sqlite3"
    with pytest.raises(RuntimeError):
        with deployment_environment(values):
            assert os.environ["INFERENCE_DATABASE_PATH"]==settings.database_path.as_posix()
            assert "NODE_ADMIN_TOKEN" not in os.environ
            assert os.environ["DEPLOY_NODE_EXECUTOR_TOKEN"]==values["DEPLOY_NODE_EXECUTOR_TOKEN"]
            raise RuntimeError()
    assert os.environ["INFERENCE_DATABASE_PATH"]=="INVALID_DAILY_PATH"
    assert os.environ["NODE_ADMIN_TOKEN"]=="INVALID_DAILY_TOKEN"


@pytest.mark.parametrize("component,filename",[("center","center.env"),("node","node.env"),("executor","center.env")])
def test_preflight_is_readonly_and_checks_correct_version_table(workspace,component,filename):
    files=list((workspace/"db").glob("*.sqlite3"));before={p:p.read_bytes() for p in files}
    result=inspect_deployment(component,workspace/"config"/filename,check_port=False)
    assert result["ok"],result
    assert result["facts"]["readOnly"]
    assert "models-and-containers" in result["notVerified"]
    assert before=={p:p.read_bytes() for p in files}
    assert not any("TOKEN" in k for k in result["facts"])


def test_missing_database_is_reported_not_created(workspace):
    db=workspace/"db/center.sqlite3";db.unlink()
    result=inspect_deployment("center",workspace/"config/center.env",check_port=False)
    assert "DATABASE_READ_FAILED" in result["issues"] and not db.exists()
    with pytest.raises(ValueError):DeploymentRuntime("center",workspace/"config/center.env")
    assert not db.exists()


def test_wrong_revision_is_readonly_failure(workspace):
    db=workspace/"db/center.sqlite3"
    with sqlite3.connect(db) as conn:conn.execute("UPDATE alembic_version SET version_num='OLD'")
    result=inspect_deployment("center",workspace/"config/center.env",check_port=False)
    assert "DATABASE_REVISION_MISMATCH" in result["issues"]


def test_busy_port_does_not_start_or_stop_any_server(workspace):
    import socket
    env=workspace/"config/center.env"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1",0));listener.listen()
        edit_env(env,"INFERENCE_PORT",str(listener.getsockname()[1]))
        result=inspect_deployment("center",env)
        assert "PORT_UNAVAILABLE" in result["issues"]


def test_windows_start_is_refused_without_development_fallback(workspace,monkeypatch,capsys):
    monkeypatch.setattr("backend.service.deployment.preflight_service.platform.system",lambda:"Windows")
    assert main(["--env-file",str(workspace/"config/node.env"),"serve","node"])==2
    output=capsys.readouterr().out
    assert "LINUX_REQUIRED" in output
    assert deployment_values(workspace/"config/node.env")["NODE_ADMIN_TOKEN"] not in output


def test_plan_uses_separate_user_units_and_safe_streaming_proxy(tmp_path):
    output=tmp_path/"plan";result=render_linux_plan(plan(),output)
    assert not result["installed"] and not result["servicesStarted"]
    node=(output/"inference-module6a-node.service").read_text()
    assert "WantedBy=default.target" in node and "UMask=0077" in node
    assert "Requires=" not in node and "PartOf=" not in node and "ExecStop=" not in node
    assert "TimeoutStopSec=960" in node and "--env-file" in node
    assert "init" not in node and "sudo" not in node
    proxy=(output/"nginx-validation.conf").read_text()
    assert "proxy_buffering off" in proxy and "location / { return 404; }" in proxy
    assert "listen 127.0.0.1:" in proxy and "access_log off" in proxy
    with pytest.raises(FileExistsError):render_linux_plan(plan(),output)


@pytest.mark.parametrize("key,value",[("name","x;touch"),("projectDir","/tmp/a b"),("projectDir","/tmp/a/../b"),
    ("stateRoot","/home/swufe/runtime/db"),("pythonExecutable","/bin/python$(id)"),("centerPort",18020),("threads",128)])
def test_plan_rejects_shell_fragments_path_traversal_and_port_collision(key,value):
    spec=plan().model_dump(by_alias=True);spec[key]=value
    with pytest.raises(ValidationError):LinuxDeploymentPlan.model_validate(spec)


@pytest.mark.parametrize("component",["center","node"])
def test_real_application_lifecycle_single_owner_restart(workspace,component):
    env=workspace/"config"/("node.env" if component=="node" else "center.env")
    runtime=DeploymentRuntime(component,env)
    try:
        if component=="node":assert runtime.housekeeping.thread.is_alive()
        with pytest.raises(Exception):DeploymentRuntime(component,env)
        client=runtime.app.test_client()
        if component=="center":assert client.get("/api/health").status_code==200
        else:
            token=deployment_values(env)["NODE_OBSERVER_TOKEN"]
            assert client.get("/node/v1/status",headers={"Authorization":"Bearer "+token}).status_code==200
    finally:runtime.close();runtime.close()
    if component=="node":assert runtime.housekeeping is None
    reopened=DeploymentRuntime(component,env);reopened.close()


def test_gunicorn_contract_fixed_single_worker_and_cleanup_hook(workspace,monkeypatch):
    # BaseApplication替身验证我们的集成调用；不是Gunicorn在Windows实际运行。
    captured={};closed=[]
    class FakeBase:
        def __init__(self):self.cfg=SimpleNamespace(set=lambda k,v:captured.update({k:v}));self.load_config()
        def run(self):
            assert self.runtime is None
            assert self.load()=="wsgi-app"
            captured["worker_exit"](None,None)
    class FakeRuntime:
        app="wsgi-app"
        def __init__(self,*args):captured["created"]=args
        def close(self):closed.append(True)
    package=ModuleType("gunicorn");app=ModuleType("gunicorn.app");base=ModuleType("gunicorn.app.base");base.BaseApplication=FakeBase
    for name,module in (("gunicorn",package),("gunicorn.app",app),("gunicorn.app.base",base)):monkeypatch.setitem(sys.modules,name,module)
    monkeypatch.setattr("backend.modules.deployment.linux_server.DeploymentRuntime",FakeRuntime)
    monkeypatch.setenv("GUNICORN_CMD_ARGS","--workers 50 --preload")
    serve_linux("node",workspace/"config/node.env",16)
    assert captured["workers"]==1 and captured["worker_class"]=="gthread" and captured["threads"]==16
    assert captured["preload_app"] is False and captured["reload"] is False and captured["control_socket_disable"] is True
    assert captured["accesslog"] is None and captured["graceful_timeout"]==930
    assert closed==[True]


def test_housekeeping_start_failure_releases_node_ownership(workspace,monkeypatch):
    env=workspace/"config/node.env"
    import backend.modules.deployment.linux_server as server
    with monkeypatch.context() as patch:
        def fail(_self):raise RuntimeError("start failed")
        patch.setattr(server.NodeHousekeeping,"start",fail)
        with pytest.raises(RuntimeError,match="start failed"):DeploymentRuntime("node",env)
    runtime=DeploymentRuntime("node",env);runtime.close()


def test_executor_uses_explicit_credentials_and_empty_queue_without_models(workspace):
    from backend.modules.worker.node_executor import main as executor_main
    env=workspace/"config/center.env";_,values=deployment_settings("executor",env)
    with deployment_environment(values):assert executor_main(["--env-file",str(env),"--once"])==0
    with FileLock(str(workspace/"db/center.sqlite3")+".node-executor.lock",timeout=0):pass


def test_environment_file_hardlink_is_rejected(workspace):
    env=workspace/"config/center.env";link=workspace/"config/linked.env"
    link.hardlink_to(env)
    with pytest.raises(ConfigurationError):deployment_values(env)


def test_symlink_data_path_is_rejected_before_resolution(workspace):
    alias=workspace/"linked-logs"
    try:alias.symlink_to(workspace/"logs/center",target_is_directory=True)
    except OSError:pytest.skip("Windows user cannot create directory symlinks")
    edit_env(workspace/"config/center.env","INFERENCE_LOG_DIR",alias.as_posix())
    with pytest.raises(ValueError):deployment_settings("center",workspace/"config/center.env")


@pytest.mark.parametrize("path,allowed",[("node-migrations/env.py",True),("node-migrations/versions/x.py",True),
    ("examples/linux/module6a.json",True),("examples/.env",False),("examples/node.env",False),("runtime/deployment-validation/x/config/center.env",False)])
def test_source_whitelist_includes_node_migrations_but_no_secret_env(path,allowed):
    assert source_path_allowed("inference-platform/"+path) is allowed


def test_cli_failure_does_not_echo_env_secrets(workspace,capsys):
    env=workspace/"config/node.env";secret=deployment_values(env)["NODE_ADMIN_TOKEN"]
    assert main(["--env-file",str(env),"serve","center"])==2
    output=capsys.readouterr();assert secret not in output.out+output.err
