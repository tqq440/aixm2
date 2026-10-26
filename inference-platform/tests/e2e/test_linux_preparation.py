"""新部署运行工厂的回环HTTP验收；WSGI测试服务器，不是Linux/Gunicorn验收。"""

from contextlib import ExitStack
import json
import sqlite3
import httpx2 as httpx
from backend.config.deployment.linux_settings import deployment_values
from backend.modules.deployment.linux_server import DeploymentRuntime
from backend.schema.deployment.linux_schema import ValidationWorkspace
from backend.schema.gateway.gateway_schema import KeyCommand
from backend.schema.invocations.invocation_schema import ApplicationBudget
from backend.service.deployment.validation_service import create_validation_workspace
from tests.e2e.test_node_protocol_http import serving


def test_independent_runtime_node_forwards_auth_and_center_outage_without_local_inference(tmp_path):
    root=tmp_path/"new-deployment"
    create_validation_workspace(ValidationWorkspace(root=root))
    center_env=root/"config/center.env";node_env=root/"config/node.env"
    center=DeploymentRuntime("center",center_env)
    node=None
    try:
        core=center.app.extensions["invocation_service"]
        core.configure_application(ApplicationBudget(application_id="deployment-test",allowed_services=["not-deployed"]))
        key=center.app.extensions["gateway_keys"].issue("deployment-test",KeyCommand())["apiKey"]
        with ExitStack() as stack:
            center_url=stack.enter_context(serving(center.app))
            lines=node_env.read_text(encoding="utf-8").splitlines()
            lines=[line for line in lines if not line.startswith("NODE_CENTRAL_URL=")]
            node_env.write_text('\n'.join(lines)+f'\nNODE_CENTRAL_URL={json.dumps(center_url)}\n',encoding="utf-8")
            node=DeploymentRuntime("node",node_env)
            with serving(node.app) as node_url,httpx.Client(trust_env=False,timeout=5) as client:
                assert client.get(node_url+"/api/v1/services").status_code==401
                response=client.get(node_url+"/api/v1/services",headers={"Authorization":"Bearer "+key})
                assert response.status_code==200 and response.json()["data"]["items"]==[]
                observer=deployment_values(node_env)["NODE_OBSERVER_TOKEN"]
                assert client.get(node_url+"/node/v1/status",headers={"Authorization":"Bearer "+observer}).status_code==200
                # 仅关闭本测试的中心HTTP服务器；Node仍运行，不能本地接管。
                stack.close()
                assert client.get(node_url+"/api/v1/services",headers={"Authorization":"Bearer "+key}).status_code==503
                assert client.get(node_url+"/node/v1/status",headers={"Authorization":"Bearer "+observer}).status_code==200
        with sqlite3.connect(root/"db/node.sqlite3") as connection:
            assert connection.execute("SELECT count(*) FROM host_node_executions").fetchone()[0]==0
        assert not any((root/"models").iterdir())
    finally:
        if node:node.close()
        center.close()
