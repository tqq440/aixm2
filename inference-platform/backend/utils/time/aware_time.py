"""显式解析带时区的ISO时间，不从数字猜测时间戳单位。"""

from datetime import datetime


def require_aware_datetime(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("时间必须为带时区的ISO时间") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须为带时区的ISO时间，不接受时间戳数字")
    return value
