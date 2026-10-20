"""运行信息只报告实际进程心跳，不伪造资源监控。"""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from backend.schema.common.api_schema import ApiSchema
from backend.utils.time.time_utils import as_utc


class ComponentView(ApiSchema):
    component_id: Literal["worker", "vision_worker", "dispatcher", "remote_asr_worker"]
    reported_state: str | None = None
    availability: Literal["not_started", "online", "stale", "stopped", "error"]
    ready: bool = False
    model_ready: bool = False
    pid: int | None = None
    child_pid: int | None = None
    active_task_id: str | None = None
    heartbeat_at: datetime | None = None
    heartbeat_age_seconds: float | None = None
    last_error: str | None = None

    @field_validator("heartbeat_at")
    @classmethod
    def utc_time(cls, value):
        return as_utc(value) if value is not None else None


class RuntimeView(ApiSchema):
    observed_at: datetime
    api_status: Literal["running"] = "running"
    worker: ComponentView
    vision_worker: ComponentView
    dispatcher: ComponentView
    remote_asr_worker: ComponentView | None = None
    external_call_concurrency: int = Field(default=0, ge=0, le=8)
    external_automatic_retry: Literal[False] = False
    execution_concurrency: Literal[2] = 2
    automatic_execution_retry: Literal[True] = True
    message: str = "本机ASR与Vision各配置1个执行槽位，共2个；就绪以各自报告为准，未提供 GPU 或集群资源监控。"

    @field_validator("observed_at")
    @classmethod
    def utc_time(cls, value):
        return as_utc(value)
