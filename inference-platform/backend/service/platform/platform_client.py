"""一键命令的客户端：通过实例请求让原管理器关闭自己的句柄。"""

import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from filelock import FileLock
from pydantic import ValidationError

from backend.common.process.platform_control import local_http, port_available
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.platform.platform_repository import ControlFiles
from backend.schema.platform.platform_schema import PlatformOptions, PlatformState, StopRequest


class PlatformClient:
    def __init__(self, control: ControlFiles):
        self.control = control

    def read_state(self):
        try:
            return PlatformState.model_validate(self.control.read(self.control.state_path))
        except ValidationError:
            return None

    def status(self) -> dict:
        state = self.read_state()
        locked = self.control.manager_locked()
        result = {
            "managerRunning": locked, "state": state.model_dump(mode="json") if state else None,
            "message": "管理器未运行；未接管任何手动启动的进程。",
        }
        if locked:
            age = None if state is None else max(0, time.time() - state.updated_at)
            result["heartbeatAgeSeconds"] = age
            result["message"] = "管理器持有锁，正在运行。" if age is not None and age < 10 else "管理器持有锁，但状态暂未更新，请检查日志。"
        if state is not None:
            result["apiPortAvailable"] = port_available(state.api_port)
            result["frontendPortAvailable"] = port_available(state.frontend_port)
            result["healthReachable"] = bool(local_http(state.api_port, "/api/health"))
            result["pageReachable"] = bool(local_http(state.frontend_port, "/", json_response=False))
            if not locked and state.status in ("starting", "running", "stopping"):
                result["message"] = "管理器已退出，上次状态不是正常停止；尚未确认 ASR 退出，请核对 Agent 状态与日志。"
        return result

    def request_stop(self, state: PlatformState):
        self.request_stop_instance(state.instance_id)

    def request_stop_instance(self, instance_id: str):
        request = StopRequest(instance_id=instance_id)
        self.control.write(self.control.stop_path, request.model_dump())

    def wait_stopped(self, instance_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.read_state()
            if state is None or state.instance_id != instance_id:
                return False
            if not self.control.manager_locked():
                return state.status in ("stopped", "failed")
            time.sleep(0.2)
        return False

    def stop(self, timeout: float) -> tuple[int, dict]:
        self.control.prepare()
        # 启动客户端可能仍在等待模型加载；停止不能被它持有的 client.lock 阻塞。
        # 精确实例令牌使并发/迟到的请求不会取消随后启动的新实例。
        if not self.control.manager_locked():
            result = self.status()
            state = self.read_state()
            if state is not None and state.status in ("starting", "running", "stopping"):
                return 2, result
            return 0, result
        state = self.read_state()
        if state is None:
            return 2, {"message": "管理器持有锁但实例状态不可读；无法安全定位实例，请检查管理器日志。"}
        self.request_stop(state)
        if not self.wait_stopped(state.instance_id, timeout):
            result = self.status()
            result["message"] = "已发送停止请求，但尚未确认全部进程退出；管理器会继续等待当前 ASR 音频完成及退出确认，请保留状态文件并检查日志。可再次执行停止命令。"
            return 2, result
        return 0, self.status()

    def launch_manager(self, settings: AppSettings, options: PlatformOptions, instance_id: str):
        logs = settings.log_dir / "platform"
        logs.mkdir(parents=True, exist_ok=True)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        command = [
            sys.executable, "-m", "backend.modules.platform.platform_cli", "supervise",
            "--node-path", str(options.node_path), "--frontend-port", str(options.frontend_port),
            "--startup-timeout-seconds", str(options.startup_timeout_seconds), "--instance-id", instance_id,
        ]
        with (logs / f"manager-{instance_id}.log").open("ab", buffering=0) as stream:
            return subprocess.Popen(
                command, cwd=PROJECT_ROOT, env=dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1"),
                stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup,
            )

    def start(self, settings: AppSettings, options: PlatformOptions) -> tuple[int, dict]:
        if os.name != "nt":
            raise RuntimeError("服务管理脚本仅支持 Windows。")
        self.control.prepare()
        with FileLock(self.control.client_lock_path, timeout=5):
            if self.control.manager_locked():
                state = self.read_state()
                result = self.status()
                if state is not None and state.status == "running" and time.time() - state.updated_at < 10:
                    if (state.api_port == settings.port and state.frontend_port == options.frontend_port
                            and result.get("healthReachable") and result.get("pageReachable")):
                        result["message"] = "本项目已有运行中的实例，API 与页面可访问，未重复启动；模型状态以页面报告为准。"
                        return 0, result
                result["message"] = "已有管理器运行或正在启动/停止；未创建第二个实例。请先查看状态，必要时停止后重启。"
                return 2, result
            instance_id = uuid4().hex
            process = self.launch_manager(settings, options, instance_id)
            deadline = time.monotonic() + options.startup_timeout_seconds + 60
            try:
                while time.monotonic() < deadline:
                    state = self.read_state()
                    if state is not None and state.instance_id == instance_id:
                        if state.status == "running" and self.control.manager_locked():
                            result = self.status()
                            result["message"] = f"平台已就绪，ASR 等待显式启动；请打开 http://127.0.0.1:{options.frontend_port}，停止整个平台时运行 stop-platform.ps1。"
                            return 0, result
                        if state.status in ("failed", "stopped") and not self.control.manager_locked():
                            return 2, self.status()
                    if process.poll() is not None:
                        result = self.status()
                        result["message"] = f"管理器启动失败（退出码 {process.returncode}），请查看 {settings.log_dir}/platform/manager-{instance_id}.log"
                        return 2, result
                    time.sleep(0.2)
            except KeyboardInterrupt:
                # 可能还没出现首次 state；本次 UUID 已知，不能依赖首次写盘才发请求。
                self.request_stop_instance(instance_id)
                self.wait_stopped(instance_id, 45)
                return 2, {"message": "启动等待被中断，已请求停止本次实例；请运行状态命令确认。"}
            self.request_stop_instance(instance_id)
            self.wait_stopped(instance_id, 45)
            return 2, {"message": "启动等待超时，已请求回滚本次实例；请查看状态与管理器日志。"}
