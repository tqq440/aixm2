"""常驻本机 Agent 入口；信号仅请求排空，不中断正在转写的音频。"""

import signal
import sys
import threading

from backend.common.logging.app_logging import close_logging, configure_logging
from backend.config.settings.app_settings import load_settings
from backend.data.database.database import Database
from backend.service.agent.node_agent_service import NodeAgentService


def run_agent():
    stop = threading.Event()
    previous = {}
    database = logger = agent = None
    started = False
    result = 0
    try:
        for name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
            if hasattr(signal, name):
                number = getattr(signal, name)
                previous[number] = signal.signal(number, lambda *_: stop.set())
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8')
        settings = load_settings()
        logger = configure_logging(settings.log_dir, settings.log_level, 'agent.log')
        database = Database(settings)
        database.check_connection()
        agent = NodeAgentService(database, settings, logger)
        agent.start()
        started = True
        while not agent.shutdown_confirmed:
            if stop.is_set():
                agent.request_shutdown()
            try:
                agent.tick()
            except Exception:
                # 无法持久化退出证据时保留进程、所有权和锁，数据库恢复后继续确认。
                logger.exception('Agent 控制失败，停止新增工作并等待确认退出')
                result = 2
                agent.request_shutdown()
            threading.Event().wait(settings.poll_interval_seconds)
    except Exception:
        result = 2
        if logger:
            logger.exception('Agent 初始化失败')
        else:
            print('Agent 无法启动，请检查配置与日志。', file=sys.stderr)
    finally:
        if started and not agent.shutdown_confirmed:
            agent.request_shutdown()
            while not agent.shutdown_confirmed:
                try:
                    agent.tick()
                except Exception:
                    logger.exception('Agent 退出尚未确认，保留管理锁')
                threading.Event().wait(1)
        if database:
            database.dispose()
        if logger:
            close_logging(logger)
        for number, handler in previous.items():
            signal.signal(number, handler)
    return result


if __name__ == '__main__':
    raise SystemExit(run_agent())
