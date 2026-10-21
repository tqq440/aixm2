"""真实 Windows 回环 HTTP 的中心→Node→隔离适配器；不调用模型。"""

from threading import Thread

from werkzeug.serving import make_server

from backend.service.host_node.execution_client import NodeExecutionClient
from backend.service.invocations.node_bridge import NodeInvocationBridge
from tests.integration.test_common_execution import case
from tests.integration.test_node_execution_bridge import EXECUTOR, linked, lease_for


def test_real_http_dispatch_duplicate_and_result_retrieval(linked):
    server = make_server("127.0.0.1", 0, linked.node.app, threaded=True)
    thread = Thread(target=server.serve_forever); thread.start()
    try:
        target = {**linked.target, "node": {**linked.target["node"], "baseUrl": f"http://127.0.0.1:{server.server_port}"}}
        linked.case.service(targets=[target], version=linked.case.entries["asr-main"]["version"])
        bridge = NodeInvocationBridge(linked.case.core, client_factory=lambda binding: NodeExecutionClient(binding,
            environ={"TEST_NODE_EXECUTOR": EXECUTOR}, timeout_seconds=5))
        task, lease = lease_for(linked)
        assert bridge.dispatch(lease)
        assert not bridge.dispatch(lease)
        assert bridge.fetch_result(lease)
        client = bridge.client_factory(target["node"])
        command = bridge.prepare(lease)
        assert client.execute(command).state == "succeeded"  # HTTP 重复也不重做
        assert len(linked.adapter.calls) == 1
        assert bridge.snapshot(lease)["resultSaved"]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
