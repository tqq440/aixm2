"""有界只读健康 GET；不发送业务请求，不跟随重定向，不推断设备。"""

import asyncio
import json
from time import perf_counter

import httpx2 as httpx

from backend.common.exceptions.app_error import ConfigurationError
from backend.config.catalog.connection_config import connection_security
from backend.schema.asr.remote_v2_schema import RemoteHealthResponse


async def _probe(config, target, transport):
    verify, credentials = connection_security(target)
    deadline = perf_counter() + 5
    async with httpx.AsyncClient(verify=verify, transport=transport, trust_env=False, follow_redirects=False,
                                 timeout=httpx.Timeout(5, connect=min(5, target.connect_timeout_seconds))) as client:
        async with client.stream("GET", target.base_url + target.health_path,
                                 headers={"Accept": "application/json", "Accept-Encoding": "identity", **credentials}) as response:
            if response.status_code != 200:
                return False, "HEALTH_HTTP_ERROR"
            if (response.headers.get("content-encoding", "identity") != "identity"
                    or response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"):
                return False, "HEALTH_PROTOCOL_INVALID"
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 65_536 or perf_counter() > deadline:
                    return False, "HEALTH_RESPONSE_LIMIT"
    payload = json.loads(body)
    if config.protocol == "remote-v2":
        health = RemoteHealthResponse.model_validate(payload)
        ready = health.data.status == "ok" and health.data.model_loaded
    elif config.protocol in {"openai-audio", "chat-completions-sse"}:
        ready = isinstance(payload, dict) and isinstance(payload.get("data"), list) and any(
            isinstance(item, dict) and item.get("id") == target.upstream_model for item in payload["data"])
    else:
        ready = (isinstance(payload, dict) and payload.get("status") == "ok"
                 and payload.get("modelLoaded") is True and payload.get("model") == target.upstream_model)
    return ready, None if ready else "HEALTH_MODEL_NOT_READY"


def probe_target(config, target, *, transport=None):
    started = perf_counter()
    try:
        success, error = asyncio.run(asyncio.wait_for(_probe(config, target, transport), timeout=5))
    except ConfigurationError:
        success, error = False, "HEALTH_CREDENTIALS_INVALID"
    except (httpx.TimeoutException, TimeoutError):
        success, error = False, "HEALTH_TIMEOUT"
    except httpx.HTTPError:
        success, error = False, "HEALTH_CONNECTION_FAILED"
    except (ValueError, TypeError, KeyError):
        success, error = False, "HEALTH_PROTOCOL_INVALID"
    except OSError:
        success, error = False, "HEALTH_TLS_CONFIGURATION_INVALID"
    return {"success": success, "errorCode": error, "durationMs": round((perf_counter() - started) * 1000),
            "phase": "health", "inferenceVerified": False, "actualDevice": None,
            "message": "连接检查通过；未验证推理、实际执行设备或共享容量。" if success else "连接检查失败，请核对服务、协议、凭据或证书。"}
