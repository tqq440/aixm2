"""统一采集并发、过时响应、事件顺序和输入/租约安全。"""

from copy import deepcopy
from threading import Event,Lock
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from backend.common.exceptions.app_error import AppError
from backend.data.host_node.execution_models import HostNodeExecution
from backend.schema.host_node.execution_schema import ExecutionCommand,AdapterOutcome
from backend.schema.host_node.job_schema import JobStatus
from backend.schema.asr.asr_schema import TranscriptionResult
from tests.integration.test_common_execution import case
from tests.integration.test_node_protocols import create
from tests.integration.host_node_support import register,bind,send,data,auth
from tests.integration.test_node_execution_bridge import EXECUTOR


def test_queries_are_global_four_and_one_per_actual_service_including_aliases(create):
    reports={f"NODE_REPORT_TOKEN_EXTRA_{i}":"TEST_ONLY_EXTRA_"+str(i)+"x"*40 for i in range(5)}
    value=create(mode="async",query=True,extra_reports=reports);_,lease=value.task();value.bridge.dispatch(lease)
    with value.node.service.database.transaction() as session:
        original=session.get(HostNodeExecution,lease.invocation_id).to_dict()
        definition=deepcopy(value.node.service._service(session,"asr").config_json)
    for i in range(5):
        cfg=deepcopy(definition);cfg["reportTokenEnv"]=f"NODE_REPORT_TOKEN_EXTRA_{i}"
        cfg["internalBaseUrl"]=definition["internalBaseUrl"] if i==4 else f"http://127.0.0.1:{19000+i}"
        entry=bind(value.node,register(value.node,service_id=f"extra-{i}",definition=cfg))
        data(send(value.node,entry,token=reports[cfg["reportTokenEnv"]]))
        for n in range(2):
            command={**original["command_json"],"serviceId":entry["serviceId"],"configRevision":entry["configRevision"],
                "instanceId":entry["instanceId"],"invocationId":uuid4().hex}
            with value.node.service.database.transaction(immediate=True) as session:
                now=value.node.service._now(session)
                session.add(HostNodeExecution(invocation_id=command["invocationId"],service_id=entry["serviceId"],instance_id=entry["instanceId"],
                    request_hash="a"*64,command_json=command,node_epoch_id=value.node.service.epoch_id,state="sending",
                    monitoring_json={**original["monitoring_json"],"jobId":"job-a"},created_at=now,updated_at=now))
    ready,finish,guard=Event(),Event(),Lock();active={};counts={"total":0,"maximum":0};started=[]
    def query(command,config,job_id,**kwargs):
        key=config["internalBaseUrl"]
        with guard:
            active[key]=active.get(key,0)+1;counts["total"]+=1;counts["maximum"]=max(counts["maximum"],counts["total"])
            assert active[key]==1 and counts["total"]<=4
            started.append(key)
            if counts["total"]==4:ready.set()
        try:
            assert finish.wait(8)
            return JobStatus(invocation_id=command.invocation_id,attempt=command.attempt,service_revision=command.service_revision,
                config_revision=command.config_revision,job_id=job_id,state="running",progress=20.0)
        finally:
            with guard:active[key]-=1;counts["total"]-=1
    value.executor.adapter.query=query
    value.monitor.tick()
    try:
        assert ready.wait(5)
        value.monitor.tick()
        with pytest.raises(AppError):value.monitor.reconcile(lease.invocation_id)
        for _ in range(4):data(value.node.client.get(f"/node/internal/v1/executions/{lease.invocation_id}/monitoring",headers=auth(EXECUTOR)))
        assert len(started)==4 and counts["maximum"]==4
    finally:
        finish.set()
        for future in value.monitor.futures:future.result(timeout=5)


def test_query_arriving_after_result_cannot_overwrite_terminal_or_actual_snapshot(create):
    value=create(mode="async",query=True);_,lease=value.task();value.bridge.dispatch(lease)
    started,finish=Event(),Event()
    def delayed(command,config,job_id,**kwargs):
        started.set();assert finish.wait(5)
        return JobStatus(invocation_id=command.invocation_id,attempt=command.attempt,service_revision=command.service_revision,
            config_revision=command.config_revision,job_id=job_id,state="running",progress=80.0)
    value.executor.adapter.query=delayed
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(value.monitor.reconcile,lease.invocation_id);assert started.wait(3)
        try:
            value.executor.commit(lease.invocation_id,AdapterOutcome(state="succeeded",result=TranscriptionResult.model_validate(value.result)))
        finally:finish.set()
        assert future.result(timeout=5)["state"]=="succeeded"
    assert value.executor.get(lease.invocation_id)["monitoring"]["progress"] is None
    assert value.bridge.fetch_result(lease)


def test_result_event_precedes_platform_completion_and_single_release(create):
    value=create();task,lease=value.task();assert value.bridge.dispatch(lease)
    types=[event["eventType"] for event in value.case.events(task)]
    assert types.index("invocation.result_saved")<types.index("invocation.finished")<types.index("invocation.capacity_released")
    assert types.count("invocation.capacity_released")==1


def test_stream_disconnect_does_not_count_as_terminal_success(create):
    value=create(kind="llm",protocol="chat-completions-sse",mode="stream")
    accepted,request=value.case.online(value.service)
    lease=value.case.core.claim_online(accepted["invocationId"],accepted["workerId"])
    stream=value.bridge.stream_online(lease,request)
    assert next(stream)["event"]=="delta"
    stream.close()
    assert value.case.core.get(lease.invocation_id)["permitState"]=="uncertain"
    assert sum(method=="POST" for method,_ in value.calls)==1
