"""python -m backend.modules.api.run：仅本机开发，Ctrl+C 停止。"""

import signal
import sys

from backend.common.exceptions.app_error import ConfigurationError, StartupError
from backend.modules.api.app import close_app, create_app


def _handle_console_break(_signum, _frame) -> None:
    raise KeyboardInterrupt


def main() -> int:
    # Windows Ctrl+Break 与 Ctrl+C 都进入资源清理路径。
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_console_break)
    try:
        app = create_app()
    except (ConfigurationError, StartupError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2
    settings = app.config["APP_SETTINGS"]
    logger = app.extensions["app_logger"]
    try:
        logger.info("启动本机 API：http://%s:%s；按 Ctrl+C 停止", settings.host, settings.port)
        app.run(host=settings.host, port=settings.port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        close_app(app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
