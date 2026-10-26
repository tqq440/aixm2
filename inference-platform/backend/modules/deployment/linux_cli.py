"""独立验证部署工具；所有写操作均须显式新目录，不提供迁移日常库入口。"""

import argparse
import json
from pathlib import Path
import sys
from backend.config.deployment.linux_settings import deployment_settings,deployment_environment
from backend.schema.deployment.linux_schema import LinuxDeploymentPlan,ValidationWorkspace
from backend.service.deployment.linux_plan_service import render_linux_plan
from backend.service.deployment.validation_service import create_validation_workspace
from backend.service.deployment.preflight_service import inspect_deployment


def main(argv=None):
    parser=argparse.ArgumentParser(description="模块6A：离线部署资料、独立验证初始化与只读检查。")
    parser.add_argument("--env-file",type=Path)
    sub=parser.add_subparsers(dest="command",required=True)
    plan=sub.add_parser("plan");plan.add_argument("--spec",type=Path,required=True);plan.add_argument("--output",type=Path,required=True)
    init=sub.add_parser("init");init.add_argument("--root",type=Path,required=True);init.add_argument("--name",default="module6a")
    init.add_argument("--center-port",type=int,default=15000);init.add_argument("--node-port",type=int,default=18020)
    inspect=sub.add_parser("inspect");inspect.add_argument("--require-linux",action="store_true")
    serve=sub.add_parser("serve");serve.add_argument("component",choices=("center","node"));serve.add_argument("--threads",type=int,choices=range(4,65),default=16)
    sub.add_parser("executor")
    args=parser.parse_args(argv)
    try:
        if args.command=="plan":
            if args.spec.stat().st_size>16384:raise ValueError()
            result=render_linux_plan(LinuxDeploymentPlan.model_validate_json(args.spec.read_text(encoding="utf-8")),args.output)
        elif args.command=="init":result=create_validation_workspace(ValidationWorkspace(root=args.root,name=args.name,centerPort=args.center_port,nodePort=args.node_port))
        else:
            if args.env_file is None:raise ValueError()
            # 先按env组件读取，再验证命令身份；只允许显式隔离配置。
            from backend.config.deployment.linux_settings import deployment_values
            component=deployment_values(args.env_file).get("DEPLOY_COMPONENT")
            if args.command=="serve" and component!=args.component:raise ValueError()
            if args.command=="executor" and component!="center":raise ValueError()
            if args.command=="executor":component="executor"
            require_linux=args.command in {"serve","executor"} or getattr(args,"require_linux",False)
            result=inspect_deployment(component,args.env_file,require_linux=require_linux)
            if args.command in {"serve","executor"} and result["ok"]:
                _,values=deployment_settings(component,args.env_file)
                with deployment_environment(values):
                    if args.command=="executor":
                        from backend.modules.worker.node_executor import main as executor_main
                        return executor_main(["--env-file",str(args.env_file)])
                    from backend.modules.deployment.linux_server import serve_linux
                    serve_linux(component,args.env_file,args.threads)
                return 0
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result.get("ok",True) else 2
    except Exception:
        # 不打印Pydantic/JSON/env异常，避免原配置或凭据随traceback输出。
        print("DEPLOYMENT_CHECK_FAILED：检查显式配置、新目录、依赖和权限；未自动修复或操作模型。",file=sys.stderr)
        return 2


if __name__=="__main__":raise SystemExit(main())
