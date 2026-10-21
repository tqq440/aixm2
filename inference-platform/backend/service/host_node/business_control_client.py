"""有界业务排空/恢复命令；ACK 不代替后续关联的业务状态报告。"""

import httpx2 as httpx
from time import monotonic

from backend.common.containers.docker_engine import DockerFailure
from backend.schema.host_node.control_schema import ControlAcknowledgement


class BusinessControlClient:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport

    def send(self, config, runtime, action, command):
        path = runtime["drainPath" if action == "drain" else "resumePath"]
        if not path or not config.get("internalBaseUrl"):
            raise DockerFailure("BUSINESS_CONTROL_UNSUPPORTED")
        token = self.settings.report_tokens.get(config.get("reportTokenEnv"))
        if token is None:
            raise DockerFailure("BUSINESS_CREDENTIAL_MISSING")
        try:
            started = monotonic()
            with httpx.Client(timeout=self.settings.control_timeout_seconds, follow_redirects=False,
                              trust_env=False, transport=self.transport) as client:
                with client.stream("POST", config["internalBaseUrl"] + path, json=command,
                    headers={"Authorization": "Bearer " + token.get_secret_value(), "Accept-Encoding": "identity"}) as response:
                    if response.status_code != 200:
                        raise DockerFailure("BUSINESS_CONTROL_REJECTED")
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise DockerFailure("BUSINESS_CONTROL_ENCODING_UNSUPPORTED")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if monotonic() - started > self.settings.control_timeout_seconds:
                            raise DockerFailure("BUSINESS_CONTROL_UNKNOWN")
                        data.extend(chunk)
                        if len(data) > 16384:
                            raise DockerFailure("BUSINESS_CONTROL_TOO_LARGE")
            ack = ControlAcknowledgement.model_validate_json(data)
            if not ack.accepted or any(str(getattr(ack, key)) != str(command[alias]) for key, alias in
                (("operation_id", "operationId"), ("instance_id", "instanceId"),
                 ("config_revision", "configRevision"), ("action", "action"))):
                raise DockerFailure("BUSINESS_CONTROL_MISMATCH")
        except (httpx.HTTPError, ValueError, TypeError):
            raise DockerFailure("BUSINESS_CONTROL_UNKNOWN") from None
