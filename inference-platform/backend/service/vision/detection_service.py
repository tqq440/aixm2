"""第九部分同步本地图像检测；不创建任务或连接通知队列。"""

from collections.abc import Callable
from importlib.metadata import version
import logging
from time import perf_counter
from typing import Any

from pydantic import ValidationError

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.config.vision.model_catalog import CLASS_NAMES, INPUT_SIZE, MODEL_FILENAME, MODEL_ID, MODEL_SHA256, MODEL_VERSION
from backend.schema.vision.detection_schema import BoundingBox, Detection, DetectionRequest, DetectionResult
from backend.service.vision.model_preparation_service import inspect_model
from backend.utils.image.detection_utils import DetectionOutputError, letterbox_rgb, postprocess_yolov5
from backend.utils.image.image_utils import (
    ImageDecodeError,
    ImageFileTooLargeError,
    ImageFormatError,
    ImageNotFoundError,
    ImagePixelLimitError,
    PreparedImage,
    decode_image_file,
)

LOGGER = logging.getLogger(__name__)


class DetectionService:
    """只从已经通过完整性校验的本地模型建立CPU会话。"""

    def __init__(self, settings: AppSettings, *, session_factory: Callable[..., Any] | None = None) -> None:
        self.settings = settings
        self._session_factory = session_factory
        self._session: Any | None = None

    def prepare_model(self) -> float:
        if self._session is not None:
            return 0.0
        inspect_model(self.settings)
        started = perf_counter()
        try:
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.intra_op_num_threads = self.settings.vision_cpu_threads
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            factory = self._session_factory or ort.InferenceSession
            session = factory(
                str(self.settings.vision_model_dir / MODEL_FILENAME),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            if session.get_providers() != ["CPUExecutionProvider"]:
                raise DetectionOutputError("检测会话并非固定CPU运行")
            inputs, outputs = session.get_inputs(), session.get_outputs()
            if (
                len(inputs) != 1 or inputs[0].name != "images"
                or inputs[0].type != "tensor(float16)" or list(inputs[0].shape) != [1, 3, INPUT_SIZE, INPUT_SIZE]
                or len(outputs) != 1 or outputs[0].name != "output0"
                or outputs[0].type != "tensor(float16)" or list(outputs[0].shape) != [1, 25200, 85]
            ):
                raise DetectionOutputError("模型输入输出与锁定版本不一致")
        except DetectionOutputError as exc:
            raise AppError("VISION_MODEL_CONTRACT_INVALID", "视觉模型的设备或输入输出契约与锁定版本不一致。", 503) from exc
        except Exception as exc:
            LOGGER.exception("本地视觉模型加载失败")
            raise AppError("VISION_MODEL_LOAD_FAILED", "本地视觉模型加载失败，请检查依赖、模型文件及可用内存。", 503) from exc
        self._session = session
        return perf_counter() - started

    def warmup(self) -> float:
        """由 Vision Worker 显式调用；完成一次原生CPU推理才允许报告就绪。

        本地第九部分CLI继续只执行用户图片，不因增加Worker偷偷预热。
        """
        self.prepare_model()
        started = perf_counter()
        try:
            import numpy as np

            transform = letterbox_rgb(np.zeros((INPUT_SIZE, INPUT_SIZE, 3), dtype=np.uint8), input_size=INPUT_SIZE)
            outputs = self._session.run(["output0"], {"images": transform.tensor})
            if not isinstance(outputs, list) or len(outputs) != 1 or str(outputs[0].dtype) != "float16":
                raise DetectionOutputError("预热输出不符合固定模型契约")
            postprocess_yolov5(outputs[0], transform, confidence_threshold=0.25, iou_threshold=0.45, max_detections=100)
        except Exception as exc:
            LOGGER.exception("视觉模型预热失败")
            raise AppError("VISION_MODEL_WARMUP_FAILED", "视觉模型预热失败，Worker 未进入可接单状态。", 503) from exc
        return perf_counter() - started

    def run(self, request: DetectionRequest) -> tuple[DetectionResult, PreparedImage]:
        started = perf_counter()
        try:
            # 再校验model_construct/model_copy产生的对象，避免绕过边界。
            request = DetectionRequest.model_validate(request.model_dump())
        except (ValidationError, AttributeError, TypeError) as exc:
            raise AppError("DETECTION_REQUEST_INVALID", "图片检测参数不符合要求。") from exc
        try:
            prepared = decode_image_file(
                request.image_path,
                max_file_bytes=self.settings.vision_max_file_bytes,
                max_pixels=self.settings.vision_max_pixels,
            )
            transform = letterbox_rgb(prepared.rgb, input_size=INPUT_SIZE)
        except ImageNotFoundError as exc:
            raise AppError("IMAGE_NOT_FOUND", "图片不存在或路径不是普通文件。", 404) from exc
        except ImageFileTooLargeError as exc:
            raise AppError("IMAGE_TOO_LARGE", f"图片超过文件大小上限（{self.settings.vision_max_file_bytes} 字节）。", 413) from exc
        except ImagePixelLimitError as exc:
            raise AppError("IMAGE_TOO_MANY_PIXELS", f"图片超过像素上限（{self.settings.vision_max_pixels} 像素）。", 413) from exc
        except ImageFormatError as exc:
            raise AppError("IMAGE_FORMAT_UNSUPPORTED", "仅支持单张静态 JPEG 或 PNG 图片。") from exc
        except ImageDecodeError as exc:
            raise AppError("IMAGE_DECODE_FAILED", "图片损坏、截断或无法读取，请检查输入文件。") from exc
        except ImportError as exc:
            raise AppError("VISION_DEPENDENCY_MISSING", "图片运行库不可用，请按 requirements/vision.txt 安装依赖。", 503) from exc
        except ValueError as exc:
            raise AppError("IMAGE_DECODE_FAILED", "图片像素或解码配置无效。") from exc

        load_seconds = self.prepare_model()
        inference_started = perf_counter()
        try:
            outputs = self._session.run(["output0"], {"images": transform.tensor})
            inference_seconds = perf_counter() - inference_started
        except Exception as exc:
            LOGGER.exception("视觉模型推理失败")
            raise AppError("DETECTION_FAILED", "模型检测失败，请检查日志中的原因。", 500) from exc
        try:
            if not isinstance(outputs, list) or len(outputs) != 1:
                raise DetectionOutputError("模型必须仅返回一个检测输出")
            if str(outputs[0].dtype) != "float16":
                raise DetectionOutputError("固定模型应返回float16")
            boxes = postprocess_yolov5(
                outputs[0], transform,
                confidence_threshold=request.confidence_threshold,
                iou_threshold=request.iou_threshold,
                max_detections=request.max_detections,
            )
            detections = [Detection(
                class_id=box.class_id,
                class_name=CLASS_NAMES[box.class_id],
                confidence=box.confidence,
                box=BoundingBox(x_min=box.x_min, y_min=box.y_min, x_max=box.x_max, y_max=box.y_max),
            ) for box in boxes]
            result = DetectionResult(
                model_id=MODEL_ID, model_version=MODEL_VERSION, model_sha256=MODEL_SHA256,
                image_width=prepared.width, image_height=prepared.height,
                detections=detections, runtime_version=version("onnxruntime"),
                load_seconds=load_seconds, inference_seconds=inference_seconds,
                total_seconds=perf_counter() - started,
            )
        except Exception as exc:
            LOGGER.exception("视觉模型输出不符合约定")
            raise AppError("DETECTION_OUTPUT_INVALID", "模型输出不符合检测契约，没有保存为成功结果。", 500) from exc
        LOGGER.info("视觉检测完成 width=%s height=%s detections=%s device=cpu inference_seconds=%.3f", prepared.width, prepared.height, len(detections), inference_seconds)
        return result, prepared
