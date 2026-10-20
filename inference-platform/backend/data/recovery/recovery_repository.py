"""有界寻找过期候选；返回任务编号，不赋予候选任何状态修改权。"""

from sqlalchemy import and_, or_, select

from backend.data.database.database import Database, database_now
from backend.data.tasks.task_models import TaskRecord


class RecoveryRepository:
    def __init__(self, database: Database):
        self.database = database

    def due_ids(self, *, limit: int = 20) -> list[str]:
        with self.database.session_factory() as session:
            now = database_now(session)
            waiting = and_(TaskRecord.status.in_(("created", "queued", "retry_wait")),
                           TaskRecord.queue_deadline_at <= now)
            executing = and_(TaskRecord.status.in_(("running", "cancel_requested")), or_(
                TaskRecord.lease_expires_at <= now, TaskRecord.attempt_deadline_at <= now,
            ))
            total = and_(TaskRecord.status.in_(("created", "queued", "retry_wait", "running", "cancel_requested")),
                         TaskRecord.total_deadline_at <= now)
            return list(session.scalars(select(TaskRecord.task_id).where(or_(waiting, executing, total))
                                        .order_by(TaskRecord.updated_at, TaskRecord.task_id).limit(limit)))
