"""每次新调用前的有界 Redis 检查；采样在 SQL 事务外，GET 不触发检查。"""

from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from backend.common.exceptions.app_error import AppError
from backend.common.queue.redis_streams import RedisStreams
from backend.config.queue.redis_config import create_redis_client
from backend.data.database.database import database_now
from backend.data.queue.queue_models import InferenceQueueState


class RedisGate:
    def __init__(self, database, settings, *, client=None, consumer=None):
        self.database, self.settings, self.client = database, settings, client
        self.consumer = consumer or "consumer-" + uuid4().hex
        self.streams = None

    def _prepare(self):
        if self.settings.common_queue_backend != "redis":
            raise AppError("REDIS_QUEUE_DISABLED", "当前未选择 Redis 公共队列。", 503)
        try:
            with self.database.transaction() as session:
                row = session.get(InferenceQueueState, "redis")
                if row is None:
                    raise SQLAlchemyError()
                broker_id, started = row.broker_id, database_now(session)
        except SQLAlchemyError:
            raise AppError("REDIS_QUEUE_STORAGE_UNAVAILABLE", "Redis 队列资料不可用，请核对 0017 迁移。", 503) from None
        if self.client is None:
            self.client = create_redis_client(self.settings)
        if self.streams is None:
            stream = f"{self.settings.redis_namespace}:{{{broker_id}}}:offline"
            self.streams = RedisStreams(self.client, stream, self.consumer, self.settings.notification_visibility_seconds)
        return started

    def record(self, started, error=None):
        with self.database.transaction(immediate=True) as session:
            row = session.get(InferenceQueueState, "redis")
            if row.checked_at is None or row.checked_at <= started:
                row.checked_at, row.last_error_code = started, error
                row.availability = "unavailable" if error else "available"

    def check(self):
        # 缺少客户端也留有明确故障快照；不输出 URL、异常正文或连接凭据。
        if self.settings.common_queue_backend != "redis":
            raise AppError("REDIS_QUEUE_DISABLED", "当前未选择 Redis 公共队列。", 503)
        started = None
        try:
            started = self._prepare()
            self.streams.ensure()
        except AppError as error:
            if error.code != "REDIS_QUEUE_STORAGE_UNAVAILABLE":
                if started is None:
                    with self.database.transaction() as session:
                        started = database_now(session)
                self.record(started, error.code)
            raise
        self.record(started)

    def close(self):
        if self.streams:
            self.streams.close()
