"""只读检查入口；检查失败退出 2，不自动修复环境。"""

import argparse
import sys

from backend.common.exceptions.app_error import ConfigurationError
from backend.config.settings.app_settings import load_settings
from backend.schema.maintenance.environment_schema import EnvironmentCheck, EnvironmentReport, EnvironmentRequest
from backend.service.maintenance.environment_service import check_environment


def main() -> int:
    parser = argparse.ArgumentParser(description="检查现有 Python、数据库迁移、本地模型和前端环境。")
    parser.add_argument("--node-path")
    request = EnvironmentRequest.model_validate(vars(parser.parse_args()))
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        report = check_environment(load_settings(), node_path=request.node_path)
    except ConfigurationError as exc:
        report = EnvironmentReport(passed=False, checks=[EnvironmentCheck(name="configuration", passed=False, message=str(exc))])
    print(report.model_dump_json(by_alias=True, indent=2))
    return 0 if report.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
