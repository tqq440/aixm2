"""内部查询许可协议；不同于应用公共 API。"""

from backend.schema.common.api_schema import ErrorResponse, SuccessResponse
from backend.schema.monitoring.budget_schema import MonitorPermitCommand, MonitorPermitReply


def monitor_budget_openapi():
    schemas={}
    for name,model in (("MonitorPermitCommand",MonitorPermitCommand),("MonitorPermitResponse",SuccessResponse[MonitorPermitReply]),
                       ("ErrorResponse",ErrorResponse)):
        value=model.model_json_schema(by_alias=True,ref_template="#/components/schemas/{model}")
        schemas.update(value.pop("$defs",{}));schemas[name]=value
    paths={}
    def response(model, description):
        return {"description":description,"content":{"application/json":{
            "schema":{"$ref":"#/components/schemas/"+model}}}}
    for action in ("acquire","release"):
        operation={
            "summary":"申请全局查询名额" if action=="acquire" else "幂等归还；先归还会留下阻止迟到申请的墓碑",
            "security":[{"MonitorToken":[]}],
            "requestBody":{"required":True,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/MonitorPermitCommand"}}}},
            "responses":{"200":response("MonitorPermitResponse","held/released/denied；拒绝不改变推理任务"),
                **{str(code):response("ErrorResponse",message) for code,message in (
                    (401,"独立凭据无效"),(409,"调用身份冲突"),(413,"请求过大"),(422,"参数无效"),(503,"未启用或暂不可用"))}}}
        paths["/internal/v1/monitor-budget/"+action]={"post":operation}
    return {"openapi":"3.1.0","info":{"title":"Cross-Node monitoring budget","version":"1.0.0",
        "description":"单中心SQLite事务裁决，最多四个并发查询，同一物理服务最多一个；无TTL自动释放。"},
        "paths":paths,"components":{"schemas":schemas,"securitySchemes":{"MonitorToken":{"type":"http","scheme":"bearer",
            "description":"INFERENCE_MONITOR_TOKEN；只供受信Node使用，不能复用入口或执行凭据"}}}}
