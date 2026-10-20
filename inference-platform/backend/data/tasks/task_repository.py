"""任务相关持久化；幂等冲突裁决与文件处理由 Service 负责。"""

from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.data.database.database import Database, database_now
from backend.data.tasks.task_models import DispatchEvent, IdempotencyRecord, TaskRecord
from backend.data.llm.online_models import OnlineRequestRecord


class TaskRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def find_idempotent(self, caller_id: str, key: str) -> tuple[str, TaskRecord] | None:
        statement = (
            select(IdempotencyRecord.request_fingerprint, TaskRecord)
            .join(TaskRecord, TaskRecord.task_id == IdempotencyRecord.task_id)
            .where(IdempotencyRecord.caller_id == caller_id, IdempotencyRecord.idempotency_key == key)
        )
        with self.database.session_factory() as session:
            row = session.execute(statement).one_or_none()
            return (row[0], row[1]) if row is not None else None

    def create_with_event(
        self,
        *,
        task: TaskRecord,
        idempotency_key: str,
        request_fingerprint: str,
        event: DispatchEvent,
        before_create: Callable[[datetime, int], None] | None = None,
        admission_check: Callable[[Session], None] | None = None,
        after_create: Callable[[Session], None] | None = None,
    ) -> None:
        """同一事务写入三张表；唯一约束与外键失败均整体回滚。"""
        with self.database.transaction(immediate=True) as session:
            if before_create is not None or admission_check is not None:
                # Service 在同一写锁内根据权威时间、在途数量作受理裁决。
                # 已有幂等记录仍交由唯一约束及 Service 的重读路径处理。
                existing = session.scalar(select(IdempotencyRecord.id).where(
                    IdempotencyRecord.caller_id == task.caller_id,
                    IdempotencyRecord.idempotency_key == idempotency_key,
                ))
                if existing is None:
                    if admission_check is not None:
                        admission_check(session)
                    if before_create is not None:
                        count = session.scalar(select(func.count()).select_from(TaskRecord).where(
                            TaskRecord.status.in_(("created", "queued", "running", "retry_wait", "cancel_requested")),
                        ))
                        online_count = session.scalar(select(func.count()).select_from(OnlineRequestRecord).where(
                            OnlineRequestRecord.state.in_(("accepted", "waiting_instance", "streaming")),
                        ))
                        before_create(database_now(session), int(count or 0) + int(online_count or 0))
            session.add(task)
            session.flush()  # 没有 ORM relationship，显式保证主记录先于外键记录写入。
            if after_create is not None:
                after_create(session)
            session.add(IdempotencyRecord(
                id=uuid4().hex,
                caller_id=task.caller_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                task_id=task.task_id,
                created_at=task.created_at,
            ))
            session.add(event)
            session.flush()

    def get_task(self, caller_id: str, task_id: str) -> TaskRecord | None:
        with self.database.session_factory() as session:
            return session.scalar(select(TaskRecord).where(
                TaskRecord.caller_id == caller_id, TaskRecord.task_id == task_id,
            ))

    def list_tasks(self, caller_id: str, *, page: int, page_size: int,
                   task_type: str | None = "asr") -> tuple[list[TaskRecord], int]:
        # 未传类型保留 ASR 列表；维护场景显式传 None 才读取全部类型。
        conditions = [TaskRecord.caller_id == caller_id]
        if task_type is not None:
            conditions.append(TaskRecord.task_type == task_type)
        with self.database.session_factory() as session:
            total = session.scalar(select(func.count()).select_from(TaskRecord).where(*conditions))
            tasks = session.scalars(
                select(TaskRecord)
                .where(*conditions)
                .order_by(TaskRecord.created_at.desc(), TaskRecord.task_id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return list(tasks), int(total or 0)

    def referenced_input_refs(self) -> set[str]:
        """返回文件引用快照；维护调用者必须持有与任务提交共用的存储锁。"""
        with self.database.session_factory() as session:
            return set(session.scalars(select(TaskRecord.input_ref)).all())
