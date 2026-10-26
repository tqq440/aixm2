"""Node 实际接口与默认未配置的适配边界；公共应用中转留后续模块。"""

from backend.schema.common.api_schema import ErrorResponse, SuccessResponse
from backend.schema.host_node.node_schema import (
    ConfigurationView, EventPage, NodeServiceReport, NodeStatus, OpenNodeInstance,
    ReportReceipt, SaveNodeService, ServicePage, ServiceSnapshot,
)
from backend.schema.host_node.control_schema import (
    AbortControlCommand, ControlCommand, InstanceAssignment, OperationPage, OperationView,
    ReconcileCommand, ResourceView, RuntimeView, SaveRuntimeConfig,
)
from backend.schema.host_node.execution_schema import ExecutionAvailability, ExecutionCommand, ExecutionReceipt, StreamExecutionInput
from backend.schema.host_node.upstream_schema import ExecutionManifest
from backend.schema.monitoring.budget_schema import ConfirmQueriesStopped


def node_openapi():
    schemas = {}
    outputs = (NodeStatus, ServicePage, ServiceSnapshot, EventPage, ConfigurationView, ReportReceipt,
               InstanceAssignment, OperationView, OperationPage, ResourceView, RuntimeView,
               ExecutionAvailability, ExecutionReceipt)
    for model in (SaveNodeService, OpenNodeInstance, NodeServiceReport, ErrorResponse,
                  SaveRuntimeConfig, ControlCommand, ReconcileCommand, AbortControlCommand, ExecutionCommand,
                  StreamExecutionInput, ExecutionManifest, ConfirmQueriesStopped,
                  *(SuccessResponse[output] for output in outputs)):
        schema = model.model_json_schema(by_alias=True, ref_template="#/components/schemas/{model}")
        schemas.update(schema.pop("$defs", {}))
        schemas[model.__name__.replace("[", "_").replace("]", "")] = schema
    errors = {str(status): {"description": description, "content": {"application/json": {
        "schema": {"$ref": "#/components/schemas/ErrorResponse"}}}} for status, description in (
        (400, "请求格式错误"), (401, "凭据无效或角色不符"), (404, "服务或接口不存在"),
        (409, "配置、实例、调用或序号冲突"), (413, "请求超过该接口上限"), (415,"内容类型不匹配"), (422, "字段校验失败"),
        (500, "内部错误"), (503, "业务适配器尚未配置或服务不可用"))}
    paths = {}

    def operation(path, method, summary, *, model=None, output=None, role="read", parameters=None, status="200"):
        security = [{"NodeExecutor": []}] if role == "executor" else ([{"NodeAdmin": []}] if role == "admin" else (
            [{"ServiceReporter": []}] if role == "reporter" else [{"NodeAdmin": []}, {"NodeObserver": []}])
        )
        reply = {"description": "UTC ISO8601 时间；未知字段为 null。", "content": {"application/json": {"schema":
            {"$ref": f"#/components/schemas/SuccessResponse_{output.__name__}"} if output else {"type": "object"}}}}
        value = {"summary": summary, "security": security, "responses": {status: reply, **errors}}
        params = list(parameters or [])
        if "{serviceId}" in path:
            params.append({"name": "serviceId", "in": "path", "required": True,
                "schema": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$", "maxLength": 64}})
        if "{operationId}" in path:
            params.append({"name": "operationId", "in": "path", "required": True,
                           "schema": {"type": "string", "format": "uuid"}})
        if "{invocationId}" in path:
            params.append({"name": "invocationId", "in": "path", "required": True,
                           "schema": {"type": "string", "pattern": "^[a-f0-9]{32}$"}})
        if params:
            value["parameters"] = params
        if model:
            value["requestBody"] = {"required": True, "content": {"application/json": {
                "schema": {"$ref": f"#/components/schemas/{model.__name__}"}}}}
        paths.setdefault(path, {})[method] = value

    def integer_parameter(name, default, minimum, maximum):
        return {"name": name, "in": "query", "schema": {
            "type": "integer", "default": default, "minimum": minimum, "maximum": maximum}}

    operation("/node/v1/status", "get", "Node 本地运行状态与已实现能力", output=NodeStatus)
    operation("/node/v1/services", "get", "分页读取服务快照", parameters=[
        integer_parameter("page", 1, 1, 1_000_000), integer_parameter("pageSize", 20, 1, 100)], output=ServicePage)
    operation("/node/v1/services/{serviceId}", "get", "读取服务快照；不查询业务服务", output=ServiceSnapshot)
    for path in ("/node/v1/events", "/node/v1/services/{serviceId}/events"):
        operation(path, "get", "游标读取事件", parameters=[integer_parameter("afterSequence", 0, 0, 9_007_199_254_740_991),
            integer_parameter("limit", 50, 1, 200)], output=EventPage)
    operation("/node/v1/admin/services/{serviceId}", "get", "读取配置及版本", role="admin", output=ConfigurationView)
    operation("/node/v1/admin/services/{serviceId}", "put", "保存配置；新建 expectedVersion 为 null", model=SaveNodeService, role="admin", output=ServiceSnapshot)
    operation("/node/v1/admin/services/{serviceId}/instances", "post", "绑定上报身份；不启动容器",
              model=OpenNodeInstance, role="admin", status="201", output=ServiceSnapshot)
    operation("/node/internal/v1/services/{serviceId}/reports", "post", "业务生命周期上报；重复不刷新心跳",
              model=NodeServiceReport, role="reporter", output=ReportReceipt)
    operation("/node/v1/openapi.json", "get", "读取此 OpenAPI 文档（直接返回文档）")
    operation("/node/v1/resources", "get", "只读主机指标快照；不触发采样", output=ResourceView)
    operation("/node/v1/admin/services/{serviceId}/runtime", "get", "读取固定版本容器资料", role="admin", output=RuntimeView)
    operation("/node/v1/admin/services/{serviceId}/runtime", "put", "保存固定版本容器资料；不操作 Docker",
              role="admin", model=SaveRuntimeConfig, output=RuntimeView)
    operation("/node/v1/services/{serviceId}/operations", "get", "读取控制操作历史", output=OperationPage,
              parameters=[integer_parameter("page", 1, 1, 1_000_000), integer_parameter("pageSize", 20, 1, 100)])
    operation("/node/v1/services/{serviceId}/operations", "post", "受理受控启动/停止/升级，后台执行",
              role="admin", model=ControlCommand, output=OperationView, status="202")
    operation("/node/v1/operations/{operationId}", "get", "读取控制操作", output=OperationView)
    operation("/node/v1/operations/{operationId}/reconcile", "post", "显式观察核对后恢复安全阶段，不重发未知命令",
              role="admin", model=ReconcileCommand, output=OperationView)
    operation("/node/v1/operations/{operationId}/abort", "post", "确认所有容器停止并人工核实未决命令后终结操作；不终结推理任务",
              role="admin", model=AbortControlCommand, output=OperationView)
    operation("/node/internal/v1/services/{serviceId}/assignment", "get", "本机业务获取当前启动身份和暂停接单要求",
              role="reporter", output=InstanceAssignment)
    operation("/node/internal/v1/services/{serviceId}/execution-availability", "get", "只读执行门禁与适配器资料，不查询业务",
              role="executor", output=ExecutionAvailability)
    operation("/node/internal/v1/executions", "post", "执行适配边界；默认未配置适配器返回 503；重复不重做",
              role="executor", model=ExecutionCommand, output=ExecutionReceipt)
    operation("/node/internal/v1/executions/{invocationId}", "get", "只读执行档案，不返回结果正文",
              role="executor", output=ExecutionReceipt)
    operation("/node/internal/v1/executions/{invocationId}/result", "get", "读取已保存的结果，不重做推理",
              role="executor", output=ExecutionReceipt)
    operation("/node/internal/v1/executions/file","post","已校验的离线文件调用；不重试；需显式开启业务适配",role="executor",output=ExecutionReceipt)
    paths["/node/internal/v1/executions/file"]["post"]["requestBody"]={"required":True,"content":{"multipart/form-data":{
        "schema":{"type":"object","additionalProperties":False,"required":["command","manifest","file"],"properties":{
            "command":{"type":"string","maxLength":16384,"contentMediaType":"application/json","contentSchema":{"$ref":"#/components/schemas/ExecutionCommand"}},
            "manifest":{"type":"string","maxLength":16384,"contentMediaType":"application/json","contentSchema":{"$ref":"#/components/schemas/ExecutionManifest"}},
            "file":{"type":"string","format":"binary","description":"音频最多20MiB/300秒；静态图片最多10MiB/2000万像素；不采用文件名作为路径。"}}}}}}
    operation("/node/internal/v1/executions/stream","post","文本流；终帧 receipt 仅含完成元数据，普通断线未知",role="executor",model=StreamExecutionInput)
    paths["/node/internal/v1/executions/stream"]["post"]["responses"]["200"]={"description":"SSE event=delta: {invocationId,text}; event=receipt: ExecutionReceipt。不会保存或重播 token。",
        "content":{"text/event-stream":{"schema":{"type":"string"}}}}
    operation("/node/internal/v1/executions/{invocationId}/monitoring","get","只读缓存进度/实际设备，不查询业务",role="executor",output=ExecutionReceipt)
    operation("/node/internal/v1/executions/{invocationId}/reconcile","post","显式核对，包括过期调用；不重新推理",role="executor",output=ExecutionReceipt)
    operation("/node/internal/v1/executions/{invocationId}/cancel","post","请求取消；ACK不作为远端停止证明",role="executor",output=ExecutionReceipt)
    operation("/node/v1/admin/executions/{invocationId}/monitor-budget/release","post",
              "核实原查询已停止后归还未知查询许可；不释放推理容量",role="admin",model=ConfirmQueriesStopped,output=ExecutionReceipt)
    for suffix in ("reconcile","cancel"):
        paths["/node/internal/v1/executions/{invocationId}/"+suffix]["post"]["requestBody"]={"required":True,"content":{"application/json":{"schema":{"type":"object","additionalProperties":False}}}}
    for method in ("get","post"):
        operation("/api/v1/{path}",method,"公共接口白名单代理；需应用Key，完整契约见 /api/v1/openapi.json",role="executor")
        paths["/api/v1/{path}"][method]["security"]=[{"ApplicationKey":[]}]
        paths["/api/v1/{path}"][method]["parameters"]=[{"name":"path","in":"path","required":True,"schema":{"type":"string"},"description":"仅 services、tasks、llm/requests、openapi.json 及文档列明的子路径；不代理管理或执行。"}]
    return {"openapi": "3.1.0", "info": {"title": "Host Node Foundation", "version": "lifecycle-v1",
        "description": "独立 Node 控制与受鉴权的中心内部执行；4C2 提供 HTTP/文件/SSE、后台状态采集，业务开关默认关闭；公共应用鉴权接口留模块5。"},
        "paths": paths, "components": {"schemas": schemas, "securitySchemes": {
            name: {"type": "http", "scheme": "bearer", "description": description}
            for name, description in (("NodeAdmin", "独立 Node 管理凭据"), ("NodeObserver", "只读观察凭据"),
                                      ("NodeExecutor", "中心内部执行凭据；不能管理服务或伪造业务报告"),
                                      ("ServiceReporter", "对应服务的独立上报凭据；无管理或查询权限"),
                                      ("ApplicationKey","外部应用Key，仅公共白名单代理；由中心鉴权"))}}}
