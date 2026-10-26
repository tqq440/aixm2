"""合成旧版本迁移演练；没有现有源库、部署、运行或解除隔离选项。"""

import argparse
import json
from pathlib import Path
import sys
from backend.schema.deployment.migration_schema import MigrationRehearsalRequest
from backend.service.deployment.migration_service import inspect_migration_rehearsal, run_migration_rehearsal


def main(argv=None):
    parser = argparse.ArgumentParser(description="只用合成0014资料演练升级0021，不处理日常库。")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            result = run_migration_rehearsal(MigrationRehearsalRequest(output=args.output))
        else:
            result = inspect_migration_rehearsal(args.root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        print("MIGRATION_REHEARSAL_FAILED：核对新隔离目录、迁移版本、文件与历史一致性；未切换或启用数据库。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
