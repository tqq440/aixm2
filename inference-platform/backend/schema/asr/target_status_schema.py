"""仅投影调用容量和本机执行槽，不把服务配额称为硬件资源。"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from backend.schema.common.api_schema import ApiSchema


class TargetSlotStatus(ApiSchema):
    slot_index: int
    state: str
    fresh: bool
    model_ready: bool
    session_id: str | None = None
    current_task_id: str | None = None
    last_error: str | None = None
    heartbeat_at: datetime | None = None


class TargetActiveCall(ApiSchema):
    task_id: str
    state: Literal["held", "uncertain"]
    target_id: str
    slot_index: int
    owner_fresh: bool


class TargetPoolStatus(ApiSchema):
    observed_at: datetime
    target_id: str = "remote-default"
    registered: bool = False
    enabled: bool = False
    revision_id: str | None = None
    capacity_group: str | None = None
    device_kind: Literal["unknown", "cpu", "gpu"] = "unknown"
    target_limit: int = 0
    group_limit: int = 0
    held: int = 0
    uncertain: int = 0
    group_held: int = 0
    group_uncertain: int = 0
    remaining_capacity: int = 0
    queued: int = 0
    incompatible_queued: int = 0
    legacy_unknown_count: int = 0
    pool_state: str = "not_started"
    slots: list[TargetSlotStatus] = Field(default_factory=list)
    active_calls: list[TargetActiveCall] = Field(default_factory=list)
    active_calls_total: int = 0
    active_calls_truncated: bool = False

