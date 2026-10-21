"""job-json-v1 参考协议，所有可信查询必须关联同一次调用。"""

from typing import Literal
from pydantic import Field

from backend.schema.common.api_schema import ApiSchema
from backend.schema.host_node.execution_schema import InvocationId, Result
from backend.schema.host_node.node_schema import Revision
from backend.schema.host_node.upstream_schema import ActualDevice


class JobIdentity(ApiSchema):
    invocation_id: InvocationId
    attempt: int = Field(ge=1, strict=True)
    service_revision: Revision
    config_revision: Revision
    job_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


class JobStatus(JobIdentity):
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    progress: float | None = Field(default=None, ge=0, le=100, strict=True, allow_inf_nan=False)
    stage: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    queue_position: int | None = Field(default=None, ge=0, strict=True)
    actual_device: ActualDevice | None = None
    result: Result | None = None


class JobCancellation(JobIdentity):
    accepted: Literal[True]
