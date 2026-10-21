"""租约令牌仅交执行器；查询响应不公开令牌、连接或正文。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StrictBool

from backend.schema.asr.device_policy_schema import CpuPerformanceProfile
from backend.schema.catalog.service_schema import Identifier, Revision
from backend.schema.common.api_schema import ApiSchema


CallId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class ApplicationBudget(ApiSchema):
    application_id: Identifier
    concurrency_limit: int = Field(default=1, strict=True, ge=1, le=64)
    enabled: StrictBool = True
    allowed_services: list[Identifier] = Field(min_length=1, max_length=64)
    expected_version: int | None = Field(default=None, strict=True, ge=1)


class InvocationBinding(ApiSchema):
    application_id: Identifier
    service_id: Identifier
    owner_id: CallId
    owner_kind: Literal["task", "online"]


class InvocationLease(ApiSchema):
    invocation_id: CallId
    lease_token: CallId
    worker_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    attempt: int = Field(strict=True, ge=1)
    service_revision: Revision
    target_id: Identifier
    deadline_at: datetime


class EventQuery(ApiSchema):
    after_sequence: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)


class CpuProfileRegistration(ApiSchema):
    service_revision: Revision
    target_id: Identifier
    profile: CpuPerformanceProfile
    cpu_budget_seconds: int = Field(strict=True, ge=1, le=900)


class OnlineCompletion(ApiSchema):
    output_characters: int = Field(strict=True, ge=0, le=131072)
    finish_reason: Literal["stop", "length"]


class InvocationPath(ApiSchema):
    invocation_id: CallId
