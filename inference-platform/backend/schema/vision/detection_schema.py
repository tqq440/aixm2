"""第九部分本地 CLI 契约，尚不用于 HTTP 上传或任务。"""

from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from backend.schema.common.api_schema import ApiSchema


class DetectionRequest(ApiSchema):
    model_config = ConfigDict(allow_inf_nan=False)
    image_path: Path
    confidence_threshold: float = Field(default=0.25, gt=0, le=1)
    iou_threshold: float = Field(default=0.45, gt=0, le=1)
    max_detections: int = Field(default=100, ge=1, le=300)

    @field_validator("image_path", mode="before")
    @classmethod
    def resolve_image_path(cls, value: object) -> Path:
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError("图片路径不能为空")
        return Path(value).expanduser().resolve()


class DetectionOptions(DetectionRequest):
    output_dir: Path | None = None

    @field_validator("output_dir", mode="before")
    @classmethod
    def resolve_output_directory(cls, value: object) -> Path | None:
        if value is None:
            return None
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError("结果目录不能为空")
        return Path(value).expanduser().resolve()


class VisionPrepareOptions(ApiSchema):
    check: bool = False


class VisionModelManifest(ApiSchema):
    model_id: str
    model_version: str
    source_url: str
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BoundingBox(ApiSchema):
    model_config = ConfigDict(allow_inf_nan=False)
    x_min: float = Field(ge=0)
    y_min: float = Field(ge=0)
    x_max: float = Field(ge=0)
    y_max: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_area(self):
        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            raise ValueError("检测框必须有正面积")
        return self


class Detection(ApiSchema):
    class_id: int = Field(ge=0, le=79)
    class_name: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    box: BoundingBox


class DetectionResult(ApiSchema):
    model_id: Literal["yolo-general"] = "yolo-general"
    model_version: Literal["yolov5n-v7.0-onnx"] = "yolov5n-v7.0-onnx"
    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    coordinate_system: Literal["original-pixels-exif-normalized"] = "original-pixels-exif-normalized"
    detections: list[Detection] = Field(max_length=300)
    device: Literal["cpu"] = "cpu"
    runtime_version: str
    input_type: Literal["float16"] = "float16"
    preprocessing_version: Literal["pillow-bilinear-letterbox-v1"] = "pillow-bilinear-letterbox-v1"
    load_seconds: float = Field(ge=0, allow_inf_nan=False)
    inference_seconds: float = Field(ge=0, allow_inf_nan=False)
    total_seconds: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_image_bounds(self):
        for detection in self.detections:
            if detection.box.x_max > self.image_width or detection.box.y_max > self.image_height:
                raise ValueError("检测框不能超出原图边界")
        return self
