"""纯离线设备选择预演契约；输入证据不等于平台已核实的运行事实。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, StrictInt, model_validator
from pydantic.alias_generators import to_camel

from backend.utils.time.aware_time import require_aware_datetime

AwareTime = Annotated[datetime, BeforeValidator(require_aware_datetime)]
Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
EvidenceText = Annotated[str, Field(strict=True, min_length=1, max_length=2048, pattern=r"\S")]
Positive = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
Nonnegative = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
Limit = Annotated[StrictInt, Field(ge=1, le=8)]
Count = Annotated[StrictInt, Field(ge=0, le=1_000_000)]
EvidenceKind = Literal["isolated", "provided"]


class PolicySchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, alias_generator=to_camel,
                              validate_by_alias=True, validate_by_name=False,
                              serialize_by_alias=True, allow_inf_nan=False)


class DevicePolicyTask(PolicySchema):
    status: Literal["created", "queued", "running", "retry_wait", "cancel_requested", "cancelled", "failed", "succeeded"]
    attempt: Count
    has_permit: StrictBool
    upstream_execution_started: StrictBool
    audio_seconds: Annotated[float, Field(strict=True, gt=0, le=300, allow_inf_nan=False)]
    queue_deadline_at: AwareTime
    total_deadline_at: AwareTime
    attempt_budget_seconds: Positive
    remote_budget_seconds: Positive
    cpu_budget_seconds: Positive | None = None


class DeviceCapacityGroup(PolicySchema):
    capacity_group_id: Identifier
    limit: Limit
    held: Count
    uncertain: Count
    capacity_known: StrictBool
    observed_at: AwareTime | None
    ttl_seconds: Positive
    capacity_evidence: EvidenceText | None = None
    evidence_kind: EvidenceKind
    resource_required: StrictBool = False
    resource_available: Count | None = None
    resource_exhaustion_confirmed: StrictBool = True


class CpuPerformanceProfile(PolicySchema):
    target_revision: Identifier
    evidence_kind: EvidenceKind
    evidence_reference: EvidenceText
    measured_at: AwareTime
    valid_for_seconds: Positive
    sample_count: Annotated[StrictInt, Field(ge=1, le=1_000_000)]
    min_audio_seconds: Nonnegative
    max_audio_seconds: Annotated[float, Field(strict=True, gt=0, le=300, allow_inf_nan=False)]
    max_tested_concurrency: Limit
    fixed_overhead_seconds: Nonnegative
    seconds_per_audio_second: Positive
    safety_factor: Annotated[float, Field(strict=True, ge=1, allow_inf_nan=False)]
    queue_wait_upper_bound_seconds: Nonnegative

    @model_validator(mode="after")
    def valid_range(self):
        if self.min_audio_seconds > self.max_audio_seconds:
            raise ValueError("CPU样本音频范围上下限颠倒")
        return self


class DevicePolicyTarget(PolicySchema):
    target_id: Identifier
    target_revision: Identifier
    compatibility_key: Identifier
    device_kind: Literal["unknown", "gpu", "cpu"]
    device_evidence: EvidenceText | None = None
    compatibility_evidence: EvidenceText | None = None
    evidence_kind: EvidenceKind
    capacity_group_id: Identifier
    limit: Limit
    held: Count
    uncertain: Count
    capacity_known: StrictBool
    capacity_observed_at: AwareTime | None
    capacity_ttl_seconds: Positive
    capacity_evidence: EvidenceText | None = None
    health_state: Literal["ready", "unavailable", "unknown"]
    health_observed_at: AwareTime | None
    health_ttl_seconds: Positive
    slot_ready: StrictBool
    slot_observed_at: AwareTime | None
    slot_ttl_seconds: Positive
    cpu_supported: StrictBool = False
    cpu_support_evidence: EvidenceText | None = None
    cpu_profile: CpuPerformanceProfile | None = None
    resource_required: StrictBool = False
    resource_available: Count | None = None
    resource_exhaustion_confirmed: StrictBool = True
    cpu_profile_usable: StrictBool = True


class DevicePolicyRequest(PolicySchema):
    evaluated_at: AwareTime
    execution_enabled: StrictBool = False
    routing_revision: Identifier | None = None
    compatibility_key: Identifier | None = None
    gpu_configuration: Literal["configured", "none", "unknown"]
    task: DevicePolicyTask
    groups: Annotated[tuple[DeviceCapacityGroup, ...], Field(max_length=64)]
    targets: Annotated[tuple[DevicePolicyTarget, ...], Field(max_length=64)]

    @model_validator(mode="after")
    def consistent_inventory(self):
        if self.execution_enabled:
            raise ValueError("离线预演不允许executionEnabled=true")
        if len({item.target_id for item in self.targets}) != len(self.targets):
            raise ValueError("targetId不得重复")
        if len({item.capacity_group_id for item in self.groups}) != len(self.groups):
            raise ValueError("capacityGroupId不得重复")
        groups = {item.capacity_group_id: item for item in self.groups}
        if any(item.capacity_group_id not in groups for item in self.targets):
            raise ValueError("每个目标必须引用输入中已有的共享容量组")
        for group in self.groups:
            members = [item for item in self.targets if item.capacity_group_id == group.capacity_group_id and item.capacity_known]
            if group.capacity_known and (sum(item.held for item in members) > group.held
                    or sum(item.uncertain for item in members) > group.uncertain):
                raise ValueError("共享组占位不能小于已知成员目标占位之和")
        if self.gpu_configuration == "none" and any(item.device_kind == "gpu"
                and item.compatibility_key == self.compatibility_key for item in self.targets):
            raise ValueError("声明无相关GPU时不能同时提供相关GPU候选")
        return self


class DeviceCandidateDecision(PolicySchema):
    target_id: Identifier
    device_kind: Literal["unknown", "gpu", "cpu"]
    eligible: StrictBool
    reason_code: str


class DevicePolicyDecision(PolicySchema):
    mode: Literal["preview"] = "preview"
    execution_enabled: Literal[False] = False
    action: Literal["select", "wait"]
    reason_code: str
    evaluated_at: AwareTime
    routing_revision: Identifier | None = None
    target_id: Identifier | None = None
    target_revision: Identifier | None = None
    device_kind: Literal["gpu", "cpu"] | None = None
    evidence_kind: Literal["isolated", "provided", "none"]
    estimated_cpu_seconds: Nonnegative | None = None
    estimated_processing_seconds: Nonnegative | None = None
    estimated_queue_seconds: Nonnegative | None = None
    candidates: tuple[DeviceCandidateDecision, ...] = ()
    notice: str = "仅离线策略预演；未核实输入证据、未签发许可、未调用设备，也未接入真实任务领取。"
