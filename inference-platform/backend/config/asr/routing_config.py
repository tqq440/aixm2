"""本阶段只允许隔离目录与回环替身运行新路由，日常配置默认关闭。"""

from pathlib import Path
from tempfile import gettempdir
from urllib.parse import urlsplit

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import PROJECT_ROOT


def require_routing_isolation(settings, origins=()):
    roots = [PROJECT_ROOT / "runtime" / "tmp", PROJECT_ROOT / "runtime" / "verification", Path(gettempdir())]
    paths = [settings.database_path, settings.upload_dir, settings.log_dir]
    if (not settings.remote_asr_routing_isolation
            or any(not any(path.resolve().is_relative_to(root.resolve()) for root in roots) for path in paths)):
        raise AppError("ASR_ROUTING_ISOLATION_REQUIRED", "本阶段新路由仅允许独立隔离数据库、上传和日志目录；日常执行保持原通道。", 409)
    for origin in origins:
        host = urlsplit(origin).hostname or ""
        if host not in {"localhost", "127.0.0.1", "::1"} and not host.endswith(".invalid"):
            raise AppError("ASR_ROUTING_REAL_TARGET_PENDING", "真实目标须在后续阶段完成设备、容量与性能核实后启用。", 409)
