"""显式 Docker/业务替身，保留精确动作记录，不连接 Docker 或真实服务。"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from backend.common.containers.docker_engine import DockerFailure
from backend.modules.host_node.app import create_node_app
from backend.service.host_node.migration_service import upgrade_node_database
from tests.integration.host_node_support import Clock, auth, data, node_settings, register, report_body


FIRST = "1" * 64
SECOND = "2" * 64


def runtime_config(**overrides):
    value = {"versions": [{"versionId": "v1", "containerId": FIRST, "imageId": "sha256:" + "a" * 64,
                          "compatibilityEvidence": "隔离同模型 v1"},
                         {"versionId": "v2", "containerId": SECOND, "imageId": "sha256:" + "b" * 64,
                          "compatibilityEvidence": "隔离同模型 v2"}],
        "drainPath": "/internal/drain", "resumePath": "/internal/resume", "drainTimeoutSeconds": 10,
        "readyTimeoutSeconds": 10, "exitTimeoutSeconds": 10,
        "autoRollback": True, "rollbackCompatibilityEvidence": "隔离替身声明：v1/v2 数据格式和空队列恢复兼容"}
    value.update(overrides)
    return value


class DockerFake:
    def __init__(self):
        self.containers = {}
        for version in runtime_config()["versions"]:
            self.containers[version["containerId"]] = {"containerId": version["containerId"],
                "imageId": version["imageId"], "state": "created", "running": False,
                "restartPolicy": "no", "autoRemove": False, "labels": {
                    "inference.node_id": "isolated-node", "inference.service_id": "asr",
                    "inference.version_id": version["versionId"]}}
        self.actions = []
        self.inspections = []
        self.fail_inspect = self.start_unknown = self.term_unknown = False
        self.ignore_term = False
        self.after_start = None

    def inspect(self, identifier):
        self.inspections.append(identifier)
        if self.fail_inspect:
            raise DockerFailure("DOCKER_QUERY_FAILED")
        return deepcopy(self.containers[identifier])

    def start(self, identifier):
        self.actions.append(("start", identifier))
        self.containers[identifier].update(state="running", running=True)
        if self.after_start:
            self.after_start(identifier)
        if self.start_unknown:
            raise DockerFailure("DOCKER_ACTION_UNKNOWN")

    def terminate(self, identifier):
        self.actions.append(("SIGTERM", identifier))
        if not self.ignore_term:
            self.containers[identifier].update(state="exited", running=False)
        if self.term_unknown:
            raise DockerFailure("DOCKER_ACTION_UNKNOWN")


class BusinessFake:
    def __init__(self):
        self.node = None
        self.commands = []
        self.ack_only = False
        self.fail = False

    def send(self, config, runtime, action, command):
        self.commands.append(deepcopy(command))
        if not self.ack_only:
            entry = self.node.get_service("asr")
            state = "draining" if action == "drain" else "ready"
            self.node.report("asr", report_body(entry, entry["sequence"] + 1, state,
                queuedTasks=0, activeTasks=0, controlOperationId=command["operationId"], controlAction=action))
        if self.fail:
            raise DockerFailure("BUSINESS_CONTROL_UNKNOWN")


def control_case(folder, *, profile=None):
    settings = node_settings(folder, docker_enabled=True)
    upgrade_node_database(settings)
    docker, business, clock = DockerFake(), BusinessFake(), Clock()
    app = create_node_app(settings, clock=clock, docker=docker, business=business)
    node = app.extensions["host_node_service"]; business.node = node
    case = SimpleNamespace(app=app, client=app.test_client(), settings=settings, clock=clock,
        service=node, control=app.extensions["host_node_control"], docker=docker, business=business)
    entry = register(case)
    case.entry = entry
    case.runtime = data(case.client.put("/node/v1/admin/services/asr/runtime", headers=auth(),
                                       json={"expectedControlVersion": 0, "config": profile or runtime_config()}))
    return case


def submit(case, action="start", version="v1", *, operation_id=None):
    runtime = case.control.runtime("asr")
    command = {"operationId": operation_id or str(uuid4()), "action": action,
        "versionId": None if action == "stop" else version,
        "expectedControlVersion": runtime["controlVersion"], "configRevision": case.entry["configRevision"]}
    result = data(case.client.post("/node/v1/services/asr/operations", headers=auth(), json=command), 202)
    return result, command


def paused_ready(case, *, state="ready"):
    entry = case.service.get_service("asr")
    return case.service.report("asr", report_body(entry, entry["sequence"] + 1, state,
        acceptingRequests=False, queuedTasks=0, activeTasks=0))


def finish_start(case):
    operation, _ = submit(case)
    case.control.tick(); case.control.tick()
    paused_ready(case)
    case.control.tick(); case.control.tick()
    assert case.control.get_operation(operation["operationId"])["status"] == "succeeded"
    return operation
