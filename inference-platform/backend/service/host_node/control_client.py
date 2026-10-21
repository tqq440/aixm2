"""一键管理命令的 Node HTTP 客户端；不直连 Docker 或绕过控制版本。"""

from time import monotonic, sleep
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx2 as httpx

from backend.common.exceptions.app_error import AppError
from backend.schema.host_node.control_schema import OperationView
from backend.schema.host_node.node_schema import NodeServicePath


class NodeControlClient:
    def __init__(self, settings, url=None, *, transport=None):
        self.settings = settings
        self.url = (url or f"http://127.0.0.1:{settings.port}").rstrip("/")
        parsed = urlsplit(self.url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or any(c.isspace() for c in self.url)
                or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"})):
            raise AppError("NODE_CLIENT_URL_INVALID", "管理客户端使用回环 HTTP 或可信 HTTPS 根地址。", 422)
        self.transport = transport

    def execute(self, service_id, action, version_id=None, *, operation_id=None, wait_seconds=0):
        service_id = NodeServicePath(service_id=service_id).service_id
        identifier = str(UUID(operation_id)) if operation_id else str(uuid4())
        with httpx.Client(timeout=self.settings.control_timeout_seconds, trust_env=False, follow_redirects=False,
                          transport=self.transport, headers={"Authorization": "Bearer " + self.settings.admin_token.get_secret_value()}) as client:
            def call(method, path, body=None, *, missing_ok=False):
                response = client.request(method, self.url + path, json=body)
                if response.status_code == 404 and missing_ok:
                    return None
                if response.status_code not in {200, 201, 202} or len(response.content) > 65536:
                    raise AppError("NODE_CLIENT_REQUEST_FAILED", f"Node 操作失败，请查询 operationId={identifier}。", 409)
                return response.json()["data"]
            try:
                if call("GET", "/node/v1/status")["nodeId"] != self.settings.node_id:
                    raise AppError("NODE_CLIENT_ID_MISMATCH", "Node 身份与所选配置不符。", 409)
                existing = call("GET", "/node/v1/operations/" + identifier, missing_ok=True) if operation_id else None
                if existing:
                    if existing["serviceId"] != service_id or existing["action"] != action or existing["requestedVersion"] != version_id:
                        raise AppError("NODE_OPERATION_CONFLICT", "操作编号已用于其他内容。", 409)
                    value = existing
                else:
                    runtime = call("GET", f"/node/v1/admin/services/{service_id}/runtime")
                    configuration = call("GET", f"/node/v1/admin/services/{service_id}")
                    command = {"operationId": identifier, "action": action, "versionId": version_id,
                        "expectedControlVersion": runtime["controlVersion"], "configRevision": configuration["configRevision"]}
                    try:
                        value = call("POST", f"/node/v1/services/{service_id}/operations", command)
                    except httpx.HTTPError:
                        value = call("GET", "/node/v1/operations/" + identifier, missing_ok=True)
                        if value is None:
                            raise AppError("NODE_CLIENT_OUTCOME_UNKNOWN", f"提交结果未知，仅核对 operationId={identifier}，不要换编号重发。", 503) from None
                deadline = monotonic() + wait_seconds
                while value["status"] in {"pending", "running"} and monotonic() < deadline:
                    sleep(min(0.25, max(0, deadline - monotonic())))
                    value = call("GET", "/node/v1/operations/" + identifier)
                return OperationView.model_validate(value)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise AppError("NODE_CLIENT_REQUEST_FAILED", f"无法确认 Node 操作，请核对 operationId={identifier}。", 503) from None
