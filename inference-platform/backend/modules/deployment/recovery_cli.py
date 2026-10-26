"""本地离线备份、完整性核对与隔离恢复；不提供启用恢复副本的命令。"""

import argparse
import json
from pathlib import Path
import sys
from backend.schema.deployment.recovery_schema import RestoreRequest, SnapshotRequest
from backend.service.deployment.recovery_service import create_snapshot, inspect_snapshot, restore_snapshot


def main(argv=None):
    parser=argparse.ArgumentParser(description="成套备份独立验证资料；恢复副本始终禁止运行。")
    commands=parser.add_subparsers(dest="command",required=True)
    backup=commands.add_parser("backup")
    backup.add_argument("--root",type=Path,required=True)
    backup.add_argument("--output",type=Path,required=True)
    backup.add_argument("--controllers-stopped-confirmed",action="store_true",required=True)
    verify=commands.add_parser("verify");verify.add_argument("--snapshot",type=Path,required=True)
    restore=commands.add_parser("restore");restore.add_argument("--snapshot",type=Path,required=True)
    restore.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=="backup":
            result=create_snapshot(SnapshotRequest(root=args.root,output=args.output,controllers_stopped_confirmed=args.controllers_stopped_confirmed))
        elif args.command=="verify":result=inspect_snapshot(args.snapshot)
        else:result=restore_snapshot(RestoreRequest(snapshot=args.snapshot,output=args.output))
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except Exception:
        print("RECOVERY_CHECK_FAILED：核对独立验证目录、停止确认、锁、完整性及新输出目录；未自动修复或启动。",file=sys.stderr)
        return 2


if __name__=="__main__":raise SystemExit(main())
