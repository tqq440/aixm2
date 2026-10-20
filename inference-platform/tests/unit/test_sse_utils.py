"""SSE 协议测试；不调用模型。"""

import pytest

from backend.utils.stream.sse_utils import SSEEvent, SSELimitError, SSEParseError, decode_sse_json_object, iter_sse_events


@pytest.mark.parametrize("separator", [b"\n", b"\r", b"\r\n"])
def test_multiline_comments_and_utf8_survive_every_byte_boundary(separator):
    payload = separator.join([
        b"\xef\xbb\xbf: heartbeat", b"event: message", "data: 你好".encode(),
        b"data: second", b"id: ignored", b"retry: 3000", b"", b"",
    ])
    assert list(iter_sse_events(bytes([value]) for value in payload)) == [SSEEvent("你好\nsecond", "message")]


def test_optional_space_empty_data_and_unknown_fields():
    assert list(iter_sse_events([b"data:\ndata:  x\nunknown: y\n\ndata\n\n"])) == [
        SSEEvent("\n x"), SSEEvent(""),
    ]


def test_comment_frames_do_not_emit_messages():
    assert list(iter_sse_events([b": ping\n\n\n: pong\r\n\r\n"])) == []


@pytest.mark.parametrize("payload", [b"data: \xff\n\n", b"data: \xe4\n\n", b"data: cut", b"data: cut\n"])
def test_malformed_utf8_and_incomplete_frames_are_rejected(payload):
    with pytest.raises(SSEParseError):
        list(iter_sse_events([payload]))


@pytest.mark.parametrize("limits,payload", [
    ({"max_line_bytes": 5}, b"data:x\n\n"),
    ({"max_event_bytes": 10}, b"data:a\ndata:b\n\n"),
    ({"max_total_bytes": 10}, b":ping\n\n:ping\n\n"),
    ({"max_event_bytes": 5}, b":12345\n\n"),
])
def test_limits_also_count_comments(limits, payload):
    with pytest.raises(SSELimitError):
        list(iter_sse_events([payload], **limits))


def test_line_budget_does_not_require_a_newline():
    with pytest.raises(SSELimitError):
        list(iter_sse_events([b"123", b"456"], max_line_bytes=5))


@pytest.mark.parametrize("value", [0, -1, True, 1.2])
def test_invalid_budget_rejected(value):
    with pytest.raises(ValueError):
        list(iter_sse_events([], max_line_bytes=value))


def test_non_bytes_chunk_is_rejected():
    with pytest.raises(SSEParseError):
        list(iter_sse_events(["data: x\n\n"]))


@pytest.mark.parametrize("value", ['{"x": 1, "x": 2}', '{"x": NaN}', '{"x": Infinity}', '[]', '{invalid}'])
def test_json_payload_rejects_ambiguous_and_nonstandard_values(value):
    with pytest.raises(SSEParseError):
        decode_sse_json_object(value)


def test_json_payload_accepts_nested_standard_values():
    assert decode_sse_json_object('{"x": [1, null, true, "你好"]}') == {"x": [1, None, True, "你好"]}
