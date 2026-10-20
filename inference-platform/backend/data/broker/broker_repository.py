"""SQLite 通知持久化：接收所有权不代表任务执行租约。"""

from collections.abc import Callable
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.data.broker.broker_models import NotificationRecord
from backend.data.database.database import Database, database_now
from backend.data.tasks.task_models import DispatchEvent
from backend.schema.execution.execution_schema import Delivery


class BrokerRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def publish(self, session: Session, event: DispatchEvent) -> NotificationRecord:
        """在调用者的事务中发布；同一待投递事件至多对应一条通知。"""
        existing = session.scalar(select(NotificationRecord).where(NotificationRecord.event_id == event.event_id))
        if existing is not None:
            return existing
        now = database_now(session)
        notification = NotificationRecord(
            notification_id=uuid4().hex,
            event_id=event.event_id,
            task_id=event.task_id,
            queue_key=event.queue_key,
            dispatch_version=event.dispatch_version,
            execution_plan_version=event.execution_plan_version,
            visible_at=now,
            created_at=now,
        )
        session.add(notification)
        session.flush()
        return notification

    def receive(self, *, worker_id: str, queue_key: str, visibility_seconds: float,
                receive_check: Callable[[Session], bool] | None = None,
                candidate_check: Callable[[Session, NotificationRecord], bool] | None = None) -> Delivery | None:
        """一次原子领取一条；可见期到期仅更换消息令牌，不改任务状态。"""
        with self.database.transaction(immediate=True) as session:
            if receive_check is not None and not receive_check(session):
                return None
            now = database_now(session)
            candidates = session.scalars(
                select(NotificationRecord)
                .where(
                    NotificationRecord.queue_key == queue_key,
                    NotificationRecord.status.in_(("ready", "inflight")),
                    NotificationRecord.visible_at <= now,
                )
                .order_by(NotificationRecord.visible_at, NotificationRecord.created_at, NotificationRecord.notification_id)
            )
            record = next((candidate for candidate in candidates
                           if candidate_check is None or candidate_check(session, candidate)), None)
            if record is None:
                return None
            record.status = "inflight"
            record.delivery_owner = worker_id
            record.delivery_token = uuid4().hex
            record.visible_at = now + timedelta(seconds=visibility_seconds)
            record.deliveries += 1
            return Delivery(
                notification_id=record.notification_id,
                task_id=record.task_id,
                worker_id=worker_id,
                delivery_token=record.delivery_token,
                dispatch_version=record.dispatch_version,
                execution_plan_version=record.execution_plan_version,
                queue_key=record.queue_key,
            )

    def release(self, delivery: Delivery) -> bool:
        """失去执行资格时按本轮领取令牌归还；不改变任务次数或期限。"""
        with self.database.transaction(immediate=True) as session:
            record = session.get(NotificationRecord, delivery.notification_id)
            now = database_now(session)
            if (record is None or record.status != "inflight"
                    or record.delivery_owner != delivery.worker_id
                    or record.delivery_token != delivery.delivery_token
                    or record.visible_at <= now):
                return False
            record.status = "ready"
            record.delivery_owner = record.delivery_token = None
            record.visible_at = now
            return True

    def extend(self, delivery: Delivery, *, visibility_seconds: float) -> bool:
        with self.database.transaction(immediate=True) as session:
            now = database_now(session)
            record = session.get(NotificationRecord, delivery.notification_id)
            if (
                record is None
                or record.status != "inflight"
                or record.delivery_owner != delivery.worker_id
                or record.delivery_token != delivery.delivery_token
                or record.visible_at <= now
            ):
                return False
            record.visible_at = now + timedelta(seconds=visibility_seconds)
            return True

    def acknowledge(self, delivery: Delivery) -> bool:
        """同一消息令牌的确认可重复；被重新领取后的旧令牌无权确认。"""
        with self.database.transaction(immediate=True) as session:
            record = session.get(NotificationRecord, delivery.notification_id)
            if (
                record is None
                or record.delivery_owner != delivery.worker_id
                or record.delivery_token != delivery.delivery_token
                or record.status not in ("inflight", "acked")
            ):
                return False
            if record.status != "acked":
                record.status = "acked"
                record.acked_at = database_now(session)
            return True
