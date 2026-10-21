"""手动启动的 Whisper HTTP 服务契约；只接收已实现的音频字段。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.config.asr.model_catalog import MODEL_ID, SUPPORTED_LANGUAGES


class AsrHttpOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: int = Field(default=8001, ge=1, le=65535)


class AsrHttpTranscription(BaseModel):
    """遵循 OpenAI Audio 的 snake_case 表单命名，不接受模型路径。"""

    model_config = ConfigDict(extra="forbid")

    model: Literal[MODEL_ID]
    response_format: Literal["json", "verbose_json"] = "json"
    language: str | None = Field(default=None, max_length=3)

    @field_validator("language", mode="before")
    @classmethod
    def validate_language(cls, value: str | None) -> str | None:
        if value is None or value == "auto":
            return None
        if not isinstance(value, str) or value not in SUPPORTED_LANGUAGES:
            raise ValueError("不支持此语言代码")
        return value
