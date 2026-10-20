"""当前适配器仅支持单答案、纯文本 Chat Completions。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class EngineMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32_768)


class EngineParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True, allow_inf_nan=False)
    max_tokens: int = Field(default=512, ge=1, le=4096, alias="maxTokens")
    temperature: float = Field(default=0.7, ge=0, le=2)
    top_p: float = Field(default=1, gt=0, le=1, alias="topP")


class EngineInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    messages: list[EngineMessage] = Field(min_length=1, max_length=32)
    parameters: EngineParameters


class UpstreamDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["assistant"] | None = None
    content: str | None = None
    tool_calls: JsonValue = None
    function_call: JsonValue = None
    refusal: JsonValue = None
    audio: JsonValue = None
    reasoning_content: JsonValue = None
    reasoning: JsonValue = None

    @model_validator(mode="after")
    def text_only(self):
        if any(getattr(self, name) is not None for name in (
            "tool_calls", "function_call", "refusal", "audio", "reasoning_content", "reasoning",
        )):
            raise ValueError("Unsupported non-text delta")
        return self


class UpstreamChoice(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    index: Annotated[int, Field(ge=0, le=0)]
    delta: UpstreamDelta
    finish_reason: Literal["stop", "length", "content_filter"] | None = None


class UpstreamChunk(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=512)
    object: Literal["chat.completion.chunk"]
    model: str = Field(min_length=1, max_length=512)
    choices: list[UpstreamChoice] = Field(max_length=1)
    usage: dict[str, JsonValue] | None = None
    error: JsonValue = None

    @model_validator(mode="after")
    def validate_payload(self):
        if self.error is not None or (not self.choices and self.usage is None):
            raise ValueError("Invalid stream payload")
        return self
