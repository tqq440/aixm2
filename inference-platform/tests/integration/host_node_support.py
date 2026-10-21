"""隔离 Node 与模拟 ASR 状态；不运行推理、不操作容器。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from backend.config.host_node.node_settings import NodeSettings
from backend.modules.host_node.app import create_node_app
from backend.service.host_node.migration_service import upgrade_node_database


ADMIN = "TEST_ONLY_ADMIN_" + "a" * 40
OBSERVER = "TEST_ONLY_OBSERVER_" + "o" * 40
REPORT = "TEST_ONLY_ASR_" + "r" * 40
VISION_REPORT = "TEST_ONLY_VISION_" + "v" * 40
LLM_REPORT = "TEST_ONLY_LLM_" + "l" * 40


def auth(token=ADMIN):
    return {"Authorization": f"Bearer {token}"}


def node_settings(folder, **overrides):
    values = dict(node_id="isolated-node", database_path=folder / "node.sqlite3", log_dir=folder / "logs",
        admin_token=ADMIN, observer_token=OBSERVER, report_tokens={"NODE_REPORT_TOKEN_ASR": REPORT,
            "NODE_REPORT_TOKEN_VISION": VISION_REPORT, "NODE_REPORT_TOKEN_LLM": LLM_REPORT})
    values.update(overrides)
    return NodeSettings(**values)


class Clock:
    def __init__(self):
        self.value = datetime.now(timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def node_case(folder, **overrides):
    settings = node_settings(folder, **overrides)
    upgrade_node_database(settings)
    clock = Clock()
    app = create_node_app(settings, clock=clock)
    return SimpleNamespace(app=app, client=app.test_client(), settings=settings, clock=clock,
                           service=app.extensions["host_node_service"])


def config(kind="asr"):
    return {"name": "隔离模拟 " + kind, "kind": kind, "modelId": "fixture-model",
            "internalBaseUrl": "http://127.0.0.1:18003", "reportProtocol": "lifecycle-v1",
            "reportTokenEnv": "NODE_REPORT_TOKEN_" + ("VISION" if kind == "vision" else kind.upper())}


def data(response, status=200):
    assert response.status_code == status, response.get_json()
    return response.get_json()["data"]


def register(case, service_id="asr", *, kind="asr", definition=None, expected=None):
    return data(case.client.put(f"/node/v1/admin/services/{service_id}", headers=auth(),
        json={"config": definition or config(kind), "expectedVersion": expected}))


def bind(case, entry, *, instance_id=None, generation=None):
    return data(case.client.post(f"/node/v1/admin/services/{entry['serviceId']}/instances", headers=auth(),
        json={"instanceId": instance_id or str(uuid4()), "configRevision": entry["configRevision"],
              "expectedGeneration": entry["generation"] if generation is None else generation}), 201)


def report_body(entry, sequence=1, state="ready", **overrides):
    body = {"instanceId": entry["instanceId"], "configRevision": entry["configRevision"],
        "sequence": sequence, "state": state, "health": "healthy" if state in {"ready", "busy"} else "unknown",
        "modelReady": state in {"ready", "busy", "draining"}, "acceptingRequests": state in {"ready", "busy"}}
    if state == "stopped":
        body.update(queuedTasks=0, activeTasks=0)
    if state == "busy":
        body.update(activeTasks=1)
    body.update(overrides)
    return body


def send(case, entry, sequence=1, state="ready", token=REPORT, **overrides):
    return case.client.post(f"/node/internal/v1/services/{entry['serviceId']}/reports", headers=auth(token),
                           json=report_body(entry, sequence, state, **overrides))
