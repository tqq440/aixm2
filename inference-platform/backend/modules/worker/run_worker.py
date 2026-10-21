"""两类 Windows Worker 共用生命周期，不直接修改任务状态。"""

import signal
import sys
import threading

from backend.common.exceptions.app_error import AppError, ConfigurationError
from backend.common.logging.app_logging import close_logging, configure_logging
from backend.config.settings.app_settings import load_settings
from backend.data.database.database import Database
from backend.service.worker.worker_service import WorkerService
from backend.service.worker.remote_pool_service import RemotePoolService


def run_worker(task_type: str, *, asr_profile: str = "local") -> int:
    stop = threading.Event()
    previous = {}
    database = logger = worker = None
    result = 0
    try:
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                number = getattr(signal, name)
                previous[number] = signal.signal(number, lambda *_: stop.set())
        settings = load_settings()
        log_name = ("remote-asr-worker.log" if asr_profile == "remote" else
                    "worker.log" if task_type == "asr" else "vision-worker.log")
        logger = configure_logging(settings.log_dir, settings.log_level, log_name, process_scoped=settings.common_execution_enabled)
        database = Database(settings)
        database.check_connection()
        worker = (RemotePoolService(database, settings, logger) if asr_profile == "remote" else
                  WorkerService(database, settings, logger, task_type=task_type, asr_profile=asr_profile))
        worker.start()
        while True:
            if stop.is_set():
                if asr_profile != "remote":
                    break
                worker.request_drain()
            worker.tick()
            if asr_profile == "remote" and worker.drained:
                break
            if worker.can_exit:
                result = 2
                break
            # 停止信号置位后仍按原节奏等待在途 HTTP，不能形成忙循环。
            if stop.is_set():
                threading.Event().wait(settings.poll_interval_seconds)
            else:
                stop.wait(settings.poll_interval_seconds)
    except (ConfigurationError, AppError) as exc:
        print(str(exc), file=sys.stderr)
        result = 2
    except KeyboardInterrupt:
        pass
    except Exception:
        if logger is not None:
            logger.exception("Worker 启动或运行失败")
        else:
            print("Worker 无法启动，请检查配置与日志目录。", file=sys.stderr)
        result = 2
    finally:
        # 无法确认子进程退出时，保持本进程和锁，持续报告，绝不释放单槽。
        if worker is not None:
            while not worker.close():
                threading.Event().wait(1)
        if database is not None:
            database.dispose()
        if logger is not None:
            close_logging(logger)
        for number, handler in previous.items():
            signal.signal(number, handler)
    return result
