"""Node 到中心的有界查询许可客户端；不重试、不回退到本地名额。"""

from backend.common.exceptions.app_error import AppError
from backend.common.http.bounded_client import request_bytes
from backend.schema.common.api_schema import SuccessResponse
from backend.schema.monitoring.budget_schema import MonitorPermitReply


class MonitorBudgetClient:
    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport

    def change(self, command, *, release=False):
        if not self.settings.monitor_central_url or not self.settings.monitor_token:
            raise AppError("MONITOR_BUDGET_UNAVAILABLE", "未配置中心查询预算，暂停新查询。", 503)
        try:
            reply = request_bytes("POST", self.settings.monitor_central_url + "/internal/v1/monitor-budget/" +
                ("release" if release else "acquire"), token=self.settings.monitor_token.get_secret_value(),
                timeout=3, maximum=8192, transport=self.transport,
                json=command.model_dump(mode="json", by_alias=True))
            value = SuccessResponse[MonitorPermitReply].model_validate_json(reply.body).data
            if value.poll_id != command.poll_id or (release and value.state != "released"):
                raise ValueError()
            return value
        except Exception:
            raise AppError("MONITOR_BUDGET_UNAVAILABLE", "中心查询许可暂不可确认；不绕过名额限制。", 503) from None
