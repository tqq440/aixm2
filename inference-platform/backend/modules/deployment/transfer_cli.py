"""显式源码传输命令；没有后台同步、启动或数据迁移操作。"""

import argparse
import json
import sys

from pydantic import ValidationError

from backend.common.transfer.openssh_transport import TransferFailure
from backend.config.deployment.lab_settings import LabSettings
from backend.schema.deployment.transfer_schema import TransferRequest
from backend.service.deployment.transfer_service import TransferService


def main(argv=None):
    parser = argparse.ArgumentParser(description="固定实验室目标源码传输，不迁移数据、不启动服务")
    parser.add_argument("action", choices=("check", "package", "upload", "verify"))
    parser.add_argument("--bundle-id", help="复核或重传已有64位SHA256批次")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        request = TransferRequest(action=args.action, bundle_id=args.bundle_id)
        result = TransferService(LabSettings.local()).execute(request)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except TransferFailure as exc:
        code, message = exc.code, str(exc)
    except ValidationError:
        code, message = "INVALID_TRANSFER_REQUEST", "命令参数无效；verify需要准确批次SHA256。"
    except (OSError, ValueError, RuntimeError) as exc:
        code, message = "SOURCE_TRANSFER_REJECTED", str(exc)[:300]
    except KeyboardInterrupt:
        code, message = "TRANSFER_INTERRUPTED", "传输中断；请使用同一批次核验远端状态后重试。"
    print(json.dumps({"ok": False, "error": {"code": code, "message": message}}, ensure_ascii=False, indent=2), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
