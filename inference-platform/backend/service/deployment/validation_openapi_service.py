"""仅本机管理协议；不增加Node入口或公开验收证据文件。"""

from backend.schema.common.api_schema import ErrorResponse,SuccessResponse
from backend.schema.deployment.validation_issue_schema import ValidationIssueQuery,ValidationIssuePage


def validation_openapi():
    schemas={}
    output=SuccessResponse[ValidationIssuePage]
    for model in (output,ErrorResponse):
        schema=model.model_json_schema(by_alias=True,ref_template="#/components/schemas/{model}")
        schemas.update(schema.pop("$defs",{}));schemas["ValidationIssuePageResponse" if model is output else model.__name__]=schema
    raw=ValidationIssueQuery.model_json_schema(by_alias=True)
    responses={"200":{"description":"只读本地验收记录；不访问校园网、Redis或模型。","content":{"application/json":{"schema":{"$ref":"#/components/schemas/ValidationIssuePageResponse"}}}}}
    for status in (403,422,503):
        responses[str(status)]={"description":"本机权限、参数或本地记录读取错误，不改变任务状态。","content":{"application/json":{"schema":{"$ref":"#/components/schemas/ErrorResponse"}}}}
    return {"openapi":"3.1.0","info":{"title":"本机环境验收记录","version":"1.0.0",
        "description":"仅本机管理页面/客户端；不是应用API。记录为空不表示验收通过，证据原文件不公开。"},
        "paths":{"/api/admin/validation/issues":{"get":{"operationId":"listValidationIssues","summary":"只读已保存的环境验收问题",
            "parameters":[{"name":key,"in":"query","required":False,"schema":value} for key,value in raw["properties"].items()],"responses":responses}},
            "/api/admin/validation/openapi.json":{"get":{"operationId":"getValidationOpenapi","responses":{"200":{"description":"本接口OpenAPI定义"},"403":{"description":"仅本机访问"},"422":{"description":"不接受查询参数"}}}}},
        "components":{"schemas":schemas}}
