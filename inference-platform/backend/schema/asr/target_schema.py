"""受控目标和本地执行槽；不接受用户提供的命令或推理代码。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel, StrictInt, field_validator


class TargetBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str = Field(default="remote-default", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    slot_index: StrictInt = Field(default=0, ge=0, le=1023)


class TargetRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    group_id: str = Field(default="remote-asr-shared", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    independence_evidence: str | None = Field(default=None, min_length=1, max_length=2048)


class TargetCommandResult(RootModel[dict[str, Any]]):
    """仅本机CLI输出，日期等字段由Pydantic统一序列化。"""


class TargetCapacityCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_limit: StrictInt = Field(ge=1, le=8)
    group_limit: StrictInt = Field(ge=1, le=8)
    evidence: str = Field(min_length=1, max_length=2048)

    @field_validator("evidence")
    @classmethod
    def require_evidence(cls, value):
        if not value.strip():
            raise ValueError("必须提供容量依据，不能把配置值当作已实测容量")
        return value.strip()
