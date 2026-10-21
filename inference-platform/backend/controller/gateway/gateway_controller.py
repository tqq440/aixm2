"""中心应用鉴权、公共任务 API 和本机管理。"""

import hmac
import json
from flask import Blueprint, Response, current_app, g, jsonify, request, stream_with_context
from backend.common.exceptions.app_error import AppError
from backend.common.middleware.local_configuration import authorize_local_configuration
from backend.schema.common.api_schema import ApiSchema, SuccessResponse
from backend.schema.catalog.service_schema import ServicePath
from backend.schema.gateway.gateway_schema import KeyCommand, PublicPage, SubmitFile, SubmitHeaders, SubmitOnline, DownloadQuery
from backend.schema.invocations.invocation_schema import ApplicationBudget, EventQuery, InvocationPath

public_blueprint=Blueprint("gateway_public",__name__,url_prefix="/api/v1")
gateway_admin_blueprint=Blueprint("gateway_admin",__name__,url_prefix="/api/admin/gateway")


def response(data,status=200):
    return jsonify(SuccessResponse(data=data,request_id=g.request_id).model_dump(mode="json",by_alias=True)),status


def query(model):
    return model.model_validate({key:values[0] if len(values)==1 else values for key,values in request.args.lists()})


def requests_service():return current_app.extensions["gateway_requests"]


def protect_legacy():
    if current_app.config["APP_SETTINGS"].public_api_enabled and request.path.startswith("/api/") and not request.path.startswith("/api/v1/"):
        authorize_local_configuration(request,require_json=False)


@public_blueprint.before_request
def authorize_public():
    settings=current_app.config["APP_SETTINGS"]
    if not settings.public_api_enabled:raise AppError("PUBLIC_API_DISABLED","公共 API 未启用。",503)
    if not hmac.compare_digest(request.headers.get("X-Node-Ingress-Token","").encode(),settings.ingress_token.get_secret_value().encode()):
        raise AppError("INGRESS_REQUIRED","需要通过已配置的入口 Node 调用。",401)
    g.application_id=current_app.extensions["gateway_keys"].authenticate(request.headers.get("Authorization"))
    if request.method!="GET":query(ApiSchema)


@gateway_admin_blueprint.before_request
def authorize_admin():
    authorize_local_configuration(request)
    request.max_content_length=32*1024
    if request.method!="GET":query(ApiSchema)


@public_blueprint.get("/services")
def services():
    query(ApiSchema)
    return response(requests_service().services(g.application_id))


@public_blueprint.get("/openapi.json")
def public_openapi():
    query(ApiSchema)
    from backend.service.gateway.openapi_service import gateway_openapi
    return jsonify(gateway_openapi())


@public_blueprint.post("/tasks/<kind>")
def submit(kind):
    if kind not in {"asr","vision"}:raise AppError("TASK_KIND_INVALID","仅支持 ASR 和 vision 异步提交。",404)
    header=SubmitHeaders(idempotency_key=request.headers.get("Idempotency-Key"))
    if request.mimetype!="multipart/form-data":raise AppError("MULTIPART_REQUIRED","请使用 multipart/form-data。",415)
    if set(request.files)!={"file"} or len(request.files.getlist("file"))!=1:raise AppError("FILE_REQUIRED","需要且只能提交一个 file。",422)
    if any(len(values)!=1 for _,values in request.form.lists()):raise AppError("DUPLICATE_FIELD","请求字段不能重复。",422)
    command=SubmitFile.model_validate(request.form.to_dict())
    if kind=="vision" and command.language is not None:raise AppError("LANGUAGE_NOT_APPLICABLE","图片任务不使用 language。",422)
    return response(requests_service().submit_file(g.application_id,kind,command,header.idempotency_key,request.files["file"].stream),202)


@public_blueprint.get("/tasks")
def tasks():return response(requests_service().listing(g.application_id,query(PublicPage)))


@public_blueprint.get("/tasks/<identifier>")
def task(identifier):
    query(ApiSchema);InvocationPath(invocation_id=identifier)
    return response(requests_service().get(g.application_id,identifier))


@public_blueprint.get("/tasks/<identifier>/result")
def result(identifier):
    query(ApiSchema);InvocationPath(invocation_id=identifier)
    return response(requests_service().result(g.application_id,identifier))


@public_blueprint.get("/tasks/<identifier>/result/download")
def download(identifier):
    InvocationPath(invocation_id=identifier);parameters=query(DownloadQuery)
    value=requests_service().result(g.application_id,identifier)["result"]
    if parameters.format=="txt":
        if "text" not in value:raise AppError("FORMAT_UNAVAILABLE","检测结果请使用JSON下载。",422)
        payload=value["text"];mime="text/plain"
    else:payload=json.dumps(value,ensure_ascii=False);mime="application/json"
    return Response(payload,mimetype=mime,headers={"Content-Disposition":f'attachment; filename="{identifier}.{parameters.format}"'})


def resource(identifier,kind,part):
    InvocationPath(invocation_id=identifier)
    if part=="events":return response(requests_service().events(g.application_id,identifier,query(EventQuery),kind))
    query(ApiSchema)
    return response(requests_service().monitoring(g.application_id,identifier,kind))


@public_blueprint.get("/tasks/<identifier>/events")
def events(identifier):return resource(identifier,"task","events")


@public_blueprint.get("/tasks/<identifier>/monitoring")
def monitoring(identifier):return resource(identifier,"task","monitoring")


@public_blueprint.post("/tasks/<identifier>/cancel")
def cancel(identifier):
    InvocationPath(invocation_id=identifier);ApiSchema.model_validate(request.get_json())
    return response(requests_service().cancel(g.application_id,identifier))


@public_blueprint.post("/llm/requests")
def llm():
    request.max_content_length=192*1024
    key=SubmitHeaders(idempotency_key=request.headers.get("Idempotency-Key"))
    command=SubmitOnline.model_validate(request.get_json())
    identifier,source,existing=requests_service().online(g.application_id,command,key.idempotency_key)
    if source is None:return response({"request":existing,"streamReplayed":False})
    service=requests_service();application=g.application_id
    def stream():
        try:
            accepted=service.get(application,identifier,"online")
            yield "event: accepted\ndata: "+json.dumps(accepted,default=str)+"\n\n"
            for event in source:
                data={"requestId":identifier,"text":event["data"]["text"]} if event["event"]=="delta" else service.get(application,identifier,"online")
                yield "event: "+event["event"]+"\ndata: "+json.dumps(data,ensure_ascii=False,default=str)+"\n\n"
        finally:
            try:source.close()
            finally:service.disconnect_online(application,identifier)
    return Response(stream_with_context(stream()),mimetype="text/event-stream",headers={"X-Accel-Buffering":"no","X-Online-Request-ID":identifier})


@public_blueprint.get("/llm/requests/<identifier>")
def online(identifier):
    query(ApiSchema);InvocationPath(invocation_id=identifier)
    return response(requests_service().get(g.application_id,identifier,"online"))


@public_blueprint.post("/llm/requests/<identifier>/cancel")
def online_cancel(identifier):
    InvocationPath(invocation_id=identifier);ApiSchema.model_validate(request.get_json())
    return response(requests_service().cancel(g.application_id,identifier,"online"))


@public_blueprint.get("/llm/requests/<identifier>/events")
def online_events(identifier):return resource(identifier,"online","events")


@public_blueprint.get("/llm/requests/<identifier>/monitoring")
def online_monitoring(identifier):return resource(identifier,"online","monitoring")


@gateway_admin_blueprint.get("/applications")
def applications():
    query(ApiSchema)
    return response(current_app.extensions["gateway_keys"].applications())


@gateway_admin_blueprint.get("/status")
def gateway_status():
    query(ApiSchema)
    settings=current_app.config["APP_SETTINGS"]
    available=settings.common_execution_enabled
    if available:
        from sqlalchemy import text
        with current_app.extensions["database"].engine.connect() as connection:
            available=connection.scalar(text("SELECT version_num FROM alembic_version"))=="20261021_0020"
    notice="应用通过入口Node携带Key调用；管理页面仅本机开放。" if available else (
        "公共管理资料尚未升级，请先按说明备份和迁移。" if settings.common_execution_enabled else "公共调用与应用管理尚未启用。")
    return response({"enabled":settings.public_api_enabled,"managementAvailable":available,"notice":notice})


@gateway_admin_blueprint.get("/nodes")
def gateway_nodes():
    query(ApiSchema)
    return response(current_app.extensions["gateway_administration"].nodes())


@gateway_admin_blueprint.post("/nodes/refresh")
def gateway_nodes_refresh():
    ApiSchema.model_validate(request.get_json())
    return response(current_app.extensions["gateway_administration"].refresh())


@gateway_admin_blueprint.put("/applications/<identifier>")
def save_application(identifier):
    ServicePath(service_id=identifier);command=ApplicationBudget.model_validate(request.get_json())
    if identifier=="local-user" or command.application_id!=identifier:raise AppError("APPLICATION_ID_INVALID","请登记独立且一致的应用编号。",422)
    return response(current_app.extensions["invocation_service"].configure_application(command))


@gateway_admin_blueprint.post("/applications/<identifier>/keys")
def issue_key(identifier):
    ServicePath(service_id=identifier)
    return response(current_app.extensions["gateway_keys"].issue(identifier,KeyCommand.model_validate(request.get_json())),201)


@gateway_admin_blueprint.post("/applications/<identifier>/keys/<key_id>/<action>")
def change_key(identifier,key_id,action):
    ServicePath(service_id=identifier);InvocationPath(invocation_id=key_id)
    service=current_app.extensions["gateway_keys"]
    if action=="rotate":return response(service.issue(identifier,KeyCommand.model_validate(request.get_json()),replace=key_id),201)
    if action=="revoke":
        ApiSchema.model_validate(request.get_json())
        return response(service.revoke(identifier,key_id))
    raise AppError("HTTP_404","接口不存在。",404)


@gateway_admin_blueprint.get("/requests")
def admin_requests():return response(requests_service().listing(None,query(PublicPage),admin=True))


@gateway_admin_blueprint.get("/requests/<identifier>/<part>")
def admin_request(identifier,part):
    from backend.data.invocations.invocation_models import InferenceInvocation
    InvocationPath(invocation_id=identifier)
    core=current_app.extensions["invocation_service"]
    with core._transaction() as session:
        row=session.get(InferenceInvocation,identifier)
        if row is None:raise AppError("REQUEST_NOT_FOUND","请求不存在。",404)
        app,owner,kind=row.application_id,row.task_id or row.online_request_id,"task" if row.task_id else "online"
    if part=="monitoring":
        query(ApiSchema)
        return response(requests_service().clean_monitor(current_app.extensions["node_invocation_bridge"].monitoring(identifier)))
    if part=="events":return response(core.events(kind,owner,query(EventQuery),application_id=app))
    raise AppError("HTTP_404","接口不存在。",404)
