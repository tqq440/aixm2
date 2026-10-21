"""协议传输限制与动态凭据隔离，不访问任何外部服务。"""

import asyncio
from datetime import datetime,timezone,timedelta
from email.utils import format_datetime

import httpx2 as httpx
import pytest
from pydantic import ValidationError

from backend.common.http.bounded_client import ProtocolFailure,request_bytes
from backend.config.host_node.node_settings import load_node_settings
from backend.schema.host_node.upstream_schema import NodeUpstreamConfig,TaskQueryConfig
from tests.integration.host_node_support import ADMIN,REPORT


@pytest.mark.parametrize("response,code",[(httpx.Response(404,text="PRIVATE"),"UPSTREAM_HTTP_404"),
    (httpx.Response(302,headers={"Location":"http://wrong.invalid"}),"UPSTREAM_HTTP_302"),
    (httpx.Response(200,content=b"x"*101,headers={"Content-Type":"application/json"}),"UPSTREAM_RESPONSE_TOO_LARGE"),
    (httpx.Response(200,text="{}"),"UPSTREAM_CONTENT_TYPE_INVALID"),
    (httpx.Response(200,headers={"content-encoding":"br"},content=b""),"UPSTREAM_ENCODING_INVALID")])
def test_http_limits_and_errors_never_expose_body(response,code):
    with pytest.raises(ProtocolFailure) as error:
        request_bytes("GET","http://127.0.0.1/status",maximum=100,transport=httpx.MockTransport(lambda _:response))
    assert error.value.code==code and "PRIVATE" not in str(error.value)


def test_retry_after_http_date_is_respected():
    when=format_datetime(datetime.now(timezone.utc)+timedelta(seconds=120),usegmt=True)
    with pytest.raises(ProtocolFailure) as error:
        request_bytes("GET","http://127.0.0.1/status",transport=httpx.MockTransport(lambda _:httpx.Response(429,headers={"Retry-After":when})))
    assert error.value.retry_after>=118


def test_absolute_timeout_covers_body_read():
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(.3);yield b"{}"
    with pytest.raises(ProtocolFailure):
        request_bytes("GET","http://127.0.0.1/status",timeout=.05,transport=httpx.MockTransport(
            lambda _:httpx.Response(200,headers={"Content-Type":"application/json"},stream=Slow())))


@pytest.mark.parametrize("path",["http://other/path","/x/{requestId}/{requestId}","/x/{other}","/../{requestId}"])
def test_monitor_paths_cannot_change_origin_or_escape(path):
    with pytest.raises(ValueError):TaskQueryConfig(protocol="job-json-v1",status_path=path)


def test_business_tokens_are_not_public_or_control_credentials():
    settings=load_node_settings(environ={"NODE_ID":"n","NODE_ADMIN_TOKEN":ADMIN,"NODE_UPSTREAM_TOKEN_ASR":REPORT})
    assert REPORT not in settings.model_dump_json()+repr(settings)
    with pytest.raises(Exception):load_node_settings(environ={"NODE_ID":"n","NODE_ADMIN_TOKEN":ADMIN,"NODE_UPSTREAM_TOKEN_ASR":ADMIN})
