"""业务协议、输入清单与逐调用监控；配置资料不代表实测。"""

from typing import Literal
from pydantic import Field, field_validator, model_validator

from backend.config.settings.app_settings import asr_service_path
from backend.schema.catalog.service_schema import Identifier
from backend.schema.common.api_schema import ApiSchema
from backend.schema.vision.detection_schema import BoundingBox


class TaskQueryConfig(ApiSchema):
    protocol: Literal["none", "job-json-v1"] = "none"
    status_path: str | None = Field(default=None, max_length=512)
    result_path: str | None = Field(default=None, max_length=512)
    cancel_path: str | None = Field(default=None, max_length=512)
    result_in_status: bool = False
    id_source: Literal["invocation", "job"] = "invocation"
    interval_seconds: float = Field(default=5, ge=5, le=60, allow_inf_nan=False)
    timeout_seconds: float = Field(default=3, gt=0, le=10, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_paths(self):
        if self.protocol == "none" and any((self.status_path, self.result_path, self.cancel_path,self.result_in_status)):
            raise ValueError("未声明查询协议不能配置路径")
        if self.protocol != "none" and not self.status_path:
            raise ValueError("查询协议需要状态路径")
        for path in (self.status_path, self.result_path, self.cancel_path):
            if path:
                if path.count("{requestId}") != 1:
                    raise ValueError("路径须含一个 {requestId}")
                value = path.replace("{requestId}", "request-id")
                if "{" in value or "}" in value: raise ValueError("不能包含其他模板")
                asr_service_path(value)
        return self


class NodeUpstreamConfig(ApiSchema):
    protocol: Literal["remote-v2", "openai-audio", "vision-json-v1", "chat-completions-sse"]
    invoke_path: str = Field(max_length=512)
    mode: Literal["sync", "async", "stream"] = "sync"
    upstream_model: str | None = Field(default=None, min_length=1, max_length=256)
    token_env: str | None = Field(default=None, pattern=r"^NODE_UPSTREAM_TOKEN_[A-Z0-9_]+$", max_length=128)
    ca_file: str | None = Field(default=None, min_length=1, max_length=2048)
    timeout_seconds: float = Field(default=300, gt=0, le=900, allow_inf_nan=False)
    connect_timeout_seconds: float = Field(default=5, gt=0, le=30, allow_inf_nan=False)
    query: TaskQueryConfig = Field(default_factory=TaskQueryConfig)

    @field_validator("invoke_path")
    @classmethod
    def valid_path(cls, value): return asr_service_path(value)

    @field_validator("ca_file")
    @classmethod
    def local_ca(cls,value):
        if value and (value.startswith(("//","\\\\")) or "://" in value):raise ValueError("CA 必须为本机证书文件")
        return value

    @model_validator(mode="after")
    def coherent(self):
        if (self.protocol == "chat-completions-sse") != (self.mode == "stream"):
            raise ValueError("LLM 必须用 stream，其他协议不能用 stream")
        if self.protocol != "remote-v2" and not self.upstream_model:
            raise ValueError("此协议须配置服务端模型名")
        return self


class RemoteDetection(ApiSchema):
    class_id: int = Field(ge=0, le=1_000_000, strict=True)
    class_name: str = Field(min_length=1, max_length=128)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    box: BoundingBox


class RemoteDetectionResult(ApiSchema):
    schema_version: Literal["vision-json-v1"] = "vision-json-v1"
    model_id: Identifier
    model_version: str | None = Field(default=None, max_length=64)
    image_width: int = Field(gt=0, le=20_000, strict=True)
    image_height: int = Field(gt=0, le=20_000, strict=True)
    coordinate_system: Literal["original-pixels-exif-normalized"] = "original-pixels-exif-normalized"
    detections: list[RemoteDetection] = Field(max_length=300)

    @model_validator(mode="after")
    def bounds(self):
        if any(d.box.x_max > self.image_width or d.box.y_max > self.image_height for d in self.detections):
            raise ValueError("检测框越界")
        return self


class ActualDevice(ApiSchema):
    kind: Literal["cpu", "gpu"]
    device_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:/-]+$")
    namespace: Literal["service","host"] = "service"


class ExecutionManifest(ApiSchema):
    kind: Literal["asr", "vision"]
    size_bytes: int = Field(gt=0, le=20*1024*1024, strict=True)
    model_version: str | None = Field(default=None, max_length=64)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")
    audio_duration_seconds: float | None = Field(default=None, gt=0, le=300, allow_inf_nan=False)
    image_width: int | None = Field(default=None, gt=0, le=20_000, strict=True)
    image_height: int | None = Field(default=None, gt=0, le=20_000, strict=True)
    confidence_threshold: float = Field(default=0.25, gt=0, le=1, allow_inf_nan=False)
    iou_threshold: float = Field(default=0.45, gt=0, le=1, allow_inf_nan=False)
    max_detections: int = Field(default=100, ge=1, le=300, strict=True)

    @model_validator(mode="after")
    def matches_kind(self):
        if self.kind == "asr" and (self.audio_duration_seconds is None or self.image_width or self.image_height):
            raise ValueError("音频需时长，不能含图片尺寸")
        if self.kind == "vision" and (not self.image_width or not self.image_height or self.size_bytes > 10*1024*1024
                or self.audio_duration_seconds is not None or self.language is not None):
            raise ValueError("图片需尺寸且不能含音频参数")
        return self
