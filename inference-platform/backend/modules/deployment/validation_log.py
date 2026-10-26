"""手动登记/只读查看本地验收缺口；不接入校园网、不重新推理。"""

import argparse
import json
import sys
from pathlib import Path
from backend.schema.deployment.validation_issue_schema import ValidationDeferral
from backend.service.deployment.validation_issue_service import ValidationIssueLog


def main(argv=None):
    parser=argparse.ArgumentParser(description="环境验收问题日志，与推理任务失败分开。")
    parser.add_argument("--log-dir",type=Path,help="显式验收记录目录；自定义后台日志目录时使用其validation子目录")
    sub=parser.add_subparsers(dest="command",required=True)
    for name in ("defer","show"):
        action=sub.add_parser(name)
        action.add_argument("--stage",choices=("module6a","module6b"),required=True)
        action.add_argument("--target",required=True)
        if name=="defer":
            action.add_argument("--code",choices=("CAMPUS_NETWORK_UNAVAILABLE","REDIS_CLIENT_MISSING","REDIS_SERVER_UNVERIFIED","LINUX_RUNTIME_UNVERIFIED"),required=True)
            action.add_argument("--source",choices=("platform","operator"),default="platform")
            action.add_argument("--evidence-ref")
    args=parser.parse_args(argv)
    try:
        log=ValidationIssueLog(args.log_dir) if args.log_dir is not None else ValidationIssueLog()
        result=log.latest(args.stage,args.target) if args.command=="show" else log.defer(ValidationDeferral(
            stage=args.stage,target=args.target,code=args.code,source=args.source,evidence_ref=args.evidence_ref))
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except Exception:
        print("VALIDATION_LOG_UNAVAILABLE：请检查本地日志目录、问题身份与verification证据路径。",file=sys.stderr)
        return 2


if __name__=="__main__":raise SystemExit(main())
