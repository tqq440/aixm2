"""主机 Node 开发/协议联调入口；生产 WSGI 与 systemd 留 Linux 模块。"""

import argparse
from pathlib import Path
import signal
import sys
from threading import Event

from werkzeug.serving import WSGIRequestHandler, make_server

from backend.common.exceptions.app_error import ConfigurationError, StartupError
from backend.config.host_node.node_settings import load_node_settings
from backend.modules.host_node.app import close_node_app, create_node_app
from backend.modules.host_node.housekeeping import NodeHousekeeping
from backend.service.host_node.migration_service import upgrade_node_database


class NodeRequestHandler(WSGIRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_request(self, code="-", size="-"):
        # HTTP hooks 记录路径和状态；不把完整查询串或请求行再次写入 access log。
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="独立主机 Node：显式初始化或启动主机控制与协议联调服务。")
    parser.add_argument("--env-file", type=Path, help="显式 Node 环境文件；不自动读取平台 .env")
    parser.add_argument("command", choices=("init-db", "serve"))
    args = parser.parse_args(argv)
    app = server = housekeeping = None
    previous = {}
    try:
        settings = load_node_settings(env_file=args.env_file)
        if args.command == "init-db":
            upgrade_node_database(settings)
            print("Node 状态库已初始化；未迁移中心平台数据库。")
            return 0
        app = create_node_app(settings)
        stop = Event()
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                number = getattr(signal, name)
                previous[number] = signal.signal(number, lambda *_: stop.set())
        server = make_server(settings.host, settings.port, app, threaded=True, request_handler=NodeRequestHandler)
        server.daemon_threads = False
        server.timeout = 0.25
        housekeeping = NodeHousekeeping(app)
        housekeeping.start()
        print(f"Node {settings.node_id} listening on {settings.host}:{server.server_port}; no inference gateway", flush=True)
        while not stop.is_set():
            server.handle_request()
        return 0
    except (ConfigurationError, StartupError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (Exception, SystemExit):
        print("Node 运行失败；请检查独立配置、数据库权限、端口及日志。", file=sys.stderr)
        return 2
    finally:
        if server:
            server.server_close()
        if housekeeping:
            housekeeping.close()
        if app:
            close_node_app(app)
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
