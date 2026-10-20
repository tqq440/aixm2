"""通用 ModelSpec 注册契约；注册不代表存在对应执行器。"""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, JsonValue, model_validator

from backend.schema.common.api_schema import ApiSchema


class ModelIdentity(ApiSchema):
    model_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    model_version: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


class VersionInfo(ApiSchema):
    """未知上游版本时，登记键仅标识本地接入配置，不承诺权重可复现。"""

    model_config = ConfigDict(frozen=True)
    version_basis: Literal["integration_revision"]
    upstream_version: None = None


class ModelRegistration(ModelIdentity):
    model_config = ConfigDict(frozen=True, revalidate_instances="always", allow_inf_nan=False)

    task_type: Literal["asr", "vision", "llm"]
    execution_mode: Literal["offline", "online"]
    model_repo: str | None = Field(default=None, min_length=1, max_length=256)
    parameter_schema: dict[str, JsonValue]
    input_constraints: dict[str, JsonValue]
    output_capabilities: list[Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]] = Field(min_length=1, max_length=64)
    adapter_key: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    resource_requirements: dict[str, JsonValue]
    version_info: VersionInfo | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_execution_contract(self):
        if self.version_info is not None and (
            self.task_type != "asr" or self.adapter_key != "remote-v2-sensevoice"
            or not self.model_version.startswith("integration-")
        ):
            raise ValueError("本地接入修订仅用于明确标识的外部 ASR")
        if (self.task_type == "llm") != (self.execution_mode == "online"):
            raise ValueError("ASR/Vision 使用 offline，LLM 使用 online 执行模式")
        if len(set(self.output_capabilities)) != len(self.output_capabilities):
            raise ValueError("输出能力不得重复")
        return self


class RegistryVersionView(ApiSchema):
    spec: ModelRegistration
    enabled: bool
    is_default: bool
    execution_supported: bool


class RegistrationReceipt(ApiSchema):
    item: RegistryVersionView
    created: bool


class RegistryListQuery(ApiSchema):
    page: int = Field(default=1, ge=1, le=1_000_000)
    page_size: int = Field(default=20, ge=1, le=100)


class RegistryPage(ApiSchema):
    items: list[RegistryVersionView]
    total: int
    page: int
    page_size: int


class RegistrationFile(ApiSchema):
    file: Path
