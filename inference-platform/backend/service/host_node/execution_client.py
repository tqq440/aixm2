"""中心到 Node 的有界内部 HTTP 客户端；不自动重试 POST、不记录凭据。"""

import os
from time import monotonic
from datetime import datetime, timezone
from threading import Timer
import json

import httpx2 as httpx

from backend.common.exceptions.app_error import AppError
from backend.schema.catalog.service_schema import NodeTargetBinding
from backend.schema.common.api_schema import SuccessResponse, ApiSchema
from backend.schema.host_node.execution_schema import ExecutionAvailability, ExecutionReceipt
from backend.utils.stream.sse_utils import iter_sse_events, decode_sse_json_object
from backend.utils.time.time_utils import as_utc


class NodeExecutionClient:
    def __init__(self, binding, *, environ=None, transport=None, timeout_seconds=10):
        self.binding = NodeTargetBinding.model_validate(binding)
        environment = os.environ if environ is None else environ
        self.token = environment.get(self.binding.executor_token_env, "")
        if (not isinstance(self.token, str) or not 32 <= len(self.token) <= 256 or not self.token.isascii()
                or any(c.isspace() for c in self.token)):
            raise AppError("NODE_EXECUTOR_CREDENTIAL_MISSING", "未配置中心到 Node 的独立执行凭据。", 503)
        if not 0 < timeout_seconds <= 900:
            raise ValueError("Node 请求超时须有界")
        self.transport, self.timeout_seconds = transport, timeout_seconds

    def _request(self, method, path, model, *, command=None, content_args=None, timeout=None):
        timeout=self.timeout_seconds if timeout is None else timeout
        try:
            start = monotonic()
            with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False,
                              transport=self.transport) as client:
                with client.stream(method, self.binding.base_url + path,
                    headers={"Authorization": "Bearer " + self.token, "Accept-Encoding": "identity"},
                    json=command.model_dump(mode="json", by_alias=True) if command else None, **(content_args or {})) as response:
                    if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                        raise ValueError("Node 未返回可核对结果")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if monotonic() - start > timeout or len(data) + len(chunk) > 3 * 1024 * 1024:
                            raise ValueError("Node 响应超过时间或大小上限")
                        data.extend(chunk)
            return SuccessResponse[model].model_validate_json(data).data
        except (httpx.HTTPError, ValueError, TypeError):
            raise AppError("NODE_RESPONSE_UNCONFIRMED", "Node 响应不可确认；不重发推理或自动释放容量。", 502) from None

    def availability(self):
        return self._request("GET", "/node/internal/v1/services/" + self.binding.service_id + "/execution-availability", ExecutionAvailability)

    def execute(self, command):
        return self._request("POST", "/node/internal/v1/executions", ExecutionReceipt, command=command)

    def execute_file(self,command,material):
        remaining=min(900,(as_utc(command.deadline_at)-datetime.now(timezone.utc)).total_seconds())
        if remaining<=0:raise AppError("NODE_EXECUTION_EXPIRED","派发期限已过。",504)
        return self._request("POST","/node/internal/v1/executions/file",ExecutionReceipt,content_args={
            "data":{"command":json.dumps(command.model_dump(mode="json",by_alias=True)),
                    "manifest":json.dumps(material.manifest.model_dump(mode="json",by_alias=True))},
            "files":{"file":("input.upload",material.content,"application/octet-stream")}},timeout=remaining)

    def stream(self,command,request):
        remaining=(as_utc(command.deadline_at)-datetime.now(timezone.utc)).total_seconds()
        if remaining<=0:raise AppError("NODE_STREAM_EXPIRED","在线期限已过。",504)
        try:
            with httpx.Client(timeout=httpx.Timeout(remaining,connect=min(5,remaining)),trust_env=False,
                              follow_redirects=False,transport=self.transport) as client:
                def close_transport():
                    try:client.close()
                    except Exception:pass
                timer=Timer(remaining,close_transport);timer.daemon=True;timer.start()
                try:
                    with client.stream("POST",self.binding.base_url+"/node/internal/v1/executions/stream",
                            headers={"Authorization":"Bearer "+self.token,"Accept-Encoding":"identity"},
                            json={"command":command.model_dump(mode="json",by_alias=True),"request":request.model_dump(mode="json",by_alias=True)}) as response:
                        if (response.status_code!=200 or response.headers.get("content-type","").split(";")[0]!="text/event-stream"
                                or response.headers.get("content-encoding","identity")!="identity"):raise ValueError()
                        count=0
                        for event in iter_sse_events(response.iter_raw()):
                            value=decode_sse_json_object(event.data)
                            if event.event=="delta":
                                if set(value)!={"invocationId","text"} or value["invocationId"]!=command.invocation_id or not isinstance(value["text"],str):raise ValueError()
                                count+=len(value["text"])
                                if count>131072:raise ValueError()
                                yield {"event":"delta","data":value}
                            elif event.event=="receipt":
                                receipt=ExecutionReceipt.model_validate(value)
                                yield {"event":"receipt","data":receipt};return
                            else:raise ValueError()
                        raise ValueError()
                finally:timer.cancel()
        except Exception:raise AppError("NODE_STREAM_UNCONFIRMED","Node 流未确认结束，不重放、不释放未知调用。",502) from None

    def result(self, invocation_id):
        # ID 来自已持久化并通过 schema 的 command，仍独立检查避免路径注入。
        from pydantic import TypeAdapter
        from backend.schema.host_node.execution_schema import InvocationId
        identifier = TypeAdapter(InvocationId).validate_python(invocation_id)
        return self._request("GET", "/node/internal/v1/executions/" + identifier + "/result", ExecutionReceipt)

    def reconcile(self,invocation_id):
        from pydantic import TypeAdapter
        from backend.schema.host_node.execution_schema import InvocationId
        identifier=TypeAdapter(InvocationId).validate_python(invocation_id)
        return self._request("POST","/node/internal/v1/executions/"+identifier+"/reconcile",ExecutionReceipt,command=ApiSchema())

    def cancel(self,invocation_id):
        from pydantic import TypeAdapter
        from backend.schema.host_node.execution_schema import InvocationId
        identifier=TypeAdapter(InvocationId).validate_python(invocation_id)
        return self._request("POST","/node/internal/v1/executions/"+identifier+"/cancel",ExecutionReceipt,command=ApiSchema())
