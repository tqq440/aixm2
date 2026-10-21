"""独立通知维护入口；不导入或执行模型服务。"""

import argparse
import signal
from threading import Event

from backend.common.cli.execution import execute
from backend.data.database.database import Database
from backend.schema.queue.queue_schema import QueueSummary
from backend.service.queue.queue_service import QueueService


def main(argv=None):
    parser = argparse.ArgumentParser(description="Redis 公共通知投递、采集、补投及确认；不执行模型。")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    stop, previous = Event(), {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            previous[number] = signal.signal(number, lambda *_: stop.set())

    def operation(settings, logger):
        database = Database(settings)
        queue = QueueService(database, settings, logger=logger)
        totals = {"published": 0, "received": 0, "acknowledged": 0}
        try:
            while not stop.is_set():
                try:
                    summary = queue.tick()
                    for key in totals:
                        totals[key] += getattr(summary, key)
                except Exception as error:
                    from backend.common.exceptions.app_error import AppError
                    if not isinstance(error, AppError) or args.once or error.code in {"REDIS_QUEUE_DISABLED", "REDIS_CLIENT_MISSING", "REDIS_QUEUE_STORAGE_UNAVAILABLE", "REDIS_VERSION_UNSUPPORTED"}:
                        raise
                    logger.warning("Redis 通知维护暂不可用（%s），稍后重试。", error.code)
                    if stop.wait(settings.dispatch_retry_seconds):
                        break
                    continue
                if args.once:
                    break
                stop.wait(settings.poll_interval_seconds)
            return QueueSummary(**totals)
        finally:
            queue.close()
            database.dispose()

    try:
        return execute(operation, log_filename="redis-queue.log", command_name="Redis 通知维护")
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
