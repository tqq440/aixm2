"""中心与 Node 间的查询许可协议，不接受客户端自定服务分组。"""

from typing import Literal
from pydantic import field_validator
from backend.schema.common.api_schema import ApiSchema
from backend.schema.host_node.execution_schema import ExecutionCommand, InvocationId


class MonitorPermitCommand(ApiSchema):
    poll_id: InvocationId
    command: ExecutionCommand


class ConfirmQueriesStopped(ApiSchema):
    queries_stopped_confirmed: Literal[True]

    @field_validator("queries_stopped_confirmed", mode="before")
    @classmethod
    def explicit_true(cls, value):
        if value is not True:
            raise ValueError("需要明确确认原查询已停止")
        return value


class MonitorPermitReply(ApiSchema):
    poll_id: InvocationId
    state: Literal["held", "released", "denied"]
    reason: Literal["GLOBAL_QUERY_LIMIT", "SERVICE_QUERY_LIMIT"] | None = None
