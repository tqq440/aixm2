"""数据库 UTC 日期与 JSON 时区之间的纯转换。"""

from datetime import datetime, timezone


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
