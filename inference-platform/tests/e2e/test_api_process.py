"""实际启动进程、访问本机 HTTP、发停止信号；不依赖模型或业务数据。"""

import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from backend.config.settings.app_settings import PROJECT_ROOT


@pytest.mark.e2e
def test_real_api_process_starts_serves_health_and_stops_cleanly(tmp_path):
    # 使用当前源码的临时副本，隔离用户本地 .env，不修改用户配置。
    isolated_project = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / "backend", isolated_project / "backend", ignore=shutil.ignore_patterns("__pycache__"))
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("INFERENCE_")}
    environment.update({
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(isolated_project),
        "INFERENCE_HOST": "127.0.0.1",
        "INFERENCE_PORT": str(port),
        "INFERENCE_DATABASE_PATH": str(tmp_path / "db/api.sqlite3"),
        "INFERENCE_LOG_DIR": str(tmp_path / "logs"),
        "INFERENCE_LOG_LEVEL": "INFO",
    })
    output_path = tmp_path / "process-output.log"
    with output_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "backend.modules.api.run"],
            cwd=isolated_project,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/health",
                headers={"X-Request-ID": "e2e-real-http"},
            )
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    pytest.fail("API exited before becoming ready: " + output_path.read_text(encoding="utf-8", errors="replace"))
                try:
                    with opener.open(request, timeout=1) as response:
                        payload = json.loads(response.read())
                        assert response.status == 200
                        assert payload["data"]["status"] == "ok"
                        assert payload["data"]["database"] == "ok"
                        assert payload["requestId"] == "e2e-real-http"
                        assert response.headers["X-Request-ID"] == payload["requestId"]
                    break
                except urllib.error.URLError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
            process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            assert process.wait(timeout=10) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    entries = [json.loads(line) for line in (tmp_path / "logs/api.log").read_text(encoding="utf-8").splitlines()]
    assert any(entry["requestId"] == "e2e-real-http" for entry in entries)
    assert any("应用资源已释放" in entry["message"] for entry in entries)
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex(("127.0.0.1", port)) != 0
