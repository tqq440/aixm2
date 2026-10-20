"""显式准备固定 YOLOv5n ONNX；--check 永不连接网络。"""

import argparse

from backend.common.cli.execution import execute
from backend.schema.vision.detection_schema import VisionPrepareOptions
from backend.service.vision.model_preparation_service import inspect_model, prepare_model


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="准备固定 YOLOv5n ONNX 模型；首次需要网络。")
    parser.add_argument("--check", action="store_true", help="只核验本地模型清单、大小与 SHA256")
    args = parser.parse_args(argv)

    def operation(settings, logger):
        options = VisionPrepareOptions.model_validate(vars(args))
        logger.info("%s本地 YOLOv5n 模型", "检查" if options.check else "准备")
        return inspect_model(settings) if options.check else prepare_model(settings)

    return execute(operation, log_filename="vision.log", command_name="本地 YOLO")


if __name__ == "__main__":
    raise SystemExit(main())
