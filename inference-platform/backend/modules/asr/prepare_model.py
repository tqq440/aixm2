"""显式准备模型；--check 仅检查本地文件，不连接网络。"""

import argparse

from backend.common.cli.execution import execute
from backend.schema.asr.asr_schema import PrepareOptions
from backend.service.asr.model_preparation_service import inspect_model, prepare_model


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="准备固定版本的 Whisper small（多语言）；首次需要网络。")
    parser.add_argument("--check", action="store_true", help="只验证本地模型完整性，不下载")
    args = parser.parse_args(argv)

    def operation(settings, logger):
        options = PrepareOptions.model_validate(vars(args))
        logger.info("%s模型：%s@%s", "检查" if options.check else "准备", settings.asr_model_repo, settings.asr_model_revision)
        return inspect_model(settings) if options.check else prepare_model(settings)

    return execute(operation)


if __name__ == "__main__":
    raise SystemExit(main())
