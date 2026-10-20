"""只访问回环地址的端口和 HTTP 探测。"""

import json
import socket
import urllib.error
import urllib.request

def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def local_http(port: int, path: str, *, json_response: bool = True):
    """忽略系统代理，限定回环；小超时避免停止命令被探测阻塞。"""
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=0.5) as response:
            if response.status != 200:
                return None
            content = response.read(1024 * 1024)
            return json.loads(content) if json_response else bool(content)
    except (OSError, ValueError, urllib.error.URLError):
        return None
