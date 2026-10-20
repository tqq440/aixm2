"""上传、任务查询和固定模型目录的对外数据契约。"""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from backend.schema.common.api_schema import ApiSchema
from backend.schema.models.model_schema import VersionInfo
from backend.schema.asr.asr_schema import TranscriptionResult
from backend.schema.vision.detection_schema import DetectionResult
from backend.schema.execution.execution_schema import FailureInfo
from backend.schema.llm.online_schema import LlmInputConstraints, LlmModelParameters
from backend.utils.time.time_utils import as_utc
from backend.schema.asr.routing_schema import TaskRoutingView


class SubmissionHeaders(ApiSchema):
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class AsrTaskForm(ApiSchema):
    model_id: str = Field(default="whisper-small", min_length=1, max_length=64)
    model_version: str | None = Field(default=None, min_length=1, max_length=64)
    language: str | None = Field(default="zh", pattern=r"^[a-z]{2,3}$")

    @field_validator("language", mode="before")
    @classmethod
    def normalize_language(cls, value):
        return None if value == "auto" else value


class UploadMetadata(ApiSchema):
    file_name: str = Field(min_length=1, max_length=255)

    @field_validator("file_name", mode="before")
    @classmethod
    def display_name_only(cls, value):
        if not isinstance(value, str):
            return value
        # 仅保存显示名称；磁盘文件名完全由服务端生成。
        name = value.replace("\\", "/").rsplit("/", 1)[-1]
        name = "".join(char for char in name if ord(char) >= 32 and ord(char) != 127).strip()
        if name in (".", ".."):
            raise ValueError("文件名无效")
        return name


class TaskPath(ApiSchema):
    task_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class TaskListQuery(ApiSchema):
    page: int = Field(default=1, ge=1, le=1_000_000)
    page_size: int = Field(default=20, ge=1, le=100)
    task_type: Literal["asr", "vision", "all"] = "asr"


class ModelListQuery(ApiSchema):
    task_type: Literal["asr", "vision", "llm", "all"] = "asr"


class TaskParameters(ApiSchema):
    language: str | None


class VisionTaskParameters(ApiSchema):
    confidence_threshold: float = Field(default=0.25, gt=0, le=1, allow_inf_nan=False)
    iou_threshold: float = Field(default=0.45, gt=0, le=1, allow_inf_nan=False)
    max_detections: int = Field(default=100, ge=1, le=300)


class VisionTaskForm(VisionTaskParameters):
    model_id: str = Field(default="yolo-general", min_length=1, max_length=64)
    model_version: str | None = Field(default=None, min_length=1, max_length=64)


class ImageAccessQuery(ApiSchema):
    variant: Literal["original", "annotated"] = "original"


class TaskInput(ApiSchema):
    file_name: str
    size_bytes: int = Field(gt=0)
    sha256: str
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)


class ImageTaskInput(ApiSchema):
    file_name: str
    size_bytes: int = Field(gt=0)
    sha256: str
    media_type: Literal["image"] = "image"
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class TaskView(ApiSchema):
    task_id: str
    caller_id: str
    task_type: Literal["asr", "vision"]
    model_id: str
    model_version: str
    version_info: VersionInfo | None = Field(default=None, exclude_if=lambda value: value is None)
    parameters: TaskParameters | VisionTaskParameters
    status: Literal["created", "queued", "running", "retry_wait", "cancel_requested", "succeeded", "failed", "cancelled"]
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=2, ge=1)
    execution_plan_version: int
    input: TaskInput | ImageTaskInput
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    queue_deadline_at: datetime | None = None
    total_deadline_at: datetime | None = None
    attempt_deadline_at: datetime | None = None
    next_retry_at: datetime | None = None
    error: FailureInfo | None = None
    result_available: bool = False
    message: str = "任务已保存，等待投递器发送通知。"
    cancel_allowed: bool = False
    cancel_unavailable_reason: str | None = None
    remote_outcome: Literal["not_dispatched", "pending", "confirmed", "unknown"] | None = None
    routing: TaskRoutingView | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_task_contract(self):
        if self.task_type == "asr":
            valid = isinstance(self.parameters, TaskParameters) and isinstance(self.input, TaskInput)
        else:
            valid = isinstance(self.parameters, VisionTaskParameters) and isinstance(self.input, ImageTaskInput)
        if not valid:
            raise ValueError("任务类型必须与输入及参数契约相符")
        return self

    @field_validator("created_at", "updated_at", "started_at", "finished_at",
                     "queue_deadline_at", "total_deadline_at", "attempt_deadline_at", "next_retry_at")
    @classmethod
    def utc_time(cls, value):
        # SQLite DateTime 读取时不带时区；数据库约定统一写 UTC。
        return as_utc(value) if value is not None else None


class TaskResultView(ApiSchema):
    task_id: str
    model_id: str
    model_version: str
    version_info: VersionInfo | None = Field(default=None, exclude_if=lambda value: value is None)
    schema_version: Literal["1"] = "1"
    output: TranscriptionResult | DetectionResult


class TaskReceipt(ApiSchema):
    task: TaskView
    replayed: bool


class CancelReceipt(ApiSchema):
    task: TaskView
    decision: Literal["cancelled", "cancel_requested", "already_finished"]


class TaskPage(ApiSchema):
    items: list[TaskView]
    total: int
    page: int
    page_size: int


class InputConstraints(ApiSchema):
    max_file_bytes: int
    max_audio_seconds: float


class VisionInputConstraints(ApiSchema):
    max_file_bytes: int = Field(gt=0)
    max_pixels: int = Field(gt=0)
    formats: list[Literal["JPEG", "PNG"]]


class VisionModelParameters(ApiSchema):
    default_confidence_threshold: float
    default_iou_threshold: float
    default_max_detections: int
    max_detections: int


class ModelParameters(ApiSchema):
    default_language: str = "zh"
    auto_detection_value: str = "auto"
    languages: list[str]
    language_mode: Literal["client", "server"] = "client"


class ModelSpec(ApiSchema):
    model_id: str
    model_version: str
    version_info: VersionInfo | None = Field(default=None, exclude_if=lambda value: value is None)
    model_repo: str | None
    task_type: Literal["asr", "vision", "llm"] = "asr"
    execution_mode: Literal["offline", "online"] = "offline"
    adapter_key: str = "faster-whisper"
    parameter_schema: ModelParameters | VisionModelParameters | LlmModelParameters
    input_constraints: InputConstraints | VisionInputConstraints | LlmInputConstraints
    output_capabilities: list[str] = ["text", "segments", "language"]
    resource_requirements: dict = Field(default_factory=dict)
    execution_location: Literal["local", "external"] = "local"
    management_mode: Literal["managed", "external_call_only"] = "managed"


class ModelList(ApiSchema):
    items: list[ModelSpec]
