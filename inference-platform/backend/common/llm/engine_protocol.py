"""引擎适配与在线编排之间的最小契约，不保存聊天正文。"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class EngineChunk:
    kind: Literal["delta", "done"]
    text: str = ""
    finish_reason: str | None = None


class EngineStream(Protocol):
    def events(self) -> Iterator[EngineChunk]: ...

    def cancel(self) -> bool:
        """只有确认上游协议已经完成才为 True；关闭 HTTP 连接不是确认。"""
        ...

    def close(self) -> None: ...


class EngineAdapter(Protocol):
    def open(self, messages: list[dict], parameters: dict, deadline: float) -> EngineStream:
        """创建单次流句柄；deadline 为 monotonic 截止，调用本身不联网。"""
        ...
