"""不可变任务路由配置；候选是固定目录修订，不接受任意调用命令。"""

from datetime import datetime
from typing import Literal

from pydantic import Field, StrictBool, model_validator

from backend.schema.asr.catalog_schema import CatalogInput, Digest, Evidence, EvidenceKind, Identifier
from backend.schema.asr.device_policy_schema import CpuPerformanceProfile
from backend.schema.common.api_schema import ApiSchema
from backend.schema.asr.scheduling_schema import SchedulingCandidateView
from backend.utils.time.time_utils import as_utc


class RoutingCandidate(CatalogInput):
    catalog_revision_id: Digest
    cpu_supported: StrictBool = False
    cpu_support_evidence: Evidence | None = None
    cpu_profile: CpuPerformanceProfile | None = None

    @model_validator(mode="after")
    def cpu_time_range(self):
        if self.cpu_profile is not None:
            try:
                as_utc(self.cpu_profile.measured_at)
            except (OverflowError, ValueError):
                raise ValueError("CPU采样时间必须能转换为UTC") from None
        return self


class RoutingRegistration(CatalogInput):
    model_id: Identifier
    model_version: str = Field(strict=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
    compatibility_key: Identifier
    gpu_configuration: Literal["configured", "none", "unknown"] = "unknown"
    evidence_kind: EvidenceKind
    evidence: Evidence
    cpu_budget_seconds: float | None = Field(default=None, strict=True, gt=0, le=900)
    candidates: tuple[RoutingCandidate, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_candidates(self):
        if len({item.catalog_revision_id for item in self.candidates}) != len(self.candidates):
            raise ValueError("路由候选不能重复")
        return self


class RoutingWriteResult(ApiSchema):
    routing_revision_id: str
    created: bool
    execution_enabled: Literal[False] = False


class TaskRoutingView(ApiSchema):
    routing_revision_id: str
    selection_state: Literal["waiting", "selected"]
    catalog_revision_id: str | None = None
    target_id: str | None = None
    device_kind: Literal["gpu", "cpu"] | None = None
    reason_code: str | None = None
    evidence_kind: EvidenceKind | None = None
    selected_at: datetime | None = None
    observed_at: datetime | None = None
    message: str | None = None
    reason_category: Literal["selected", "waiting", "blocked", "outcome_unknown", "terminal"] | None = None
    status_code: str | None = None
    candidates: list[SchedulingCandidateView] = Field(default_factory=list, max_length=8)
