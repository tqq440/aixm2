"""仅本机 Docker Unix socket 的固定 API；不拉镜像、不创建/删除容器、不强杀。"""

from http.client import HTTPConnection, HTTPException
import json
import re
import socket
import sys


class DockerFailure(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class UnixConnection(HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class DockerEngine:
    def __init__(self, settings, *, connection_factory=None):
        self.settings = settings
        self.connection_factory = connection_factory or UnixConnection

    def _request(self, method, container_id, suffix, *, json_response=False):
        if not self.settings.docker_enabled:
            raise DockerFailure("DOCKER_DISABLED")
        if self.connection_factory is UnixConnection and sys.platform != "linux":
            raise DockerFailure("DOCKER_LINUX_REQUIRED")
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise DockerFailure("DOCKER_ID_INVALID")
        connection = self.connection_factory(self.settings.docker_socket, self.settings.control_timeout_seconds)
        try:
            connection.request(method, f"/v1.47/containers/{container_id}/{suffix}", body=None,
                               headers={"Content-Length": "0"})
            response = connection.getresponse()
            body = response.read(65537)
            if len(body) > 65536:
                raise DockerFailure("DOCKER_RESPONSE_TOO_LARGE")
            if response.status not in ({200} if json_response else {204, 304}):
                raise DockerFailure("DOCKER_NOT_FOUND" if response.status == 404 else "DOCKER_API_ERROR")
            return json.loads(body) if json_response else None
        except (OSError, ValueError, TypeError, HTTPException):
            raise DockerFailure("DOCKER_QUERY_FAILED" if method == "GET" else "DOCKER_ACTION_UNKNOWN") from None
        finally:
            connection.close()

    def inspect(self, container_id):
        raw = self._request("GET", container_id, "json", json_response=True)
        try:
            state, labels = raw["State"], raw["Config"]["Labels"]
            if raw["Id"] != container_id or not isinstance(state["Running"], bool) or not isinstance(labels, dict):
                raise ValueError()
            status = state["Status"]
            if status not in {"created", "running", "paused", "restarting", "removing", "exited", "dead"}:
                raise ValueError()
            if (status == "running") != state["Running"] and status not in {"paused", "restarting"}:
                raise ValueError()
            return {"containerId": raw["Id"], "imageId": raw["Image"], "state": status,
                    "restartPolicy": raw["HostConfig"]["RestartPolicy"]["Name"],
                    "autoRemove": raw["HostConfig"].get("AutoRemove", False),
                    "running": state["Running"], "labels": {key: labels.get(key) for key in
                        ("inference.node_id", "inference.service_id", "inference.version_id")}}
        except (KeyError, TypeError, ValueError):
            raise DockerFailure("DOCKER_RESPONSE_INVALID") from None

    def start(self, container_id):
        self._request("POST", container_id, "start")

    def terminate(self, container_id):
        # 只发 SIGTERM 并独立等待退出，避免 Docker stop 超时后升级为 SIGKILL。
        self._request("POST", container_id, "kill?signal=SIGTERM")
