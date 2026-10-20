"""纯离线命令错误封装，不读取应用配置、不初始化日志、数据库或网络。"""

import sys
from collections.abc import Callable
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from backend.schema.common.api_schema import ErrorDetail, ErrorResponse, ValidationIssue


def execute_offline(operation: Callable[[], BaseModel]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        result = operation()
        print(result.model_dump_json(by_alias=True, indent=2))
        return 0
    except ValidationError as exc:
        error = ErrorDetail(code="VALIDATION_ERROR", message="离线输入不符合策略契约。", details=[
            ValidationIssue(field=".".join(map(str, item["loc"])), code=item["type"])
            for item in exc.errors(include_input=False)
        ])
        code = 2
    except (OSError, ValueError, RecursionError):
        error = ErrorDetail(code="OFFLINE_INPUT_INVALID", message="请提供本机UTF-8 JSON文件：不超过512KiB、不含重复键，且数字必须有限。")
        code = 2
    except KeyboardInterrupt:
        error = ErrorDetail(code="INTERRUPTED", message="离线预演已中断。")
        code = 130
    except Exception:
        error = ErrorDetail(code="OFFLINE_PREVIEW_FAILED", message="离线预演失败；没有取得执行许可或调用模型。")
        code = 1
    print(ErrorResponse(error=error, request_id=uuid4().hex).model_dump_json(by_alias=True, indent=2), file=sys.stderr)
    return code
