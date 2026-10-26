"""Node 采集真正接入中心账本；断连、重启不绕过许可或影响推理额度。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from sqlalchemy import func, select
from backend.common.exceptions.app_error import AppError
from backend.data.host_node.execution_models import HostNodeExecution
from backend.data.host_node.node_models import HostNodeEvent
from backend.data.monitoring.budget_models import MonitoringPermit
from backend.modules.host_node.app import close_node_app, create_node_app
from backend.service.host_node.monitor_budget_client import MonitorBudgetClient
from tests.integration.test_common_execution import case
from tests.integration.test_node_protocols import create
from tests.integration.node_protocol_support import protocol_case
from tests.integration.host_node_support import ADMIN, OBSERVER, auth
from tests.integration.test_node_execution_bridge import EXECUTOR


def held(value):
    with value.case.db.transaction() as session:
        return session.scalar(select(func.count()).select_from(MonitoringPermit).where(MonitoringPermit.state=="held"))


def test_missing_center_does_not_query_or_mark_inference_failed(create):
    value=create(mode="async",query=True);_,lease=value.task();value.bridge.dispatch(lease)
    value.monitor.budget.client=MonitorBudgetClient(value.node.settings)
    before=value.case.core.capacity();snapshot=value.monitor.reconcile(lease.invocation_id)
    assert snapshot["state"]=="sending" and snapshot["monitoring"]["queryBudgetReason"]=="MONITOR_BUDGET_UNAVAILABLE"
    assert snapshot["monitoring"]["lastQueryAt"] is None and snapshot["monitoring"]["lastError"] is None
    assert value.calls==[("POST","/infer")] and value.case.core.capacity()==before


def test_lost_acquire_response_is_closed_before_new_id_and_never_queries_twice(create):
    value=create(mode="async",query=True);_,lease=value.task();value.bridge.dispatch(lease)
    actual=value.node.budget;old=[]
    class LostAcquire:
        def change(self,request,*,release=False):
            reply=actual.change(request,release=release)
            if not release:
                old.append(request)
                raise AppError("TEST_DROP","isolated dropped response",503)
            return reply
    value.monitor.budget.client=LostAcquire()
    value.monitor.reconcile(lease.invocation_id)
    assert held(value)==1 and value.calls==[("POST","/infer")]
    value.monitor.budget.client=actual
    value.monitor.reconcile(lease.invocation_id)
    assert held(value)==0 and value.executor.get(lease.invocation_id)["monitoring"]["progress"]==35
    assert actual.change(old[0]).state=="released"
    assert value.calls.count(("GET","/status/job-a"))==0  # default idSource is invocation
    assert sum(m=="GET" for m,_ in value.calls)==1


def test_release_failure_preserves_trusted_terminal_and_retries_even_for_finished_call(create):
    value=create(mode="async",query=True);_,lease=value.task();value.bridge.dispatch(lease)
    actual=value.node.budget
    class LostRelease:
        def change(self,request,*,release=False):
            if release:raise AppError("TEST_DROP","isolated",503)
            return actual.change(request)
    value.monitor.budget.client=LostRelease();value.phase.update(state="succeeded",result=True)
    view=value.monitor.reconcile(lease.invocation_id)
    assert view["state"]=="succeeded" and view["monitoring"]["queryBudgetState"]=="release_pending"
    assert held(value)==1
    assert value.bridge.fetch_result(lease) and value.case.core.get(lease.invocation_id)["permitState"]=="released"
    before=list(value.calls);value.monitor.budget.client=actual;value.node.clock.advance(6)
    value.monitor.tick()
    for future in value.monitor.futures:future.result(timeout=5)
    assert held(value)==0 and value.calls==before
    assert value.executor.get(lease.invocation_id)["state"]=="succeeded"


def test_crash_during_query_quarantines_permit_until_admin_confirms_no_query_running(create):
    value=create(mode="async",query=True);_,lease=value.task();value.bridge.dispatch(lease)
    assert value.monitor.budget.acquire(lease.invocation_id) is not None and held(value)==1
    adapter=value.executor.adapter
    close_node_app(value.node.app)
    value.node.app=create_node_app(value.node.settings,clock=value.node.clock,execution_adapter=adapter,
                                  monitor_budget_client=value.node.budget)
    value.node.client=value.node.app.test_client();value.node.service=value.node.app.extensions["host_node_service"]
    value.executor=value.node.app.extensions["host_node_executions"];value.monitor=value.node.app.extensions["host_node_monitor"]
    assert value.executor.get(lease.invocation_id)["monitoring"]["queryBudgetState"]=="uncertain"
    value.monitor.reconcile(lease.invocation_id);value.monitor.tick()
    for future in value.monitor.futures:future.result(timeout=5)
    assert held(value)==1 and value.calls==[("POST","/infer")]
    url=f"/node/v1/admin/executions/{lease.invocation_id}/monitor-budget/release"
    for credential in (OBSERVER,EXECUTOR):
        assert value.node.client.post(url,headers=auth(credential),json={"queriesStoppedConfirmed":True}).status_code==401
    assert value.node.client.post(url,headers=auth(),json={"queriesStoppedConfirmed":False}).status_code==422
    assert value.node.client.post(url,headers=auth(),json={"queriesStoppedConfirmed":1}).status_code==422
    assert value.node.client.get(url,headers=auth()).status_code==405
    assert value.node.client.post(url,headers=auth(),json={"queriesStoppedConfirmed":True}).status_code==200
    assert held(value)==0 and value.case.core.get(lease.invocation_id)["permitState"]=="held"
    value.monitor.reconcile(lease.invocation_id)
    assert value.executor.get(lease.invocation_id)["monitoring"]["progress"]==35
    assert sum(m=="POST" for m,_ in value.calls)==1


def test_two_independent_node_collectors_cannot_query_shared_service_together(case,tmp_path):
    first=protocol_case(case,tmp_path/"first",mode="async",query=True,node_id="first",service_suffix="-first",
                        application_limit=8,monitor_group="physical-asr")
    second=None;finish=Event();started=Event()
    try:
        second=protocol_case(case,tmp_path/"second",mode="async",query=True,node_id="second",service_suffix="-second",
                             application_version=1,application_limit=8,monitor_group="physical-asr",base_url="http://127.0.0.1:18004")
        _,left=first.task();first.bridge.dispatch(left)
        _,right=second.task();second.bridge.dispatch(right)
        query=first.executor.adapter.query
        def block(*args,**kwargs):
            # Both databases remain writable while HTTP query is in progress.
            with first.node.service.database.transaction(immediate=True):pass
            with case.db.transaction(immediate=True):pass
            started.set();assert finish.wait(8)
            return query(*args,**kwargs)
        first.executor.adapter.query=block
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(first.monitor.reconcile,left.invocation_id)
            try:
                assert started.wait(5)
                for _ in range(2):
                    view=second.monitor.reconcile(right.invocation_id)
                    assert view["monitoring"]["queryBudgetReason"]=="SERVICE_QUERY_LIMIT"
                assert sum(m=="GET" for m,_ in second.calls)==0
                with second.node.service.database.transaction() as session:
                    events=session.scalars(select(HostNodeEvent).where(HostNodeEvent.event_type=="MONITOR_BUDGET_CHANGED")).all()
                    assert len(events)==1
                # No operator assertion can release a locally active query.
                url=f"/node/v1/admin/executions/{left.invocation_id}/monitor-budget/release"
                assert first.node.client.post(url,headers=auth(),json={"queriesStoppedConfirmed":True}).status_code==409
            finally:finish.set()
            future.result(timeout=8)
        second.monitor.reconcile(right.invocation_id)
        assert sum(m=="GET" for m,_ in second.calls)==1
        assert held(first)==0
    finally:
        finish.set()
        if second:close_node_app(second.node.app)
        close_node_app(first.node.app)
