"""跨中心连接/Node HTTP 客户端争用，持久恢复与鉴权。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
from alembic import command as alembic_command
import httpx2 as httpx
from pydantic import SecretStr, ValidationError
import pytest
from sqlalchemy import func, select, text
from backend.common.exceptions.app_error import AppError, StartupError
from backend.config.settings.app_settings import AppSettings
from backend.data.database.database import Database
from backend.data.monitoring.budget_models import MonitoringPermit
from backend.modules.api.app import create_app
from backend.schema.monitoring.budget_schema import MonitorPermitCommand
from backend.service.host_node.monitor_budget_client import MonitorBudgetClient
from backend.service.monitoring.budget_service import MonitorBudgetService
from tests.integration.test_common_execution import case
from tests.integration.monitor_budget_support import build_calls
from tests.integration.host_node_support import node_settings

TOKEN="TEST_ONLY_MONITOR_"+"q"*40


def held(case):
    with case.db.transaction() as session:
        return session.scalar(select(func.count()).select_from(MonitoringPermit).where(MonitoringPermit.state=="held"))


def test_two_coordinators_share_four_atomic_slots_and_preserve_inference_capacity(case):
    requests=build_calls(case);before=case.core.capacity();gate=Barrier(len(requests))
    other=Database(case.settings)
    coordinators=[MonitorBudgetService(case.db),MonitorBudgetService(other)]
    def acquire(index):
        gate.wait(timeout=10)
        return coordinators[index%2].change(requests[index])
    try:
        with ThreadPoolExecutor(max_workers=len(requests)) as pool:replies=list(pool.map(acquire,range(len(requests))))
        assert [r.state for r in replies].count("held")==4 and held(case)==4
        assert all(r.reason=="GLOBAL_QUERY_LIMIT" for r in replies if r.state=="denied")
        remaining=4
        for request,reply in zip(requests,replies):
            if reply.state=="held":
                assert coordinators[1].change(request).state=="held" and held(case)==remaining
                assert coordinators[0].change(request,release=True).state=="released"
                assert coordinators[1].change(request,release=True).state=="released"
                remaining-=1
        assert held(case)==0 and case.core.capacity()==before
    finally:other.dispose()


@pytest.mark.parametrize("explicit",[True,False])
def test_same_physical_service_across_nodes_has_one_slot(case,explicit):
    requests=build_calls(case,count=2,group="physical-asr" if explicit else None,
                         origin=None if explicit else "http://shared-service.invalid:8003")
    budget=MonitorBudgetService(case.db)
    assert budget.change(requests[0]).state=="held"
    assert budget.change(requests[1]).reason=="SERVICE_QUERY_LIMIT"
    budget.change(requests[0],release=True)
    assert budget.change(requests[1]).state=="held"


def test_loopback_on_different_hosts_is_not_same_physical_service(case):
    requests=build_calls(case,count=2,origin="http://127.0.0.1:8003")
    budget=MonitorBudgetService(case.db)
    assert all(budget.change(r).state=="held" for r in requests)


def test_release_before_delayed_acquire_leaves_tombstone_and_old_poll_cannot_reopen(case):
    request=build_calls(case,count=1)[0];budget=MonitorBudgetService(case.db)
    assert budget.change(request,release=True).state=="released"
    assert budget.change(request).state=="released" and held(case)==0
    fresh=MonitorPermitCommand(poll_id=uuid4().hex,command=request.command)
    assert budget.change(fresh).state=="held"
    budget.change(request,release=True)
    assert held(case)==1
    with pytest.raises(AppError,match="查询身份"):
        budget.change(request.model_copy(update={"command":request.command.model_copy(update={"attempt":2})}))
    assert held(case)==1


def test_coordinator_restart_and_age_do_not_discard_held_queries(case):
    request=build_calls(case,count=1)[0]
    MonitorBudgetService(case.db).change(request)
    with case.db.transaction(immediate=True) as session:
        session.execute(text("UPDATE monitoring_permits SET created_at='2000-01-01 00:00:00'"))
    database=Database(case.settings)
    try:
        restarted=MonitorBudgetService(database)
        assert restarted.change(request).state=="held"
        with pytest.raises(RuntimeError,match="MONITOR_BUDGET_HELD"):
            alembic_command.downgrade(case.migration,"20261021_0020")
        assert held(case)==1
        restarted.change(request,release=True)
        alembic_command.downgrade(case.migration,"20261021_0020")
        alembic_command.upgrade(case.migration,"head")
        assert held(case)==0
    finally:database.dispose()


def test_two_node_http_clients_authentication_and_no_network_inside_sql_transaction(case,tmp_path):
    requests=build_calls(case,count=2,group="shared-asr")
    case.app.config["APP_SETTINGS"]=case.settings.model_copy(update={"monitor_token":SecretStr(TOKEN)})
    async def route(request):
        response=case.app.test_client().post(request.url.path,headers={"Authorization":request.headers["Authorization"],
            "Content-Type":"application/json"},data=request.content)
        return httpx.Response(response.status_code,headers={"content-type":"application/json"},content=response.data)
    clients=[MonitorBudgetClient(node_settings(tmp_path/str(i),monitor_central_url="http://center.invalid",monitor_token=TOKEN),
                transport=httpx.MockTransport(route)) for i in range(2)]
    assert clients[0].change(requests[0]).state=="held"
    assert clients[1].change(requests[1]).reason=="SERVICE_QUERY_LIMIT"
    assert clients[0].change(requests[0],release=True).state=="released"
    assert clients[1].change(requests[1]).state=="held"
    endpoint="/internal/v1/monitor-budget/acquire";client=case.app.test_client()
    for token in ("", "Bearer APP_KEY", "Bearer "+"e"*40):
        assert client.post(endpoint,headers={"Authorization":token},json=requests[0].model_dump(mode="json",by_alias=True)).status_code==401
    assert client.get(endpoint,headers={"Authorization":"Bearer "+TOKEN}).status_code==405
    assert client.post(endpoint+"?anything=1",headers={"Authorization":"Bearer "+TOKEN},json={}).status_code==422
    assert client.post(endpoint,headers={"Authorization":"Bearer "+TOKEN},json={"pollId":"bad"}).status_code==422
    assert TOKEN not in str(clients[0].settings) + clients[0].settings.model_dump_json()


def test_budget_disabled_and_missing_migration_fail_closed_without_implicit_upgrade(case):
    request=build_calls(case,count=1)[0];client=case.app.test_client()
    assert client.post("/internal/v1/monitor-budget/acquire",json=request.model_dump(mode="json",by_alias=True)).status_code==503
    alembic_command.downgrade(case.migration,"20261021_0020")
    with pytest.raises(StartupError,match="0021"):
        create_app(case.settings.model_copy(update={"monitor_token":SecretStr(TOKEN)}))
    with case.db.transaction() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version"))=="20261021_0020"


@pytest.mark.parametrize("override",[{"monitor_token":"short"},{"monitor_token":TOKEN,"ingress_token":TOKEN}])
def test_monitor_credentials_are_validated_and_separate(override):
    with pytest.raises(ValidationError):AppSettings(common_execution_enabled=True,**override)


def test_different_explicit_groups_do_not_bypass_same_endpoint_guard(case):
    requests=build_calls(case,count=2,group=lambda i:"group-"+str(i),origin="http://shared-service.invalid:8003")
    budget=MonitorBudgetService(case.db)
    assert budget.change(requests[0]).state=="held"
    assert budget.change(requests[1]).reason=="SERVICE_QUERY_LIMIT"


def test_internal_contract_is_saved_and_readonly_on_authenticated_get(case):
    import json
    from backend.config.settings.app_settings import PROJECT_ROOT
    from backend.service.monitoring.openapi_service import monitor_budget_openapi
    case.app.config["APP_SETTINGS"]=case.settings.model_copy(update={"monitor_token":SecretStr(TOKEN)})
    client=case.app.test_client()
    response=client.get("/internal/v1/monitor-budget/openapi.json",headers={"Authorization":"Bearer "+TOKEN})
    assert response.status_code==200 and response.headers["Cache-Control"]=="no-store"
    assert response.get_json()==monitor_budget_openapi()==json.loads((PROJECT_ROOT/"docs/monitor-budget.openapi.json").read_text(encoding="utf-8"))
    assert held(case)==0
