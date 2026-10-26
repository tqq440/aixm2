"""Linux WSGI 入口：固定单进程、线程服务；应用与采集在fork后创建。"""

from filelock import FileLock
from backend.config.deployment.linux_settings import deployment_settings
from backend.modules.api.app import create_app,close_app
from backend.modules.host_node.app import create_node_app,close_node_app
from backend.modules.host_node.housekeeping import NodeHousekeeping


class DeploymentRuntime:
    def __init__(self,component,env_file):
        self.app=self.housekeeping=self.lock=None;self.component=component
        settings,_=deployment_settings(component,env_file)
        if not settings.database_path.is_file():raise ValueError("Explicit database initialization required")
        try:
            if component=="node":
                self.app=create_node_app(settings)
                housekeeping=NodeHousekeeping(self.app);housekeeping.start();self.housekeeping=housekeeping
            elif component=="center":
                self.lock=FileLock(str(settings.database_path)+".linux-api.lock",timeout=0,thread_local=False);self.lock.acquire()
                self.app=create_app(settings)
            else:raise ValueError("WSGI component required")
        except BaseException:
            self.close();raise

    def close(self):
        try:
            try:
                if self.housekeeping:
                    housekeeping=self.housekeeping;self.housekeeping=None;housekeeping.close()
            finally:
                if self.app:
                    app=self.app;self.app=None
                    (close_node_app if self.component=="node" else close_app)(app)
        finally:
            if self.lock:self.lock.release();self.lock=None


def gunicorn_options(settings,threads,exit_hook):
    return {"bind":f"{settings.host}:{settings.port}","workers":1,"worker_class":"gthread","threads":threads,
        "preload_app":False,"reload":False,"timeout":30,"graceful_timeout":930,"keepalive":2,
        "max_requests":0,"control_socket_disable":True,"accesslog":None,"errorlog":"-","loglevel":"warning","capture_output":False,
        "limit_request_line":4094,"limit_request_fields":64,"limit_request_field_size":8190,"worker_exit":exit_hook}


def serve_linux(component,env_file,threads):
    # 导入可选依赖只在显式Linux启动时发生，Windows/测试不偷偷改用开发服务器。
    from gunicorn.app.base import BaseApplication
    settings,_=deployment_settings(component,env_file)

    class Server(BaseApplication):
        runtime=None

        def load_config(self):
            # 不调用读取GUNICORN_CMD_ARGS/任意Python配置的Application.load_config。
            for key,value in gunicorn_options(settings,threads,self.worker_exit).items():self.cfg.set(key,value)

        def load(self):
            self.runtime=DeploymentRuntime(component,env_file)
            return self.runtime.app

        def worker_exit(self,server,worker):
            if self.runtime:self.runtime.close();self.runtime=None

    Server().run()
