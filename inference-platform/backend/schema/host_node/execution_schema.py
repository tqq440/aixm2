"""内部调用身份与回执；媒体独立传输，在线输入只在流请求中短暂使用。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from backend.schema.asr.asr_schema import TranscriptionResult
from backend.schema.common.api_schema import ApiSchema
from backend.schema.host_node.node_schema import NodeId, Revision
from backend.schema.invocations.invocation_schema import OnlineCompletion
from backend.schema.vision.detection_schema import DetectionResult
from backend.utils.time.aware_time import require_aware_datetime
from backend.schema.host_node.upstream_schema import RemoteDetectionResult, ActualDevice
from backend.schema.llm.online_schema import OnlineRequestInput


InvocationId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Result = TranscriptionResult | DetectionResult | RemoteDetectionResult | OnlineCompletion


class ExecutionCommand(ApiSchema):
    node_id: NodeId
    service_id: NodeId
    config_revision: Revision
    instance_id: UUID
    invocation_id: InvocationId
    attempt: int = Field(ge=1, le=1_000_000, strict=True)
    service_revision: Revision
    target_id: NodeId
    kind: Literal["asr", "vision", "llm"]
    model_id: NodeId
    protocol: Literal["remote-v2", "openai-audio", "vision-json-v1", "chat-completions-sse"]
    input_ref: InvocationId
    input_sha256: Revision
    deadline_at: datetime
    manifest_hash: Revision | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("deadline_at", mode="before")
    @classmethod
    def aware_deadline(cls, value):
        return require_aware_datetime(value)

    @model_validator(mode="after")
    def matching_protocol(self):
        if self.protocol not in {"asr": {"remote-v2", "openai-audio"}, "vision": {"vision-json-v1"},
                                 "llm": {"chat-completions-sse"}}[self.kind]:
            raise ValueError("执行类型与协议不一致")
        return self


class AdapterOutcome(ApiSchema):
    """只有协议适配器取得可信最终响应时才能构造；普通异常不能使用 failed。"""
    state: Literal["succeeded", "failed", "cancelled", "pending"]
    result: Result | None = None
    error_code: str | None = Field(default=None, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    job_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")

    @model_validator(mode="after")
    def coherent(self):
        if (self.state == "succeeded") != (self.result is not None):
            raise ValueError("成功须有已校验结果，失败不能携带结果")
        if self.state == "succeeded" and self.error_code:
            raise ValueError("成功不能同时报告失败")
        if self.state=="pending" and self.job_id is None:raise ValueError("异步受理须返回可关联任务编号")
        return self


class TaskMonitoringSnapshot(ApiSchema):
    query_budget_state: Literal["waiting", "querying", "release_pending", "uncertain"] | None = None
    query_budget_reason: str | None = Field(default=None, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    supported: bool | None = None
    remote_state: Literal["queued","running","succeeded","failed","cancelled"] | None = None
    progress: float | None = Field(default=None,ge=0,le=100,allow_inf_nan=False)
    stage: str | None = Field(default=None,max_length=64,pattern=r"^[A-Za-z0-9._-]+$")
    queue_position: int | None = Field(default=None,ge=0)
    actual_device: ActualDevice | None = None
    last_query_at: datetime | None = None
    last_trusted_at: datetime | None = None
    last_error: str | None = Field(default=None,max_length=64,pattern=r"^[A-Z][A-Z0-9_]*$")
    last_error_at: datetime | None = None
    poll_stopped: Literal["deadline"] | None = None
    cancel_requested: bool | None = None
    state_source: Literal["response","query"] | None = None

    @field_validator("last_query_at","last_trusted_at","last_error_at",mode="before")
    @classmethod
    def aware_times(cls,value):return require_aware_datetime(value) if value is not None else None


class ExecutionReceipt(ApiSchema):
    command: ExecutionCommand
    request_hash: Revision
    state: Literal["sending", "succeeded", "failed", "cancelled", "unknown"]
    result_hash: Revision | None = None
    result: Result | None = None
    error_code: str | None = Field(default=None, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    created_at: datetime
    updated_at: datetime
    monitoring: TaskMonitoringSnapshot = Field(default_factory=TaskMonitoringSnapshot)

    @model_validator(mode="after")
    def coherent(self):
        if (self.state == "succeeded") != (self.result_hash is not None):
            raise ValueError("成功须提供结果指纹")
        if self.result is not None and self.state != "succeeded":
            raise ValueError("非成功状态不能携带结果")
        return self


class ExecutionAvailability(ApiSchema):
    node_id: NodeId
    service_id: NodeId
    config_revision: Revision
    instance_id: UUID | None
    kind: Literal["asr", "vision", "llm"]
    model_id: NodeId
    accepting_requests: bool
    adapter_configured: bool
    # 仅真正配置适配器时声明；旧元数据适配端口默认 false。
    protocols: list[str] = Field(default_factory=list, max_length=4)
    input_required: bool = False
    invocation_mode: Literal["sync","async","stream"] | None = Field(default=None,exclude_if=lambda value:value is None)
    issues: list[str]


class StreamExecutionInput(ApiSchema):
    command: ExecutionCommand
    request: OnlineRequestInput
