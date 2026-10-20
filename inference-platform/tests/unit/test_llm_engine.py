"""仅验证人工 HTTP/SSE 协议与故障，不是实际 LLM 模型验收。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Event, Thread
from time import monotonic

import httpx2 as httpx
import pytest

from backend.common.exceptions.app_error import AppError, ConfigurationError
from backend.common.llm.engine_protocol import EngineChunk
from backend.service.llm import engine_adapter
from backend.service.llm.engine_adapter import HttpChatAdapter

MESSAGES = [{"role": "user", "content": "协议测试"}]


def frame(text=None, finish=None, **overrides):
    value = {
        "id": "chatcmpl-protocol-test", "object": "chat.completion.chunk", "model": "test-model",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}],
    }
    value.update(overrides)
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


DONE = b"data: [DONE]\n\n"


class ByteStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = Event()

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed.set()


def adapted(monkeypatch, chunks, *, status=200, headers=None):
    captured = []
    stream = ByteStream(chunks)
    original_client = httpx.Client

    def handler(request):
        captured.append(request)
        return httpx.Response(status, headers=headers or {"content-type": "text/event-stream"}, stream=stream)

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(engine_adapter.httpx, "Client", client)
    return HttpChatAdapter("http://127.0.0.1:12345/v1/chat/completions", "test-model", "secret-key", 1, 1), captured, stream


def test_open_is_lazy_snapshots_input_and_preserves_text_finish(monkeypatch):
    adapter, requests, transport = adapted(monkeypatch, [frame("你"), frame("好", "length"), DONE])
    messages = [dict(MESSAGES[0])]
    params = {"maxTokens": 12, "temperature": 0, "topP": 0.8}
    stream = adapter.open(messages, params, monotonic() + 5)
    messages[0]["content"] = "changed"
    params["maxTokens"] = 100
    assert requests == []
    assert list(stream.events()) == [EngineChunk("delta", "你"), EngineChunk("delta", "好"), EngineChunk("done", finish_reason="length")]
    request = requests[0]
    assert request.headers["authorization"] == "Bearer secret-key"
    assert request.headers["accept-encoding"] == "identity"
    body = json.loads(request.content)
    assert body["messages"] == MESSAGES
    assert (body["max_tokens"], body["top_p"], body["n"], body["stream"]) == (12, 0.8, 1, True)
    assert stream.cancel() is True and transport.closed.wait(1)
    stream.close()
    with pytest.raises(AppError, match="不能重新执行"):
        list(stream.events())
    assert len(requests) == 1


def test_every_byte_utf8_chunk_role_only_and_usage_frame(monkeypatch):
    payload = frame(None, choices=[{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}])
    payload += frame("你好") + frame(None, "stop") + frame(choices=[], usage={"completion_tokens": 2}) + DONE
    adapter, _, _ = adapted(monkeypatch, (bytes([byte]) for byte in payload))
    assert list(adapter.open(MESSAGES, {}, monotonic() + 5).events()) == [EngineChunk("delta", "你好"), EngineChunk("done", finish_reason="stop")]


@pytest.mark.parametrize("payload,code", [
    (frame("partial"), "LLM_STREAM_INTERRUPTED"),
    (frame("partial", "stop"), "LLM_STREAM_INTERRUPTED"),
    (DONE, "LLM_UPSTREAM_PROTOCOL_ERROR"),
    (b"data: {bad json}\n\n", "LLM_UPSTREAM_PROTOCOL_ERROR"),
    (b"data: \xff\n\n", "LLM_UPSTREAM_PROTOCOL_ERROR"),
    (b"event: error\ndata: private upstream detail\n\n", "LLM_UPSTREAM_PROTOCOL_ERROR"),
    (frame("x", model="wrong-model"), "LLM_UPSTREAM_PROTOCOL_ERROR"),
    (frame("x") + frame("y", id="different"), "LLM_UPSTREAM_PROTOCOL_ERROR"),
    (frame(None, "stop") + frame("after-finish") + DONE, "LLM_UPSTREAM_PROTOCOL_ERROR"),
    (frame(choices=[]), "LLM_UPSTREAM_PROTOCOL_ERROR"),
    (frame(error={"message": "private upstream detail"}), "LLM_UPSTREAM_PROTOCOL_ERROR"),
    (frame(choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]), "LLM_UPSTREAM_PROTOCOL_ERROR"),
    (frame(choices=[{"index": 1, "delta": {}, "finish_reason": "stop"}]), "LLM_UPSTREAM_PROTOCOL_ERROR"),
    (frame(choices=[{"index": True, "delta": {}, "finish_reason": "stop"}]), "LLM_UPSTREAM_PROTOCOL_ERROR"),
    (b"data:" + b"x" * 65_536, "LLM_STREAM_LIMIT_EXCEEDED"),
], ids=[f"case-{index}" for index in range(15)])
def test_bad_stream_never_yields_success_or_replays(monkeypatch, payload, code):
    adapter, requests, transport = adapted(monkeypatch, [payload])
    stream = adapter.open(MESSAGES, {}, monotonic() + 5)
    output = []
    with pytest.raises(AppError) as raised:
        for event in stream.events():
            output.append(event)
    assert raised.value.code == code
    assert "private upstream detail" not in str(raised.value)
    assert "secret-key" not in str(raised.value)
    assert all(event.kind != "done" for event in output)
    assert stream.cancel() is False and transport.closed.wait(1)
    assert len(requests) == 1


@pytest.mark.parametrize("unsupported", ["tool_calls", "function_call", "audio", "reasoning_content", "reasoning", "refusal"])
def test_non_text_delta_is_rejected(monkeypatch, unsupported):
    adapter, _, _ = adapted(monkeypatch, [frame(choices=[{"index": 0, "delta": {unsupported: "unsupported"}, "finish_reason": None}])])
    with pytest.raises(AppError) as raised:
        list(adapter.open(MESSAGES, {}, monotonic() + 5).events())
    assert raised.value.code == "LLM_UPSTREAM_PROTOCOL_ERROR"


@pytest.mark.parametrize("status", [301, 307, 400, 401, 429, 500])
def test_http_errors_are_safe_no_redirect_no_retry_and_no_body_read(monkeypatch, status):
    def body_must_not_be_read():
        raise AssertionError("error body read")
        yield b""
    adapter, requests, transport = adapted(monkeypatch, body_must_not_be_read(), status=status)
    with pytest.raises(AppError) as raised:
        list(adapter.open(MESSAGES, {}, monotonic() + 5).events())
    assert raised.value.code == "LLM_UPSTREAM_REJECTED"
    assert len(requests) == 1 and transport.closed.wait(1)


@pytest.mark.parametrize("headers", [{"content-type": "application/json"}, {"content-type": "text/event-stream", "content-encoding": "gzip"}])
def test_wrong_mime_and_compressed_body_rejected(monkeypatch, headers):
    adapter, _, _ = adapted(monkeypatch, [DONE], headers=headers)
    with pytest.raises(AppError) as raised:
        list(adapter.open(MESSAGES, {}, monotonic() + 5).events())
    assert raised.value.code == "LLM_UPSTREAM_PROTOCOL_ERROR"


@pytest.mark.parametrize("messages,parameters", [([], {}), ([{"role": "tool", "content": "x"}], {}), (MESSAGES, {"max_tokens": True}), (MESSAGES, {"temperature": float("nan")}), (MESSAGES, {"tools": []}), (MESSAGES, {"n": 2})])
def test_input_contract_refuses_non_text_or_overriding_transport(monkeypatch, messages, parameters):
    adapter, requests, _ = adapted(monkeypatch, [])
    with pytest.raises(AppError) as raised:
        adapter.open(messages, parameters, monotonic() + 5)
    assert raised.value.code == "LLM_INVALID_REQUEST" and not requests


def test_output_character_budget_is_cumulative(monkeypatch):
    monkeypatch.setattr(engine_adapter, "MAX_OUTPUT_CHARACTERS", 5)
    adapter, requests, _ = adapted(monkeypatch, [frame("abcd"), frame("ef"), frame(None, "stop"), DONE])
    stream = adapter.open(MESSAGES, {}, monotonic() + 5)
    iterator = stream.events()
    assert next(iterator).text == "abcd"
    with pytest.raises(AppError) as raised:
        next(iterator)
    assert raised.value.code == "LLM_STREAM_LIMIT_EXCEEDED" and len(requests) == 1


def test_unpaired_surrogate_in_json_text_is_rejected(monkeypatch):
    malformed = frame("replace-me").replace(b"replace-me", b"\\ud800")
    adapter, _, _ = adapted(monkeypatch, [malformed])
    with pytest.raises(AppError) as raised:
        list(adapter.open(MESSAGES, {}, monotonic() + 5).events())
    assert raised.value.code == "LLM_UPSTREAM_PROTOCOL_ERROR"


def test_input_byte_budget_prevents_sending(monkeypatch):
    monkeypatch.setattr(engine_adapter, "MAX_REQUEST_BYTES", 50)
    adapter, requests, _ = adapted(monkeypatch, [])
    with pytest.raises(AppError) as raised:
        adapter.open(MESSAGES, {}, monotonic() + 5)
    assert raised.value.status_code == 413 and not requests


def test_cancel_before_read_is_fast_idempotent_and_never_sends(monkeypatch):
    adapter, requests, _ = adapted(monkeypatch, [])
    stream = adapter.open(MESSAGES, {}, monotonic() + 5)
    assert stream.cancel() is False and stream.cancel() is False
    stream.close()
    with pytest.raises(AppError) as raised:
        list(stream.events())
    assert raised.value.code == "LLM_CANCELLED" and not requests


def test_expired_deadline_never_sends(monkeypatch):
    adapter, requests, _ = adapted(monkeypatch, [])
    with pytest.raises(AppError) as raised:
        list(adapter.open(MESSAGES, {}, monotonic() - 1).events())
    assert raised.value.code == "LLM_TIMEOUT" and not requests


@pytest.mark.parametrize("endpoint", ["", "ftp://127.0.0.1/test", "http://user:secret@127.0.0.1/test", "http://127.0.0.1/test?key=secret", "http://127.0.0.1/#fragment"])
def test_unsafe_endpoint_configuration_is_rejected(endpoint):
    with pytest.raises(ConfigurationError):
        HttpChatAdapter(endpoint, "model", None, 1, 1)


@contextmanager
def local_protocol_server(mode):
    connected = Event()
    finish = Event()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(self.rfile.read(int(self.headers["Content-Length"])))
            if mode == "headersilent":
                connected.set()
                finish.wait(2)
                self.close_connection = True
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            connected.set()
            try:
                if mode == "complete":
                    for chunk in [frame("真实HTTP协议替身"), frame(None, "stop"), DONE]:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                elif mode == "chatty":
                    while not finish.wait(0.03):
                        self.wfile.write(b"8\r\n: ping\n\n\r\n")
                        self.wfile.flush()
                else:
                    finish.wait(2)
            except (BrokenPipeError, ConnectionError, OSError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions", connected, calls
    finally:
        finish.set()
        server.shutdown()
        server.server_close()
        thread.join(1)


def test_real_local_http_protocol_completion():
    with local_protocol_server("complete") as (url, _, calls):
        stream = HttpChatAdapter(url, "test-model", None, 1, 1).open(MESSAGES, {}, monotonic() + 3)
        output = list(stream.events())
        assert output[0].text == "真实HTTP协议替身" and output[-1].kind == "done"
        assert stream.cancel() is True and len(calls) == 1


@pytest.mark.parametrize("mode", ["silent", "headersilent"])
def test_real_http_cancel_fast_then_blocked_reader_exits(mode):
    with local_protocol_server(mode) as (url, connected, calls), ThreadPoolExecutor(max_workers=1) as pool:
        stream = HttpChatAdapter(url, "test-model", None, 0.5, 0.3).open(MESSAGES, {}, monotonic() + 3)
        future = pool.submit(lambda: list(stream.events()))
        assert connected.wait(1)
        started = monotonic()
        assert stream.cancel() is False
        assert monotonic() - started < 0.15
        with pytest.raises(AppError) as raised:
            future.result(timeout=1)
        assert raised.value.code == "LLM_CANCELLED" and len(calls) == 1


@pytest.mark.parametrize("mode", ["silent", "chatty"])
def test_real_http_total_deadline_survives_no_tokens_or_heartbeats(mode):
    with local_protocol_server(mode) as (url, _, calls):
        stream = HttpChatAdapter(url, "test-model", None, 0.2, 1).open(MESSAGES, {}, monotonic() + 0.2)
        started = monotonic()
        with pytest.raises(AppError) as raised:
            list(stream.events())
        assert raised.value.code == "LLM_TIMEOUT"
        assert monotonic() - started < 1 and len(calls) == 1


def test_real_http_idle_timeout_distinct_from_total_deadline():
    with local_protocol_server("silent") as (url, _, calls):
        stream = HttpChatAdapter(url, "test-model", None, 0.5, 0.15).open(MESSAGES, {}, monotonic() + 3)
        with pytest.raises(AppError) as raised:
            list(stream.events())
        assert raised.value.code == "LLM_TIMEOUT" and len(calls) == 1
