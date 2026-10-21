"""入口 Node 只转发白名单公共 API；无本地推理排队、身份库或重试。"""

import json
import re
from threading import Timer
import httpx2 as httpx
from backend.common.exceptions.app_error import AppError

PUBLIC_ROUTES = (
    ("GET",r"services"),("GET",r"openapi.json"),("GET",r"tasks"),
    ("POST",r"tasks/(asr|vision)"),("GET",r"tasks/[a-f0-9]{32}(/(result|events|monitoring))?"),
    ("GET",r"tasks/[a-f0-9]{32}/result/download"),("POST",r"tasks/[a-f0-9]{32}/cancel"),
    ("POST",r"llm/requests"),("GET",r"llm/requests/[a-f0-9]{32}(/(events|monitoring))?"),
    ("POST",r"llm/requests/[a-f0-9]{32}/cancel"),
)


class ForwardedStream:
    def __init__(self, iterator, close):self.iterator,self.finish=iterator,close
    def __iter__(self):return self
    def __next__(self):return next(self.iterator)
    def close(self):
        try:self.iterator.close()
        finally:self.finish()


class NodeIngress:
    def __init__(self,settings,*,transport=None):self.settings,self.transport=settings,transport

    def forward(self,method,path,query,headers,body):
        if not self.settings.central_url:raise AppError("CENTER_UNAVAILABLE","入口 Node 尚未配置中心平台。",503)
        if not any(verb==method and re.fullmatch(pattern,path) for verb,pattern in PUBLIC_ROUTES):
            raise AppError("PUBLIC_ROUTE_NOT_FOUND","公共接口不存在。",404)
        token=headers.get("Authorization","")
        if not re.fullmatch(r"Bearer ipk_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}",token):raise AppError("API_KEY_UNAUTHORIZED","需要应用 API Key。",401)
        forwarded={"Authorization":token,"X-Node-Ingress-Token":self.settings.ingress_token.get_secret_value(),"Accept-Encoding":"identity"}
        for key in ("Content-Type","Idempotency-Key"):
            if headers.get(key):forwarded[key]=headers[key]
        client=httpx.Client(timeout=httpx.Timeout(900,connect=5),trust_env=False,follow_redirects=False,transport=self.transport)
        response=None;streaming=False
        def close_transport():
            try:
                if response is not None:response.close()
                client.close()
            except Exception:pass
        timer=Timer(900,close_transport);timer.daemon=True;timer.start()
        try:
            request=client.build_request(method,self.settings.central_url+"/api/v1/"+path,params=query,headers=forwarded,content=body)
            response=client.send(request,stream=True)
            content_type=response.headers.get("content-type","").split(";")[0]
            if response.headers.get("content-encoding","identity")!="identity":raise ValueError()
            if content_type=="text/event-stream" and response.status_code==200 and method=="POST" and path=="llm/requests":
                streaming=True
                def chunks():
                    total=0
                    try:
                        for chunk in response.iter_raw():
                            total+=len(chunk)
                            if total>2*1024*1024:break
                            yield chunk
                    except Exception:pass  # 普通 EOF 仍由中心/Node 按未知处理，不能补造 done。
                    finally:timer.cancel();close_transport()
                def finish():timer.cancel();close_transport()
                return response.status_code,"text/event-stream",ForwardedStream(chunks(),finish),response.headers.get("X-Online-Request-ID")
            download=method=="GET" and path.endswith("/result/download") and response.status_code==200
            if content_type!="application/json" and not (download and content_type=="text/plain"):raise ValueError()
            raw=bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                raw.extend(chunk)
                if len(raw)>3*1024*1024:raise ValueError()
            # 只转发平台 JSON；不会把代理、网关 HTML 错误体暴露出去。
            if content_type=="application/json":
                value=json.loads(raw)
                if not isinstance(value,dict) or (not download and not any(key in value for key in ("data","error","openapi"))):raise ValueError()
            else:bytes(raw).decode("utf-8",errors="strict")
            return response.status_code,content_type,bytes(raw),None
        except Exception:
            raise AppError("CENTER_UNAVAILABLE","中心暂不可用；提交结果可能未知，请保留幂等键查询或重试，不要更换键重发。",503) from None
        finally:
            if not streaming:timer.cancel();close_transport()
