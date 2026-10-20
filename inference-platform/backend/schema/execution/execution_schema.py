"""通知和执行许可证是独立契约；内部字段不直接作为公共 API 输出。"""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from backend.schema.common.api_schema import ApiSchema


class Delivery(ApiSchema):
    notification_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    worker_id: str = Field(min_length=1, max_length=64)
    delivery_token: str = Field(min_length=1, max_length=64)
    dispatch_version: int = Field(ge=1)
    execution_plan_version: int = Field(ge=1)
    queue_key: str = Field(min_length=1, max_length=128)


class ExecutionLease(ApiSchema):
    task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    worker_id: str = Field(min_length=1, max_length=64)
    lease_token: str = Field(min_length=1, max_length=64)
    attempt: int = Field(ge=1)
    dispatch_version: int = Field(ge=1)
    execution_plan_version: int = Field(ge=1)
    lease_expires_at: datetime
    # 本次执行最晚可用预算，已取单次与任务总期限的较小值；续租不能刷新它。
    attempt_budget_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class ExecutionFileInput(ApiSchema):
    task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    input_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_size_bytes: int = Field(gt=0)
    model_id: str = Field(min_length=1, max_length=64)
    model_version: str = Field(min_length=1, max_length=64)


class ExecutionInput(ExecutionFileInput):
    # 旧 ASR JSON 没有 taskType，缺省值保留原进程协议兼容。
    task_type: Literal["asr"] = "asr"
    language: str | None = Field(pattern=r"^[a-z]{2,3}$")


class VisionExecutionInput(ExecutionFileInput):
    task_type: Literal["vision"] = "vision"
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    confidence_threshold: float = Field(gt=0, le=1, allow_inf_nan=False)
    iou_threshold: float = Field(gt=0, le=1, allow_inf_nan=False)
    max_detections: int = Field(ge=1, le=300)


class AcquireDecision(ApiSchema):
    decision: Literal["acquired", "terminal_or_stale", "busy"]
    lease: ExecutionLease | None = None
    input: ExecutionInput | VisionExecutionInput | None = None

    @model_validator(mode="after")
    def validate_acquired_fields(self):
        if self.decision == "acquired" and (self.lease is None or self.input is None):
            raise ValueError("获准执行必须同时返回租约与输入档案")
        return self


class FailureInfo(ApiSchema):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    message: str = Field(min_length=1, max_length=512)
