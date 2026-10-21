"""目录声明不等于实际设备、容量或执行能力已经验证。"""

from typing import Annotated, Literal

from pydantic import Field, StrictBool, field_validator, model_validator

from backend.config.settings.app_settings import asr_service_origin, asr_service_path, remote_asr_origin
from backend.schema.common.api_schema import ApiSchema


Identifier = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
Revision = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class MonitoringConfig(ApiSchema):
    protocol: Literal["none", "job-json-v1"] = "none"
    status_path: str | None = Field(default=None, max_length=512)
    result_path: str | None = Field(default=None, max_length=512)
    result_in_status: StrictBool = Field(default=False,exclude_if=lambda value:not value)
    reports_progress: StrictBool = False
    reports_device: StrictBool = False
    poll_seconds: float = Field(default=5, ge=5, le=60, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_paths(self):
        if self.protocol == "none":
            if self.status_path or self.result_path or self.reports_progress or self.reports_device or self.result_in_status:
                raise ValueError("未提供状态查询协议时不能声明进度或设备查询能力")
        elif not self.status_path:
            raise ValueError("任务查询协议需要 statusPath")
        for path in (self.status_path, self.result_path):
            if path is not None:
                if path.count("{requestId}") != 1 or "{" in path.replace("{requestId}", "") or "}" in path.replace("{requestId}", ""):
                    raise ValueError("查询路径必须且只能包含一个 {requestId}")
                asr_service_path(path.replace("{requestId}", "request-id"))
        return self


class NodeTargetBinding(ApiSchema):
    """不可变的 Node 路由；baseUrl/invokePath 仍描述真实业务，沿用容量身份。"""
    node_id: Identifier
    service_id: Identifier
    config_revision: Revision
    base_url: str = Field(max_length=2048)
    executor_token_env: str = Field(max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")

    @field_validator("base_url")
    @classmethod
    def valid_origin(cls, value):
        return asr_service_origin(value)


class ServiceTarget(ApiSchema):
    target_id: Identifier
    name: str = Field(min_length=1, max_length=100, pattern=r"\S")
    enabled: StrictBool = True
    base_url: str = Field(max_length=2048)
    invoke_path: str = Field(max_length=512)
    health_path: str = Field(max_length=512)
    upstream_model: str | None = Field(default=None, min_length=1, max_length=256)
    api_key_env: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    ca_file: str | None = Field(default=None, min_length=1, max_length=2048)
    insecure_tls_origin: str | None = Field(default=None, max_length=2048)
    connect_timeout_seconds: float = Field(default=5, gt=0, le=30, allow_inf_nan=False)
    total_timeout_seconds: float = Field(default=600, gt=0, le=900, allow_inf_nan=False)
    concurrency_limit: int = Field(default=1, strict=True, ge=1, le=64)
    capacity_evidence: str | None = Field(default=None, min_length=1, max_length=512)
    resource_group_ids: list[Identifier] = Field(default_factory=list, max_length=8)
    device_kind: Literal["unknown", "cpu", "gpu"] = "unknown"
    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    device_evidence: str | None = Field(default=None, min_length=1, max_length=512)
    compatibility_evidence: str | None = Field(default=None, min_length=1, max_length=512)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    node: NodeTargetBinding | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("base_url")
    @classmethod
    def validate_origin(cls, value):
        return asr_service_origin(value)

    @field_validator("invoke_path", "health_path")
    @classmethod
    def validate_path(cls, value):
        return asr_service_path(value)

    @model_validator(mode="after")
    def validate_target(self):
        if len(set(self.resource_group_ids)) != len(self.resource_group_ids):
            raise ValueError("共享组不能重复")
        if self.insecure_tls_origin:
            origin = remote_asr_origin(self.insecure_tls_origin)
            if self.ca_file or origin != self.base_url:
                raise ValueError("TLS 例外须匹配准确 HTTPS Origin，且不能同时配置 CA")
            self.insecure_tls_origin = origin
        if self.ca_file and not self.base_url.startswith("https://"):
            raise ValueError("CA 只适用于 HTTPS")
        if self.device_kind == "unknown" and (self.device_id or self.device_evidence):
            raise ValueError("设备未知时不能填写已核实设备编号或依据")
        return self


class ServiceDefinition(ApiSchema):
    service_id: Identifier
    name: str = Field(min_length=1, max_length=100, pattern=r"\S")
    kind: Literal["asr", "vision", "llm"]
    model_id: Identifier
    model_version: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
    protocol: Literal["remote-v2", "openai-audio", "vision-json-v1", "chat-completions-sse"]
    invocation_mode: Literal["sync", "async", "stream"] = "sync"
    policy: Literal["fixed", "gpu_preferred"] = "fixed"
    max_file_bytes: int = Field(default=20 * 1024 * 1024, strict=True, ge=1, le=20 * 1024 * 1024)
    max_audio_seconds: float = Field(default=300, gt=0, le=300, allow_inf_nan=False)
    targets: list[ServiceTarget] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_definition(self):
        allowed = {"asr": {"remote-v2", "openai-audio"}, "vision": {"vision-json-v1"}, "llm": {"chat-completions-sse"}}
        if self.protocol not in allowed[self.kind]:
            raise ValueError("服务类型与协议不匹配")
        if (self.kind == "llm") != (self.invocation_mode == "stream"):
            raise ValueError("LLM 使用 stream；ASR/YOLO 使用 sync 或 async")
        if len({target.target_id for target in self.targets}) != len(self.targets):
            raise ValueError("目标编号不能重复")
        if self.kind == "vision" and self.max_file_bytes > 10 * 1024 * 1024:
            raise ValueError("图片输入暂不扩大原 10 MiB 上限")
        if self.protocol != "remote-v2" and any(not target.upstream_model for target in self.targets):
            raise ValueError("该协议需要填写上游模型标识")
        return self


class SaveService(ApiSchema):
    config: ServiceDefinition
    expected_version: int | None = Field(default=None, strict=True, ge=1)


class ActivateService(ApiSchema):
    expected_version: int = Field(strict=True, ge=1)
    revision: Revision
    enabled: StrictBool


class CheckTarget(ApiSchema):
    expected_version: int = Field(strict=True, ge=1)
    revision: Revision


class ResourceGroupDefinition(ApiSchema):
    group_id: Identifier
    name: str = Field(min_length=1, max_length=100, pattern=r"\S")
    concurrency_limit: int = Field(default=1, strict=True, ge=1, le=64)
    capacity_evidence: str | None = Field(default=None, min_length=1, max_length=512)


class SaveResourceGroup(ApiSchema):
    config: ResourceGroupDefinition
    expected_version: int | None = Field(default=None, strict=True, ge=1)


class CatalogQuery(ApiSchema):
    page: int = Field(default=1, ge=1, le=1_000_000)
    page_size: int = Field(default=20, ge=1, le=100)


class ServicePath(ApiSchema):
    service_id: Identifier


class TargetPath(ServicePath):
    target_id: Identifier
