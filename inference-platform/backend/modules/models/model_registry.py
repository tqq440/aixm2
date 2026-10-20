"""本地维护命令只调用目录 Service，不自动迁移、下载或启动 Worker。"""

import argparse

from backend.common.cli.execution import execute
from backend.data.database.database import Database
from backend.schema.models.model_schema import ModelIdentity, RegistrationFile, RegistryListQuery
from backend.service.models.model_registry_service import ModelRegistryService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="维护持久化模型目录；登记不代表模型已经可执行。")
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register", help="登记不可覆盖的模型版本，初始禁用")
    register.add_argument("--file", required=True, help="UTF-8 JSON 模型规范路径")
    listing = commands.add_parser("list", help="分页列出全部已登记版本及真实可执行状态")
    listing.add_argument("--page", default=1, type=int)
    listing.add_argument("--page-size", default=20, type=int)
    for command in ("enable", "disable", "set-default"):
        action = commands.add_parser(command)
        action.add_argument("--model-id", required=True)
        action.add_argument("--model-version", required=True)
    args = parser.parse_args(argv)

    def operation(settings, logger):
        values = vars(args).copy()
        command = values.pop("command")
        database = Database(settings)
        try:
            service = ModelRegistryService(database, settings)
            if command == "register":
                request = RegistrationFile.model_validate(values)
                return service.register_file(request.file)
            if command == "list":
                request = RegistryListQuery.model_validate(values)
                return service.list_versions(**request.model_dump())
            request = ModelIdentity.model_validate(values)
            return getattr(service, command.replace("-", "_"))(**request.model_dump())
        finally:
            database.dispose()

    return execute(operation, log_filename="model-registry.log", command_name="模型目录维护")


if __name__ == "__main__":
    raise SystemExit(main())
