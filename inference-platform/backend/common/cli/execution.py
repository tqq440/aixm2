"""本地命令共用配置、日志和错误输出，不启动 HTTP 或后台循环。"""

import logging
import sys
from collections.abc import Callable
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from backend.common.exceptions.app_error import AppError, ConfigurationError
from backend.common.logging.app_logging import close_logging, configure_logging
from backend.config.settings.app_settings import AppSettings, load_settings
from backend.schema.common.api_schema import ErrorDetail, ErrorResponse, ValidationIssue


def execute(operation: Callable[[AppSettings, logging.Logger], BaseModel], *,
            log_filename: str = "asr.log", command_name: str = "本地 ASR") -> int:
    run_id = uuid4().hex
    logger = None
    error = None
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        settings = load_settings()
        logger = configure_logging(settings.log_dir, settings.log_level, log_filename)
        logger.info("%s 命令开始，编号 %s", command_name, run_id)
        result = operation(settings, logger)
        print(result.model_dump_json(by_alias=True, indent=2))
        logger.info("%s 命令完成，编号 %s", command_name, run_id)
        return 0
    except KeyboardInterrupt:
        error = ErrorDetail(code="INTERRUPTED", message="操作已由用户中断。")
        exit_code = 130
    except ConfigurationError as exc:
        error = ErrorDetail(code="CONFIGURATION_ERROR", message=str(exc))
        exit_code = 2
    except ValidationError as exc:
        error = ErrorDetail(code="VALIDATION_ERROR", message="命令参数不符合要求。", details=[
            ValidationIssue(field=".".join(map(str, item["loc"])), code=item["type"]) for item in exc.errors()
        ])
        exit_code = 2
    except AppError as exc:
        error = ErrorDetail(code=exc.code, message=exc.message)
        exit_code = 2
        if logger is not None:
            logger.error("命令失败 %s，编号 %s", exc.code, run_id, exc_info=True)
    except Exception:
        error = ErrorDetail(code="LOCAL_EXECUTION_FAILED", message=f"本地操作失败，请根据编号检查日志目录中的 {log_filename} 和目录权限。")
        exit_code = 1
        if logger is not None:
            logger.exception("未处理的本地命令异常，编号 %s", run_id)
    finally:
        if logger is not None:
            if error is not None:
                logger.error("本地命令失败 %s，编号 %s", error.code, run_id)
            close_logging(logger)
    print(ErrorResponse(error=error, request_id=run_id).model_dump_json(by_alias=True, indent=2), file=sys.stderr)
    return exit_code
