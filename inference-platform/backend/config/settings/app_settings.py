"""唯一环境配置读取入口；导入本模块不会读取配置或创建目录。"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv.parser import parse_stream
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator

from backend.common.exceptions.app_error import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
ENV_FIELDS = {
    "INFERENCE_PUBLIC_API_ENABLED": "public_api_enabled",
    "INFERENCE_MONITOR_TOKEN": "monitor_token",
    "INFERENCE_INGRESS_TOKEN": "ingress_token",
    "INFERENCE_COMMON_EXECUTION_ENABLED": "common_execution_enabled",
    "INFERENCE_COMMON_QUEUE_BACKEND": "common_queue_backend",
    "INFERENCE_REDIS_URL": "redis_url",
    "INFERENCE_REDIS_NAMESPACE": "redis_namespace",
    "INFERENCE_REDIS_TIMEOUT_SECONDS": "redis_timeout_seconds",
    "INFERENCE_REDIS_REPUBLISH_SECONDS": "redis_republish_seconds",
    "INFERENCE_AGENT_TOKEN": "agent_token",
    "INFERENCE_HOST": "host",
    "INFERENCE_PORT": "port",
    "INFERENCE_DATABASE_PATH": "database_path",
    "INFERENCE_LOG_DIR": "log_dir",
    "INFERENCE_LOG_LEVEL": "log_level",
    "INFERENCE_UPLOAD_DIR": "upload_dir",
    "INFERENCE_ASR_MODEL_DIR": "asr_model_dir",
    "INFERENCE_ASR_MODEL_REPO": "asr_model_repo",
    "INFERENCE_ASR_MODEL_REVISION": "asr_model_revision",
    "INFERENCE_ASR_DEVICE": "asr_device",
    "INFERENCE_ASR_COMPUTE_TYPE": "asr_compute_type",
    "INFERENCE_ASR_CPU_THREADS": "asr_cpu_threads",
    "INFERENCE_ASR_MAX_FILE_BYTES": "asr_max_file_bytes",
    "INFERENCE_ASR_MAX_AUDIO_SECONDS": "asr_max_audio_seconds",
    "INFERENCE_REMOTE_ASR_BASE_URL": "remote_asr_base_url",
    "INFERENCE_REMOTE_ASR_MODEL_ID": "remote_asr_model_id",
    "INFERENCE_REMOTE_ASR_MODEL_VERSION": "remote_asr_model_version",
    "INFERENCE_REMOTE_ASR_PROTOCOL": "remote_asr_protocol",
    "INFERENCE_REMOTE_ASR_TRANSCRIBE_PATH": "remote_asr_transcribe_path",
    "INFERENCE_REMOTE_ASR_HEALTH_PATH": "remote_asr_health_path",
    "INFERENCE_REMOTE_ASR_UPSTREAM_MODEL": "remote_asr_upstream_model",
    "INFERENCE_REMOTE_ASR_API_KEY_ENV": "remote_asr_api_key_env",
    "INFERENCE_ASR_SERVICE_REVISION": "asr_service_revision",
    "INFERENCE_REMOTE_ASR_CA_FILE": "remote_asr_ca_file",
    "INFERENCE_REMOTE_ASR_INSECURE_TLS_ORIGIN": "remote_asr_insecure_tls_origin",
    "INFERENCE_REMOTE_ASR_CONNECT_TIMEOUT_SECONDS": "remote_asr_connect_timeout_seconds",
    "INFERENCE_REMOTE_ASR_TOTAL_TIMEOUT_SECONDS": "remote_asr_total_timeout_seconds",
    "INFERENCE_REMOTE_ASR_ROUTING_REVISION": "remote_asr_routing_revision",
    "INFERENCE_REMOTE_ASR_ROUTING_ISOLATION": "remote_asr_routing_isolation",
    "INFERENCE_VISION_MODEL_DIR": "vision_model_dir",
    "INFERENCE_VISION_CPU_THREADS": "vision_cpu_threads",
    "INFERENCE_VISION_MAX_FILE_BYTES": "vision_max_file_bytes",
    "INFERENCE_VISION_MAX_PIXELS": "vision_max_pixels",
    "INFERENCE_LEASE_SECONDS": "lease_seconds",
    "INFERENCE_RENEW_INTERVAL_SECONDS": "renew_interval_seconds",
    "INFERENCE_NOTIFICATION_VISIBILITY_SECONDS": "notification_visibility_seconds",
    "INFERENCE_RUNTIME_HEARTBEAT_SECONDS": "runtime_heartbeat_seconds",
    "INFERENCE_RUNTIME_STALE_SECONDS": "runtime_stale_seconds",
    "INFERENCE_POLL_INTERVAL_SECONDS": "poll_interval_seconds",
    "INFERENCE_DISPATCH_RETRY_SECONDS": "dispatch_retry_seconds",
    "INFERENCE_WORKER_STARTUP_TIMEOUT_SECONDS": "worker_startup_timeout_seconds",
    "INFERENCE_WORKER_STOP_TIMEOUT_SECONDS": "worker_stop_timeout_seconds",
    "INFERENCE_MAX_INFLIGHT_TASKS": "max_inflight_tasks",
    "INFERENCE_QUEUE_TIMEOUT_SECONDS": "queue_timeout_seconds",
    "INFERENCE_ATTEMPT_TIMEOUT_SECONDS": "attempt_timeout_seconds",
    "INFERENCE_TOTAL_TIMEOUT_SECONDS": "total_timeout_seconds",
    "INFERENCE_MAX_ATTEMPTS": "max_attempts",
    "INFERENCE_RETRY_WAIT_SECONDS": "retry_wait_seconds",
    "INFERENCE_LLM_ENDPOINT": "llm_endpoint",
    "INFERENCE_LLM_MODEL_ID": "llm_model_id",
    "INFERENCE_LLM_MODEL_VERSION": "llm_model_version",
    "INFERENCE_LLM_UPSTREAM_MODEL": "llm_upstream_model",
    "INFERENCE_LLM_API_KEY": "llm_api_key",
    "INFERENCE_LLM_MAX_CONCURRENT_REQUESTS": "llm_max_concurrent_requests",
    "INFERENCE_LLM_ADMISSION_WAIT_SECONDS": "llm_admission_wait_seconds",
    "INFERENCE_LLM_CONNECT_TIMEOUT_SECONDS": "llm_connect_timeout_seconds",
    "INFERENCE_LLM_READ_TIMEOUT_SECONDS": "llm_read_timeout_seconds",
    "INFERENCE_LLM_TOTAL_TIMEOUT_SECONDS": "llm_total_timeout_seconds",
    "INFERENCE_LLM_SLOW_CONSUMER_SECONDS": "llm_slow_consumer_seconds",
    "INFERENCE_LLM_LEASE_SECONDS": "llm_lease_seconds",
}


def asr_service_origin(value: str) -> str:
    """规范化手动配置的 HTTP(S) 服务根地址；不读取网络或推断运行位置。"""
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.port == 0
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or "?" in value or "#" in value
            or any(c in value for c in ("\\", "*", "%")) or parsed.netloc.endswith(":")
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("ASR 地址必须为 HTTP(S) 服务根地址，不含路径、凭据、通配或查询串")
    hostname = parsed.hostname.lower().encode("idna").decode("ascii")
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default_port
    return f"{parsed.scheme}://{host}" + (f":{port}" if port != default_port else "")


def asr_service_path(value: str) -> str:
    """接口路径始终属于已配置来源，不允许完整 URL、查询串或路径跳转。"""
    if (not value.startswith("/") or value.startswith("//") or len(value) > 512
            or any(part in (".", "..") for part in value.split("/"))
            or any(not (c.isascii() and (c.isalnum() or c in "/-._~")) for c in value)):
        raise ValueError("ASR 接口须为以 / 开头的路径，不含来源、查询串或相对跳转")
    return value


def resolve_remote_asr_api_key(settings: "AppSettings", *, environ: Mapping[str, str] | None = None) -> str | None:
    """秘密只在调用时从环境读取，不写入服务配置、任务指纹或公开错误。"""
    if settings.remote_asr_api_key_env is None:
        return None
    secret = (os.environ if environ is None else environ).get(settings.remote_asr_api_key_env)
    if (not secret or not secret.strip() or len(secret) > 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in secret)):
        raise ConfigurationError("ASR 鉴权环境变量未设置或值无效；请求尚未发送。")
    return secret


def remote_asr_origin(value: str) -> str:
    """规范化受控 HTTPS 来源；不解析 DNS，也不放宽到通配或任意请求路径。"""
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.port == 0
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or "?" in value or "#" in value
            or any(c in value for c in ("\\", "*", "%")) or parsed.netloc.endswith(":")
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("TLS 例外须绑定明确的 HTTPS 来源，不含路径、凭据、通配或查询串")
    if not parsed.hostname.isascii():
        raise ValueError("TLS 例外只接受 ASCII 主机名；国际化域名须使用明确的 xn-- 编码")
    hostname = parsed.hostname.lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port or 443
    return f"https://{host}" + (f":{port}" if port != 443 else "")


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)
    monitor_token: SecretStr | None = Field(default=None, repr=False, exclude=True)
    public_api_enabled: bool = False
    ingress_token: SecretStr | None = Field(default=None, repr=False, exclude=True)
    common_execution_enabled: bool = False

    @model_validator(mode="after")
    def public_configuration(self):
        if self.monitor_token:
            token=self.monitor_token.get_secret_value()
            if not self.common_execution_enabled or not 32<=len(token)<=256 or not token.isascii() or any(c.isspace() for c in token):
                raise ValueError("查询预算凭据须独立且启用公共执行")
            if any(secret and token==secret.get_secret_value() for secret in (self.ingress_token,self.agent_token,self.llm_api_key)):
                raise ValueError("查询预算不得复用入口/Agent/业务凭据")
        if self.ingress_token:
            value=self.ingress_token.get_secret_value()
            if not 32<=len(value)<=256 or not value.isascii() or any(c.isspace() for c in value):
                raise ValueError("入口凭据须为32到256位非空白ASCII字符")
        if self.public_api_enabled and (not self.common_execution_enabled or not self.ingress_token):
            raise ValueError("公共API须启用公共执行并提供独立入口凭据")
        return self
    common_queue_backend: Literal["sqlite", "redis"] = "sqlite"
    redis_url: SecretStr | None = Field(default=None, repr=False, exclude=True)
    redis_namespace: str = Field(default="inference", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    redis_timeout_seconds: float = Field(default=1, ge=0.1, le=3, allow_inf_nan=False)
    redis_republish_seconds: float = Field(default=30, ge=1, le=300, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_common_queue(self):
        if self.common_queue_backend == "redis" and (not self.common_execution_enabled or self.redis_url is None):
            raise ValueError("Redis 队列需要启用公共执行并配置 Redis 连接")
        if self.redis_url is not None:
            value = self.redis_url.get_secret_value()
            parsed = urlsplit(value)
            if (len(value) > 4096 or parsed.scheme not in {"redis", "rediss"} or not parsed.hostname
                    or parsed.port == 0 or parsed.query or parsed.fragment
                    or parsed.path not in {"", *('/' + str(i) for i in range(16))}
                    or any(c.isspace() or ord(c) < 32 for c in value)):
                raise ValueError("Redis 连接必须为 redis/rediss URL 和 0—15 数据库，不含查询串或片段")
        return self

    host: Literal["127.0.0.1"] = "127.0.0.1"
    agent_token: SecretStr | None = Field(default=None, min_length=32, max_length=256, repr=False, exclude=True)
    port: int = Field(default=5000, ge=1, le=65535)
    database_path: Path = Path("runtime/db/platform.sqlite3")
    log_dir: Path = Path("runtime/logs")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    upload_dir: Path = Path("runtime/uploads")
    asr_model_dir: Path = Path("runtime/models/whisper-small")
    asr_model_repo: Literal["Systran/faster-whisper-small"] = "Systran/faster-whisper-small"
    asr_model_revision: Literal["536b0662742c02347bc0e980a01041f333bce120"] = "536b0662742c02347bc0e980a01041f333bce120"
    asr_device: Literal["cpu"] = "cpu"
    asr_compute_type: Literal["int8"] = "int8"
    asr_cpu_threads: int = Field(default=4, ge=1, le=32)
    asr_max_file_bytes: int = Field(default=20 * 1024 * 1024, ge=1, le=20 * 1024 * 1024)
    asr_max_audio_seconds: float = Field(default=300, ge=1 / 16000, le=300, allow_inf_nan=False)
    remote_asr_base_url: str | None = Field(default=None, max_length=2048, repr=False)
    remote_asr_model_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    remote_asr_model_version: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
    remote_asr_protocol: Literal["remote-v2", "openai-audio"] = "remote-v2"
    remote_asr_transcribe_path: str = "/api/v1/asr/transcripts"
    remote_asr_health_path: str = "/api/v1/asr/health"
    remote_asr_upstream_model: str | None = Field(default=None, min_length=1, max_length=256)
    remote_asr_api_key_env: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    asr_service_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    asr_service_revision: str | None = Field(default=None, max_length=64, pattern=r"^(?:[a-f0-9]{64}|legacy)$")
    remote_asr_ca_file: Path | None = Field(default=None, repr=False)
    remote_asr_insecure_tls_origin: str | None = Field(default=None, max_length=2048, repr=False)
    remote_asr_connect_timeout_seconds: float = Field(default=5, ge=0.1, le=30, allow_inf_nan=False)
    remote_asr_total_timeout_seconds: float = Field(default=600, ge=0.1, le=900, allow_inf_nan=False)
    remote_asr_routing_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    remote_asr_routing_isolation: bool = False
    vision_model_dir: Path = Path("runtime/models/yolov5n-onnx")
    vision_cpu_threads: int = Field(default=4, ge=1, le=32)
    vision_max_file_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=10 * 1024 * 1024)
    vision_max_pixels: int = Field(default=20_000_000, ge=1, le=20_000_000)
    lease_seconds: float = Field(default=30, ge=1, le=300, allow_inf_nan=False)
    renew_interval_seconds: float = Field(default=10, ge=0.1, le=100, allow_inf_nan=False)
    notification_visibility_seconds: float = Field(default=30, ge=1, le=300, allow_inf_nan=False)
    runtime_heartbeat_seconds: float = Field(default=2, ge=0.1, le=30, allow_inf_nan=False)
    runtime_stale_seconds: float = Field(default=15, ge=1, le=300, allow_inf_nan=False)
    poll_interval_seconds: float = Field(default=0.25, ge=0.05, le=5, allow_inf_nan=False)
    dispatch_retry_seconds: float = Field(default=5, ge=0.1, le=300, allow_inf_nan=False)
    worker_startup_timeout_seconds: float = Field(default=120, ge=1, le=600, allow_inf_nan=False)
    worker_stop_timeout_seconds: float = Field(default=10, ge=1, le=60, allow_inf_nan=False)
    max_inflight_tasks: int = Field(default=20, ge=1, le=20)
    queue_timeout_seconds: float = Field(default=600, ge=0.1, le=86400, allow_inf_nan=False)
    attempt_timeout_seconds: float = Field(default=900, ge=0.1, le=86400, allow_inf_nan=False)
    total_timeout_seconds: float = Field(default=1800, ge=0.1, le=86400, allow_inf_nan=False)
    max_attempts: int = Field(default=2, ge=1, le=10)
    retry_wait_seconds: float = Field(default=5, ge=0.1, le=3600, allow_inf_nan=False)
    llm_endpoint: str | None = Field(default=None, max_length=2048, repr=False)
    llm_model_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    llm_model_version: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
    llm_upstream_model: str | None = Field(default=None, min_length=1, max_length=256)
    llm_api_key: SecretStr | None = Field(default=None, repr=False, exclude=True)
    llm_max_concurrent_requests: int = Field(default=1, ge=1, le=4)
    llm_admission_wait_seconds: float = Field(default=2, ge=0.1, le=10, allow_inf_nan=False)
    llm_connect_timeout_seconds: float = Field(default=5, ge=0.1, le=30, allow_inf_nan=False)
    llm_read_timeout_seconds: float = Field(default=30, ge=0.1, le=120, allow_inf_nan=False)
    llm_total_timeout_seconds: float = Field(default=120, ge=0.1, le=600, allow_inf_nan=False)
    llm_slow_consumer_seconds: float = Field(default=5, ge=0.1, le=30, allow_inf_nan=False)
    llm_lease_seconds: float = Field(default=15, ge=3, le=60, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_remote_asr_config(self):
        group = (self.remote_asr_base_url, self.remote_asr_model_id)
        if any(value is not None for value in group) and not all(group):
            raise ValueError("外部 ASR 地址与平台模型标识必须同时配置")
        if self.remote_asr_model_version is not None and not all(group):
            raise ValueError("外部 ASR 上游版本须与实际连接配置一同提供")
        if self.remote_asr_ca_file is not None and not all(group):
            raise ValueError("外部 ASR CA 文件须与完整连接配置一同提供")
        if self.remote_asr_base_url:
            asr_service_origin(self.remote_asr_base_url)
            if self.remote_asr_ca_file is not None and urlsplit(self.remote_asr_base_url).scheme != "https":
                raise ValueError("ASR 可信 CA 只能用于 HTTPS 服务")
        if self.remote_asr_protocol == "openai-audio" and not self.remote_asr_upstream_model:
            raise ValueError("OpenAI Audio 协议须配置服务端模型名")
        if self.remote_asr_upstream_model is not None:
            if (not self.remote_asr_upstream_model.strip()
                    or any(ord(c) < 32 or ord(c) == 127 for c in self.remote_asr_upstream_model)):
                raise ValueError("ASR 服务端模型名无效")
            if not all(group):
                raise ValueError("ASR 服务端模型名须与完整连接配置一同提供")
        if self.remote_asr_api_key_env is not None and not all(group):
            raise ValueError("ASR 鉴权环境变量名须与完整连接配置一同提供")
        if self.remote_asr_insecure_tls_origin is not None:
            if not all(group):
                raise ValueError("TLS 例外须与完整外部 ASR 配置一同提供")
            if self.remote_asr_ca_file is not None:
                raise ValueError("可信 CA 与跳过证书验证不能同时配置")
            if self.remote_asr_insecure_tls_origin != remote_asr_origin(self.remote_asr_base_url):
                raise ValueError("TLS 例外仅能用于明确指定的外部 ASR 来源")
        return self

    @field_validator("remote_asr_transcribe_path", "remote_asr_health_path")
    @classmethod
    def validate_remote_paths(cls, value: str) -> str:
        return asr_service_path(value)

    @field_validator("remote_asr_model_version", mode="before")
    @classmethod
    def normalize_optional_remote_version(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("remote_asr_insecure_tls_origin", mode="before")
    @classmethod
    def validate_remote_insecure_origin(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or urlsplit(value).path:
            raise ValueError("TLS 例外必须为不含路径的明确 HTTPS 来源")
        return remote_asr_origin(value)

    @field_validator("remote_asr_ca_file", mode="before")
    @classmethod
    def resolve_remote_ca_path(cls, value: object) -> Path | None:
        if value is None:
            return None
        path = cls.resolve_path(value)
        if not path.is_file():
            raise ValueError("外部 ASR CA 文件必须是可用的证书文件")
        return path

    @model_validator(mode="after")
    def validate_llm_config(self):
        group = (self.llm_endpoint, self.llm_model_id, self.llm_model_version, self.llm_upstream_model)
        if any(value is not None for value in group) and not all(group):
            raise ValueError("LLM 地址、平台模型标识、版本和上游模型名必须同时配置")
        if self.llm_upstream_model is not None and not self.llm_upstream_model.strip():
            raise ValueError("LLM 上游模型名不能为空白")
        if self.llm_api_key is not None:
            secret = self.llm_api_key.get_secret_value()
            if not all(group) or not secret.strip() or any(ord(c) < 32 or ord(c) == 127 for c in secret):
                raise ValueError("LLM 密钥配置无效")
        if self.llm_endpoint:
            parsed = urlsplit(self.llm_endpoint)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.port == 0 or parsed.username
                    or parsed.password or parsed.query or parsed.fragment
                    or parsed.path.rstrip("/") != "/v1/chat/completions"
                    or any(c.isspace() or ord(c) < 32 for c in self.llm_endpoint)):
                raise ValueError("LLM 地址必须为完整的 HTTP(S) /v1/chat/completions 地址，不含凭据或查询串")
            _ = parsed.port
        return self

    @model_validator(mode="after")
    def validate_runtime_intervals(self):
        if self.renew_interval_seconds * 2 >= min(self.lease_seconds, self.notification_visibility_seconds):
            raise ValueError("续租间隔必须小于租约与通知可见期的一半")
        if self.runtime_heartbeat_seconds * 2 >= self.runtime_stale_seconds:
            raise ValueError("状态报告间隔必须小于失联阈值的一半")
        if self.poll_interval_seconds > min(self.renew_interval_seconds, self.runtime_heartbeat_seconds):
            raise ValueError("轮询间隔不得大于续租与状态报告间隔")
        return self

    @field_validator("database_path", "log_dir", "asr_model_dir", "upload_dir", "vision_model_dir", mode="before")
    @classmethod
    def resolve_path(cls, value: object) -> Path:
        if isinstance(value, str) and not value.strip():
            raise ValueError("路径不能为空")
        if not isinstance(value, (str, Path)):
            raise ValueError("必须为文件路径")
        path = Path(value).expanduser()
        return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()

    @field_validator("database_path")
    @classmethod
    def validate_database_file(cls, value: Path) -> Path:
        if value.is_dir():
            raise ValueError("数据库路径必须是文件")
        return value

    @field_validator("log_dir", "asr_model_dir", "upload_dir", "vision_model_dir")
    @classmethod
    def validate_log_directory(cls, value: Path) -> Path:
        if value.exists() and not value.is_dir():
            raise ValueError("必须是目录路径")
        return value


def load_settings(
    env_file: Path | None = DEFAULT_ENV_FILE,
    *,
    environ: Mapping[str, str] | None = None,
    include_service_selection: bool = True,
) -> AppSettings:
    """优先级：进程环境变量 > 项目 .env > 默认值；不修改进程环境。"""
    values: dict[str, str | None] = {}
    try:
        if env_file is not None and env_file.exists():
            # 使用锁定版本的 dotenv 解析器检查语法；损坏配置不能静默回退。
            # 不展开 ${...}，避免依赖读取过程之外的隐式环境来源。
            with env_file.open(encoding="utf-8-sig") as stream:
                for binding in parse_stream(stream):
                    if binding.error:
                        raise ConfigurationError(f".env 第 {binding.original.line} 行语法错误，请检查引号或等号。")
                    if binding.key is not None:
                        values[binding.key] = binding.value
        values.update(os.environ if environ is None else environ)
        unknown = sorted(key for key in values if key.startswith("INFERENCE_") and key not in ENV_FIELDS)
        if unknown:
            raise ConfigurationError("未知配置项：" + ", ".join(unknown))
        settings = {field: values[key] for key, field in ENV_FIELDS.items() if key in values}
        configured = AppSettings.model_validate(settings)
        if include_service_selection:
            from backend.config.asr.service_config import apply_service_selection

            try:
                configured = apply_service_selection(configured)
            except (ValueError, OSError):
                raise ConfigurationError("ASR 接口配置文件无效或修订不可用；未回退到旧连接。") from None
        return configured
    except ValidationError as exc:
        names = {value: key for key, value in ENV_FIELDS.items()}
        issues = [f"{names.get(str(item['loc'][0]), str(item['loc'][0])) if item['loc'] else '运行间隔组合'} ({item['type']})" for item in exc.errors()]
        raise ConfigurationError("配置无效：" + "; ".join(issues) + "。请参考 env.example。") from None
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("无法读取配置或解析路径，请检查 .env 编码和目录权限。") from exc
