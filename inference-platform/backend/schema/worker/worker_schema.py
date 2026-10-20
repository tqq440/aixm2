"""Worker 父子进程边界；仅传递经 Pydantic 校验的 JSON。"""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from backend.config.settings.app_settings import AppSettings
from backend.schema.asr.asr_schema import TranscriptionResult
from backend.schema.common.api_schema import ApiSchema
from backend.schema.execution.execution_schema import ExecutionInput, FailureInfo, VisionExecutionInput
from backend.schema.vision.detection_schema import DetectionResult


class StartCommand(ApiSchema):
    type: Literal["start"] = "start"
    settings: AppSettings
    task_type: Literal["asr", "vision"] = "asr"
    asr_profile: Literal["local", "remote"] = "local"

    @model_validator(mode="after")
    def validate_profile(self):
        if self.task_type != "asr" and self.asr_profile != "local":
            raise ValueError("远端 ASR 配置只能用于 ASR Worker")
        return self

    @field_validator("settings", mode="before")
    @classmethod
    def validate_settings(cls, value):
        # AppSettings 的路径解析返回 Path，需先走 Python 校验再交给 JSON 模式。
        return AppSettings.model_validate(value)


class RunCommand(ApiSchema):
    type: Literal["run"] = "run"
    input: ExecutionInput | VisionExecutionInput


class StopCommand(ApiSchema):
    type: Literal["stop"] = "stop"


class ReadyEvent(ApiSchema):
    type: Literal["ready"] = "ready"
    model_load_seconds: float = Field(ge=0, allow_inf_nan=False)
    warmup_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)


class ResultEvent(ApiSchema):
    type: Literal["result"] = "result"
    task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    result: TranscriptionResult | DetectionResult


class FailedEvent(ApiSchema):
    type: Literal["failed"] = "failed"
    task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    failure: FailureInfo


class FatalEvent(ApiSchema):
    type: Literal["fatal"] = "fatal"
    failure: FailureInfo


COMMAND_ADAPTER = TypeAdapter(Annotated[StartCommand | RunCommand | StopCommand, Field(discriminator="type")])
EVENT_ADAPTER = TypeAdapter(Annotated[ReadyEvent | ResultEvent | FailedEvent | FatalEvent, Field(discriminator="type")])
