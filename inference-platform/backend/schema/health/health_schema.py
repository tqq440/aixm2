from typing import Literal

from backend.schema.common.api_schema import ApiSchema


class HealthQuery(ApiSchema):
    """此接口不接受查询参数；意外参数会明确返回校验错误。"""


class HealthStatus(ApiSchema):
    status: Literal["ok"] = "ok"
    database: Literal["ok"] = "ok"
    service: Literal["inference-platform"] = "inference-platform"
