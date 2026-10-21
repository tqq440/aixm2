"""可选官方客户端延迟装配；导入和旧 SQLite 模式不需要 Redis 依赖。"""

from backend.common.exceptions.app_error import AppError


def create_redis_client(settings):
    try:
        from redis import Redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry
    except ImportError:
        raise AppError("REDIS_CLIENT_MISSING", "Redis 客户端未安装，请使用独立的 requirements/queue.txt 准备环境。", 503) from None
    options = dict(socket_timeout=settings.redis_timeout_seconds, socket_connect_timeout=settings.redis_timeout_seconds,
                   decode_responses=True, protocol=2, max_connections=8, retry=Retry(NoBackoff(), 0), retry_on_timeout=False)
    if settings.redis_url.get_secret_value().startswith("rediss:"):
        options["ssl_cert_reqs"] = "required"
    return Redis.from_url(settings.redis_url.get_secret_value(), **options)
