"""公共输入不接受 callerId、目标、执行身份或任意地址。"""

from typing import Literal
import json
from datetime import datetime
from pydantic import Field, field_validator
from backend.schema.common.api_schema import ApiSchema
from backend.schema.catalog.service_schema import Identifier
from backend.schema.llm.online_schema import OnlineMessage, OnlineParameters
from backend.schema.tasks.task_schema import VisionTaskParameters


class KeyCommand(ApiSchema):
    label: str = Field(default="default", min_length=1, max_length=64, pattern=r"\S")


class SubmitFile(ApiSchema):
    service_id: Identifier
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")
    parameters: VisionTaskParameters = Field(default_factory=VisionTaskParameters)

    @field_validator("parameters",mode="before")
    @classmethod
    def parameters_json(cls,value):
        if isinstance(value,str):
            try:return json.loads(value)
            except ValueError:raise ValueError("parameters须为JSON对象") from None
        return value


class SubmitOnline(ApiSchema):
    service_id: Identifier
    messages: list[OnlineMessage] = Field(min_length=1, max_length=32)
    parameters: OnlineParameters = Field(default_factory=OnlineParameters)


class PublicPage(ApiSchema):
    page: int = Field(default=1, ge=1, le=1000000)
    page_size: int = Field(default=20, ge=1, le=100)
    status: Literal["waiting", "running", "succeeded", "failed", "cancelled", "unknown"] | None = None
    service_id: Identifier | None = None
    kind: Literal["asr", "vision", "llm"] | None = None


class SubmitHeaders(ApiSchema):
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class DownloadQuery(ApiSchema):
    format: Literal["json","txt"] = "json"


class PublicInvocation(ApiSchema):
    invocation_id: str
    task_id: str | None
    online_request_id: str | None
    application_id: str
    service_id: str
    service_revision: str
    kind: Literal["asr","vision","llm"]
    state: Literal["waiting","reserved","sent","finished"]
    permit_state: Literal["waiting","held","released","uncertain"]
    target_id: str | None
    attempt: int
    reason: str | None
    outcome: str | None
    resolved_outcome: str | None
    accepted_at: datetime
    deadline_at: datetime
    finished_at: datetime | None


class PublicRequestView(PublicInvocation):
    id: str
    status: Literal["waiting","running","succeeded","failed","cancelled","unknown"]
    model_id: str
    model_version: str
    result_available: bool
    error: dict | None
    cancel_requested: bool
    input: dict | None
    output_characters: int | None
    finish_reason: str | None
