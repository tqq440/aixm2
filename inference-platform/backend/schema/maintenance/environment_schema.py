"""只读交付环境检查结果。"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from backend.schema.common.api_schema import ApiSchema


class EnvironmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_path: Path | None = None


class EnvironmentCheck(ApiSchema):
    name: str
    passed: bool
    message: str


class EnvironmentReport(ApiSchema):
    passed: bool
    checks: list[EnvironmentCheck]
