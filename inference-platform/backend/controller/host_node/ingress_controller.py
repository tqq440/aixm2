"""外部只访问 Node；管理/内部执行不能通过此代理。"""

from flask import Blueprint,Response,current_app,request,stream_with_context
from backend.common.exceptions.app_error import AppError

ingress_blueprint=Blueprint("node_public_ingress",__name__,url_prefix="/api/v1")


@ingress_blueprint.route("/<path:path>",methods=["GET","POST"])
def ingress(path):
    request.max_content_length=20*1024*1024+64*1024
    if len(request.query_string)>2048:raise AppError("QUERY_TOO_LARGE","查询参数过长。",413)
    status,kind,payload,identifier=current_app.extensions["node_ingress"].forward(request.method,path,list(request.args.items(multi=True)),request.headers,request.get_data())
    if kind=="text/event-stream":
        response=Response(stream_with_context(payload),status=status,mimetype=kind,headers={"X-Accel-Buffering":"no","X-Online-Request-ID":identifier or ""})
        response.call_on_close(payload.close)
        return response
    headers={"Content-Disposition":"attachment; filename=\"result.txt\"" if kind=="text/plain" else "attachment; filename=\"result.json\""} if path.endswith("/result/download") and status==200 else {}
    return Response(payload,status=status,mimetype=kind,headers=headers)
