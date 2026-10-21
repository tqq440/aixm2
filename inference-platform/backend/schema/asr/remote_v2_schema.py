"""Remote-V2 输入响应契约；忽略无关能力字段，不保存上游原始响应。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RemotePayload(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)

    @field_validator("success", mode="before", check_fields=False)
    @classmethod
    def validate_success_boolean(cls, value):
        # Literal[True] 自身会接受整数 1；外部成功标记必须是 JSON 布尔值。
        if value is not True:
            raise ValueError("成功标记必须为 true")
        return value


class RemoteHealthData(RemotePayload):
    status: str
    model_loaded: bool
    version: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+\-]*$")


class RemoteHealthResponse(RemotePayload):
    success: Literal[True]
    data: RemoteHealthData


class RemoteAsrHealth(BaseModel):
    """可公开的连接观察，不包含地址、证书路径或上游错误正文。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    configured: bool
    ready: bool
    api_version: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class RemoteSegment(RemotePayload):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str = Field(max_length=131_072)

    @model_validator(mode="after")
    def validate_times(self):
        if self.end < self.start:
            raise ValueError("分段结束不能早于开始")
        self.text.encode("utf-8", errors="strict")
        return self


class RemoteTranscriptDetails(RemotePayload):
    request_id: str | None = Field(default=None, min_length=1, max_length=64)
    segments: list[RemoteSegment] = Field(max_length=20_000)


class RemoteTranscriptResponse(RemotePayload):
    success: Literal[True]
    engine: Literal["sensevoice"]
    details: RemoteTranscriptDetails


class RemoteRejection(RemotePayload):
    detail: str = Field(min_length=1, max_length=8192)


class OpenAiAudioModel(RemotePayload):
    id: str = Field(min_length=1, max_length=256)


class OpenAiAudioModels(RemotePayload):
    data: list[OpenAiAudioModel] = Field(max_length=10_000)


class OpenAiAudioTranscript(RemotePayload):
    """只接受响应中实际提供的文字和分段，不补造设备、模型版本或时间戳。"""

    text: str = Field(max_length=131_072)
    segments: list[RemoteSegment] = Field(default_factory=list, max_length=20_000)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value):
        value.encode("utf-8", errors="strict")
        return value
