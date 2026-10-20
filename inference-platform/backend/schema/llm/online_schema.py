"""在线请求的公共输入与元数据输出；流内容由连接层发送，不持久化。"""

from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator

from backend.schema.common.api_schema import ApiSchema
from backend.schema.execution.execution_schema import FailureInfo
from backend.schema.models.model_schema import ModelIdentity
from backend.utils.time.time_utils import as_utc

OnlineState = Literal["accepted", "waiting_instance", "streaming", "completed", "failed", "cancelled"]
OnlinePermitState = Literal["waiting", "held", "released", "uncertain"]


class OnlineMessage(ApiSchema):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=16_000)


class OnlineParameters(ApiSchema):
    temperature: float = Field(default=0.7, ge=0, le=2, allow_inf_nan=False, strict=True)
    max_tokens: int = Field(default=512, ge=1, le=2048, strict=True)


class OnlineRequestInput(ModelIdentity):
    online_request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    messages: list[OnlineMessage] = Field(min_length=1, max_length=32)
    parameters: OnlineParameters = Field(default_factory=OnlineParameters)

    @model_validator(mode="after")
    def validate_messages(self):
        if sum(len(message.content) for message in self.messages) > 16_000:
            raise ValueError("消息正文总长度不能超过 16000 个字符")
        if not any(message.role == "user" for message in self.messages):
            raise ValueError("在线请求必须至少包含一条用户消息")
        return self


class OnlinePath(ApiSchema):
    online_request_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class OnlineListQuery(ApiSchema):
    page: int = Field(default=1, ge=1, le=1_000_000)
    page_size: int = Field(default=20, ge=1, le=100)


class OnlineRequestView(ModelIdentity):
    online_request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    caller_id: str = Field(min_length=1, max_length=64)
    instance_id: str | None = None
    state: OnlineState
    permit_state: OnlinePermitState
    accepted_at: datetime
    deadline_at: datetime
    first_token_at: datetime | None = None
    ended_at: datetime | None = None
    output_characters: int = Field(ge=0)
    finish_reason: str | None = None
    error: FailureInfo | None = Field(default=None, validation_alias=AliasChoices("error", "error_json"))
    cancel_requested: bool
    cancellation_confirmed: bool

    @field_validator("accepted_at", "deadline_at", "first_token_at", "ended_at")
    @classmethod
    def utc_time(cls, value: datetime | None) -> datetime | None:
        return as_utc(value) if value is not None else None


class OnlineRequestPage(ApiSchema):
    items: list[OnlineRequestView]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


class OnlineCancelReceipt(ApiSchema):
    request: OnlineRequestView
    decision: Literal["cancel_requested", "already_finished"]


class LlmModelParameters(ApiSchema):
    default_temperature: float = Field(default=0.7, ge=0, le=2, allow_inf_nan=False)
    default_max_tokens: int = Field(default=512, ge=1, le=2048)
    max_tokens: int = Field(default=2048, ge=1, le=2048)


class LlmInputConstraints(ApiSchema):
    max_messages: int = Field(default=32, ge=1, le=32)
    max_input_characters: int = Field(default=16_000, ge=1, le=16_000)


class LlmStatus(ApiSchema):
    configured: bool
    availability: Literal["unconfigured", "configured"]
    message: str
    max_concurrent_requests: int = Field(ge=1)
    active_requests: int = Field(ge=0)
    uncertain_permits: int = Field(ge=0)
    service_verified: Literal[False] = False
