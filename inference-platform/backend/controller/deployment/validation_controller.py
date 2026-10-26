"""本机只读环境验收记录；与公共应用任务API隔离。"""

from flask import Blueprint,current_app,g,jsonify,request
from backend.common.exceptions.app_error import AppError
from backend.common.middleware.local_configuration import authorize_local_configuration
from backend.schema.common.api_schema import ApiSchema,SuccessResponse
from backend.schema.deployment.validation_issue_schema import ValidationIssueQuery
from backend.service.deployment.validation_openapi_service import validation_openapi

validation_blueprint=Blueprint("validation_admin",__name__,url_prefix="/api/admin/validation")


@validation_blueprint.before_request
def authorize():authorize_local_configuration(request,require_json=False)


@validation_blueprint.after_request
def no_cache(response):
    response.headers["Cache-Control"]="no-store"
    return response


def query(model):
    return model.model_validate({key:values[0] if len(values)==1 else values for key,values in request.args.lists()})


@validation_blueprint.get("/issues")
def issues():
    parameters=query(ValidationIssueQuery)
    try:page=current_app.extensions["validation_issue_log_factory"]().listing(parameters)
    except (OSError,ValueError):
        raise AppError("VALIDATION_LOG_UNAVAILABLE","本地验收记录暂时不可读，请检查日志目录权限。",503) from None
    return jsonify(SuccessResponse(data=page,request_id=g.request_id).model_dump(mode="json",by_alias=True))


@validation_blueprint.get("/openapi.json")
def openapi():
    query(ApiSchema)
    return jsonify(validation_openapi())
