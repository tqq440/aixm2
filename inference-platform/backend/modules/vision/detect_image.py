"""单次本地图片检测；只消费已经显式准备的模型文件。"""

import argparse
from uuid import uuid4

from backend.common.cli.execution import execute
from backend.config.settings.app_settings import PROJECT_ROOT
from backend.schema.vision.detection_schema import DetectionOptions, DetectionRequest
from backend.service.vision.local_detection_service import detect_to_directory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地 CPU 单图 YOLO 检测，不联网下载模型。")
    parser.add_argument("image_path", help="JPEG/PNG 路径，相对路径以当前工作目录为准")
    parser.add_argument("--output-dir", help="新的输出目录，保存 result.json 与 annotated.png")
    parser.add_argument("--confidence-threshold", default="0.25", help="置信度阈值，0 < 数值 <= 1")
    parser.add_argument("--iou-threshold", default="0.45", help="同类别 NMS 的 IoU 阈值")
    parser.add_argument("--max-detections", default="100", help="最多输出 1～300 个检测框")
    args = parser.parse_args(argv)

    def operation(settings, logger):
        options = DetectionOptions.model_validate(vars(args))
        request = DetectionRequest.model_validate(options.model_dump(exclude={"output_dir"}))
        output = options.output_dir or PROJECT_ROOT / "runtime/results" / f"vision-{uuid4().hex}"
        result = detect_to_directory(settings, request, output)
        logger.info("检测完成，共 %s 个目标；JSON 与标注图片保存于：%s", len(result.detections), output)
        return result

    return execute(operation, log_filename="vision.log", command_name="本地 YOLO")


if __name__ == "__main__":
    raise SystemExit(main())
