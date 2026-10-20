"""显式准备 YOLO；检测和 --check 只读取、核验本地固定文件。"""

from pydantic import ValidationError

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.config.vision.model_catalog import (
    MODEL_FILENAME, MODEL_ID, MODEL_SHA256, MODEL_SIZE_BYTES, MODEL_URL, MODEL_VERSION, SOURCE_REVISION,
)
from backend.data.vision.model_store import VisionModelStore
from backend.schema.vision.detection_schema import VisionModelManifest

PREPARE_COMMAND = "python -m backend.modules.vision.prepare_model"


def _expected_manifest() -> VisionModelManifest:
    return VisionModelManifest(
        model_id=MODEL_ID, model_version=MODEL_VERSION, source_url=MODEL_URL,
        source_revision=SOURCE_REVISION, size_bytes=MODEL_SIZE_BYTES, sha256=MODEL_SHA256,
    )


def _verify_weights(store: VisionModelStore) -> None:
    try:
        actual = store.file_info(MODEL_FILENAME)
    except OSError as exc:
        raise AppError("VISION_MODEL_INVALID", "YOLO 权重缺失或不可读，请检查本地模型目录。") from exc
    if actual != (MODEL_SIZE_BYTES, MODEL_SHA256):
        raise AppError("VISION_MODEL_INVALID", "YOLO 权重大小或 SHA256 不符合固定版本，不能运行。")


def inspect_model(settings: AppSettings) -> VisionModelManifest:
    store = VisionModelStore(settings.vision_model_dir)
    try:
        manifest = VisionModelManifest.model_validate_json(store.read_manifest())
    except FileNotFoundError as exc:
        raise AppError("VISION_MODEL_NOT_READY", f"请先显式运行 {PREPARE_COMMAND} 准备本地 YOLO 模型。", 503) from exc
    except (OSError, UnicodeError, ValidationError) as exc:
        raise AppError("VISION_MODEL_INVALID", "YOLO 模型清单无法读取或格式损坏。") from exc
    if manifest != _expected_manifest():
        raise AppError("VISION_MODEL_INVALID", "YOLO 清单与固定版本不符。")
    _verify_weights(store)
    return manifest


def prepare_model(settings: AppSettings) -> VisionModelManifest:
    try:
        return inspect_model(settings)
    except AppError as exc:
        if exc.code not in {"VISION_MODEL_NOT_READY", "VISION_MODEL_INVALID"}:
            raise
    store = VisionModelStore(settings.vision_model_dir)
    try:
        if store.has_entries():
            raise AppError("VISION_MODEL_INVALID", "模型目录中已有未通过校验的内容；为保留原文件，请把 INFERENCE_VISION_MODEL_DIR 指向一个新的空目录。")
        with store.stage() as staged:
            try:
                staged.download(MODEL_URL, MODEL_FILENAME, MODEL_SIZE_BYTES)
            except Exception as exc:
                raise AppError("VISION_MODEL_DOWNLOAD_FAILED", "YOLO 下载失败，未发布就绪模型；请检查网络后重试。", 503) from exc
            _verify_weights(staged)
            manifest = _expected_manifest()
            staged.write_manifest(manifest.model_dump_json(by_alias=True, indent=2) + "\n")
            store.publish(staged)
    except OSError as exc:
        raise AppError("VISION_MODEL_WRITE_FAILED", "YOLO 模型发布失败，请检查目录权限或是否有其他操作正在准备模型。", 500) from exc
    return manifest
