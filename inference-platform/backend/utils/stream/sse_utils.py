"""严格 UTF-8、有界的 SSE 分帧；与任何模型或 HTTP 客户端无关。"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
import json


class SSEParseError(ValueError):
    """编码或帧边界损坏，错误信息不含原始流内容。"""


class SSELimitError(SSEParseError):
    """单行、单帧或总字节超过调用者的预算。"""


@dataclass(frozen=True)
class SSEEvent:
    data: str
    event: str = ""


def decode_sse_json_object(data: str) -> dict:
    """解析单个有界帧的 JSON，拒绝重复键与 NaN/Infinity，错误不含正文。"""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("Non-finite JSON value")

    try:
        value = json.loads(data, object_pairs_hook=unique_object, parse_constant=reject_constant)
        if not isinstance(value, dict):
            raise ValueError("JSON frame must be an object")
        return value
    except (ValueError, TypeError, RecursionError):
        raise SSEParseError("Invalid JSON event") from None


def iter_sse_events(
    chunks: Iterable[bytes], *, max_line_bytes: int = 65_536,
    max_event_bytes: int = 131_072, max_total_bytes: int = 8_388_608,
) -> Iterator[SSEEvent]:
    """按空行分发，支持 CR/LF/CRLF、跨块 UTF-8 与多行 data。

    注释及未知 SSE 字段按规范忽略，但仍计入预算。EOF 的半行/半帧拒绝，
    不把网络截断解释为已完成事件。流语义（如 [DONE]）由调用者裁决。
    """
    if any(type(value) is not int or value < 1 for value in (max_line_bytes, max_event_bytes, max_total_bytes)):
        raise ValueError("SSE limits must be positive integers")
    line = bytearray()
    data: list[str] = []
    event_name = ""
    frame_bytes = total_bytes = 0
    first_line = True
    after_cr = False

    def complete_line() -> SSEEvent | None:
        nonlocal frame_bytes, event_name, first_line
        raw = bytes(line)
        line.clear()
        try:
            value = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise SSEParseError("Invalid UTF-8 stream") from None
        if first_line:
            value = value.removeprefix("\ufeff")
            first_line = False
        if not value:
            result = SSEEvent("\n".join(data), event_name) if data else None
            data.clear()
            event_name = ""
            frame_bytes = 0
            return result
        frame_bytes += len(raw) + 1
        if frame_bytes > max_event_bytes:
            raise SSELimitError("SSE event exceeds limit")
        if value.startswith(":"):
            return None
        field, separator, content = value.partition(":")
        if separator and content.startswith(" "):
            content = content[1:]
        if field == "data":
            data.append(content)
        elif field == "event":
            event_name = content
        return None

    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise SSEParseError("Stream chunks must be bytes")
        total_bytes += len(chunk)
        if total_bytes > max_total_bytes:
            raise SSELimitError("SSE stream exceeds limit")
        for byte in chunk:
            if after_cr:
                after_cr = False
                if byte == 10:
                    continue
            if byte in (10, 13):
                completed = complete_line()
                after_cr = byte == 13
                if completed is not None:
                    yield completed
            else:
                if len(line) >= max_line_bytes:
                    raise SSELimitError("SSE line exceeds limit")
                line.append(byte)
    if line or data or frame_bytes:
        raise SSEParseError("Truncated SSE event")
