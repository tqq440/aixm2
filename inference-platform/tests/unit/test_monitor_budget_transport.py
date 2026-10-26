"""查询许可响应不可信时关闭门禁，绝不回退本地名额。"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
import httpx2 as httpx
import pytest
from backend.common.exceptions.app_error import AppError
from backend.schema.monitoring.budget_schema import MonitorPermitCommand
from backend.service.host_node.monitor_budget_client import MonitorBudgetClient
from tests.integration.host_node_support import node_settings


@pytest.mark.parametrize("failure",[404,429,500,302,"oversize","invalid-json","wrong-id","invalid-state","release-held"])
def test_invalid_grants_do_not_authorize_or_retry(tmp_path,failure):
    command=MonitorPermitCommand(poll_id=uuid4().hex,command={"nodeId":"node","serviceId":"asr","configRevision":"a"*64,
        "instanceId":str(uuid4()),"invocationId":uuid4().hex,"attempt":1,"serviceRevision":"b"*64,"targetId":"target",
        "kind":"asr","modelId":"model","protocol":"remote-v2","inputRef":uuid4().hex,"inputSha256":"c"*64,
        "deadlineAt":datetime.now(timezone.utc)+timedelta(seconds=60)})
    calls=[]
    async def response(request):
        calls.append(request)
        if isinstance(failure,int):return httpx.Response(failure,headers={"content-type":"application/json"},json={})
        payload={"data":{"pollId":"d"*32 if failure=="wrong-id" else command.poll_id,
            "state":"free" if failure=="invalid-state" else "held","reason":None},"requestId":"fixture"}
        if failure in {"oversize","invalid-json"}:
            return httpx.Response(200,headers={"content-type":"application/json"},content=b"x"*(8193 if failure=="oversize" else 1))
        return httpx.Response(200,json=payload)
    settings=node_settings(tmp_path,monitor_central_url="http://center.invalid",monitor_token="TEST_ONLY_MONITOR_"+"x"*40)
    client=MonitorBudgetClient(settings,transport=httpx.MockTransport(response))
    with pytest.raises(AppError) as error:client.change(command,release=failure=="release-held")
    assert error.value.code=="MONITOR_BUDGET_UNAVAILABLE" and len(calls)==1
