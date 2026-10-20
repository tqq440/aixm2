"""M08 待投递记录的查询与失败留档；投递资格由 Service 裁决。"""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.data.database.database import Database, database_now
from backend.data.tasks.task_models import DispatchEvent, TaskRecord


class DispatchRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def due_ids(self, *, limit: int = 20) -> list[str]:
        with self.database.session_factory() as session:
            return list(session.scalars(
                select(DispatchEvent.event_id)
                .where(DispatchEvent.status == "pending", DispatchEvent.available_at <= database_now(session))
                .order_by(DispatchEvent.available_at, DispatchEvent.created_at, DispatchEvent.event_id)
                .limit(limit)
            ).all())

    def pending_error(self) -> str | None:
        """保留补投等待期内的失败摘要，不因本轮没有到期事件而误报正常。"""
        with self.database.session_factory() as session:
            return session.scalar(select(DispatchEvent.last_error).where(
                DispatchEvent.status == "pending", DispatchEvent.last_error.is_not(None),
            ).order_by(DispatchEvent.available_at, DispatchEvent.event_id).limit(1))

    def load(self, session: Session, event_id: str) -> tuple[DispatchEvent, TaskRecord] | None:
        row = session.execute(
            select(DispatchEvent, TaskRecord)
            .join(TaskRecord, TaskRecord.task_id == DispatchEvent.task_id)
            .where(DispatchEvent.event_id == event_id)
        ).one_or_none()
        return (row[0], row[1]) if row else None

    def discard(self, session: Session, event: DispatchEvent) -> None:
        event.status = "discarded"
        session.flush()

    def record_published(
        self,
        session: Session,
        *,
        event: DispatchEvent,
        task: TaskRecord,
        notification_id: str,
        published_at: datetime,
        queue_task: bool,
    ) -> None:
        """落地 Service 在同一写锁内作出的投递与排队裁决。"""
        event.notification_id = notification_id
        event.status = "published"
        event.published_at = published_at
        event.publish_attempts += 1
        event.last_error = None
        if queue_task:
            task.status = "queued"
            task.next_retry_at = None
            task.updated_at = published_at
        session.flush()

    def record_failure(self, event_id: str, *, retry_seconds: float, error: str) -> None:
        with self.database.transaction(immediate=True) as session:
            event = session.get(DispatchEvent, event_id)
            # 若提交已成功但应答丢失，不把已发布事件变回 pending。
            if event is not None and event.status == "pending":
                event.publish_attempts += 1
                event.last_error = error[:256]
                event.available_at = database_now(session) + timedelta(seconds=retry_seconds)
