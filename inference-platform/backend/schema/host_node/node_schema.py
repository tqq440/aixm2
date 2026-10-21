"""状态上报只接受明确字段，不接收脚本、任务正文或自由日志。"""

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, StrictBool, field_validator, model_validator

from backend.schema.common.api_schema import ApiSchema
from backend.utils.time.aware_time import require_aware_datetime
from backend.schema.host_node.upstream_schema import NodeUpstreamConfig


NodeId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
Revision = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Counter = Annotated[int, Field(ge=0, le=1_000_000, strict=True)]


class NodeServiceConfig(ApiSchema):
    name: str = Field(min_length=1, max_length=100, pattern=r"\S")
    kind: Literal["asr", "vision", "llm"]
    model_id: NodeId
    internal_base_url: str | None = Field(default=None, max_length=2048)
    report_protocol: Literal["none", "lifecycle-v1"] = "lifecycle-v1"
    report_token_env: str | None = Field(default=None, pattern=r"^NODE_REPORT_TOKEN_[A-Z0-9_]+$", max_length=128)
    catalog_service_id: NodeId | None = None
    catalog_revision: Revision | None = None
    upstream: NodeUpstreamConfig | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("internal_base_url")
    @classmethod
    def valid_origin(cls, value):
        if value is None:
            return value
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port == 0
                or parsed.username is not None or parsed.password is not None or parsed.path not in {"", "/"}
                or any(c in value for c in "?#\\%*") or any(c.isspace() or ord(c) < 32 for c in value)):
            raise ValueError("业务地址只允许无凭据、路径或查询的 HTTP(S) 根地址")
        return value.rstrip("/")

    @model_validator(mode="after")
    def capabilities_match(self):
        if self.upstream and self.upstream.protocol not in {"asr": {"remote-v2", "openai-audio"},
                "vision": {"vision-json-v1"}, "llm": {"chat-completions-sse"}}[self.kind]:
            raise ValueError("业务类型与协议不匹配")
        if (self.report_protocol == "lifecycle-v1") != (self.report_token_env is not None):
            raise ValueError("状态上报需要独立凭据；none 模式不配置上报凭据")
        if (self.catalog_service_id is None) != (self.catalog_revision is None):
            raise ValueError("中心目录编号与修订须同时提供")
        return self


class NodeServicePath(ApiSchema):
    service_id: NodeId


class SaveNodeService(ApiSchema):
    config: NodeServiceConfig
    expected_version: int | None = Field(default=None, ge=1, strict=True)


class OpenNodeInstance(ApiSchema):
    instance_id: UUID
    config_revision: Revision
    expected_generation: int = Field(ge=0, le=1_000_000, strict=True)


class NodeServiceReport(ApiSchema):
    instance_id: UUID
    config_revision: Revision
    sequence: int = Field(ge=1, le=9_007_199_254_740_991, strict=True)
    state: Literal["starting", "warming", "ready", "busy", "draining", "stopped", "failed"]
    health: Literal["unknown", "healthy", "unhealthy"] = "unknown"
    model_ready: StrictBool = False
    accepting_requests: StrictBool = False
    queued_tasks: Counter | None = None
    active_tasks: Counter | None = None
    error_code: str | None = Field(default=None, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    reported_at: datetime | None = None
    control_operation_id: UUID | None = None
    control_action: Literal["drain", "resume"] | None = None

    @field_validator("reported_at", mode="before")
    @classmethod
    def aware_time(cls, value):
        return None if value is None else require_aware_datetime(value)

    @model_validator(mode="after")
    def coherent_state(self):
        if (self.control_operation_id is None) != (self.control_action is None):
            raise ValueError("控制操作编号和动作须同时提供")
        if self.state in {"ready", "busy"} and (not self.model_ready or self.health != "healthy"):
            raise ValueError("就绪或运行状态需要模型就绪及健康声明")
        if self.accepting_requests and (self.state not in {"ready", "busy"} or not self.model_ready or self.health != "healthy"):
            raise ValueError("只有健康且就绪的业务可以声明接单")
        if self.state == "stopped" and (self.model_ready or self.queued_tasks != 0 or self.active_tasks != 0):
            raise ValueError("停止报告须明确模型未就绪、排队及运行任务均为零")
        if self.state == "busy" and self.active_tasks == 0:
            raise ValueError("运行状态不能同时声明零活动任务")
        return self


class NodeListQuery(ApiSchema):
    page: int = Field(default=1, ge=1, le=1_000_000)
    page_size: int = Field(default=20, ge=1, le=100)


class NodeEventQuery(ApiSchema):
    after_sequence: int = Field(default=0, ge=0, le=9_007_199_254_740_991)
    limit: int = Field(default=50, ge=1, le=200)


class ServiceCapabilities(ApiSchema):
    lifecycle_reports: bool
    container_control: bool = False
    resource_metrics: bool = False
    task_monitoring: bool = False
    business_proxy: bool = False


class ServiceSnapshot(ApiSchema):
    service_id: NodeId
    node_id: NodeId
    name: str
    kind: Literal["asr", "vision", "llm"]
    model_id: NodeId
    version: int
    config_revision: Revision
    generation: int
    instance_id: str | None
    sequence: int
    state: Literal["unknown", "starting", "warming", "ready", "busy", "draining", "stopped", "failed"]
    reported_state: str | None
    health: Literal["unknown", "healthy", "unhealthy"]
    reported_health: str | None
    model_ready: bool
    accepting_requests: bool
    reported_accepting_requests: bool | None
    queued_tasks: int | None
    active_tasks: int | None
    report_fresh: bool
    last_received_at: datetime | None
    reported_at: datetime | None
    evaluated_at: datetime
    last_error_code: str | None
    container_state: str = "unknown"
    control: dict = Field(default_factory=dict)
    actual_execution_device: None = None
    progress: None = None
    execution_ready: bool = False
    issues: list[str]
    capabilities: ServiceCapabilities


class NodeCapabilities(ApiSchema):
    service_registry: Literal[True] = True
    lifecycle_reports: Literal[True] = True
    inference_queue: Literal[False] = False
    public_gateway: Literal[False] = False
    container_control: bool = False
    resource_metrics: bool = False


class NodeStatus(ApiSchema):
    node_id: NodeId
    epoch_id: str
    status: Literal["online"]
    started_at: datetime
    evaluated_at: datetime
    heartbeat_seconds: int
    stale_seconds: int
    capabilities: NodeCapabilities


class ServicePage(ApiSchema):
    items: list[ServiceSnapshot]
    total: int
    page: int
    page_size: int


class ConfigurationView(ApiSchema):
    service_id: NodeId
    version: int
    config_revision: Revision
    config: NodeServiceConfig


class ReportReceipt(ApiSchema):
    duplicate: bool
    service: ServiceSnapshot


class NodeEventView(ApiSchema):
    sequence: int
    service_id: str | None
    instance_id: str | None
    config_revision: str | None
    event_type: str
    source: Literal["node", "service"]
    details: dict
    created_at: datetime


class EventPage(ApiSchema):
    items: list[NodeEventView]
    has_more: bool
    next_sequence: int
