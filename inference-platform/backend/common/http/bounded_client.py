"""有界 HTTP 字节传输，无重试/跳转/代理环境；异常不带响应正文。"""

import asyncio
from dataclasses import dataclass
import ssl
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime

import httpx2 as httpx


class ProtocolFailure(Exception):
    def __init__(self, code="UPSTREAM_QUERY_FAILED", *, retry_after=None):
        super().__init__(code)
        self.code, self.retry_after = code, retry_after


@dataclass(frozen=True)
class HttpReply:
    status: int
    body: bytes


def request_bytes(method, url, *, token=None, ca_file=None, timeout=3, connect_timeout=3,
                  maximum=2*1024*1024, transport=None, **kwargs):
    async def run():
        context = ssl.create_default_context(cafile=ca_file)
        async with httpx.AsyncClient(verify=context, timeout=httpx.Timeout(timeout, connect=min(timeout, connect_timeout)),
                trust_env=False, follow_redirects=False, transport=transport) as client:
            headers = {"Accept-Encoding":"identity", "Accept":"application/json"}
            if token: headers["Authorization"] = "Bearer " + token
            async with client.stream(method, url, headers=headers, **kwargs) as response:
                if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                    raise ProtocolFailure("UPSTREAM_ENCODING_INVALID")
                if response.status_code == 429:
                    raw = response.headers.get("retry-after", "")
                    retry = int(raw) if raw.isdigit() and len(raw) <= 6 else None
                    if retry is None:
                        try:retry=max(0,int((parsedate_to_datetime(raw)-datetime.now(timezone.utc)).total_seconds()))
                        except (ValueError,TypeError,OverflowError):pass
                    raise ProtocolFailure("UPSTREAM_RATE_LIMITED", retry_after=min(retry,86400) if retry is not None else None)
                if response.status_code < 200 or response.status_code >= 300:
                    raise ProtocolFailure("UPSTREAM_HTTP_" + str(response.status_code))
                mime=response.headers.get("content-type","").split(";")[0].strip().lower()
                if mime!="application/json" and not mime.endswith("+json"):raise ProtocolFailure("UPSTREAM_CONTENT_TYPE_INVALID")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data)+len(chunk)>maximum: raise ProtocolFailure("UPSTREAM_RESPONSE_TOO_LARGE")
                    data.extend(chunk)
                return HttpReply(response.status_code, bytes(data))
    async def bounded(): return await asyncio.wait_for(run(), timeout=timeout)
    try: return asyncio.run(bounded())
    except ProtocolFailure: raise
    except Exception: raise ProtocolFailure("UPSTREAM_TRANSPORT_FAILED") from None
