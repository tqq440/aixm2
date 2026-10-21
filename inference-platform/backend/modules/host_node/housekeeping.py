"""一个后台采集与控制循环；页面 GET 不触发外部操作。"""

from threading import Event, Thread


class NodeHousekeeping:
    def __init__(self, app):
        self.app = app
        self.stop = Event()
        self.thread = Thread(target=self._run, name="host-node-housekeeping", daemon=False)

    def _run(self):
        while not self.stop.is_set():
            for name in ("host_node_control", "host_node_resources", "host_node_monitor"):
                if self.stop.is_set():
                    break
                try:
                    self.app.extensions[name].tick()
                except Exception:
                    # 数据库或采样异常不能触发重复控制动作；不记录可能含原始响应的异常正文。
                    self.app.extensions["app_logger"].error("NODE_HOUSEKEEPING_FAILED component=%s", name)
            self.stop.wait(0.25)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join()
