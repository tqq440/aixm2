"""本机配置接口仅接受本机页面或显式 JSON 客户端，不允许跨站网页探测服务。"""

from urllib.parse import urlsplit

from backend.common.exceptions.app_error import AppError

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def authorize_local_configuration(request, *, require_json=True):
    try:
        host = urlsplit("http://" + request.host).hostname
        origin = request.headers.get("Origin")
        parsed = urlsplit(origin) if origin else None
        valid_origin = (parsed is None or (parsed.scheme in {"http", "https"} and
                        parsed.hostname in LOCAL_HOSTS and not parsed.username and not parsed.password
                        and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment))
    except ValueError:
        valid_origin, host = False, None
    if (request.remote_addr not in LOCAL_HOSTS or host not in LOCAL_HOSTS or not valid_origin
            or request.headers.get("Sec-Fetch-Site") == "cross-site"):
        raise AppError("LOCAL_CONFIGURATION_ONLY", "服务配置仅允许从本机页面或本机客户端操作。", 403)
    if require_json and request.method != "GET" and not request.is_json:
        raise AppError("JSON_REQUIRED", "服务配置操作需要 application/json 请求。", 415)
