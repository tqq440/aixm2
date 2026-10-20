"""本机登记与 Worker 报告观测契约；报告不等于进程退出证明。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_serializer

from backend.schema.common.api_schema import ApiSchema
from backend.schema.models.model_schema import RegistryListQuery
from backend.utils.time.time_utils import as_utc

RegistryId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class LocalNodeSnapshot(ApiSchema):
    local_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    hostname: str = Field(min_length=1, max_length=255)
    os_name: str = Field(min_length=1, max_length=64)
    os_version: str = Field(min_length=1, max_length=255)
    architecture: str = Field(min_length=1, max_length=64)
    cpu_count: int | None = Field(default=None, gt=0)


class NodePath(ApiSchema):
    node_id: RegistryId


class InstancePath(ApiSchema):
    instance_id: RegistryId


class InstanceListQuery(RegistryListQuery):
    node_id: RegistryId | None = None


class NodeView(NodePath):
    hostname: str
    os_name: str
    os_version: str
    architecture: str
    cpu_count: int | None
    gpu_status: Literal["not_probed"] = "not_probed"
    registration_state: Literal["registered"] = "registered"
    agent_status: Literal["not_started", "online", "offline", "stale"] = "not_started"
    registered_at: datetime
    metadata_observed_at: datetime

    @field_serializer("registered_at", "metadata_observed_at")
    def serialize_time(self, value: datetime):
        return as_utc(value).isoformat()


class InstanceView(InstancePath):
    node_id: RegistryId
    instance_key: Literal["asr-default"]
    model_id: str
    model_version: str
    adapter_key: str
    execution_device: Literal["cpu"]
    registration_state: Literal["registered"] = "registered"
    runtime_state: Literal["unknown"] = "unknown"
    management_mode: Literal["legacy_worker", "node_agent"] = "legacy_worker"
    registered_at: datetime

    @field_serializer("registered_at")
    def serialize_time(self, value: datetime):
        return as_utc(value).isoformat()


class SessionView(ApiSchema):
    session_id: RegistryId
    instance_id: RegistryId
    source: Literal["legacy_worker_observation"]
    worker_session_id: RegistryId
    worker_pid: int = Field(gt=0)
    child_pid: int | None = Field(default=None, gt=0)
    reported_state: str
    reported_model_ready: bool
    active_task_id: str | None
    heartbeat_at: datetime
    worker_started_at: datetime
    observed_at: datetime
    last_error: str | None
    freshness: Literal["fresh", "stale", "superseded", "clock_skew"]
    heartbeat_age_seconds: float | None
    exit_confirmed: Literal[False] = False

    @field_serializer("heartbeat_at", "worker_started_at", "observed_at")
    def serialize_time(self, value: datetime):
        return as_utc(value).isoformat()


class InstanceDetail(ApiSchema):
    instance: InstanceView
    latest_observation: SessionView | None


class LocalRegistration(ApiSchema):
    node: NodeView
    instance: InstanceView
    node_created: bool
    instance_created: bool


class ObservationReceipt(ApiSchema):
    instance_id: RegistryId
    recorded: bool
    reason: Literal["recorded", "unchanged", "no_worker_report", "pre_registration_report", "older_report"]
    session: SessionView | None = None


class NodePage(RegistryListQuery):
    items: list[NodeView]
    total: int


class InstancePage(RegistryListQuery):
    items: list[InstanceView]
    total: int


class SessionPage(RegistryListQuery):
    items: list[SessionView]
    total: int
    evaluated_at: datetime

    @field_serializer("evaluated_at")
    def serialize_time(self, value: datetime):
        return as_utc(value).isoformat()
