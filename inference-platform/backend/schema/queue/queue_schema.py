"""通知只携带不可变身份，不包含输入、凭据或结果。"""

from typing import Literal

from pydantic import Field

from backend.schema.common.api_schema import ApiSchema
from backend.schema.invocations.invocation_schema import CallId
from backend.schema.catalog.service_schema import Revision


class QueueNotice(ApiSchema):
    version: Literal["1"] = "1"
    event_id: CallId
    invocation_id: CallId
    service_revision: Revision
    queue_key: str = Field(pattern=r"^common\.[a-f0-9]{64}$")


class QueueSummary(ApiSchema):
    published: int = Field(default=0, ge=0)
    received: int = Field(default=0, ge=0)
    acknowledged: int = Field(default=0, ge=0)
