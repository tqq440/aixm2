"""独立投递器入口；业务投递和状态报告委托 Service。"""

import argparse
import signal
from threading import Event

from backend.common.cli.execution import execute
from backend.schema.runtime.loop_schema import DispatcherOptions, DispatcherSummary
from backend.service.dispatch.dispatcher_runtime import DispatcherRuntime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行本机任务首次投递及失败补投；Ctrl+C 停止。")
    parser.add_argument("--once", action="store_true", help="只扫描一批待投递事件后退出")
    args = parser.parse_args(argv)
    stop = Event()
    previous = {}

    def handle_stop(_signum, _frame):
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.signal(signum, handle_stop)

    def operation(settings, logger):
        options = DispatcherOptions.model_validate(vars(args))
        runtime = DispatcherRuntime(settings, logger)
        count = 0
        try:
            runtime.start()
            logger.info("投递器已启动；仅投递通知，不执行模型，Ctrl+C 停止")
            while not stop.is_set():
                count += runtime.tick()
                if options.once:
                    break
                stop.wait(settings.poll_interval_seconds)
            return DispatcherSummary(published_count=count)
        finally:
            runtime.close()

    try:
        return execute(operation, log_filename="dispatcher.log", command_name="任务投递器")
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
