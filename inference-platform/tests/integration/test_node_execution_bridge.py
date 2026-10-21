"""4C1 身份/恢复边界，业务适配器全为隔离替身，绝不调用模型。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import httpx2 as httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, event

from backend.common.exceptions.app_error import AppError
from backend.data.host_node.execution_models import HostNodeExecution
from backend.data.host_node.node_models import HostNodeInstance
from backend.data.invocations.invocation_models import InferenceInvocation
from backend.data.invocations.node_dispatch_models import InferenceNodeDispatch
from backend.data.tasks.task_models import TaskRecord
from backend.modules.host_node.app import close_node_app, create_node_app
from backend.schema.host_node.execution_schema import AdapterOutcome, ExecutionCommand
from backend.schema.invocations.invocation_schema import OnlineCompletion
from backend.schema.vision.detection_schema import DetectionResult
from backend.config.vision.model_catalog import MODEL_SHA256
from backend.service.host_node.execution_client import NodeExecutionClient
from backend.service.host_node.migration_service import upgrade_node_database
from backend.service.invocations.node_bridge import NodeInvocationBridge
from backend.service.queue.queue_service import QueueService
from tests.integration.redis_stream_fake import RedisStreamFake
from backend.utils.json.json_utils import canonical_json_sha256
from tests.integration.host_node_support import ADMIN, OBSERVER, REPORT, VISION_REPORT, LLM_REPORT, auth, bind, config, data, node_settings, register, send
from tests.integration.host_control_support import control_case, finish_start, submit
from tests.integration.test_common_execution import case, asr_result


EXECUTOR = "TEST_ONLY_EXECUTOR_" + "x" * 40


class Adapter:
    protocols = ("remote-v2", "openai-audio", "vision-json-v1", "chat-completions-sse")

    def __init__(self):
        self.calls = []
        self.action = lambda command: AdapterOutcome(state="succeeded", result=asr_result())

    def execute(self, command):
        self.calls.append(command)
        return self.action(command)


@pytest.fixture
def linked(case, tmp_path):
    settings = node_settings(tmp_path / "node", executor_token=EXECUTOR, metrics_enabled=False)
    upgrade_node_database(settings)
    adapter = Adapter()
    app = create_node_app(settings, execution_adapter=adapter)
    node = SimpleNamespace(app=app, client=app.test_client(), service=app.extensions["host_node_service"], settings=settings)
    definition = config(); definition["modelId"] = "asr-fixture"
    entry = bind(node, register(node, definition=definition)); data(send(node, entry))
    target = {"targetId": "primary", "name": "fixture", "baseUrl": "http://127.0.0.1:18003", "invokePath": "/asr",
        "healthPath": "/health", "concurrencyLimit": 1, "resourceGroupIds": ["shared"], "node": {
            "nodeId": settings.node_id, "serviceId": "asr", "configRevision": entry["configRevision"],
            "baseUrl": "http://127.0.0.1:8020", "executorTokenEnv": "TEST_NODE_EXECUTOR"}}
    case.group(); case.service(targets=[target]); case.budget()
    network = []

    def handle(request):
        network.append((request.method, request.url.path))
        # HTTPX 模拟传输走真实 Flask 鉴权/Schema/服务，不替换控制器。
        response = node.app.test_client().open(request.url.path, method=request.method, data=request.content,
            headers={key: value for key, value in request.headers.items() if key.lower() != "host"})
        return httpx.Response(response.status_code, content=response.data, headers={"content-type": "application/json"})

    factory = lambda binding: NodeExecutionClient(binding, environ={"TEST_NODE_EXECUTOR": EXECUTOR}, transport=httpx.MockTransport(handle))
    value = SimpleNamespace(case=case, node=node, adapter=adapter, entry=entry, target=target,
                            bridge=NodeInvocationBridge(case.core, client_factory=factory), network=network)
    yield value
    close_node_app(node.app)


def lease_for(linked):
    task, invocation = linked.case.task()
    return task, linked.case.core.claim_round("node-worker")[0]


def test_center_node_success_is_saved_before_single_release_and_duplicate_posts(linked):
    task, lease = lease_for(linked)
    def execute(command):
        with linked.node.service.database.transaction(immediate=True) as session:
            assert session.get(HostNodeExecution, command.invocation_id).state == "sending"
        with linked.case.db.transaction(immediate=True) as session:
            assert session.get(InferenceInvocation, command.invocation_id).permit_state == "held"
        return AdapterOutcome(state="succeeded", result=asr_result())
    linked.adapter.action = execute
    assert linked.bridge.dispatch(lease)
    assert not linked.bridge.dispatch(lease)
    assert linked.bridge.fetch_result(lease)
    assert len(linked.adapter.calls) == 1
    assert sum(method == "POST" for method, _ in linked.network) == 1
    with linked.case.db.transaction() as session:
        assert session.get(TaskRecord, task).status == "succeeded"
        assert session.get(InferenceNodeDispatch, lease.invocation_id).terminal_hash
    assert sum(e["eventType"] == "invocation.capacity_released" for e in linked.case.events(task)) == 1
    assert "NEVER_LOG_THIS_RESULT" not in str(linked.case.events(task))


def test_lost_response_fetches_persisted_result_without_reinference(linked):
    task, lease = lease_for(linked)
    real_factory = linked.bridge.client_factory
    class LoseReply:
        def __init__(self, binding): self.real = real_factory(binding)
        def availability(self): return self.real.availability()
        def execute(self, command):
            self.real.execute(command)
            raise ConnectionError("SECRET_MUST_NOT_LEAK")
        def result(self, invocation): return self.real.result(invocation)
    linked.bridge.client_factory = LoseReply
    with pytest.raises(AppError, match="只能取回"):
        linked.bridge.dispatch(lease)
    assert linked.case.core.get(lease.invocation_id)["permitState"] == "uncertain"
    assert linked.bridge.fetch_result(lease)
    state = linked.case.core.get(lease.invocation_id)
    assert state["outcome"] == "unknown" and state["resolvedOutcome"] == "succeeded" and state["permitState"] == "released"
    with linked.case.db.transaction() as session:
        assert session.get(TaskRecord, task).status == "failed"  # 历史不改写
        assert session.get(InferenceNodeDispatch, lease.invocation_id).late_result_json["text"] == asr_result().text
    assert linked.bridge.fetch_result(lease) and len(linked.adapter.calls) == 1
    assert "SECRET_MUST_NOT_LEAK" not in str(linked.case.events(task))


def test_expired_lease_records_timeout_then_saves_late_result(linked):
    task, lease = lease_for(linked)
    def finish(command):
        linked.case.expire(lease.invocation_id)
        return AdapterOutcome(state="succeeded", result=asr_result())
    linked.adapter.action = finish
    assert linked.bridge.dispatch(lease)
    events = linked.case.events(task)
    kinds = [e["eventType"] for e in events]
    assert kinds.index("invocation.finished") < kinds.index("invocation.node_late_result_saved")
    assert kinds.count("invocation.capacity_released") == 1


def test_unknown_adapter_exception_never_retries_or_releases(linked):
    task, lease = lease_for(linked)
    def fail(command): raise TimeoutError("SECRET_BODY")
    linked.adapter.action = fail
    assert not linked.bridge.dispatch(lease)
    assert not linked.bridge.dispatch(lease)
    assert not linked.bridge.fetch_result(lease)
    linked.case.expire(lease.invocation_id); linked.case.core.recover()
    assert linked.case.core.get(lease.invocation_id)["permitState"] == "uncertain"
    assert len(linked.adapter.calls) == 1
    assert "SECRET_BODY" not in str(linked.node.service.events({}))


def test_node_concurrent_duplicate_and_conflicting_attempt_are_fenced(linked):
    _, lease = lease_for(linked)
    command = linked.bridge.prepare(lease); started, finish = Event(), Event()
    def block(command):
        started.set(); assert finish.wait(5)
        return AdapterOutcome(state="succeeded", result=asr_result())
    linked.adapter.action = block
    executions = linked.node.app.extensions["host_node_executions"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(executions.execute, command)
        assert started.wait(3)
        try:
            assert executions.execute(command)["state"] == "sending"
            altered = command.model_copy(update={"attempt": 2})
            with pytest.raises(AppError, match="不同执行身份"): executions.execute(altered)
        finally:
            finish.set()
        assert future.result()["state"] == "succeeded"
    assert len(linked.adapter.calls) == 1


@pytest.mark.parametrize("token", [ADMIN, OBSERVER, REPORT, "public-app-key"])
def test_execution_credentials_are_separate_from_all_other_roles(linked, token):
    _, lease = lease_for(linked); command = linked.bridge.prepare(lease)
    response = linked.node.client.post("/node/internal/v1/executions", headers=auth(token), json=command.model_dump(mode="json", by_alias=True))
    assert response.status_code == 401 and not linked.adapter.calls


@pytest.mark.parametrize("path,method", [("/node/v1/admin/services/asr", "PUT"),
    ("/node/internal/v1/services/asr/reports", "POST"), ("/node/v1/services", "GET")])
def test_executor_cannot_manage_report_or_observe(linked, path, method):
    response = linked.node.client.open(path, method=method, headers=auth(EXECUTOR), json={})
    assert response.status_code == 401


@pytest.mark.parametrize("change", [{"node_id": "wrong"}, {"instance_id": uuid4()},
    {"config_revision": "a" * 64}, {"model_id": "different"}, {"deadline_at": datetime(2000, 1, 1, tzinfo=timezone.utc)}])
def test_stale_assignment_never_calls_adapter(linked, change):
    _, lease = lease_for(linked); command = linked.bridge.prepare(lease).model_copy(update=change)
    command = ExecutionCommand.model_validate(command.model_dump())
    with pytest.raises(AppError): linked.node.app.extensions["host_node_executions"].execute(command)
    assert not linked.adapter.calls


def test_gets_are_readonly_and_default_adapter_is_unavailable(linked):
    _, lease = lease_for(linked)
    linked.node.app.extensions["host_node_executions"].adapter = None
    with pytest.raises(AppError, match="尚未就绪"): linked.bridge.prepare(lease)
    assert not linked.adapter.calls
    assert linked.case.core.get(lease.invocation_id)["state"] == "reserved"
    linked.node.app.extensions["host_node_executions"].adapter = linked.adapter
    assert linked.bridge.dispatch(lease)
    before = len(linked.network)
    for _ in range(5): linked.bridge.snapshot(lease)
    assert len(linked.network) == before
    snapshot = data(linked.node.client.get(f"/node/internal/v1/executions/{lease.invocation_id}", headers=auth(EXECUTOR)))
    assert snapshot["result"] is None


def test_node_restart_retains_result_and_never_reexecutes(linked):
    _, lease = lease_for(linked); assert linked.bridge.dispatch(lease)
    command = linked.bridge.prepare(lease)
    close_node_app(linked.node.app)
    linked.node.app = create_node_app(linked.node.settings, execution_adapter=linked.adapter)
    assert linked.node.app.extensions["host_node_executions"].execute(command)["state"] == "succeeded"
    assert len(linked.adapter.calls) == 1


def test_interrupted_node_call_becomes_unknown_without_replaying(linked):
    _, lease = lease_for(linked); command = linked.bridge.prepare(lease)
    with linked.node.service.database.transaction(immediate=True) as session:
        now = linked.node.service._now(session)
        payload = command.model_dump(mode="json", by_alias=True)
        session.add(HostNodeExecution(invocation_id=command.invocation_id, service_id="asr", instance_id=str(command.instance_id),
            command_json=payload, request_hash=canonical_json_sha256(payload), node_epoch_id=linked.node.service.epoch_id,
            state="sending", created_at=now, updated_at=now))
    close_node_app(linked.node.app)
    linked.node.app = create_node_app(linked.node.settings, execution_adapter=linked.adapter)
    executions = linked.node.app.extensions["host_node_executions"]
    assert executions.get(command.invocation_id)["state"] == "unknown"
    assert executions.execute(command)["state"] == "unknown" and not linked.adapter.calls


def test_stale_receipt_cannot_overwrite_or_release(linked):
    _, lease = lease_for(linked); command = linked.bridge.prepare(lease)
    assert linked.case.core.mark_sent(lease)
    receipt = linked.node.app.extensions["host_node_executions"].execute(command)
    receipt["command"]["attempt"] += 1
    receipt["requestHash"] = canonical_json_sha256(receipt["command"])
    with pytest.raises(AppError, match="回传调用"): linked.bridge.apply_receipt(lease, receipt)
    assert linked.case.core.get(lease.invocation_id)["permitState"] == "held"


def test_bad_result_stays_held_then_cannot_release_on_wrong_type(linked):
    _, lease = lease_for(linked)
    linked.adapter.action = lambda command: AdapterOutcome(state="succeeded", result=asr_result().model_copy(update={"audio_duration_seconds": 99}))
    with pytest.raises(AppError): linked.bridge.dispatch(lease)
    assert linked.case.core.get(lease.invocation_id)["permitState"] == "uncertain"


def test_failure_proof_releases_once_and_conflicting_result_is_rejected(linked):
    task, lease = lease_for(linked)
    linked.adapter.action = lambda command: AdapterOutcome(state="failed", error_code="BUSINESS_REJECTED")
    assert linked.bridge.dispatch(lease) and linked.bridge.fetch_result(lease)
    assert linked.case.core.get(lease.invocation_id)["permitState"] == "released"
    assert sum(e["eventType"] == "invocation.capacity_released" for e in linked.case.events(task)) == 1


def test_result_and_event_failure_roll_back_center_together(linked):
    task, lease = lease_for(linked); command = linked.bridge.prepare(lease); assert linked.case.core.mark_sent(lease)
    receipt = linked.node.app.extensions["host_node_executions"].execute(command)
    def reject(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO inference_task_events"): raise RuntimeError("isolated disk failure")
    event.listen(linked.case.db.engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError): linked.bridge.apply_receipt(lease, receipt)
    finally:
        event.remove(linked.case.db.engine, "before_cursor_execute", reject)
    with linked.case.db.transaction() as session:
        assert session.get(TaskRecord, task).status == "running"
        assert session.get(InferenceInvocation, lease.invocation_id).permit_state == "held"
        assert session.get(InferenceNodeDispatch, lease.invocation_id).terminal_hash is None
    assert linked.bridge.fetch_result(lease) and len(linked.adapter.calls) == 1


@pytest.mark.parametrize("kind,token", [("vision", VISION_REPORT), ("llm", LLM_REPORT)])
def test_three_kinds_share_identity_boundary_but_not_claim_real_protocol_support(linked, kind, token):
    definition = config(kind); definition["modelId"] = "yolo-general" if kind == "vision" else "llm-fixture"
    entry = bind(linked.node, register(linked.node, service_id=kind, kind=kind, definition=definition))
    data(send(linked.node, entry, token=token))
    target = {**linked.target, "invokePath": "/" + kind, "upstreamModel": "fixture",
              "node": {**linked.target["node"], "serviceId": kind, "configRevision": entry["configRevision"]}}
    service = kind + "-main"
    linked.case.service(service, kind, targets=[target]); linked.case.budget(version=1)
    if kind == "vision":
        result = DetectionResult(model_sha256=MODEL_SHA256, image_width=64, image_height=64, detections=[],
            runtime_version="fixture", load_seconds=0, inference_seconds=0, total_seconds=0)
        task, _ = linked.case.task(service); lease = linked.case.core.claim_round("node-worker")[0]
    else:
        result = OnlineCompletion(output_characters=7, finish_reason="stop")
        accepted, request = linked.case.online(service)
        lease = linked.case.core.claim_online(accepted["invocationId"], accepted["workerId"])
    linked.adapter.action = lambda command: AdapterOutcome(state="succeeded", result=result)
    assert linked.bridge.dispatch(lease)
    assert linked.case.core.get(lease.invocation_id)["permitState"] == "released"
    assert "NEVER_LOG_THIS_BODY" not in str(linked.bridge.snapshot(lease))
    assert linked.adapter.calls[-1].kind == kind


@pytest.mark.parametrize("action", ["stop", "upgrade"])
def test_unknown_node_execution_blocks_docker_stop_and_upgrade(tmp_path, action):
    value = control_case(tmp_path)
    try:
        finish_start(value)
        entry = value.service.get_service("asr")
        with value.service.database.transaction(immediate=True) as session:
            now = value.service._now(session)
            session.add(HostNodeExecution(invocation_id=uuid4().hex, service_id="asr", instance_id=entry["instanceId"],
                request_hash="a" * 64, command_json={"attempt": 1}, node_epoch_id=value.service.epoch_id,
                state="unknown", created_at=now, updated_at=now))
        before = list(value.docker.actions)
        operation, _ = submit(value, action, "v2" if action == "upgrade" else None)
        value.control.tick()
        detail = value.control.get_operation(operation["operationId"])
        assert detail["status"] == "blocked" and detail["errorCode"] == "NODE_EXECUTIONS_UNCONFIRMED"
        assert value.docker.actions == before and not value.service.get_service("asr")["acceptingRequests"]
    finally:
        close_node_app(value.app)


def test_unknown_record_cannot_be_bypassed_with_false_stopped_report_or_config_change(linked):
    _, lease = lease_for(linked)
    linked.adapter.action = lambda command: (_ for _ in ()).throw(TimeoutError())
    assert not linked.bridge.dispatch(lease)
    data(send(linked.node, linked.entry, sequence=2, state="stopped"))
    new_config = {**config(), "modelId": "different"}
    response = linked.node.client.put("/node/v1/admin/services/asr", headers=auth(), json={"config": new_config, "expectedVersion": 1})
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "NODE_EXECUTIONS_UNCONFIRMED"
    response = linked.node.client.post("/node/v1/admin/services/asr/instances", headers=auth(), json={
        "instanceId": str(uuid4()), "configRevision": linked.entry["configRevision"], "expectedGeneration": 1})
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "NODE_EXECUTIONS_UNCONFIRMED"


def test_node_sql_error_does_not_log_result_or_lose_duplicate_fence(linked):
    from sqlalchemy.exc import OperationalError
    _, lease = lease_for(linked); command = linked.bridge.prepare(lease)
    database = linked.node.service.database
    def reject(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE host_node_executions"):
            raise OperationalError(statement, parameters, OSError("RAW_SECRET_SQL_ERROR"))
    event.listen(database.engine, "before_cursor_execute", reject)
    try:
        response = linked.node.client.post("/node/internal/v1/executions", headers=auth(EXECUTOR),
            json=command.model_dump(mode="json", by_alias=True))
        assert response.status_code == 503
    finally:
        event.remove(database.engine, "before_cursor_execute", reject)
    log_text = "".join(path.read_text(encoding="utf-8") for path in linked.node.settings.log_dir.glob("*.log"))
    assert "RAW_SECRET_SQL_ERROR" not in log_text and "NEVER_LOG_THIS_RESULT" not in log_text
    assert linked.node.app.extensions["host_node_executions"].execute(command)["state"] == "sending"
    assert len(linked.adapter.calls) == 1


def test_node_event_failure_prevents_upstream_send(linked):
    _, lease = lease_for(linked); command = linked.bridge.prepare(lease)
    def reject(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO host_node_events"): raise RuntimeError("isolated failure")
    event.listen(linked.node.service.database.engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError): linked.node.app.extensions["host_node_executions"].execute(command)
    finally:
        event.remove(linked.node.service.database.engine, "before_cursor_execute", reject)
    with linked.node.service.database.transaction() as session:
        assert session.get(HostNodeExecution, lease.invocation_id) is None
    assert not linked.adapter.calls


def test_remote_success_without_result_is_pending_not_platform_success(linked):
    _, lease = lease_for(linked); command = linked.bridge.prepare(lease); assert linked.case.core.mark_sent(lease)
    receipt = linked.node.app.extensions["host_node_executions"].execute(command)
    without_result = {**receipt, "result": None}
    assert not linked.bridge.apply_receipt(lease, without_result)
    assert linked.bridge.snapshot(lease)["resultPending"]
    assert linked.case.core.get(lease.invocation_id)["permitState"] == "held"
    assert linked.bridge.fetch_result(lease)
    assert not linked.bridge.snapshot(lease)["resultPending"]


def test_late_result_after_matching_manual_confirmation_does_not_release_twice(linked):
    task, lease = lease_for(linked); command = linked.bridge.prepare(lease); assert linked.case.core.mark_sent(lease)
    receipt = linked.node.app.extensions["host_node_executions"].execute(command)
    assert linked.case.core.fail(lease)
    linked.case.core.resolve_unknown(lease.invocation_id, "local-user", outcome="succeeded", evidence="isolated proof")
    assert linked.bridge.apply_receipt(lease, receipt)
    assert linked.bridge.snapshot(lease)["lateResultSaved"]
    assert sum(e["eventType"] == "invocation.capacity_released" for e in linked.case.events(task)) == 1


def test_node_catalog_reference_does_not_require_mutually_recursive_revision_hashes(linked):
    # 4A 的关联资料可以指向旧目录；执行必须匹配中心实际固定的 Node 配置修订。
    data(send(linked.node, linked.entry, sequence=2, state="stopped"))
    definition = {**config(), "modelId": "asr-fixture", "catalogServiceId": "asr-main", "catalogRevision": "a"*64}
    entry = bind(linked.node, register(linked.node, definition=definition, expected=1))
    data(send(linked.node, entry))
    target = {**linked.target, "node": {**linked.target["node"], "configRevision": entry["configRevision"]}}
    linked.case.service(targets=[target], version=linked.case.entries["asr-main"]["version"])
    _, lease = lease_for(linked)
    assert lease.service_revision != definition["catalogRevision"]
    assert linked.bridge.dispatch(lease) and len(linked.adapter.calls) == 1


@pytest.mark.parametrize("lose_reply", [False, True])
def test_redis_notification_uses_node_bridge_and_ack_only_after_center_commit(linked, lose_reply):
    settings = linked.case.settings.model_copy(update={"common_queue_backend": "redis", "redis_url": SecretStr("redis://127.0.0.1:6379/0")})
    fake = RedisStreamFake()
    queue = QueueService(linked.case.db, settings, client=fake, consumer="node-test")
    linked.case.core = queue.core; linked.bridge.core = queue.core
    task, invocation = linked.case.task()
    def before_ack(_):
        with linked.case.db.transaction() as session:
            assert session.get(InferenceInvocation, invocation).permit_state == "released"
            assert session.get(InferenceNodeDispatch, invocation).terminal_hash
    fake.before_ack = before_ack
    if lose_reply:
        factory = linked.bridge.client_factory
        class LostResponse:
            def __init__(self, binding): self.client = factory(binding)
            def availability(self): return self.client.availability()
            def execute(self, command):
                self.client.execute(command)
                raise TimeoutError()
            def result(self, identifier): return self.client.result(identifier)
        linked.bridge.client_factory = LostResponse
        with pytest.raises(AppError): queue.run_once_node("node-worker", bridge=linked.bridge)
        assert fake.pending
        # 从持久记录重建同一次租约，只取原 Node 结果。
        from backend.schema.invocations.invocation_schema import InvocationLease
        with linked.case.db.transaction() as session:
            row = session.get(InferenceInvocation, invocation)
            lease = InvocationLease(invocation_id=invocation, worker_id=row.worker_id, lease_token=row.lease_token,
                attempt=row.attempt, service_revision=row.service_revision, target_id=row.selected_target_id, deadline_at=row.deadline_at)
        assert linked.bridge.fetch_result(lease)
        assert queue.acknowledge_finished() == 1
    else:
        assert queue.run_once_node("node-worker", bridge=linked.bridge)
    assert not fake.pending and not fake.entries
    assert not queue.run_once_node("node-worker", bridge=linked.bridge)
    assert len(linked.adapter.calls) == 1
