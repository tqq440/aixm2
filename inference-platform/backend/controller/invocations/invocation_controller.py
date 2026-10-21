"""模块 2 本机管理；调用受理和执行权不通过此入口对外暴露。"""

from flask import Blueprint, current_app, g, jsonify, request

from backend.common.middleware.local_configuration import authorize_local_configuration
from backend.schema.common.api_schema import ApiSchema, SuccessResponse
from backend.schema.invocations.invocation_schema import ApplicationBudget, CpuProfileRegistration, EventQuery, InvocationPath
from backend.schema.catalog.service_schema import ServicePath
from backend.schema.tasks.task_schema import TaskPath
from backend.schema.llm.online_schema import OnlinePath


invocation_blueprint = Blueprint("common_execution", __name__, url_prefix="/api/admin/common-execution")


@invocation_blueprint.before_request
def guard():
    authorize_local_configuration(request)
    request.max_content_length = 32 * 1024
    if request.method != "GET":
        ApiSchema.model_validate(request.args.to_dict(flat=False))


def _service():
    return current_app.extensions["invocation_service"]


def _response(data):
    response = jsonify(SuccessResponse(data=data, request_id=g.request_id).model_dump(mode="json", by_alias=True))
    response.headers["Cache-Control"] = "no-store"
    return response


@invocation_blueprint.put("/applications/<application_id>")
def application(application_id):
    command = ApplicationBudget.model_validate(request.get_json())
    ServicePath(service_id=application_id)
    if command.application_id != application_id:
        from backend.common.exceptions.app_error import AppError
        raise AppError("APPLICATION_ID_MISMATCH", "路径与应用编号不一致。", 422)
    return _response(_service().configure_application(command))


@invocation_blueprint.post("/cpu-profiles")
def cpu_profile():
    return _response(_service().register_cpu_profile(CpuProfileRegistration.model_validate(request.get_json())))


@invocation_blueprint.get("/invocations/<invocation_id>")
def invocation(invocation_id):
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    return _response(_service().get(InvocationPath(invocation_id=invocation_id).invocation_id))


@invocation_blueprint.get("/capacity")
def capacity():
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    return _response(_service().capacity())


@invocation_blueprint.get("/queue")
def queue_status():
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    return _response(current_app.extensions["queue_service"].status())


@invocation_blueprint.get("/invocations/<invocation_id>/monitoring")
def invocation_monitoring(invocation_id):
    ApiSchema.model_validate(request.args.to_dict(flat=False))
    identifier=InvocationPath(invocation_id=invocation_id).invocation_id
    return _response(current_app.extensions["node_invocation_bridge"].monitoring(identifier))


@invocation_blueprint.post("/invocations/<invocation_id>/reconcile")
def invocation_reconcile(invocation_id):
    ApiSchema.model_validate(request.get_json())
    identifier=InvocationPath(invocation_id=invocation_id).invocation_id
    return _response(current_app.extensions["node_invocation_bridge"].reconcile(identifier))


@invocation_blueprint.post("/invocations/<invocation_id>/cancel-remote")
def invocation_cancel_remote(invocation_id):
    ApiSchema.model_validate(request.get_json())
    identifier=InvocationPath(invocation_id=invocation_id).invocation_id
    return _response(current_app.extensions["node_invocation_bridge"].reconcile(identifier,cancel=True))


@invocation_blueprint.get("/applications/<application_id>/tasks/<task_id>/events")
def task_events(application_id, task_id):
    ServicePath(service_id=application_id)
    TaskPath(task_id=task_id)
    query = EventQuery.model_validate({key: values[0] if len(values) == 1 else values for key, values in request.args.lists()})
    return _response(_service().events("task", task_id, query, application_id=application_id))


@invocation_blueprint.get("/applications/<application_id>/online/<online_request_id>/events")
def online_events(application_id, online_request_id):
    ServicePath(service_id=application_id)
    OnlinePath(online_request_id=online_request_id)
    query = EventQuery.model_validate({key: values[0] if len(values) == 1 else values for key, values in request.args.lists()})
    return _response(_service().events("online", online_request_id, query, application_id=application_id))
