"""真实回环 HTTP 排空/恢复与管理脚本；Docker 层明确为替身。"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
from threading import Thread

from werkzeug.serving import make_server

from backend.modules.host_node.app import close_node_app
from backend.config.host_node.node_settings import PROJECT_ROOT
from backend.service.host_node.business_control_client import BusinessControlClient
from backend.service.host_node.control_client import NodeControlClient
from tests.integration.host_control_support import FIRST, control_case, paused_ready, runtime_config
from tests.integration.host_node_support import ADMIN, REPORT, config, report_body


@contextmanager
def server_in_thread(server):
    thread = Thread(target=server.serve_forever)
    thread.start()
    try: yield server.server_address[1]
    finally: server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_real_http_drain_resume_and_one_click_client_with_simulated_docker(tmp_path):
    case = control_case(tmp_path)
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.headers.get("Authorization") == "Bearer " + REPORT
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            entry = case.service.get_service("asr")
            action = body["action"]
            case.service.report("asr", report_body(entry, entry["sequence"] + 1,
                "draining" if action == "drain" else "ready", activeTasks=0, queuedTasks=0,
                controlOperationId=body["operationId"], controlAction=action))
            encoded = json.dumps({**body, "accepted": True}).encode()
            self.send_response(200);self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(encoded)));self.end_headers();self.wfile.write(encoded)
        def log_message(self, *_args): pass
    try:
        with server_in_thread(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) as business_port:
            # 修改测试替身的已登记业务地址：此时无实例/操作，不启动任何实际业务。
            definition = config();definition["internalBaseUrl"] = f"http://127.0.0.1:{business_port}"
            case.service.save_service("asr", {"config": definition, "expectedVersion": 1})
            case.control.save_runtime("asr", {"config": runtime_config(), "expectedControlVersion": 2})
            case.control.business = BusinessControlClient(case.settings)
            case.docker.after_start = lambda _identifier: paused_ready(case)
            with server_in_thread(make_server("127.0.0.1", 0, case.app, threaded=True)) as node_port:
                client = NodeControlClient(case.settings, f"http://127.0.0.1:{node_port}")
                start = client.execute("asr", "start", "v1")
                for _ in range(5): case.control.tick()
                assert client.execute("asr", "start", "v1", operation_id=str(start.operation_id)).status == "succeeded"
                stop = client.execute("asr", "stop")
                for _ in range(4): case.control.tick()
                assert case.control.get_operation(str(stop.operation_id))["status"] == "succeeded"
                environment = {key: value for key, value in os.environ.items() if not key.startswith("NODE_")}
                environment.update(PYTHONPATH=str(PROJECT_ROOT), NODE_ID=case.settings.node_id, NODE_ADMIN_TOKEN=ADMIN)
                result = subprocess.run([sys.executable, "-m", "backend.modules.host_node.control_cli", "stop", "asr",
                    "--node-url", f"http://127.0.0.1:{node_port}", "--operation-id", str(stop.operation_id)],
                    env=environment, cwd=tmp_path, capture_output=True, text=True, timeout=15)
                assert result.returncode == 0 and json.loads(result.stdout)["status"] == "succeeded", result.stderr
                assert ADMIN not in result.stdout + result.stderr
        assert [body["action"] for body in calls] == ["resume", "drain"]
        assert case.docker.actions == [("start", FIRST), ("SIGTERM", FIRST)]
    finally: close_node_app(case.app)
