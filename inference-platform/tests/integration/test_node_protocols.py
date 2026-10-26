"""4C2 实际适配器 + 隔离 HTTP 契约，非真实模型验收。"""

from datetime import timedelta
import json

import pytest
from sqlalchemy import select

from backend.modules.host_node.app import close_node_app
from backend.data.host_node.execution_models import HostNodeExecution
from backend.data.tasks.task_models import TaskRecord
from backend.service.invocations.node_collector import NodeResultCollector
from backend.service.invocations.node_runner import NodeExecutorRunner
from tests.integration.test_common_execution import case
from tests.integration.node_protocol_support import protocol_case
from tests.integration.host_node_support import auth,data
from tests.integration.host_node_support import send
from tests.integration.test_node_execution_bridge import EXECUTOR


@pytest.fixture
def create(case,tmp_path):
    nodes=[]
    def make(**kwargs):
        value=protocol_case(case,tmp_path/"node",**kwargs);nodes.append(value.node);return value
    yield make
    for node in nodes:close_node_app(node.app)


@pytest.mark.parametrize("kind,protocol",[("asr","remote-v2"),("asr","openai-audio"),("vision","vision-json-v1")])
def test_real_adapter_file_input_and_center_result(create,kind,protocol):
    value=create(kind=kind,protocol=protocol);task,lease=value.task()
    assert value.bridge.dispatch(lease)
    assert not value.bridge.dispatch(lease)
    with value.case.db.transaction() as session:
        owner=session.get(TaskRecord,task)
        assert owner.status=="succeeded"
        if kind=="vision":assert owner.result_json["modelId"]=="detector-fixture" and owner.result_json["detections"][0]["classId"]==100
    assert value.calls==[("POST","/infer")]
    snapshot=value.bridge.snapshot(lease)
    assert snapshot["monitoring"]["progress"] is None and snapshot["monitoring"]["actualDevice"] is None
    for _ in range(5):
        value.bridge.snapshot(lease)
        data(value.node.client.get(f"/node/internal/v1/executions/{lease.invocation_id}/monitoring",headers=auth(EXECUTOR)))
    assert len(value.calls)==1


def test_async_reports_progress_and_actual_device_before_result_commit(create):
    value=create(mode="async",query=True);task,lease=value.task()
    assert not value.bridge.dispatch(lease)
    assert value.case.core.get(lease.invocation_id)["permitState"]=="held"
    value.monitor.reconcile(lease.invocation_id);value.bridge.fetch_result(lease)
    snapshot=value.bridge.snapshot(lease)
    assert snapshot["assignedDevice"]["kind"]=="unknown"
    assert snapshot["monitoring"]["progress"]==35 and snapshot["monitoring"]["actualDevice"]["kind"]=="gpu"
    value.phase.update(state="succeeded",resultCode=404)
    value.monitor.reconcile(lease.invocation_id);value.bridge.fetch_result(lease)
    assert not value.bridge.snapshot(lease)["resultSaved"]
    assert value.bridge.snapshot(lease)["monitoring"]["remoteState"]=="succeeded"
    assert value.case.core.get(lease.invocation_id)["permitState"]=="held"
    value.phase["resultCode"]=200;value.monitor.reconcile(lease.invocation_id)
    assert value.bridge.fetch_result(lease)
    assert value.case.core.get(lease.invocation_id)["permitState"]=="released"


@pytest.mark.parametrize("failure",[404,429,"bad-progress","wrong-identity"])
def test_monitor_faults_keep_last_trusted_progress_and_capacity(create,failure):
    value=create(mode="async",query=True);_,lease=value.task();value.bridge.dispatch(lease)
    value.monitor.reconcile(lease.invocation_id)
    if isinstance(failure,int):value.phase["statusCode"]=failure
    elif failure=="bad-progress":value.phase["progress"]=101.0
    else:value.phase["wrongIdentity"]=True
    before=value.executor.get(lease.invocation_id)["monitoring"]
    after=value.monitor.reconcile(lease.invocation_id)
    assert after["state"]=="sending" and after["monitoring"]["progress"]==35
    assert after["monitoring"]["lastTrustedAt"]==before["lastTrustedAt"] and after["monitoring"]["lastError"]
    assert value.case.core.get(lease.invocation_id)["permitState"]=="held"
    if failure==429:
        with value.node.service.database.transaction() as session:
            m=session.get(HostNodeExecution,lease.invocation_id).monitoring_json
            from datetime import datetime
            assert (datetime.fromisoformat(m["nextPollAt"])-datetime.fromisoformat(m["lastErrorAt"])).total_seconds()>=30


@pytest.mark.parametrize("disconnect",[False,True])
def test_llm_sse_requires_finish_and_done_without_saving_chat(create,disconnect):
    value=create(kind="llm",protocol="chat-completions-sse",mode="stream")
    accepted,request=value.case.online(value.service)
    lease=value.case.core.claim_online(accepted["invocationId"],accepted["workerId"])
    value.phase["disconnect"]=disconnect
    frames=list(value.bridge.stream_online(lease,request))
    assert any(f["event"]=="delta" for f in frames)
    assert value.case.core.get(lease.invocation_id)["permitState"]==("uncertain" if disconnect else "released")
    assert frames[-1]["event"]==("error" if disconnect else "done")
    assert "SYNTHETIC_PRIVATE_DELTA" not in json.dumps(value.executor.get(lease.invocation_id))
    logs="".join(p.read_text(encoding="utf-8") for p in value.node.settings.log_dir.glob("*.log"))
    assert "NEVER_LOG_THIS_BODY" not in logs and "SYNTHETIC_PRIVATE_DELTA" not in logs


def test_cancel_ack_does_not_release_until_trusted_cancelled_status(create):
    value=create(mode="async",query=True);_,lease=value.task();value.bridge.dispatch(lease)
    receipt=data(value.node.client.post(f"/node/internal/v1/executions/{lease.invocation_id}/cancel",headers=auth(EXECUTOR),json={}))
    assert receipt["state"]=="sending" and receipt["monitoring"]["cancelRequested"]
    assert value.case.core.get(lease.invocation_id)["permitState"]=="held"
    value.phase["state"]="cancelled";value.monitor.reconcile(lease.invocation_id)
    assert value.bridge.fetch_result(lease) and value.case.core.get(lease.invocation_id)["outcome"]=="cancelled"


def test_late_query_after_deadline_requires_explicit_reconcile(create):
    value=create(mode="async",query=True);_,lease=value.task();value.bridge.dispatch(lease)
    value.node.clock.advance(200);value.monitor.tick()
    for future in value.monitor.futures:future.result(timeout=5)
    assert value.executor.get(lease.invocation_id)["monitoring"]["pollStopped"]=="deadline"
    assert not any(method=="GET" for method,_ in value.calls)
    value.case.expire(lease.invocation_id);value.case.core.recover()
    value.phase["state"]="succeeded";value.monitor.reconcile(lease.invocation_id)
    assert value.bridge.fetch_result(lease)
    assert value.bridge.snapshot(lease)["lateResultSaved"]


def test_tampered_file_is_durably_rejected_and_does_not_leak_capacity(create):
    value=create();_,lease=value.task()
    material=value.bridge.material_source.load(lease)
    material.content=b"x"*len(material.content)
    value.bridge.material_source=type("Source",(),{"load":lambda self,lease:material})()
    assert value.bridge.dispatch(lease)
    assert not value.calls and value.case.core.get(lease.invocation_id)["permitState"]=="released"
    assert value.executor.get(lease.invocation_id)["errorCode"]=="NODE_INPUT_INVALID_NOT_SENT"


def test_gate_change_after_preparation_returns_no_send_proof(create):
    value=create();_,lease=value.task();material=value.bridge.material_source.load(lease)
    value.bridge.prepare(lease,material.manifest)
    data(send(value.node,value.entry,sequence=2,state="warming"))
    assert value.bridge.dispatch(lease)
    assert value.executor.get(lease.invocation_id)["errorCode"]=="NODE_GATE_CLOSED_NOT_SENT"
    assert not value.calls and value.case.core.get(lease.invocation_id)["permitState"]=="released"


def test_node_restart_reconciles_existing_job_without_posting_again(create):
    from backend.modules.host_node.app import create_node_app
    from backend.service.host_node.http_adapter import NodeHttpAdapter
    value=create(mode="async",query=True);_,lease=value.task();value.bridge.dispatch(lease)
    adapter=value.executor.adapter
    close_node_app(value.node.app)
    value.node.app=create_node_app(value.node.settings,clock=value.node.clock,execution_adapter=adapter,monitor_budget_client=value.node.budget)
    value.node.client=value.node.app.test_client();value.node.service=value.node.app.extensions["host_node_service"]
    value.executor=value.node.app.extensions["host_node_executions"];value.monitor=value.node.app.extensions["host_node_monitor"]
    assert value.executor.get(lease.invocation_id)["state"]=="unknown"
    value.phase["state"]="succeeded";value.monitor.reconcile(lease.invocation_id)
    assert value.bridge.fetch_result(lease)
    assert sum(method=="POST" for method,_ in value.calls)==1


def test_central_monitor_get_is_readonly_and_explicit_reconcile_pulls_result(create):
    value=create(mode="async",query=True);_,lease=value.task();value.bridge.dispatch(lease)
    value.case.app.extensions["node_invocation_bridge"]=value.bridge
    client=value.case.app.test_client();url=f"/api/admin/common-execution/invocations/{lease.invocation_id}"
    before=len(value.calls)
    for _ in range(5):assert client.get(url+"/monitoring").status_code==200
    assert len(value.calls)==before
    assert client.get(url+"/monitoring",environ_overrides={"REMOTE_ADDR":"10.0.0.9"}).status_code==403
    value.phase["state"]="succeeded"
    assert client.post(url+"/reconcile",json={}).get_json()["data"]["resultSaved"]


def test_center_collector_saves_result_and_retains_node_error_without_failure(create):
    value=create(mode="async",query=True);_,lease=value.task();value.bridge.dispatch(lease)
    collector=NodeResultCollector(value.bridge)
    try:
        collector._fetch(lease)
        assert value.bridge.snapshot(lease)["monitoring"]["supported"]
        factory=value.bridge.client_factory
        class Broken:
            def __init__(self,binding):pass
            def result(self,identifier):
                from backend.common.exceptions.app_error import AppError
                raise AppError("NODE_RESPONSE_UNCONFIRMED","isolated",502)
        value.bridge.client_factory=Broken;collector._fetch(lease)
        assert value.bridge.snapshot(lease)["nodeQueryError"]=="NODE_RESPONSE_UNCONFIRMED"
        assert value.case.core.get(lease.invocation_id)["state"]=="sent"
        value.bridge.client_factory=factory;value.phase["state"]="succeeded"
        value.monitor.reconcile(lease.invocation_id);collector._fetch(lease)
        assert value.bridge.snapshot(lease)["resultSaved"]
    finally:collector.close()


def test_node_runner_waits_when_not_ready_and_reuses_fresh_node_health(create):
    from backend.data.catalog.catalog_models import InferenceTargetCheck
    value=create();task,unused=value.case.task(value.service)
    # 模拟新公共任务的有效媒体，复用同一资料准备器，不直接调用执行。
    from tests.integration.node_protocol_support import audio_bytes
    from backend.data.files.upload_store import LocalUploadStore
    from io import BytesIO
    stored=LocalUploadStore(value.case.settings.upload_dir).save(BytesIO(audio_bytes()),max_bytes=20*1024*1024)
    with value.case.db.transaction(immediate=True) as session:
        row=session.get(TaskRecord,task);row.input_ref=stored.input_ref;row.input_size_bytes=stored.size_bytes;row.input_sha256=stored.sha256
        row.audio_duration_seconds=0.1;row.parameters={"language":None}
        for check in session.scalars(select(InferenceTargetCheck)):check.checked_at-=timedelta(minutes=6)
    queue=value.case.app.extensions["queue_service"]
    runner=NodeExecutorRunner(queue,value.bridge,workers=1)
    try:
        data(send(value.node,value.entry,sequence=2,state="warming"))
        runner.tick()
        with value.case.db.transaction() as session:assert session.get(TaskRecord,task).status=="queued"
        assert not value.calls
        data(send(value.node,value.entry,sequence=3))
        runner.last_probe=0;runner.tick()
        for future in runner.futures:future.result(timeout=5)
        with value.case.db.transaction() as session:assert session.get(TaskRecord,task).status=="succeeded"
    finally:runner.close()


def test_inline_final_result_does_not_need_a_separate_result_endpoint(create):
    value=create(mode="async",query=True,inline_result=True);_,lease=value.task()
    assert not value.bridge.dispatch(lease)  # 已受理仍未终态，不能提前确认通知。
    value.phase.update(state="succeeded",result=True)
    value.monitor.reconcile(lease.invocation_id)
    assert value.bridge.fetch_result(lease)
    assert value.case.core.get(lease.invocation_id)["permitState"]=="released"
    assert not any("/result/" in path for _,path in value.calls)
