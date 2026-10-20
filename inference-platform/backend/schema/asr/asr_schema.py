"""本地转写输入输出契约；本阶段尚不通过 HTTP 接收文件路径。"""

from pathlib import Path
from typing import Annotated

from pydantic import ConfigDict, Field, field_validator, model_validator

from backend.schema.common.api_schema import ApiSchema


class ModelFileInfo(ApiSchema):
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelManifest(ApiSchema):
    model_repo: str
    model_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: dict[str, ModelFileInfo]


class TranscriptionRequest(ApiSchema):
    audio_path: Path
    language: str | None = Field(default="zh", pattern=r"^[a-z]{2,3}$")
    request_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

    @field_validator("audio_path", mode="before")
    @classmethod
    def validate_audio_path(cls, value: object) -> Path:
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError("音频路径不能为空")
        return Path(value).expanduser().resolve()


class PrepareOptions(ApiSchema):
    check: bool = False


class TranscriptionOptions(TranscriptionRequest):
    output_path: Path | None = None

    @field_validator("output_path", mode="before")
    @classmethod
    def validate_output_path(cls, value: object) -> Path | None:
        if value is None:
            return None
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError("结果路径不能为空")
        return Path(value).expanduser().resolve()


class TranscriptionSegment(ApiSchema):
    model_config = ConfigDict(allow_inf_nan=False)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str

    @model_validator(mode="after")
    def validate_interval(self):
        if self.end < self.start:
            raise ValueError("分段结束时间不能早于开始时间")
        return self


class AsrEngineMetadata(ApiSchema):
    """引擎报告的可选运行事实；不包含地址、凭据或任意扩展字段。"""

    model_config = ConfigDict(allow_inf_nan=False)
    adapter_key: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    model_repo: str | None = Field(default=None, min_length=1, max_length=256)
    model_revision: str | None = Field(default=None, min_length=1, max_length=128)
    device: str | None = Field(default=None, min_length=1, max_length=64)
    compute_type: str | None = Field(default=None, min_length=1, max_length=64)
    cpu_threads: int | None = Field(default=None, ge=1)
    model_load_seconds: float | None = Field(default=None, ge=0)
    transcription_seconds: float | None = Field(default=None, ge=0)
    total_seconds: float | None = Field(default=None, ge=0)
    runtime_versions: dict[
        Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")],
        Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+\-]*$")],
    ] = Field(default_factory=dict, max_length=16)


class TranscriptionResult(ApiSchema):
    model_config = ConfigDict(allow_inf_nan=False)
    text: str
    segments: list[TranscriptionSegment]
    language: str | None = None
    language_requested: str | None = None
    language_probability: float | None = Field(default=None, ge=0, le=1)
    audio_duration_seconds: float = Field(gt=0)
    engine_metadata: AsrEngineMetadata | None = Field(default=None, exclude_if=lambda value: value is None)
    # 第2—12部分已持久化的扁平字段继续可读；新适配器无此事实时不得虚构。
    model_repo: str | None = None
    model_revision: str | None = None
    device: str | None = None
    compute_type: str | None = None
    cpu_threads: int | None = Field(default=None, ge=1)
    model_load_seconds: float | None = Field(default=None, ge=0)
    transcription_seconds: float | None = Field(default=None, ge=0)
    total_seconds: float | None = Field(default=None, ge=0)
    faster_whisper_version: str | None = None
    ctranslate2_version: str | None = None

    @model_validator(mode="after")
    def validate_engine_metadata_consistency(self):
        metadata = self.engine_metadata
        if metadata is not None:
            for name in ("model_repo", "model_revision", "device", "compute_type", "cpu_threads",
                         "model_load_seconds", "transcription_seconds", "total_seconds"):
                legacy, current = getattr(self, name), getattr(metadata, name)
                if legacy is not None and current is not None and legacy != current:
                    raise ValueError("引擎元信息与历史兼容字段不一致")
            for name, library in (("faster_whisper_version", "faster-whisper"), ("ctranslate2_version", "ctranslate2")):
                legacy, current = getattr(self, name), metadata.runtime_versions.get(library)
                if legacy is not None and current is not None and legacy != current:
                    raise ValueError("引擎库版本与历史兼容字段不一致")
        return self
