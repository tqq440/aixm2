"""专属 env 读取；不回退到日常.env，不执行变量插值。"""

from contextlib import contextmanager
import os
from pathlib import Path
import re
from dotenv.parser import parse_stream
from backend.common.exceptions.app_error import ConfigurationError
from backend.config.settings.app_settings import load_settings,PROJECT_ROOT,ENV_FIELDS
from backend.config.host_node.node_settings import load_node_settings,NODE_ENV_FIELDS
from backend.utils.files.source_archive import plain_path

CENTRAL_HEAD="20261022_0021"
NODE_HEAD="20261021_node_0004"
GUNICORN_VERSION="26.2.0"


def deployment_values(path):
    try:
        path=plain_path(Path(path));values={}
        if path.stat().st_size>65536:raise ValueError()
        if os.name=="posix" and (path.stat().st_uid!=os.getuid() or path.stat().st_mode & 0o077):raise ValueError()
        with path.open(encoding="utf-8") as source:
            for entry in parse_stream(source):
                if entry.error:raise ValueError()
                if entry.key is None:continue
                if (entry.key in values or not re.fullmatch(r"(?:INFERENCE|NODE|DEPLOY)_[A-Z0-9_]+",entry.key)
                        or entry.value is None or any(c in entry.value for c in ("\n","\r","\x00")) or "${" in entry.value):raise ValueError()
                values[entry.key]=entry.value
        raw_root=Path(values["DEPLOY_STATE_ROOT"])
        if not raw_root.is_absolute():raise ValueError()
        root=plain_path(raw_root,allow_missing=True)
        project=plain_path(PROJECT_ROOT)
        if root==project or root in project.parents or root==project/"runtime/deployment-validation":raise ValueError()
        if root.is_relative_to(project) and not root.is_relative_to(project/"runtime/deployment-validation"):raise ValueError()
        if path.parent!=root/"config":raise ValueError()
        return values
    except (OSError,ValueError,KeyError):
        raise ConfigurationError("部署env无效；检查独立目录、权限、重复项和允许的变量名，未读取日常配置。") from None


def deployment_settings(component,path):
    if component not in {"center","node","executor"}:raise ConfigurationError("未知部署组件。")
    values=deployment_values(path)
    expected="node" if component=="node" else "center"
    if values.get("DEPLOY_COMPONENT")!=expected:raise ConfigurationError("部署env与组件不匹配。")
    allowed={"DEPLOY_STATE_ROOT","DEPLOY_COMPONENT"}
    allowed.update(NODE_ENV_FIELDS if component=="node" else ENV_FIELDS)
    if component!="node":allowed.add("DEPLOY_NODE_EXECUTOR_TOKEN")
    for key,value in values.items():
        report_key=component=="node" and key.startswith(("NODE_REPORT_TOKEN_","NODE_UPSTREAM_TOKEN_"))
        if key not in allowed and not report_key:raise ConfigurationError("部署env包含未知或跨组件配置项。")
        if key.endswith(("_DATABASE_PATH","_LOG_DIR","_UPLOAD_DIR","_ASR_MODEL_DIR","_VISION_MODEL_DIR")):
            if not Path(value).is_absolute():raise ConfigurationError("部署数据路径必须显式绝对指定。")
            plain_path(Path(value),allow_missing=True)
    settings=load_node_settings(env_file=Path(path),environ={}) if component=="node" else load_settings(Path(path),environ={},include_service_selection=False)
    root=Path(values["DEPLOY_STATE_ROOT"])
    expected_db=root/("db/node.sqlite3" if component=="node" else "db/center.sqlite3")
    if settings.database_path!=expected_db:raise ConfigurationError("验证环境必须使用各组件专属数据库路径。")
    candidates=[settings.database_path,settings.log_dir]
    if component!="node":candidates.extend([settings.upload_dir,settings.asr_model_dir,settings.vision_model_dir])
    for candidate in candidates:
        checked=plain_path(candidate,allow_missing=True)
        if not checked.is_relative_to(root) or checked==root:raise ConfigurationError("组件路径必须在独立状态目录内。")
    if component=="node" and (settings.docker_enabled or settings.business_enabled):raise ConfigurationError("6A验证环境不启用Docker控制或真实业务执行。")
    if component!="node" and (not settings.common_execution_enabled or not settings.public_api_enabled):
        raise ConfigurationError("验证中转环境须显式启用公共执行和API。")
    if component!="node":
        token=values.get("DEPLOY_NODE_EXECUTOR_TOKEN","")
        if len(token)<32 or len(token)>256 or not token.isascii() or any(c.isspace() for c in token):
            raise ConfigurationError("验证中心需要独立Node执行凭据。")
        if token==settings.ingress_token.get_secret_value() or (settings.monitor_token and token==settings.monitor_token.get_secret_value()):raise ConfigurationError("执行凭据不能与公共入口凭据复用。")
    if component=="node" and settings.central_url is not None:
        from urllib.parse import urlsplit
        if urlsplit(settings.central_url).hostname!="127.0.0.1":raise ConfigurationError("6A中心地址只使用回环验证入口。")
    if settings.host!="127.0.0.1":raise ConfigurationError("6A验证入口只监听本机回环地址。")
    return settings,values


@contextmanager
def deployment_environment(values):
    """仅供启动入口，在开始服务前固定本进程配置与凭据；不用于请求线程。"""
    affected={k:v for k,v in os.environ.items() if k.startswith(("INFERENCE_","NODE_","DEPLOY_"))}
    try:
        for key in affected:os.environ.pop(key,None)
        os.environ.update(values)
        yield
    finally:
        for key in list(os.environ):
            if key.startswith(("INFERENCE_","NODE_","DEPLOY_")):os.environ.pop(key,None)
        os.environ.update(affected)
