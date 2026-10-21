"""由用户维护的 ASR 接口配置；服务位置不参与协议选择。"""

from typing import Literal

from pydantic import Field

from backend.schema.common.api_schema import ApiSchema


class AsrServiceConfig(ApiSchema):
    service_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    name: str = Field(min_length=1, max_length=100, pattern=r"\S")
    protocol: Literal["remote-v2", "openai-audio"] = "remote-v2"
    base_url: str = Field(min_length=1, max_length=2048)
    transcribe_path: str = Field(default="/api/v1/asr/transcripts", max_length=1024)
    health_path: str = Field(default="/api/v1/asr/health", max_length=1024)
    model_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    model_version: str | None = Field(default=None, max_length=64)
    upstream_model: str | None = Field(default=None, max_length=256)
    api_key_env: str | None = Field(default=None, max_length=128)
    ca_file: str | None = Field(default=None, max_length=2048)
    insecure_tls_origin: str | None = Field(default=None, max_length=2048)
    connect_timeout_seconds: float = Field(default=5, ge=0.1, le=30, allow_inf_nan=False)
    total_timeout_seconds: float = Field(default=600, ge=0.1, le=900, allow_inf_nan=False)


class ServiceConfigSave(ApiSchema):
    config: AsrServiceConfig
    expected_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ServiceConfigDelete(ApiSchema):
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class ServiceSelection(ApiSchema):
    service_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    expected_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ServiceConfigTest(ApiSchema):
    config: AsrServiceConfig


class ServiceConfigDocument(ApiSchema):
    version: Literal[1] = 1
    items: dict[str, str] = Field(default_factory=dict, max_length=64)
    revisions: dict[str, AsrServiceConfig] = Field(default_factory=dict, max_length=512)
    selected_revision: str | None = None
