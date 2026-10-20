"""固定本机登记/报告采样入口，不接受任意进程、节点地址或管理命令。"""

import argparse

from backend.common.cli.execution import execute
from backend.data.database.database import Database
from backend.schema.models.model_schema import RegistryListQuery
from backend.schema.nodes.node_schema import InstanceListQuery, InstancePath
from backend.service.nodes.node_registry_service import NodeRegistryService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="登记当前 Windows CPU 节点及单 ASR 实例；登记不启动模型。")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("register-local", help="从本机读取稳定身份并登记固定 ASR 实例")
    commands.add_parser("observe-local", help="归档本库现有 Worker 报告；不启动或探测进程")
    for name in ("list-nodes", "list-instances", "list-sessions"):
        action = commands.add_parser(name)
        action.add_argument("--page", default=1, type=int)
        action.add_argument("--page-size", default=20, type=int)
        if name == "list-instances":
            action.add_argument("--node-id")
        if name == "list-sessions":
            action.add_argument("--instance-id", required=True)
    args = parser.parse_args(argv)

    def operation(settings, logger):
        database = Database(settings)
        try:
            service = NodeRegistryService(database, settings)
            values = vars(args).copy()
            command = values.pop("command")
            if command == "register-local":
                return service.register_local()
            if command == "observe-local":
                return service.observe_local()
            if command == "list-instances":
                return service.list_instances(InstanceListQuery.model_validate(values))
            if command == "list-sessions":
                path = InstancePath(instance_id=values.pop("instance_id"))
                return service.list_sessions(path.instance_id, RegistryListQuery.model_validate(values))
            return service.list_nodes(RegistryListQuery.model_validate(values))
        finally:
            database.dispose()

    return execute(operation, log_filename="node-registry.log", command_name="本机节点登记")


if __name__ == "__main__":
    raise SystemExit(main())
