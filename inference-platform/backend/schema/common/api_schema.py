"""所有 HTTP JSON 的字段映射和公共契约。"""

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

T = TypeVar("T")


class ApiSchema(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class RequestHeaders(ApiSchema):
    request_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ValidationIssue(ApiSchema):
    field: str
    code: str


class ErrorDetail(ApiSchema):
    code: str
    message: str
    details: list[ValidationIssue] = Field(default_factory=list)


class ErrorResponse(ApiSchema):
    error: ErrorDetail
    request_id: str


class SuccessResponse(ApiSchema, Generic[T]):
    data: T
    request_id: str
