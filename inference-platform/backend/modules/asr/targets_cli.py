"""本机受控目标登记/查询/容量维护入口，不启动模型或接收任意命令。"""

import argparse
from pathlib import Path

from backend.common.cli.execution import execute
from backend.common.exceptions.app_error import AppError
from backend.data.database.database import Database
from backend.schema.asr.catalog_schema import CatalogQuery, CatalogRegistration, ResourceObservationInput
from backend.schema.asr.target_schema import TargetCapacityCommand, TargetCommandResult
from backend.service.asr.catalog_service import AsrCatalogService
from backend.service.asr.target_service import TargetAdmissionService
from backend.service.asr.routing_service import AsrRoutingService
from backend.utils.json.bounded_json import read_bounded_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="ASR计算目标基础：使用已验证的本机配置，默认共享单槽")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("register-current", help="幂等登记当前配置为remote-default；不会启动或调用模型")
    commands.add_parser("list", help="只读查询目标、修订、容量组、调用槽会话及许可")
    register = commands.add_parser("register-config", help="从本机JSON登记待接入目标，不授予执行权限")
    register.add_argument("path", type=Path)
    route = commands.add_parser("register-route", help="登记不可变路由资料，不启用目标或启动调用")
    route.add_argument("path", type=Path)
    observe = commands.add_parser("record-observation", help="从本机JSON追加资源报告，不采集网络或改变许可")
    observe.add_argument("path", type=Path)
    catalog = commands.add_parser("catalog", help="分页只读查询安全目录和观测时效")
    catalog.add_argument("--offset", type=int, default=0)
    catalog.add_argument("--limit", type=int, default=20)
    capacity = commands.add_parser("configure-capacity", help="停止调用池后配置1至8槽；必须提供容量依据")
    capacity.add_argument("--target-limit", type=int, required=True)
    capacity.add_argument("--group-limit", type=int, required=True)
    capacity.add_argument("--evidence", required=True, help="获准容量或隔离测量依据，不填密码或令牌")
    args = parser.parse_args(argv)

    def operation(settings, logger):
        database = Database(settings)
        try:
            service = TargetAdmissionService(database, settings)
            registry = AsrCatalogService(database, settings)
            if args.command in {"register-config", "record-observation", "register-route"}:
                try:
                    value = read_bounded_json(args.path, max_bytes=64 * 1024)
                except (OSError, ValueError):
                    raise AppError("ASR_CATALOG_INVALID_JSON", "请提供不超过64KiB的本机严格JSON文件。", 422) from None
                if args.command == "register-config":
                    return registry.register(CatalogRegistration.model_validate(value))
                if args.command == "register-route":
                    return AsrRoutingService(database, settings).register(value)
                return registry.observe(ResourceObservationInput.model_validate(value))
            if args.command == "catalog":
                return registry.list_targets(CatalogQuery(offset=args.offset, limit=args.limit))
            if args.command == "register-current":
                result = service.register_current()
            elif args.command == "configure-capacity":
                result = service.configure_capacity(TargetCapacityCommand(target_limit=args.target_limit,
                    group_limit=args.group_limit, evidence=args.evidence))
            else:
                result = service.inventory()
            return TargetCommandResult(result)
        finally:
            database.dispose()

    return execute(operation, log_filename="asr-targets-maintenance.log", command_name="ASR计算目标维护")


if __name__ == "__main__":
    raise SystemExit(main())
