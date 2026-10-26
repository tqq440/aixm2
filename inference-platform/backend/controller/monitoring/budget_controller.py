"""独立内部查询凭据，公共 Key 和入口凭据不能申请查询许可。"""

import hmac
from flask import Blueprint, current_app, g, jsonify, request
from backend.common.exceptions.app_error import AppError
from backend.schema.common.api_schema import SuccessResponse
from backend.schema.monitoring.budget_schema import MonitorPermitCommand

monitor_budget_blueprint = Blueprint("monitor_budget", __name__, url_prefix="/internal/v1/monitor-budget")


@monitor_budget_blueprint.before_request
def authorize():
    token = current_app.config["APP_SETTINGS"].monitor_token
    if token is None:
        raise AppError("MONITOR_BUDGET_DISABLED", "中心查询预算未启用。", 503)
    if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), ("Bearer " + token.get_secret_value()).encode()):
        raise AppError("MONITOR_BUDGET_UNAUTHORIZED", "需要独立查询预算凭据。", 401)
    request.max_content_length = 16_384
    if request.args:
        raise AppError("MONITOR_QUERY_INVALID", "内部许可接口不接受查询参数。", 422)


@monitor_budget_blueprint.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@monitor_budget_blueprint.post("/<action>")
def change(action):
    if action not in {"acquire", "release"}:
        raise AppError("MONITOR_ACTION_INVALID", "未提供该操作。", 404)
    command = MonitorPermitCommand.model_validate(request.get_json())
    reply = current_app.extensions["monitor_budget"].change(command, release=action == "release")
    return jsonify(SuccessResponse(data=reply, request_id=g.request_id).model_dump(mode="json", by_alias=True))


@monitor_budget_blueprint.get("/openapi.json")
def openapi():
    from backend.service.monitoring.openapi_service import monitor_budget_openapi
    return jsonify(monitor_budget_openapi())
