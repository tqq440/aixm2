"""可空数据库时间的 UTC ISO8601 表达。"""

from backend.utils.time.time_utils import as_utc


def iso_utc(value):
    return as_utc(value).isoformat() if value else None
