"""ASR待接入目标与资源报告契约；登记不授予执行权限。"""

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import BeforeValidator, ConfigDict, Field, StrictInt, field_validator, model_validator

from backend.schema.common.api_schema import ApiSchema
from backend.utils.time.aware_time import require_aware_datetime


Identifier = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
Digest = Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]
Evidence = Annotated[str, Field(strict=True, min_length=1, max_length=2048, pattern=r"\S")]
AwareTime = Annotated[datetime, BeforeValidator(require_aware_datetime)]
EvidenceKind = Literal["provided", "isolated"]


class CatalogInput(ApiSchema):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)


class CatalogConnection(CatalogInput):
    base_url: str = Field(strict=True, min_length=1, max_length=2048, repr=False)
    model_id: Identifier
    model_version: str | None = Field(default=None, strict=True, max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
    ca_file: str | None = Field(default=None, strict=True, min_length=1, max_length=2048, repr=False)
    insecure_tls_origin: str | None = Field(default=None, strict=True, max_length=2048, repr=False)


class CatalogRegistration(CatalogInput):
    target_id: Identifier
    connection: CatalogConnection
    capacity_group_id: Identifier = "remote-asr-shared"
    independence_evidence: Evidence | None = None
    device_kind: Literal["unknown", "gpu", "cpu"] = "unknown"
    node_id: Identifier | None = None
    device_id: str | None = Field(default=None, strict=True, min_length=1, max_length=128, pattern=r"\S")
    device_evidence: Evidence | None = None
    compatibility_key: Identifier | None = None
    compatibility_evidence: Evidence | None = None
    evidence_kind: EvidenceKind = "provided"

    @model_validator(mode="after")
    def evidence_required(self):
        if self.device_kind != "unknown" and not all((self.node_id, self.device_id, self.device_evidence)):
            raise ValueError("设备声明必须附节点、设备标识和依据；这些仍是待核实记录")
        if self.device_kind == "unknown" and any((self.node_id, self.device_id, self.device_evidence)):
            raise ValueError("未知设备不能附带已绑定设备字段")
        if bool(self.compatibility_key) != bool(self.compatibility_evidence):
            raise ValueError("模型兼容标识和兼容依据必须同时提供")
        return self


class ResourceObservationInput(CatalogInput):
    catalog_revision_id: Digest
    scope: Literal["target", "capacity_group"] = "target"
    source: Identifier
    evidence_kind: EvidenceKind = "provided"
    evidence: Evidence
    observed_at: AwareTime
    ttl_seconds: StrictInt = Field(ge=1, le=3600)
    health_state: Literal["unknown", "ready", "unavailable"] = "unknown"
    available_concurrency: StrictInt | None = Field(default=None, ge=0, le=1024)
    gpu_utilization_percent: float | None = Field(default=None, strict=True, ge=0, le=100)
    gpu_memory_free_mi_b: StrictInt | None = Field(default=None, alias="gpuMemoryFreeMiB", ge=0, le=1_000_000_000)
    cpu_available_cores: float | None = Field(default=None, strict=True, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def time_range(self):
        try:
            sampled = self.observed_at.astimezone(timezone.utc)
            sampled + timedelta(seconds=self.ttl_seconds)
        except (OverflowError, ValueError):
            raise ValueError("采样时间及有效期必须在UTC可表示范围内") from None
        return self


class CatalogQuery(CatalogInput):
    offset: StrictInt = Field(default=0, ge=0, le=1_000_000)
    limit: StrictInt = Field(default=20, ge=1, le=100)

    @field_validator("offset", "limit", mode="before")
    @classmethod
    def query_integer(cls, value):
        if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 7:
            return int(value)
        return value


class CatalogWriteResult(ApiSchema):
    target_id: str
    catalog_revision_id: str
    created: bool
    routing_enabled: Literal[False] = False


class ObservationWriteResult(ApiSchema):
    observation_id: str
    created: bool
    routing_enabled: Literal[False] = False


class ObservationView(ApiSchema):
    observation_id: str
    scope: Literal["target", "capacity_group"]
    source: str
    evidence_kind: EvidenceKind
    observed_at: datetime
    recorded_at: datetime
    expires_at: datetime
    freshness: Literal["unknown", "fresh", "stale"]
    reported_health: Literal["unknown", "ready", "unavailable"]
    reported_available_concurrency: int | None = None
    gpu_utilization_percent: float | None = None
    gpu_memory_free_mi_b: int | None = Field(default=None, alias="gpuMemoryFreeMiB")
    cpu_available_cores: float | None = None
    usable_for_scheduling: Literal[False] = False


class CatalogTargetView(ApiSchema):
    target_id: str
    capacity_group_id: str
    target_limit: int
    group_limit: int
    legacy_enabled: bool
    mode: Literal["legacy", "catalog_only"]
    connection_revision_id: str | None = None
    catalog_revision_id: str | None = None
    device_kind: Literal["unknown", "cpu", "gpu"] = "unknown"
    verification_state: Literal["unverified"] = "unverified"
    model_id: str | None = None
    model_version: str | None = None
    has_device_evidence: bool = False
    has_compatibility_evidence: bool = False
    observations: list[ObservationView] = Field(default_factory=list)
    routing_enabled: Literal[False] = False


class CatalogPage(ApiSchema):
    observed_at: datetime
    total: int
    offset: int
    limit: int
    items: list[CatalogTargetView]
    routing_enabled: Literal[False] = False
