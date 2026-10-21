"""单 endpoint 文本流适配；无自动重试、正文落盘或假模型回退。"""

from collections.abc import Iterator
import json
import math
from threading import Event, Lock, Thread
from time import monotonic
from urllib.parse import urlsplit

import httpx2 as httpx
from pydantic import ValidationError

from backend.common.exceptions.app_error import AppError, ConfigurationError
from backend.common.llm.engine_protocol import EngineChunk
from backend.schema.llm.engine_schema import EngineInput, UpstreamChunk
from backend.utils.stream.sse_utils import SSELimitError, SSEParseError, decode_sse_json_object, iter_sse_events

MAX_REQUEST_BYTES = 131_072
MAX_OUTPUT_CHARACTERS = 131_072


def _failure(code: str, message: str, status: int = 502) -> AppError:
    return AppError(code, message, status)


class HttpChatAdapter:
    """endpoint 是完整 Chat Completions URL；不会补路径或查询模型目录。"""

    def __init__(self, endpoint: str, model: str, api_key: str | None,
                 connect_timeout: float, read_timeout: float, *, transport=None, verify=True, correlation_headers=None) -> None:
        try:
            parts = urlsplit(endpoint)
            valid = (parts.scheme in ("http", "https") and parts.hostname and parts.port != 0
                     and parts.username is None and parts.password is None and not parts.query and not parts.fragment
                     and not any(character.isspace() for character in endpoint))
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid or not isinstance(model, str) or not model.strip() or len(model) > 512:
            raise ConfigurationError("LLM endpoint 或模型配置无效。")
        if api_key is not None and (not isinstance(api_key, str) or "\r" in api_key or "\n" in api_key):
            raise ConfigurationError("LLM 密钥配置无效。")
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or not 0 < value <= 300 for value in (connect_timeout, read_timeout)):
            raise ConfigurationError("LLM 网络超时须为 0 至 300 秒之间的有限正数。")
        self._endpoint = endpoint
        self._model = model
        self._api_key = api_key
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._transport, self._verify = transport, verify
        self._correlation_headers = dict(correlation_headers or {})
        allowed={"X-Request-ID","X-Inference-Attempt","X-Inference-Service-Revision","X-Inference-Config-Revision"}
        if any(k not in allowed or not isinstance(v,str) or len(v)>64 or "\r" in v or "\n" in v for k,v in self._correlation_headers.items()):
            raise ConfigurationError("LLM 关联头无效。")

    def open(self, messages: list[dict], parameters: dict, deadline: float) -> "HttpChatStream":
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            raise _failure("LLM_INVALID_REQUEST", "在线请求截止时间无效。", 422)
        try:
            request = EngineInput.model_validate({"messages": messages, "parameters": parameters})
            body = json.dumps({
                "model": self._model, "messages": [message.model_dump() for message in request.messages],
                **request.parameters.model_dump(), "stream": True, "n": 1,
            }, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValidationError, UnicodeError, ValueError, TypeError):
            raise _failure("LLM_INVALID_REQUEST", "仅支持已校验的文本消息和生成参数。", 422) from None
        if len(body) > MAX_REQUEST_BYTES:
            raise _failure("LLM_INVALID_REQUEST", "文本请求超过适配器字节上限。", 413)
        return HttpChatStream(self, body, deadline)


class HttpChatStream:
    def __init__(self, adapter: HttpChatAdapter, body: bytes, deadline: float) -> None:
        self._adapter = adapter
        self._body = body
        self._deadline = deadline
        self._lock = Lock()
        self._stop = Event()
        self._started = False
        self._complete = False
        self._expired = False
        self._aborting = False
        self._client: httpx.Client | None = None
        self._response: httpx.Response | None = None

    def _abort_transport(self) -> None:
        # Network close runs away from the caller's control/supervision loop.
        with self._lock:
            response, client = self._response, self._client
        for resource in (response, client):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass  # No transport exception or upstream body enters public errors.

    def _abort_async(self) -> None:
        with self._lock:
            if self._aborting or self._client is None:
                return
            self._aborting = True
        Thread(target=self._abort_transport, name="llm-http-close", daemon=True).start()

    def cancel(self) -> bool:
        with self._lock:
            completed = self._complete
            self._stop.set()
        self._abort_async()
        return completed

    def close(self) -> None:
        self.cancel()

    def _watch_deadline(self) -> None:
        if not self._stop.wait(max(0, self._deadline - monotonic())):
            with self._lock:
                if self._complete:
                    return
                self._expired = True
                self._stop.set()
            self._abort_async()

    def _check_active(self) -> None:
        if self._expired or monotonic() >= self._deadline:
            raise _failure("LLM_TIMEOUT", "在线生成超过本次截止时间。", 504) from None
        if self._stop.is_set():
            raise _failure("LLM_CANCELLED", "在线生成已请求停止，上游停止情况尚未确认。", 409) from None

    def events(self) -> Iterator[EngineChunk]:
        with self._lock:
            if self._started:
                raise _failure("LLM_STREAM_ALREADY_USED", "在线流不能重新执行。", 409)
            self._started = True
        self._check_active()
        remaining = self._deadline - monotonic()
        adapter = self._adapter
        timeout = httpx.Timeout(connect=min(adapter._connect_timeout, remaining),
                                read=min(adapter._read_timeout, remaining),
                                write=min(adapter._connect_timeout, remaining),
                                pool=min(adapter._connect_timeout, remaining))
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream", "Accept-Encoding": "identity"}
        headers.update(adapter._correlation_headers)
        if adapter._api_key:
            headers["Authorization"] = f"Bearer {adapter._api_key}"
        watchdog = Thread(target=self._watch_deadline, name="llm-http-deadline", daemon=True)
        transport_options = {"verify": adapter._verify}
        if adapter._transport is not None:
            transport_options["transport"] = adapter._transport
        try:
            # Client/transport defaults never opt into redirects or connect retries.
            with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False,
                              **transport_options,
                              limits=httpx.Limits(max_connections=1, max_keepalive_connections=0)) as client:
                with self._lock:
                    self._client = client
                self._check_active()
                watchdog.start()
                with client.stream("POST", adapter._endpoint, content=self._body, headers=headers) as response:
                    with self._lock:
                        self._response = response
                    self._check_active()
                    if response.status_code != 200:
                        raise _failure("LLM_UPSTREAM_REJECTED", "上游未接受生成请求，请检查服务及模型配置。")
                    if (response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream"
                            or response.headers.get("content-encoding", "identity").strip().lower() != "identity"):
                        raise _failure("LLM_UPSTREAM_PROTOCOL_ERROR", "上游没有返回受支持的事件流。")
                    yield from self._read_events(response)
        except AppError:
            raise
        except httpx.TimeoutException:
            self._check_active()
            raise _failure("LLM_TIMEOUT", "等待上游连接或流内容超时。", 504) from None
        except (httpx.ConnectError, httpx.ProxyError):
            self._check_active()
            raise _failure("LLM_UPSTREAM_UNAVAILABLE", "无法连接已配置的模型服务。", 503) from None
        except httpx.HTTPError:
            self._check_active()
            raise _failure("LLM_STREAM_INTERRUPTED", "上游连接已中断，本次生成不会自动重放。") from None
        except (SSELimitError, SSEParseError) as error:
            self._check_active()
            if isinstance(error, SSELimitError):
                raise _failure("LLM_STREAM_LIMIT_EXCEEDED", "上游事件流超过接收上限。") from None
            raise _failure("LLM_UPSTREAM_PROTOCOL_ERROR", "上游事件流编码或帧格式无效。") from None
        except Exception:
            self._check_active()
            raise _failure("LLM_STREAM_INTERRUPTED", "上游流处理未能完成，本次生成不会自动重放。") from None
        finally:
            self._stop.set()
            self._body = b""
            with self._lock:
                self._response = None
                self._client = None

    def _read_events(self, response: httpx.Response) -> Iterator[EngineChunk]:
        stream_id: str | None = None
        finish_reason: str | None = None
        output_characters = 0
        for event in iter_sse_events(response.iter_raw()):
            self._check_active()
            if event.event not in ("", "message"):
                raise _failure("LLM_UPSTREAM_PROTOCOL_ERROR", "上游返回了不支持的事件类型。")
            if event.data == "[DONE]":
                if finish_reason is None:
                    raise _failure("LLM_UPSTREAM_PROTOCOL_ERROR", "上游缺少明确的生成结束原因。")
                with self._lock:
                    self._check_active()
                    self._complete = True
                yield EngineChunk("done", finish_reason=finish_reason)
                return
            try:
                chunk = UpstreamChunk.model_validate(decode_sse_json_object(event.data))
            except (ValidationError, ValueError):
                raise _failure("LLM_UPSTREAM_PROTOCOL_ERROR", "上游返回了无效或不支持的生成内容。") from None
            if chunk.model != self._adapter._model or (stream_id is not None and chunk.id != stream_id):
                raise _failure("LLM_UPSTREAM_PROTOCOL_ERROR", "上游流的模型或请求标识发生变化。")
            stream_id = chunk.id
            if not chunk.choices:
                continue  # Optional usage frame, never an invented text/token count.
            if finish_reason is not None:
                raise _failure("LLM_UPSTREAM_PROTOCOL_ERROR", "上游在结束后继续返回生成内容。")
            choice = chunk.choices[0]
            text = choice.delta.content or ""
            try:
                text.encode("utf-8", errors="strict")
            except UnicodeError:
                raise _failure("LLM_UPSTREAM_PROTOCOL_ERROR", "上游返回了无效的文本编码。") from None
            output_characters += len(text)
            if output_characters > MAX_OUTPUT_CHARACTERS:
                raise _failure("LLM_STREAM_LIMIT_EXCEEDED", "上游生成文本超过接收上限。")
            if text:
                yield EngineChunk("delta", text=text)
            if choice.finish_reason is not None:
                finish_reason = choice.finish_reason
        self._check_active()
        raise _failure("LLM_STREAM_INTERRUPTED", "上游流未正常结束，已收到的部分内容不代表完整回答。")
