"""JSON 入参校验与 SSE 编码入口；业务生命周期交给 Service。"""

from flask import Blueprint, Response, current_app, g, jsonify, request

from backend.common.exceptions.app_error import AppError
from backend.schema.common.api_schema import SuccessResponse
from backend.schema.llm.online_schema import OnlineListQuery, OnlinePath, OnlineRequestInput

online_blueprint = Blueprint("online", __name__, url_prefix="/api/llm")


def _service():
    return current_app.extensions["online_service"]


def _response(data):
    return jsonify(SuccessResponse(data=data, request_id=g.request_id).model_dump(mode="json", by_alias=True))


def _query():
    return {key: values[0] if len(values) == 1 else values for key, values in request.args.lists()}


def _no_query():
    if request.args:
        raise AppError("LLM_INVALID_REQUEST", "此接口不接受查询参数。", 422)


@online_blueprint.get("/status")
def status():
    _no_query()
    return _response(_service().status())


@online_blueprint.get("/requests")
def list_requests():
    return _response(_service().list_requests(OnlineListQuery.model_validate(_query())))


@online_blueprint.get("/requests/<online_request_id>")
def get_request(online_request_id):
    _no_query()
    path = OnlinePath(online_request_id=online_request_id)
    return _response(_service().get(path.online_request_id))


@online_blueprint.post("/requests")
def create_request():
    _no_query()
    request.max_content_length = 128 * 1024
    body = OnlineRequestInput.model_validate(request.get_json())
    service = _service()
    run = service.open(body)
    response = Response(service.events(run, g.request_id), content_type="text/event-stream; charset=utf-8",
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    response.call_on_close(lambda: service.disconnect(run))
    return response


@online_blueprint.post("/requests/<online_request_id>/cancel")
def cancel_request(online_request_id):
    _no_query()
    path = OnlinePath(online_request_id=online_request_id)
    request.max_content_length = 1024
    if request.get_data() and (not request.is_json or request.get_json() != {}):
        raise AppError("LLM_INVALID_REQUEST", "取消接口仅接受空请求体或空 JSON 对象。", 422)
    return _response(_service().cancel(path.online_request_id))
