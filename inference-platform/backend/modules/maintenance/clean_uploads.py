"""一次性维护入口：默认预览，--apply 才删除孤立文件；不启动后台循环。"""

import argparse

from backend.common.cli.execution import execute
from backend.schema.files.cleanup_schema import CleanupOptions
from backend.service.files.upload_cleanup_service import clean_uploads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="预览超过 24 小时且无任务引用的上传文件。")
    parser.add_argument("--apply", action="store_true", help="删除本次检查确认的孤立文件")
    args = parser.parse_args(argv)

    def operation(settings, logger):
        return clean_uploads(settings, CleanupOptions.model_validate(vars(args)), logger)

    return execute(operation, log_filename="maintenance.log", command_name="上传目录维护")


if __name__ == "__main__":
    raise SystemExit(main())
