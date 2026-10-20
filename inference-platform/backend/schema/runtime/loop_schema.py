"""后台入口的少量命令行参数与退出报告。"""

from backend.schema.common.api_schema import ApiSchema


class DispatcherOptions(ApiSchema):
    once: bool = False


class DispatcherSummary(ApiSchema):
    stopped: bool = True
    published_count: int
