"""部署验收缺口与推理任务状态分开；不接受任意错误正文。"""

from datetime import datetime
from typing import Literal
from pydantic import Field,field_validator
from backend.schema.common.api_schema import ApiSchema


ValidationStage=Literal["module6a","module6b"]
DeferralCode=Literal["CAMPUS_NETWORK_UNAVAILABLE","REDIS_CLIENT_MISSING","REDIS_SERVER_UNVERIFIED","LINUX_RUNTIME_UNVERIFIED"]


class ValidationDeferral(ApiSchema):
    stage: ValidationStage
    target: str = Field(min_length=1,max_length=64,pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    code: DeferralCode
    source: Literal["platform","operator"] = "platform"
    evidence_ref: str | None = Field(default=None,max_length=256)

    @field_validator("evidence_ref")
    @classmethod
    def safe_evidence(cls,value):
        if value is not None:
            from backend.utils.files.source_archive import archive_path
            archive_path(value)
            if not value.endswith(".json"):raise ValueError("验收证据只引用verification目录内的JSON")
        return value


class ValidationIssue(ValidationDeferral):
    schema_version: Literal[1] = 1
    state: Literal["waiting_external_verification"] = "waiting_external_verification"
    first_observed_at: datetime
    last_observed_at: datetime
    observation_count: int = Field(ge=1)
    evidence_sha256: str | None = Field(default=None,pattern=r"^[a-f0-9]{64}$")


class ValidationIssueQuery(ApiSchema):
    stage: ValidationStage | None = None
    offset: int = Field(default=0,ge=0,le=1000)
    limit: int = Field(default=50,ge=1,le=200)


class ValidationIssueView(ApiSchema):
    stage: ValidationStage
    target: str
    code: DeferralCode
    source: Literal["platform","operator"]
    state: Literal["waiting_external_verification"]
    first_observed_at: datetime
    last_observed_at: datetime
    observation_count: int
    evidence_sha256: str | None


class ValidationIssuePage(ApiSchema):
    items: list[ValidationIssueView]
    total: int
    offset: int
    limit: int
    unreadable_count: int
    scan_truncated: bool
    notice: str
