"""主机控制只接受登记容器与固定镜像身份，没有 shell 或 Docker 任意参数。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, field_validator, model_validator

from backend.schema.common.api_schema import ApiSchema
from backend.schema.host_node.node_schema import NodeId, Revision


class ContainerVersion(ApiSchema):
    version_id: NodeId
    container_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    compatibility_evidence: str = Field(min_length=1, max_length=256)


class RuntimeConfig(ApiSchema):
    versions: list[ContainerVersion] = Field(min_length=1, max_length=8)
    drain_path: str | None = Field(default=None, max_length=256)
    resume_path: str | None = Field(default=None, max_length=256)
    drain_timeout_seconds: int = Field(default=300, ge=1, le=3600, strict=True)
    ready_timeout_seconds: int = Field(default=120, ge=1, le=1800, strict=True)
    exit_timeout_seconds: int = Field(default=30, ge=1, le=300, strict=True)
    auto_rollback: StrictBool = False
    rollback_compatibility_evidence: str | None = Field(default=None, min_length=16, max_length=512)

    @field_validator("drain_path", "resume_path")
    @classmethod
    def control_path(cls, value):
        if value is not None and (not value.startswith("/") or value.startswith("//") or ".." in value.split("/")
                or any(c in value for c in "?#\\%{}") or any(c.isspace() or ord(c) < 32 for c in value)):
            raise ValueError("控制路径必须是明确的相对根路径")
        return value

    @model_validator(mode="after")
    def fixed_versions(self):
        if len({v.version_id for v in self.versions}) != len(self.versions) or len({v.container_id for v in self.versions}) != len(self.versions):
            raise ValueError("版本与容器身份不能重复")
        if (self.drain_path is None) != (self.resume_path is None):
            raise ValueError("排空和恢复接口须成对声明")
        if self.auto_rollback and (self.drain_path is None or self.rollback_compatibility_evidence is None):
            raise ValueError("自动回退需要排空/恢复接口及持久数据兼容依据")
        return self


class SaveRuntimeConfig(ApiSchema):
    expected_control_version: int = Field(ge=0, strict=True)
    config: RuntimeConfig


class ControlCommand(ApiSchema):
    operation_id: UUID
    action: Annotated[str, Field(pattern=r"^(start|stop|upgrade)$")]
    version_id: NodeId | None = None
    expected_control_version: int = Field(ge=0, strict=True)
    config_revision: Revision

    @model_validator(mode="after")
    def target_required(self):
        if (self.action == "stop") != (self.version_id is None):
            raise ValueError("启动/升级需要版本，停止不提供版本")
        return self


class OperationPath(ApiSchema):
    operation_id: UUID


class ReconcileCommand(ApiSchema):
    expected_control_version: int = Field(ge=0, strict=True)


class AbortControlCommand(ReconcileCommand):
    evidence: str = Field(min_length=16, max_length=512)


class ControlAcknowledgement(ApiSchema):
    operation_id: UUID
    instance_id: UUID
    config_revision: Revision
    action: Annotated[str, Field(pattern=r"^(drain|resume)$")]
    accepted: StrictBool


class OperationView(ApiSchema):
    operation_id: UUID
    service_id: NodeId
    action: Literal["start", "stop", "upgrade"]
    status: Literal["pending", "running", "unknown", "blocked", "succeeded", "failed", "rolled_back", "aborted"]
    phase: str
    error_code: str | None
    runtime_revision: Revision
    config_revision: Revision
    requested_version: str | None
    effective_version: str | None
    rolling_back: bool
    control_version: int
    created_at: datetime
    updated_at: datetime


class OperationPage(ApiSchema):
    items: list[OperationView]
    total: int
    page: int
    page_size: int


class RuntimeView(ApiSchema):
    service_id: NodeId
    runtime_revision: Revision
    control_version: int
    config: RuntimeConfig
    control: dict


class InstanceAssignment(ApiSchema):
    service_id: NodeId
    instance_id: UUID
    config_revision: Revision
    generation: int
    sequence: int
    start_paused: bool
    operation_id: UUID | None


class ResourceComponent(ApiSchema):
    status: Literal["ok", "warming", "unknown", "unavailable"]
    value: dict | None
    error_code: str | None
    last_success_at: datetime | None
    checked_at: datetime | None
    fresh: bool


class ResourceView(ApiSchema):
    node_id: NodeId
    enabled: bool
    sampled_at: datetime | None
    evaluated_at: datetime
    components: dict[str, ResourceComponent]
