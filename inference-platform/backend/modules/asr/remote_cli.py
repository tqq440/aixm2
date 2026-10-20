"""外部调用配置规范与人工核对命令；不执行远端进程操作。"""

import argparse

from backend.common.cli.execution import execute
from backend.data.database.database import Database
from backend.schema.asr.remote_schema import RemoteAsrRelease, RemoteAsrReleaseReceipt
from backend.service.asr.adapter_catalog import remote_asr_model_spec
from backend.service.asr.remote_service import RemoteAsrService


def main(argv=None):
    parser = argparse.ArgumentParser(description="外部 ASR 维护：只核对调用，不控制远端服务")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("spec", help="输出准确模型规范，需另行登记启用")
    commands.add_parser("status", help="查询连接与调用确认状态")
    release = commands.add_parser("release", help="核对远端请求已结束后释放未确认占位")
    release.add_argument("task_id")
    release.add_argument("--confirm-request-ended", action="store_true")
    release.add_argument("--note", required=True)
    args = parser.parse_args(argv)

    def operation(settings, logger):
        if args.command == "spec":
            return remote_asr_model_spec(settings)
        database = Database(settings)
        try:
            service = RemoteAsrService(database, settings)
            if args.command == "status":
                return service.status()
            return RemoteAsrReleaseReceipt.model_validate(service.release_uncertain(RemoteAsrRelease(task_id=args.task_id,
                confirm_request_ended=args.confirm_request_ended, note=args.note)))
        finally:
            database.dispose()
    return execute(operation, log_filename="remote-asr-maintenance.log", command_name="外部 ASR 维护")


if __name__ == "__main__":
    raise SystemExit(main())
