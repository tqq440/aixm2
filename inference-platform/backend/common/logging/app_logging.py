"""独立应用日志：控制台 + UTF-8 滚动文件，JSON 单行便于定位请求。"""

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

from flask import g, has_request_context


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "requestId": getattr(g, "request_id", "-") if has_request_context() else "-",
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(log_dir: Path, level: str, filename: str = "api.log") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"inference-platform.{uuid4().hex}")
    logger.setLevel(level)
    logger.propagate = False
    file_handler = RotatingFileHandler(log_dir / filename, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    for handler in (logging.StreamHandler(), file_handler):
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger


def close_logging(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
