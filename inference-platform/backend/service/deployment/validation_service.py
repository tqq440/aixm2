"""仅创建全新独立验证环境；不迁移已有库、不启动任何模型。"""

import json
from pathlib import Path
import secrets
from alembic import command
from alembic.config import Config
from backend.config.settings.app_settings import PROJECT_ROOT
from backend.config.deployment.linux_settings import deployment_settings
from backend.schema.deployment.linux_schema import ValidationWorkspace
from backend.service.host_node.migration_service import upgrade_node_database
from backend.utils.files.deployment_files import new_directory,private_text
from backend.utils.files.source_archive import plain_path


def create_validation_workspace(request):
    request=ValidationWorkspace.model_validate(request.model_dump());root=plain_path(request.root,allow_missing=True)
    project=plain_path(PROJECT_ROOT)
    if root==project or root in project.parents or root==project/"runtime/deployment-validation" or (root.is_relative_to(project) and not root.is_relative_to(project/"runtime/deployment-validation")):
        raise ValueError("Only an explicitly new isolated workspace is accepted")
    new_directory(root)
    for directory in ("config","db","logs","logs/center","logs/node","uploads","tmp","models"):
        new_directory(root/directory)
    private_text(root/"deployment-validation.json",json.dumps({"schemaVersion":1,"name":request.name,"purpose":"isolated-verification","state":"initializing"})+'\n')
    ingress,admin,observer,executor,report,monitor=(secrets.token_urlsafe(48) for _ in range(6))
    center={"DEPLOY_STATE_ROOT":str(root),"DEPLOY_COMPONENT":"center","INFERENCE_HOST":"127.0.0.1","INFERENCE_PORT":str(request.center_port),
        "INFERENCE_DATABASE_PATH":str(root/"db/center.sqlite3"),"INFERENCE_LOG_DIR":str(root/"logs/center"),"INFERENCE_UPLOAD_DIR":str(root/"uploads"),
        "INFERENCE_ASR_MODEL_DIR":str(root/"models/not-installed-asr"),"INFERENCE_VISION_MODEL_DIR":str(root/"models/not-installed-vision"),
        "INFERENCE_COMMON_EXECUTION_ENABLED":"true","INFERENCE_PUBLIC_API_ENABLED":"true","INFERENCE_INGRESS_TOKEN":ingress,
        "INFERENCE_MONITOR_TOKEN":monitor,"INFERENCE_COMMON_QUEUE_BACKEND":"sqlite","DEPLOY_NODE_EXECUTOR_TOKEN":executor}
    node={"DEPLOY_STATE_ROOT":str(root),"DEPLOY_COMPONENT":"node","NODE_ID":request.name+"-node","NODE_HOST":"127.0.0.1","NODE_PORT":str(request.node_port),
        "NODE_DATABASE_PATH":str(root/"db/node.sqlite3"),"NODE_LOG_DIR":str(root/"logs/node"),"NODE_ADMIN_TOKEN":admin,"NODE_OBSERVER_TOKEN":observer,
        "NODE_EXECUTOR_TOKEN":executor,"NODE_REPORT_TOKEN_ASR":report,"NODE_INGRESS_TOKEN":ingress,"NODE_CENTRAL_URL":f"http://127.0.0.1:{request.center_port}",
        "NODE_MONITOR_TOKEN":monitor,"NODE_MONITOR_CENTRAL_URL":f"http://127.0.0.1:{request.center_port}",
        "NODE_DOCKER_ENABLED":"false","NODE_BUSINESS_ENABLED":"false","NODE_METRICS_ENABLED":"false"}
    for filename,values in (("center.env",center),("node.env",node)):
        # dotenv 双引号会解释反斜线转义；统一路径分隔符避免 Windows 的 \r/\t。
        private_text(root/"config"/filename,''.join(f'{key}={json.dumps(value.replace(chr(92), chr(47)),ensure_ascii=False)}\n' for key,value in values.items()))
    central,_=deployment_settings("center",root/"config/center.env");node_settings,_=deployment_settings("node",root/"config/node.env")
    migration=Config(str(PROJECT_ROOT/"alembic.ini"));migration.set_main_option("script_location",str(PROJECT_ROOT/"migrations"));migration.attributes["settings"]=central
    command.upgrade(migration,"head");upgrade_node_database(node_settings)
    private_text(root/"READY", "Independent SQLite verification environment only. Redis, Linux, models and containers are not verified.\n")
    return {"root":str(root),"centerEnv":str(root/"config/center.env"),"nodeEnv":str(root/"config/node.env"),"modelsStarted":False,
            "queue":"sqlite-verification-only","existingDataMigrated":False,"secrets":"generated-in-private-files-not-printed"}
