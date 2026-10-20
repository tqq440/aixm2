"""受管上传目录维护命令契约。"""

from backend.schema.common.api_schema import ApiSchema


class CleanupOptions(ApiSchema):
    apply: bool = False


class CleanupReport(ApiSchema):
    applied: bool
    grace_hours: int = 24
    candidates: list[str]
    deleted: int
