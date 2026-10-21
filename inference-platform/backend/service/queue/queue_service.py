"""SQLite outbox → Redis Streams；通知所有权不替代 SQLite 执行权。"""

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from backend.common.exceptions.app_error import AppError
from backend.data.database.database import database_now
from backend.data.invocations.invocation_models import InferenceInvocation
from backend.data.queue.queue_models import InferenceQueueOutbox, InferenceQueueState
from backend.data.queue.queue_repository import QueueRepository
from backend.service.invocations.invocation_service import InvocationService
from backend.service.queue.redis_gate import RedisGate
from backend.schema.queue.queue_schema import QueueNotice, QueueSummary
from backend.utils.time.time_utils import as_utc


class QueueService:
    def __init__(self, database, settings, *, client=None, consumer=None, logger=None):
        self.database, self.settings = database, settings
        self.logger = logger
        self.gate = RedisGate(database, settings, client=client, consumer=consumer)
        self.core = InvocationService(database, settings, queue_gate=self.gate)

    def publish(self):
        self.gate.check()
        with self.core._transaction() as session:
            ids = QueueRepository.due(session, database_now(session))
        count = 0
        for event_id in ids:
            with self.core._transaction(True) as session:
                selected = QueueRepository.publication(session, event_id)
            if selected is None:
                continue
            token, notice = selected
            try:
                message_id = self.gate.streams.publish(notice)
            except AppError as error:
                with self.core._transaction(True) as session:
                    outbox = session.get(InferenceQueueOutbox, event_id)
                    changed = outbox.last_error_code != error.code
                    QueueRepository.record_publication(session, event_id, token, error=error.code,
                                                       retry_seconds=self.settings.dispatch_retry_seconds)
                    if changed:
                        invocation = session.get(InferenceInvocation, notice.invocation_id)
                        self.core.repository.event(session, invocation, "queue.notification_failed", database_now(session),
                                                   {"code": error.code})
                self._failure(error)
                raise
            # 若此处退出/提交失败，事件会补投；同 eventId 多条消息也不会重复推理。
            with self.core._transaction(True) as session:
                first = session.get(InferenceQueueOutbox, event_id).published_at is None
                recorded = QueueRepository.record_publication(session, event_id, token, message_id=message_id,
                                                              retry_seconds=self.settings.redis_republish_seconds)
                if recorded and first:
                    invocation = session.get(InferenceInvocation, notice.invocation_id)
                    self.core.repository.event(session, invocation, "queue.notification_published", database_now(session))
            count += 1
        return count

    def _failure(self, error):
        with self.database.transaction() as session:
            now = database_now(session)
        self.gate.record(now, error.code)

    def collect(self):
        self.gate.check()
        try:
            messages = self.gate.streams.receive()
        except AppError as error:
            self._failure(error); raise
        count = 0
        for message_id, values in messages:
            try:
                notice = QueueNotice.model_validate(values)
            except ValidationError:
                # 无可相信的任务身份；清除无效通知，绝不据此更改任务状态。
                if self.logger:
                    self.logger.warning("Redis 通知身份无效，清除该通知；未修改任务。")
                self.gate.streams.acknowledge(message_id, values)
                continue
            with self.core._transaction(True) as session:
                outbox = session.get(InferenceQueueOutbox, notice.event_id)
                invocation = session.get(InferenceInvocation, notice.invocation_id)
                valid = QueueRepository.matches(outbox, invocation, notice)
                terminal = valid and invocation.state == "finished" and invocation.permit_state != "uncertain"
                if valid and not terminal:
                    first = outbox.received_at is None
                    outbox.redis_required, outbox.received_at = True, database_now(session)
                    outbox.state, outbox.last_error_code = "received", None
                    if first:
                        self.core.repository.event(session, invocation, "queue.notification_received", outbox.received_at)
                    count += 1
            if not valid or terminal:
                # 可信任务只有终态才能 ACK；不匹配通知只清除其自身，不动任何任务。
                if not valid and self.logger:
                    self.logger.warning("Redis 通知与本地调用身份不匹配，未改变任务或容量。")
                self.gate.streams.acknowledge(message_id, values)
                if terminal:
                    self._settle(notice.event_id)
        return count

    def _settle(self, event_id):
        with self.core._transaction(True) as session:
            row = session.get(InferenceQueueOutbox, event_id)
            invocation = session.get(InferenceInvocation, row.invocation_id)
            if invocation.state == "finished" and invocation.permit_state != "uncertain" and row.state != "settled":
                row.state, row.settled_at = "settled", database_now(session)
                self.core.repository.event(session, invocation, "queue.notification_settled", row.settled_at)

    def acknowledge_finished(self):
        self.gate.check()
        with self.core._transaction() as session:
            ids = QueueRepository.terminal_outbox(session)
        count = 0
        for event_id in ids:
            with self.core._transaction() as session:
                row = session.get(InferenceQueueOutbox, event_id)
                message_id = row.message_id
                notice = QueueRepository.notice(row, session.get(InferenceInvocation, row.invocation_id))
            if message_id:
                self.gate.streams.acknowledge(message_id, notice.model_dump(by_alias=True))
            self._settle(event_id)
            count += 1
        return count

    def tick(self):
        # 到期恢复不依赖 Redis；未知许可仍由公共执行规则保守持有。
        if self.settings.common_queue_backend != "redis":
            raise AppError("REDIS_QUEUE_DISABLED", "Redis 公共队列未启用，未运行维护。", 503)
        self.core.recover()
        published = self.publish()
        received = self.collect()
        acknowledged = self.acknowledge_finished()
        return QueueSummary(published=published, received=received, acknowledged=acknowledged)

    def run_once(self, worker_id, sender):
        return self._run_once(worker_id, lambda lease: self.core.dispatch(lease, sender))

    def run_once_node(self, worker_id, *, bridge=None):
        """4C1 内部桥接入口；仍复用通知、轮询、许可和持久化后确认。"""
        from backend.service.invocations.node_bridge import NodeInvocationBridge
        from backend.service.host_node.input_service import CentralMaterialSource
        bridge = bridge or NodeInvocationBridge(self.core,material_source=CentralMaterialSource(self.core))
        if bridge.core is not self.core:
            raise ValueError("Node 桥接必须复用当前队列的公共执行内核")
        return self._run_once(worker_id, bridge.dispatch)

    def _run_once(self, worker_id, execute_lease):
        self.tick()
        leases = self.core.claim_round(worker_id, limit=1)
        if not leases:
            return False
        try:
            return execute_lease(leases[0])
        finally:
            # Redis 已故障时也不能撤销已经提交的结果，留待重连后的确认。
            try:
                self.acknowledge_finished()
            except AppError:
                pass

    def status(self):
        if self.settings.common_queue_backend != "redis":
            return {"backend": "sqlite", "redisEnabled": False, "availability": "not_selected"}
        try:
            with self.database.transaction() as session:
                row = session.get(InferenceQueueState, "redis")
                if row is None:
                    raise SQLAlchemyError()
                return {"backend": "redis", "redisEnabled": True, "availability": row.availability,
                        "checkedAt": as_utc(row.checked_at) if row.checked_at else None, "lastErrorCode": row.last_error_code,
                        "outboxCounts": QueueRepository.counts(session)}
        except SQLAlchemyError:
            raise AppError("REDIS_QUEUE_STORAGE_UNAVAILABLE", "Redis 队列资料不可用，请核对 0017 迁移。", 503) from None

    def close(self):
        self.gate.close()
