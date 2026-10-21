"""一键启停/升级客户端；命令由 Node 的持久控制流程执行。"""

import argparse
from pathlib import Path
import sys

from backend.common.exceptions.app_error import AppError, ConfigurationError
from backend.config.host_node.node_settings import load_node_settings
from backend.service.host_node.control_client import NodeControlClient


def main(argv=None):
    parser = argparse.ArgumentParser(description="Node 受控启动/停止/升级；等待超时不会取消或重发。")
    parser.add_argument("action", choices=("start", "stop", "upgrade"))
    parser.add_argument("service_id")
    parser.add_argument("--version")
    parser.add_argument("--operation-id", help="可选已知操作 UUID，用于查询/幂等恢复")
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--node-url")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    if not 0 <= args.wait_seconds <= 3600 or ((args.action == "stop") != (args.version is None)):
        parser.error("启动/升级须提供 --version，停止不提供；等待为 0—3600 秒")
    try:
        settings = load_node_settings(env_file=args.env_file)
        result = NodeControlClient(settings, args.node_url).execute(args.service_id, args.action, args.version,
            operation_id=args.operation_id, wait_seconds=args.wait_seconds)
        print(result.model_dump_json(by_alias=True))
        if result.status in {"succeeded", "rolled_back"}: return 0
        if result.status in {"pending", "running"}: return 3
        return 2
    except (AppError, ConfigurationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("Node 控制客户端失败，请核对参数和本地配置。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
