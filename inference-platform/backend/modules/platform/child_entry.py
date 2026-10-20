"""管理器已将本进程加入 Job 后才放行目标入口；无平台业务规则。"""

import argparse
import runpy
import signal
import subprocess
import sys

from backend.config.settings.app_settings import PROJECT_ROOT


def main() -> int:
    parser = argparse.ArgumentParser(description="内部受管服务入口，不单独运行")
    parser.add_argument("service", choices=("api", "dispatcher", "agent", "vision_worker", "remote_asr_worker", "frontend"))
    parser.add_argument("--node-path")
    parser.add_argument("--frontend-port", type=int, default=5173)
    args = parser.parse_args()
    if sys.stdin.buffer.readline(16) != b"GO\n":
        return 2
    if args.service == "frontend":
        # Node 与这个薄包装共享进程组，Break 同时到达两者；包装等待真正 Node 退出。
        signal.signal(signal.SIGBREAK, lambda *_: None)
        signal.signal(signal.SIGINT, lambda *_: None)
        process = subprocess.Popen(
            [args.node_path, str(PROJECT_ROOT / "frontend/node_modules/vite/bin/vite.js"),
             "--host", "127.0.0.1", "--port", str(args.frontend_port), "--strictPort"],
            cwd=PROJECT_ROOT / "frontend", stdin=subprocess.DEVNULL,
        )
        return process.wait()
    modules = {
        "api": "backend.modules.api.run", "dispatcher": "backend.modules.dispatcher.task_dispatcher",
        "agent": "backend.modules.agent.node_agent",
        "vision_worker": "backend.modules.worker.vision_worker",
        "remote_asr_worker": "backend.modules.worker.remote_asr_worker",
    }
    sys.argv = [modules[args.service]]
    runpy.run_module(modules[args.service], run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
