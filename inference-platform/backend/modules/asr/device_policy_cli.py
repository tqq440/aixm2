"""GPU/CPU选择策略的离线预演入口；没有应用、执行、登记或发送请求操作。"""

import argparse
import json
from pathlib import Path

from backend.common.cli.offline_execution import execute_offline
from backend.schema.asr.device_policy_schema import DevicePolicyRequest
from backend.service.asr.device_policy_service import DevicePolicyService
from backend.utils.json.bounded_json import read_bounded_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="只预演GPU优先与合条件CPU承接，不配置日常路由、不调用模型")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema", help="输出离线输入JSON Schema，不读取.env或数据库")
    preview = commands.add_parser("preview", help="从本机JSON预演；输出不是执行许可")
    preview.add_argument("--input", type=Path, required=True, help="UTF-8 JSON快照，最大512KiB；示例仅为隔离数据")
    args = parser.parse_args(argv)
    if args.command == "schema":
        print(json.dumps(DevicePolicyRequest.model_json_schema(by_alias=True), ensure_ascii=True, indent=2))
        return 0

    def operation():
        request = DevicePolicyRequest.model_validate(read_bounded_json(args.input, max_bytes=512 * 1024))
        return DevicePolicyService.evaluate(request)

    return execute_offline(operation)


if __name__ == "__main__":
    raise SystemExit(main())
