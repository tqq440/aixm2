"""可公开的接口错误与启动配置错误。"""


class ConfigurationError(RuntimeError):
    """启动前即可定位的配置错误，不包含配置原值。"""


class StartupError(RuntimeError):
    """资源初始化失败，应用不能开始接收请求。"""


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
