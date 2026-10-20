"""仅调用的外部服务状态与人工核对输入。"""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from backend.schema.common.api_schema import ApiSchema
from backend.schema.models.model_schema import VersionInfo
from backend.schema.asr.target_status_schema import TargetPoolStatus
from backend.utils.time.time_utils import as_utc


class RemoteAsrStatus(ApiSchema):
    configured: bool
    available: bool = False
    state: Literal["unconfigured", "unavailable", "ready", "running", "uncertain", "stopping"]
    observed_at: datetime | None = None
    message: str
    current_task_id: str | None = None
    uncertain_task_id: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    version_info: VersionInfo | None = Field(default=None, exclude_if=lambda value: value is None)
    capacity: TargetPoolStatus | None = None

    @field_validator("observed_at")
    @classmethod
    def utc_time(cls, value):
        return as_utc(value) if value else None


class RemoteAsrRelease(ApiSchema):
    task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    confirm_request_ended: bool = False
    note: str = Field(min_length=1, max_length=512)

    @field_validator("note")
    @classmethod
    def require_note(cls, value):
        if not value.strip():
            raise ValueError("必须填写核对说明")
        return value.strip()


class RemoteAsrReleaseReceipt(ApiSchema):
    task_id: str
    released: bool
    replayed: bool
