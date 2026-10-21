"""Node 专属环境入口；不读取日常 .env 或平台 AppSettings。"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from backend.common.exceptions.app_error import ConfigurationError


PROJECT_ROOT = Path(__file__).resolve().parents[3]
NODE_ENV_FIELDS = {
    "NODE_ID": "node_id", "NODE_DATABASE_PATH": "database_path", "NODE_LOG_DIR": "log_dir",
    "NODE_HOST": "host", "NODE_PORT": "port", "NODE_ADMIN_TOKEN": "admin_token",
    "NODE_OBSERVER_TOKEN": "observer_token", "NODE_HEARTBEAT_SECONDS": "heartbeat_seconds",
    "NODE_STALE_SECONDS": "stale_seconds", "NODE_LOG_LEVEL": "log_level",
    "NODE_DOCKER_ENABLED": "docker_enabled", "NODE_DOCKER_SOCKET": "docker_socket",
    "NODE_CONTROL_TIMEOUT_SECONDS": "control_timeout_seconds", "NODE_METRICS_ENABLED": "metrics_enabled",
    "NODE_METRICS_SECONDS": "metrics_seconds",
    "NODE_EXECUTOR_TOKEN": "executor_token",
    "NODE_BUSINESS_ENABLED": "business_enabled",
    "NODE_CENTRAL_URL": "central_url", "NODE_INGRESS_TOKEN": "ingress_token",
}


class NodeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    database_path: Path = PROJECT_ROOT / "runtime/host-node/node.sqlite3"
    log_dir: Path = PROJECT_ROOT / "runtime/host-node/logs"
    host: Literal["127.0.0.1", "0.0.0.0", "::1"] = "127.0.0.1"
    port: int = Field(default=8020, ge=1, le=65535)
    admin_token: SecretStr = Field(exclude=True, repr=False)
    observer_token: SecretStr | None = Field(default=None, exclude=True, repr=False)
    executor_token: SecretStr | None = Field(default=None, exclude=True, repr=False)
    upstream_tokens: dict[str, SecretStr] = Field(default_factory=dict, exclude=True, repr=False)
    business_enabled: bool = False
    central_url: str | None = None
    ingress_token: SecretStr | None = Field(default=None, exclude=True, repr=False)

    @field_validator("central_url")
    @classmethod
    def central_address(cls,value):
        if value is not None:
            from backend.config.settings.app_settings import asr_service_origin
            return asr_service_origin(value)
        return value
    report_tokens: dict[str, SecretStr] = Field(default_factory=dict, exclude=True, repr=False)
    heartbeat_seconds: int = Field(default=5, ge=1, le=60)
    stale_seconds: int = Field(default=15, ge=3, le=300)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    docker_enabled: bool = False
    docker_socket: str = "/var/run/docker.sock"
    control_timeout_seconds: float = Field(default=3, ge=0.1, le=10, allow_inf_nan=False)
    metrics_enabled: bool = True
    metrics_seconds: int = Field(default=5, ge=5, le=60)

    @field_validator("docker_socket")
    @classmethod
    def local_docker_socket(cls, value):
        if not value.startswith("/") or value.startswith("//") or any(c.isspace() for c in value) or ".." in value.split("/") or ":" in value:
            raise ValueError("Docker 只使用显式本机 Unix socket")
        return value

    @field_validator("admin_token", "observer_token", "executor_token")
    @classmethod
    def valid_token(cls, value):
        if value is not None:
            text = value.get_secret_value()
            if len(text) < 32 or len(text) > 256 or not text.isascii() or any(c.isspace() for c in text):
                raise ValueError("Node 凭据须为 32—256 个非空白 ASCII 字符")
        return value

    @field_validator("database_path", "log_dir")
    @classmethod
    def local_path(cls, value):
        raw = str(value)
        if raw.startswith(("\\\\", "//")) or "://" in raw:
            raise ValueError("Node 使用本机独立路径")
        resolved = value.resolve()
        if str(resolved).startswith(("\\\\", "//")):
            raise ValueError("Node 使用本机独立路径")
        return resolved

    @model_validator(mode="after")
    def isolation_and_credentials(self):
        if self.database_path == (PROJECT_ROOT / "runtime/db/platform.sqlite3").resolve():
            raise ValueError("Node 不允许使用日常平台数据库")
        if self.stale_seconds < self.heartbeat_seconds * 2:
            raise ValueError("过期间隔至少为心跳间隔两倍")
        secrets = [self.admin_token.get_secret_value()]
        if self.observer_token:
            secrets.append(self.observer_token.get_secret_value())
        if (self.central_url is None)!=(self.ingress_token is None):
            raise ValueError("中心地址与入口凭据须一起提供")
        if self.ingress_token:
            self.valid_token(self.ingress_token)
            secrets.append(self.ingress_token.get_secret_value())
        if self.executor_token:
            secrets.append(self.executor_token.get_secret_value())
        for name, token in self.report_tokens.items():
            if not name.startswith("NODE_REPORT_TOKEN_") or not name.removeprefix("NODE_REPORT_TOKEN_").replace("_", "").isalnum():
                raise ValueError("上报凭据使用 NODE_REPORT_TOKEN_ 前缀")
            self.valid_token(token)
            secrets.append(token.get_secret_value())
        if len(secrets) != len(set(secrets)):
            raise ValueError("管理、观察、中心执行及各服务上报凭据必须独立")
        for name, token in self.upstream_tokens.items():
            if not name.startswith("NODE_UPSTREAM_TOKEN_") or not name.removeprefix("NODE_UPSTREAM_TOKEN_").replace("_", "").isalnum():
                raise ValueError("业务凭据使用 NODE_UPSTREAM_TOKEN_ 前缀")
            self.valid_token(token)
            if token.get_secret_value() in secrets:
                raise ValueError("业务调用凭据不能复用控制面凭据")
        return self


def load_node_settings(*, env_file: Path | None = None, environ: Mapping[str, str] | None = None) -> NodeSettings:
    """仅显式指定时读取 Node 环境文件，进程环境优先；错误不包含秘密原值。"""
    environment = dict(os.environ if environ is None else environ)
    try:
        values = {} if env_file is None else dict(dotenv_values(env_file, interpolate=False))
        if env_file is not None and not env_file.is_file():
            raise ValueError("Node 环境文件不存在")
        values.update(environment)
        arguments = {field: values[key] for key, field in NODE_ENV_FIELDS.items() if values.get(key) not in (None, "")}
        arguments["report_tokens"] = {key: value for key, value in values.items()
                                      if key.startswith("NODE_REPORT_TOKEN_") and value}
        arguments["upstream_tokens"] = {key: value for key, value in values.items()
                                        if key.startswith("NODE_UPSTREAM_TOKEN_") and value}
        return NodeSettings.model_validate(arguments)
    except (OSError, ValueError) as exc:
        raise ConfigurationError("Node 配置无效，请核对 NODE_ID、独立路径及不同用途的凭据。") from None
