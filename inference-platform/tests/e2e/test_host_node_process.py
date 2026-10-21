"""真实本机 Node 子进程与 HTTP 上报器；不代表 Linux/Docker 或真实 ASR 验收。"""

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from backend.config.host_node.node_settings import PROJECT_ROOT
from tests.integration.host_node_support import ADMIN, OBSERVER, REPORT, config


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def request_json(url, token=ADMIN, body=None, method="GET"):
    request = Request(url, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method=method)
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=1) as response:
        return json.load(response)["data"]


@contextmanager
def running_node(folder, environment):
    with (folder / f"stdout-{uuid4().hex}.txt").open("w", encoding="utf-8") as output, (folder / f"stderr-{uuid4().hex}.txt").open("w", encoding="utf-8") as errors:
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        process = subprocess.Popen([sys.executable, "-m", "backend.modules.host_node.node_cli", "serve"],
            cwd=folder, env=environment, stdout=output, stderr=errors, **kwargs)
        try:
            url = "http://127.0.0.1:" + environment["NODE_PORT"]
            deadline = time.monotonic() + 20
            while True:
                try:
                    request_json(url + "/node/v1/status")
                    break
                except (URLError, TimeoutError, ConnectionError):
                    assert process.poll() is None, "Node 子进程意外退出，请检查隔离 stdout/stderr"
                    assert time.monotonic() < deadline, "等待 Node 超时"
                    time.sleep(0.1)
            yield url
        finally:
            if process.poll() is None:
                process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            assert process.returncode == 0


def test_real_node_restart_and_simulated_asr_http_reports_preserve_identity_and_daily_boundary(tmp_path):
    protected = tmp_path / "platform.sqlite3"
    with sqlite3.connect(protected) as connection:
        connection.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO tasks VALUES ('old-task-must-stay')")
    before = sha256(protected.read_bytes()).hexdigest()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("NODE_", "INFERENCE_"))}
    environment.update(PYTHONPATH=str(PROJECT_ROOT), PYTHONUNBUFFERED="1", NODE_ID="isolated-http-node",
        NODE_PORT=str(port), NODE_DATABASE_PATH=str(tmp_path / "host-node.sqlite3"), NODE_LOG_DIR=str(tmp_path / "logs"),
        NODE_ADMIN_TOKEN=ADMIN, NODE_OBSERVER_TOKEN=OBSERVER, NODE_REPORT_TOKEN_ASR=REPORT,
        INFERENCE_DATABASE_PATH=str(protected), INFERENCE_PORT="invalid-platform-setting")
    # 当前目录的 .env 含无效 Node 配置，独立入口不能自动读取它。
    (tmp_path / ".env").write_text("NODE_ADMIN_TOKEN=invalid\nNODE_ID=wrong\n", encoding="utf-8")
    for _ in range(2):
        initialized = subprocess.run([sys.executable, "-m", "backend.modules.host_node.node_cli", "init-db"],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20)
        assert initialized.returncode == 0, initialized.stderr
    instance_id = str(uuid4())

    def simulated_asr(url, revision, sequence, states):
        reporter_environment = {key: value for key, value in environment.items() if key not in {"NODE_ADMIN_TOKEN", "NODE_OBSERVER_TOKEN"}}
        result = subprocess.run([sys.executable, str(PROJECT_ROOT / "tests/fixtures/host-node/simulated_asr.py"),
            "--node-url", url, "--instance-id", instance_id, "--revision", revision,
            "--sequence-start", str(sequence), "--states", *states], cwd=tmp_path, env=reporter_environment,
            capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["simulation"] is True and payload["inferencePerformed"] is False
        return payload

    with running_node(tmp_path, environment) as url:
        configured = request_json(url + "/node/v1/admin/services/asr", body={"config": config()}, method="PUT")
        entry = request_json(url + "/node/v1/admin/services/asr/instances", body={"instanceId": instance_id,
            "configRevision": configured["configRevision"], "expectedGeneration": 0}, method="POST")
        first_epoch = request_json(url + "/node/v1/status")["epochId"]
        simulated_asr(url, entry["configRevision"], 1, ["starting", "warming", "ready"])
        assert request_json(url + "/node/v1/services/asr")["state"] == "ready"
    with running_node(tmp_path, environment) as url:
        assert request_json(url + "/node/v1/status")["epochId"] != first_epoch
        current = request_json(url + "/node/v1/services/asr", token=OBSERVER)
        assert current["instanceId"] == instance_id and not current["reportFresh"]
        duplicate = simulated_asr(url, entry["configRevision"], 3, ["ready"])
        assert duplicate["reports"][0]["duplicate"] is True
        assert request_json(url + "/node/v1/services/asr")["state"] == "unknown"
        simulated_asr(url, entry["configRevision"], 4, ["busy", "draining", "stopped"])
        assert request_json(url + "/node/v1/services/asr")["state"] == "stopped"
    with sqlite3.connect(tmp_path / "host-node.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM host_node_instances").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM host_node_events WHERE event_type='NODE_STOPPED'").fetchone()[0] == 2
    assert sha256(protected.read_bytes()).hexdigest() == before
    logs = "".join(p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.log"))
    for token in (ADMIN, OBSERVER, REPORT):
        assert token not in logs
