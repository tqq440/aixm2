"""一键管理入口：参数校验和命令路由，不包含任务状态规则。"""

import argparse
import json
import logging
import sys

from backend.config.settings.app_settings import PROJECT_ROOT, load_settings
from backend.data.platform.platform_repository import ControlFiles
from backend.schema.platform.platform_schema import PlatformOptions, StopRequest
from backend.service.platform.platform_client import PlatformClient
from backend.service.platform.platform_service import PlatformService


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="本机平台启动、停止和状态检查")
    parser.add_argument("action", choices=("start", "stop", "status", "supervise"))
    parser.add_argument("--node-path", default="D:/develop/node/node.exe")
    parser.add_argument("--frontend-port", type=int, default=5173)
    parser.add_argument("--startup-timeout-seconds", type=float, default=180)
    parser.add_argument("--timeout-seconds", type=float, default=45)
    parser.add_argument("--instance-id")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    control = ControlFiles(PROJECT_ROOT / "runtime/platform")
    client = PlatformClient(control)
    try:
        if args.action == "status":
            result = client.status()
            code = 0
        elif args.action == "stop":
            if not 1 <= args.timeout_seconds <= 300:
                raise ValueError("停止等待必须在 1 到 300 秒之间。")
            code, result = client.stop(args.timeout_seconds)
        else:
            settings = load_settings()
            options = PlatformOptions(node_path=args.node_path, frontend_port=args.frontend_port,
                                      startup_timeout_seconds=args.startup_timeout_seconds)
            if args.action == "supervise":
                token = StopRequest(instance_id=args.instance_id)
                logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
                return PlatformService(settings, options, token.instance_id, control,
                                       logging.getLogger("platform-manager")).run()
            code, result = client.start(settings, options)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except Exception as exc:
        print(f"平台管理命令失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
