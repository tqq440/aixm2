"""Node 管理、快照及 4C1 中心执行身份边界；无公共应用执行入口。"""

from flask import Blueprint, Response, current_app, g, jsonify, request, stream_with_context
import json
from backend.common.exceptions.app_error import AppError
from backend.schema.common.api_schema import ApiSchema, SuccessResponse
from backend.schema.host_node.node_schema import (
    ConfigurationView, EventPage, NodeEventQuery, NodeListQuery, NodeServiceReport, NodeStatus,
    OpenNodeInstance, ReportReceipt, SaveNodeService, ServicePage, ServiceSnapshot,
)
from backend.schema.host_node.control_schema import (
    AbortControlCommand, ControlCommand, InstanceAssignment, OperationPage, OperationView,
    ReconcileCommand, ResourceView, RuntimeView, SaveRuntimeConfig,
)
from backend.service.host_node.openapi_service import node_openapi
from backend.schema.host_node.execution_schema import ExecutionAvailability, ExecutionCommand, ExecutionReceipt
from backend.schema.host_node.execution_schema import StreamExecutionInput
from backend.schema.host_node.upstream_schema import ExecutionManifest
from backend.service.host_node.input_service import ExecutionMaterial


node_blueprint = Blueprint("host_node", __name__)


def _service(*, admin=False):
    service = current_app.extensions["host_node_service"]
    service.authorize(request.headers.get("Authorization"), admin=admin)
    return service


def _query(schema):
    return schema.model_validate({key: values[0] if len(values) == 1 else values for key, values in request.args.lists()})


def _response(value, model, status=200):
    return jsonify(SuccessResponse(data=model.model_validate(value), request_id=g.request_id).model_dump(mode="json", by_alias=True)), status


@node_blueprint.get("/node/v1/status")
def status():
    service = _service()
    _query(ApiSchema)
    return _response(service.status(), NodeStatus)


@node_blueprint.get("/node/v1/services")
def services():
    service = _service()
    return _response(service.list_services(_query(NodeListQuery)), ServicePage)


@node_blueprint.get("/node/v1/services/<service_id>")
def detail(service_id):
    service = _service()
    _query(ApiSchema)
    return _response(service.get_service(service_id), ServiceSnapshot)


@node_blueprint.get("/node/v1/events")
def node_events():
    service = _service()
    return _response(service.events(_query(NodeEventQuery)), EventPage)


@node_blueprint.get("/node/v1/services/<service_id>/events")
def service_events(service_id):
    service = _service()
    return _response(service.events(_query(NodeEventQuery), service_id=service_id), EventPage)


@node_blueprint.get("/node/v1/admin/services/<service_id>")
def configuration(service_id):
    service = _service(admin=True)
    _query(ApiSchema)
    return _response(service.get_service(service_id, configuration=True), ConfigurationView)


@node_blueprint.put("/node/v1/admin/services/<service_id>")
def configure(service_id):
    service = _service(admin=True)
    _query(ApiSchema)
    return _response(service.save_service(service_id, SaveNodeService.model_validate(request.get_json())), ServiceSnapshot)


@node_blueprint.post("/node/v1/admin/services/<service_id>/instances")
def bind_instance(service_id):
    service = _service(admin=True)
    _query(ApiSchema)
    return _response(service.open_instance(service_id, OpenNodeInstance.model_validate(request.get_json())), ServiceSnapshot, 201)


@node_blueprint.post("/node/internal/v1/services/<service_id>/reports")
def report(service_id):
    service = current_app.extensions["host_node_service"]
    service.authorize_report(service_id, request.headers.get("Authorization"))
    _query(ApiSchema)
    return _response(service.report(service_id, NodeServiceReport.model_validate(request.get_json())), ReportReceipt)


@node_blueprint.get("/node/v1/openapi.json")
def openapi():
    _service()
    _query(ApiSchema)
    return jsonify(node_openapi())


@node_blueprint.get("/node/v1/resources")
def resources():
    _service()
    _query(ApiSchema)
    return _response(current_app.extensions["host_node_resources"].snapshot(), ResourceView)


@node_blueprint.route("/node/v1/admin/services/<service_id>/runtime", methods=["GET", "PUT"])
def runtime(service_id):
    _service(admin=True)
    _query(ApiSchema)
    service = current_app.extensions["host_node_control"]
    if request.method == "GET":
        return _response(service.runtime(service_id), RuntimeView)
    return _response(service.save_runtime(service_id, SaveRuntimeConfig.model_validate(request.get_json())), RuntimeView)


@node_blueprint.route("/node/v1/services/<service_id>/operations", methods=["GET", "POST"])
def operations(service_id):
    _service(admin=request.method == "POST")
    service = current_app.extensions["host_node_control"]
    if request.method == "GET":
        return _response(service.operations(service_id, _query(NodeListQuery)), OperationPage)
    _query(ApiSchema)
    return _response(service.submit(service_id, ControlCommand.model_validate(request.get_json())), OperationView, 202)


@node_blueprint.get("/node/v1/operations/<operation_id>")
def operation(operation_id):
    _service()
    _query(ApiSchema)
    return _response(current_app.extensions["host_node_control"].get_operation(operation_id), OperationView)


@node_blueprint.post("/node/v1/operations/<operation_id>/reconcile")
def reconcile(operation_id):
    _service(admin=True)
    _query(ApiSchema)
    return _response(current_app.extensions["host_node_control"].reconcile(operation_id,
        ReconcileCommand.model_validate(request.get_json())), OperationView)


@node_blueprint.post("/node/v1/operations/<operation_id>/abort")
def abort_control(operation_id):
    _service(admin=True)
    _query(ApiSchema)
    return _response(current_app.extensions["host_node_control"].abort(operation_id,
        AbortControlCommand.model_validate(request.get_json())), OperationView)


@node_blueprint.get("/node/internal/v1/services/<service_id>/assignment")
def assignment(service_id):
    service = current_app.extensions["host_node_service"]
    service.authorize_report(service_id, request.headers.get("Authorization"))
    _query(ApiSchema)
    return _response(current_app.extensions["host_node_control"].assignment(service_id), InstanceAssignment)


def _executions():
    service = current_app.extensions["host_node_executions"]
    service.authorize(request.headers.get("Authorization"))
    _query(ApiSchema)
    return service


@node_blueprint.get("/node/internal/v1/services/<service_id>/execution-availability")
def execution_availability(service_id):
    return _response(_executions().availability(service_id), ExecutionAvailability)


@node_blueprint.post("/node/internal/v1/executions")
def execute():
    return _response(_executions().execute(ExecutionCommand.model_validate(request.get_json())), ExecutionReceipt)


@node_blueprint.get("/node/internal/v1/executions/<invocation_id>")
def execution(invocation_id):
    return _response(_executions().get(invocation_id), ExecutionReceipt)


@node_blueprint.get("/node/internal/v1/executions/<invocation_id>/result")
def execution_result(invocation_id):
    return _response(_executions().get(invocation_id, include_result=True), ExecutionReceipt)


@node_blueprint.post("/node/internal/v1/executions/file")
def execute_file():
    service=_executions()
    request.max_content_length=20*1024*1024+65536
    request.max_form_memory_size=131072
    request.max_form_parts=3
    if request.mimetype!="multipart/form-data":raise AppError("NODE_MULTIPART_REQUIRED","需要 multipart 文件请求。",415)
    if set(request.form)!={"command","manifest"} or set(request.files)!={"file"} or any(
            len(request.form.getlist(key))!=1 for key in request.form) or len(request.files.getlist("file"))!=1:
        raise AppError("NODE_FILE_FIELDS_INVALID","文件请求字段或数量不符。",422)
    if any(len(request.form[key].encode("utf-8"))>16384 for key in request.form):raise AppError("NODE_METADATA_TOO_LARGE","输入元数据过大。",413)
    command=ExecutionCommand.model_validate_json(request.form["command"])
    manifest=ExecutionManifest.model_validate_json(request.form["manifest"])
    content=request.files["file"].stream.read(manifest.size_bytes+1)
    return _response(service.execute(command,ExecutionMaterial(manifest,content)),ExecutionReceipt)


@node_blueprint.post("/node/internal/v1/executions/stream")
def execute_stream():
    service=_executions();request.max_content_length=196608
    payload=StreamExecutionInput.model_validate(request.get_json())
    events=service.stream(payload.command,ExecutionMaterial(online=payload.request))
    def generate():
        try:
            for item in events:yield "event: "+item["event"]+"\ndata: "+json.dumps(item["data"],ensure_ascii=False)+"\n\n"
        finally:events.close()
    return Response(stream_with_context(generate()),mimetype="text/event-stream",headers={"X-Accel-Buffering":"no"})


@node_blueprint.get("/node/internal/v1/executions/<invocation_id>/monitoring")
def execution_monitoring(invocation_id):
    return _response(_executions().get(invocation_id),ExecutionReceipt)


@node_blueprint.post("/node/internal/v1/executions/<invocation_id>/reconcile")
def reconcile_execution(invocation_id):
    _executions();ApiSchema.model_validate(request.get_json())
    return _response(current_app.extensions["host_node_monitor"].reconcile(invocation_id),ExecutionReceipt)


@node_blueprint.post("/node/internal/v1/executions/<invocation_id>/cancel")
def cancel_execution(invocation_id):
    _executions();ApiSchema.model_validate(request.get_json())
    return _response(current_app.extensions["host_node_monitor"].cancel(invocation_id),ExecutionReceipt)
