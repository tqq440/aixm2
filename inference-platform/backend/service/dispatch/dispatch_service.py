"""M08 首次/重试投递、到期恢复；任务裁决在写事务中重新检查。"""

from logging import Logger

from backend.config.settings.app_settings import AppSettings
from backend.data.broker.broker_repository import BrokerRepository
from backend.data.broker.dispatch_repository import DispatchRepository
from backend.data.database.database import Database, database_now
from backend.service.recovery.recovery_service import RecoveryService


class DispatchService:
    def __init__(self, database: Database, settings: AppSettings, logger: Logger) -> None:
        self.database = database
        self.settings = settings
        self.logger = logger
        self.repository = DispatchRepository(database)
        self.broker = BrokerRepository(database)
        self.recovery = RecoveryService(database, settings)

    def pending_error(self) -> str | None:
        return self.repository.pending_error()

    def tick(self) -> int:
        """有界扫描，每个事件独立事务，返回本轮发布成功数。"""
        published = 0
        self.recovery.tick()
        for event_id in self.repository.due_ids(limit=20):
            try:
                with self.database.transaction(immediate=True) as session:
                    loaded = self.repository.load(session, event_id)
                    if loaded is None:
                        continue
                    event, task = loaded
                    now = database_now(session)
                    if event.status != "pending" or event.available_at > now:
                        continue
                    if (
                        task.status not in ("created", "queued", "running", "retry_wait")
                        or event.dispatch_version != task.dispatch_version
                        or event.execution_plan_version != task.execution_plan_version
                        or event.queue_key != task.queue_key
                    ):
                        self.repository.discard(session, event)
                        continue
                    if task.status in ("created", "queued", "retry_wait") and (
                        (task.queue_deadline_at is not None and task.queue_deadline_at <= now)
                        or (task.total_deadline_at is not None and task.total_deadline_at <= now)
                    ):
                        # 扫描后才越过截止时间：本轮不发布，下一轮由恢复事务收尾。
                        continue
                    if task.status == "retry_wait" and (task.next_retry_at is None or task.next_retry_at > now):
                        continue
                    notification = self.broker.publish(session, event)
                    self.repository.record_published(
                        session,
                        event=event,
                        task=task,
                        notification_id=notification.notification_id,
                        published_at=now,
                        queue_task=task.status in ("created", "retry_wait"),
                    )
                published += 1
            except Exception as exc:
                self.logger.exception("通知发布失败，保留待投递事件：event_id=%s", event_id)
                try:
                    self.repository.record_failure(
                        event_id,
                        retry_seconds=self.settings.dispatch_retry_seconds,
                        error=f"通知发布失败（{type(exc).__name__}）",
                    )
                except Exception:
                    # 数据库不可用时原 pending 事件仍在，下一轮重新扫描。
                    self.logger.exception("无法登记投递失败：event_id=%s", event_id)
        return published
