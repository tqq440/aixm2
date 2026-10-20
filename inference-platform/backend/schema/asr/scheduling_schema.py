"""本地只读调度诊断；解释不签发许可，也不证明硬件已核实。"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from backend.schema.common.api_schema import ApiSchema


class SchedulingStatus(ApiSchema):
    observed_at: datetime
    mode: Literal["legacy", "isolated", "blocked"]
    routing_configured: bool
    execution_scope: Literal["legacy", "isolated", "disabled"]
    reason_code: str
    candidate_count: int = Field(ge=0, le=8)
    message: str


class SchedulingCandidateView(ApiSchema):
    target_id: str
    device_kind: Literal["unknown", "gpu", "cpu"]
    eligible: bool
    reason_code: str
    message: str
