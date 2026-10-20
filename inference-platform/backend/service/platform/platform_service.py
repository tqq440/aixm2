"""本机平台的启动就绪与有序退出；ASR 生命周期仅由 Agent 管理。"""

import logging
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time

from filelock import FileLock, Timeout
from pydantic import ValidationError

from backend.common.process.platform_control import local_http, port_available
from backend.common.process.platform_process import OwnedProcess, ProcessTreeJob
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.platform.platform_repository import ControlFiles
from backend.data.database.database import Database
from backend.schema.platform.platform_schema import PlatformOptions, PlatformState, StopRequest
from backend.service.agent.agent_status_service import agent_is_ready, agent_shutdown_confirmed
from backend.service.asr.adapter_catalog import remote_asr_configured
from backend.service.asr.remote_service import RemoteAsrService
from backend.service.maintenance.environment_service import check_environment


class PlatformService:
    def __init__(self, settings: AppSettings, options: PlatformOptions, instance_id: str,
                 control: ControlFiles, logger: logging.Logger):
        self.settings, self.options, self.control, self.logger = settings, options, control, logger
        self.processes = {}
        self.job = None
        self._agent_stop_requested = False
        self._agent_started_at = None
        self._remote_stop_requested = False
        self._remote_started_at = None
        self._remote_uncertain = False
        self.state = PlatformState(
            instance_id=instance_id, status="starting", manager_pid=os.getpid(), api_port=settings.port,
            frontend_port=options.frontend_port, updated_at=time.time(), message="正在检查环境。",
            log_dir=str(settings.log_dir / "platform" / instance_id),
        )

    def publish(self, *, status=None, message=None):
        if status is not None:
            self.state.status = status
        if message is not None:
            self.state.message = message
        self.state.updated_at = time.time()
        self.state.processes = {name: item.pid for name, item in self.processes.items()}
        self.state.exit_codes = {name: item.poll() for name, item in self.processes.items()}
        self.control.write(self.control.state_path, self.state.model_dump(mode="json"))

    def publish_cleanup(self, **updates):
        """状态盘故障不能阻止进程清理；不能保存的退出结果由锁/真实探测显示。"""
        try:
            self.publish(**updates)
        except OSError:
            self.logger.exception("停止期间无法保存状态，继续确认所属进程退出")

    def stop_requested(self) -> bool:
        try:
            request = StopRequest.model_validate(self.control.read(self.control.stop_path))
            return request.instance_id == self.state.instance_id
        except ValidationError:
            return False

    def preflight(self):
        if self.settings.port == self.options.frontend_port:
            raise RuntimeError("API 和前端端口不能相同。")
        for name, port in (("API", self.settings.port), ("前端", self.options.frontend_port)):
            if not port_available(port):
                raise RuntimeError(f"{name} 端口 {port} 已占用。请先停止原服务；本脚本不会接管或结束它。")
        for component, suffix in (("worker", ".worker.lock"), ("agent", ".agent.lock"),
                                  ("vision_worker", ".vision-worker.lock"), ("dispatcher", ".dispatcher.lock"),
                                  ("remote_asr_worker", ".remote-asr-worker.lock")):
            lock = FileLock(str(self.settings.database_path) + suffix, timeout=0)
            try:
                with lock:
                    pass
            except Timeout:
                raise RuntimeError(f"数据库已有 {component} 运行，请先在原窗口停止；不会接管它。") from None
        report = check_environment(self.settings, node_path=self.options.node_path)
        if not report.passed:
            messages = [item.message for item in report.checks if not item.passed]
            raise RuntimeError("环境检查未通过：" + "；".join(messages))

    def spawn(self, name: str):
        command = [sys.executable, "-m", "backend.modules.platform.child_entry", name]
        environment = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        # 只给 Vite 注入代理；INFERENCE_API_TARGET 不是后台配置字段。
        environment.pop("INFERENCE_API_TARGET", None)
        if name == "frontend":
            command += ["--node-path", str(self.options.node_path), "--frontend-port", str(self.options.frontend_port)]
            environment["INFERENCE_API_TARGET"] = f"http://127.0.0.1:{self.settings.port}"
        if name == "agent":
            # 为本次创建建立时间边界，避免复用 PID 时信任旧会话的退出凭据。
            self._agent_started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        elif name == "remote_asr_worker":
            self._remote_started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        item = OwnedProcess(name, command, cwd=PROJECT_ROOT, env=environment,
                            log_path=Path(self.state.log_dir) / f"{name}.log", job=self.job)
        self.processes[name] = item
        self.publish(message=f"正在等待 {name} 就绪；日志：{self.state.log_dir}")

    def services_ready(self) -> bool:
        health = local_http(self.settings.port, "/api/health")
        runtime = local_http(self.settings.port, "/api/runtime")
        if not health or not runtime or not local_http(self.options.frontend_port, "/", json_response=False):
            return False
        data = runtime.get("data", {})
        agent = self.processes.get("agent")
        if agent is None or not agent_is_ready(self.settings, agent.pid, started_after=self._agent_started_at):
            return False
        required = [("vision_worker", "visionWorker"), ("dispatcher", "dispatcher")]
        if remote_asr_configured(self.settings):
            required.append(("remote_asr_worker", "remoteAsrWorker"))
        for name, key in required:
            component = data.get(key, {})
            if name not in self.processes:
                return False
            if component.get("pid") != self.processes[name].pid or component.get("availability") != "online":
                return False
            if name == "vision_worker" and not component.get("modelReady"):
                return False
        # 同时校验 Vite 代理指向本次 API，而不是仅有一个可打开的空页面。
        proxy = local_http(self.options.frontend_port, "/api/runtime")
        return bool(proxy and all(
            proxy.get("data", {}).get(key, {}).get("pid") == self.processes[name].pid
            for name, key in required
        ))

    def assert_processes_alive(self):
        for name, item in self.processes.items():
            code = item.poll()
            if code is not None:
                raise RuntimeError(f"{name} 已退出，退出码 {code}；请查看 {self.state.log_dir}/{name}.log")

    def start(self) -> bool:
        self.publish()
        self.preflight()
        Path(self.state.log_dir).mkdir(parents=True, exist_ok=True)
        if self.stop_requested():
            return False
        self.job = ProcessTreeJob()
        services = ["api", "dispatcher", "agent", "vision_worker"]
        if remote_asr_configured(self.settings):
            services.append("remote_asr_worker")
        services.append("frontend")
        for name in services:
            if self.stop_requested():
                return False
            self.spawn(name)
        deadline = time.monotonic() + self.options.startup_timeout_seconds
        while not self.stop_requested():
            self.assert_processes_alive()
            if self.services_ready():
                message = "API、投递器、Node Agent、Vision Worker 与页面均已就绪；本机 ASR 等待显式启动。"
                if remote_asr_configured(self.settings):
                    message += " 外部调用 Worker 已运行；远端服务是否可用以外部服务状态为准。"
                self.publish(status="running", message=message)
                return True
            if time.monotonic() >= deadline:
                raise RuntimeError("等待就绪超时，将停止本次创建的全部服务；请查看各服务日志。")
            self.publish()
            time.sleep(0.25)
        return False

    def tick(self):
        self.assert_processes_alive()
        self.publish()

    def _begin_remote_drain(self):
        database = Database(self.settings)
        try:
            RemoteAsrService(database, self.settings).begin_drain()
        finally:
            database.dispose()

    def _remote_shutdown_confirmed(self, pid):
        database = Database(self.settings)
        try:
            remote = RemoteAsrService(database, self.settings)
            confirmed = remote.local_shutdown_confirmed(pid, started_after=self._remote_started_at)
            self._remote_uncertain = remote.has_uncertain()
            return confirmed
        finally:
            database.dispose()

    def stop(self) -> bool:
        """先排空本机 ASR 与外部 HTTP 调用；只确认本实例所属进程的退出。"""
        self.publish_cleanup(status="stopping", message="正在等待 Agent 停止接单、完成当前 ASR 音频并确认退出。")
        agent = self.processes.get("agent")
        remote_worker = self.processes.get("remote_asr_worker")
        if remote_worker is not None and not self._remote_stop_requested:
            # 停止意图先落库，防止等本机 ASR 排空时继续提交远端任务。
            if remote_worker.poll() is None:
                self._begin_remote_drain()
                remote_worker.stop(0)
            self._remote_stop_requested = True
        if agent is not None:
            if not self._agent_stop_requested:
                # stop(0) 只发出停止信号，不杀死进程。之后持续观察同一个 Popen。
                # 不重复发信号，也不因客户端等待超时而中断正在处理的音频。
                agent.stop(0)
                self._agent_stop_requested = True
            if agent.poll() is None:
                return False
            if not agent_shutdown_confirmed(self.settings, agent.pid, started_after=self._agent_started_at):
                self.publish_cleanup(message="Agent 已退出，但尚未确认 ASR 安全停止；保留管理器与锁，请检查日志。")
                return False
        if remote_worker is not None:
            if remote_worker.poll() is None:
                self.publish_cleanup(message="正在等待当前外部 ASR 调用按原期限收尾；不会向远端发送停机命令。")
                return False
            if not self._remote_shutdown_confirmed(remote_worker.pid):
                self.publish_cleanup(message="外部调用 Worker 已退出，但调用记录尚未确认保存；保留管理器与锁。")
                return False
        self.publish_cleanup(message=(
            "ASR 退出已确认，正在停止 Vision Worker、投递器、API 和页面。" if agent is not None
            else "本次管理器尚未创建 Agent；正在停止本次已创建的其他服务。"
        ))
        for name in ("vision_worker", "dispatcher", "api", "frontend"):
            item = self.processes.get(name)
            if item is not None:
                timeout = self.settings.worker_stop_timeout_seconds + 3 if name == "vision_worker" else 5
                if not item.stop(timeout):
                    self.logger.warning("%s 未及时响应停止，稍后由本实例 Job 兜底。", name)
                self.publish_cleanup()
        if self.job is not None:
            if not self.job.wait_empty(2):
                self.logger.warning("仍有本实例进程未退出，终止本实例 Job；没有操作其他进程。")
                self.job.terminate()
            if not self.job.wait_empty(5):
                self.publish_cleanup(message="尚未确认本实例全部进程停止，保持管理器与锁，请检查日志。")
                return False
        for item in self.processes.values():
            item.process.wait(timeout=1)
            item.close_log()
        if self.job is not None:
            self.job.close()
            self.job = None
        return True

    def run(self) -> int:
        self.control.prepare()
        lock = FileLock(self.control.lock_path, timeout=0)
        try:
            lock.acquire()
        except Timeout:
            self.logger.error("已有管理器持有锁，本进程不会接管或覆盖它。")
            return 2
        failure = None
        try:
            if self.start():
                while not self.stop_requested():
                    self.tick()
                    time.sleep(0.5)
        except (Exception, KeyboardInterrupt) as exc:
            failure = str(exc) or "管理器收到中断。"
            self.logger.exception("管理器启动或运行失败")
        finally:
            try:
                while True:
                    try:
                        if self.stop():
                            break
                    except Exception:
                        self.logger.exception("停止确认失败，保持管理器及 Job 重试")
                    time.sleep(1)
                message = (
                    failure + " 本实例创建的服务均已停止。" if failure else "本实例创建的全部服务均已确认停止。"
                )
                if self._remote_uncertain:
                    message += " 本机服务已停；远端请求停止未确认，相关调用通道仍阻塞，需人工核对。"
                self.publish_cleanup(status="failed" if failure else "stopped", message=message)
            finally:
                lock.release()
        return 2 if failure else 0
