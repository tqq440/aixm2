"""本地单图检测及两份结果的一致发布。"""

from pathlib import Path

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.data.vision.result_store import write_detection_result
from backend.schema.vision.detection_schema import DetectionRequest, DetectionResult
from backend.service.vision.detection_service import DetectionService
from backend.utils.image.annotation_utils import annotated_png


def detect_to_directory(settings: AppSettings, request: DetectionRequest, output_dir: Path) -> DetectionResult:
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists():
        raise AppError("RESULT_EXISTS", "结果目录已存在，请指定新的目录；不会覆盖已有内容。", 409)
    result, prepared = DetectionService(settings).run(request)
    try:
        image = annotated_png(prepared.rgb, [
            (item.class_name, item.confidence, (item.box.x_min, item.box.y_min, item.box.x_max, item.box.y_max))
            for item in result.detections
        ])
        write_detection_result(output_dir, result.model_dump_json(by_alias=True, indent=2) + "\n", image)
    except FileExistsError as exc:
        raise AppError("RESULT_EXISTS", "结果目录已被其他操作创建，请换一个名称。", 409) from exc
    except OSError as exc:
        raise AppError("RESULT_WRITE_FAILED", "检测已完成但结果保存失败，请检查磁盘空间与目录权限。", 500) from exc
    return result
