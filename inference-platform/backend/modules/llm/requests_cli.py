"""输出已配置模型规范，或在人工确认上游停止后释放不确定占位。"""

import argparse

from backend.common.cli.execution import execute
from backend.common.exceptions.app_error import AppError
from backend.config.llm.model_catalog import configured_spec
from backend.data.database.database import Database
from backend.schema.llm.online_schema import OnlinePath
from backend.service.llm.online_service import OnlineService
from backend.service.tasks.model_service import ModelService


def main(argv=None):
    parser = argparse.ArgumentParser(description="LLM 维护；不下载模型、不启动生成或自动释放占位。")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("spec", help="输出当前配置对应的不可变 ModelSpec；需另行登记并启用")
    release = commands.add_parser("release", help="先检查模型服务端请求确已停止，再确认释放占位")
    release.add_argument("online_request_id")
    release.add_argument("--confirm-engine-stopped", action="store_true")
    args = parser.parse_args(argv)

    def operation(settings, logger):
        if args.command == "spec":
            spec = configured_spec(settings)
            if spec is None:
                raise AppError("LLM_NOT_CONFIGURED", "尚未配置大模型连接信息。", 503)
            return spec
        path = OnlinePath(online_request_id=args.online_request_id)
        database = Database(settings)
        service = OnlineService(settings, database, ModelService(settings, database), logger)
        try:
            return service.release_uncertain(path.online_request_id, engine_stopped=args.confirm_engine_stopped)
        finally:
            service.close()
            database.dispose()

    return execute(operation, log_filename="llm-maintenance.log", command_name="LLM 在线维护")


if __name__ == "__main__":
    raise SystemExit(main())
