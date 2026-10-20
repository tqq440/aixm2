"""执行事务的持久化边界；Service 在持有 SQLite 写锁时作业务裁决。"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from backend.data.broker.broker_models import NotificationRecord
from backend.data.database.database import Database, database_now
from backend.data.execution.attempt_models import ExecutionAttempt
from backend.data.tasks.task_models import DispatchEvent, TaskRecord


@dataclass
class ExecutionTransaction:
    session: Session
    now: datetime
    task: TaskRecord | None
    notification: NotificationRecord | None

    def start(self, *, worker_id: str, lease_token: str, expires_at: datetime,
              attempt_deadline_at: datetime) -> None:
        task = self.task
        if task is None:
            raise RuntimeError("执行事务缺少任务")
        task.status = "running"
        task.attempt += 1
        task.lease_worker_id = worker_id
        task.lease_token = lease_token
        task.lease_expires_at = expires_at
        task.attempt_deadline_at = attempt_deadline_at
        task.next_retry_at = None
        task.finished_at = None
        task.error_json = None
        task.result_fingerprint = None
        task.started_at = self.now
        task.updated_at = self.now
        self.session.add(ExecutionAttempt(
            lease_token=lease_token, task_id=task.task_id, attempt=task.attempt,
            worker_id=worker_id, dispatch_version=task.dispatch_version,
            execution_plan_version=task.execution_plan_version,
            started_at=self.now, outcome="running",
        ))
        self.session.flush()

    def renew(self, *, expires_at: datetime) -> None:
        if self.task is None:
            raise RuntimeError("执行事务缺少任务")
        self.task.lease_expires_at = expires_at
        self.task.updated_at = self.now
        self.session.flush()

    def finish(self, *, status: str, result: dict[str, Any] | None,
               error: dict[str, Any] | None, fingerprint: str | None) -> None:
        task = self.task
        if task is None:
            raise RuntimeError("执行事务缺少任务")
        task.status = status
        task.result_json = result
        task.error_json = error
        task.result_fingerprint = fingerprint
        task.finished_at = self.now
        task.next_retry_at = None
        task.updated_at = self.now
        # 保留执行身份，支持同租约的最终提交重放；终态写保护由 Service 裁决。
        self.session.flush()

    def find_attempt(self, lease_token: str) -> ExecutionAttempt | None:
        return self.session.get(ExecutionAttempt, lease_token)

    def finish_attempt(self, *, outcome: str, error: dict[str, Any] | None = None,
                       fingerprint: str | None = None) -> None:
        if self.task is None or not self.task.lease_token:
            return
        attempt = self.find_attempt(self.task.lease_token)
        if attempt is not None:
            attempt.outcome = outcome
            attempt.finished_at = self.now
            attempt.error_json = error
            attempt.failure_fingerprint = fingerprint
            self.session.flush()

    def schedule_retry(self, *, available_at: datetime, error: dict[str, Any], fingerprint: str) -> None:
        if self.task is None:
            raise RuntimeError("执行事务缺少任务")
        task = self.task
        task.status = "retry_wait"
        task.error_json = error
        task.result_fingerprint = fingerprint
        task.dispatch_version += 1
        task.next_retry_at = available_at
        task.updated_at = self.now
        task.finished_at = None
        self.session.add(DispatchEvent(
            event_id=uuid4().hex, task_id=task.task_id, event_type="task.retry",
            dispatch_version=task.dispatch_version, execution_plan_version=task.execution_plan_version,
            queue_key=task.queue_key, available_at=available_at, created_at=self.now,
        ))
        self.session.flush()

    def request_cancellation(self) -> None:
        if self.task is None:
            raise RuntimeError("执行事务缺少任务")
        self.task.status = "cancel_requested"
        self.task.updated_at = self.now
        self.session.flush()


class ExecutionRepository:
    def __init__(self, database: Database):
        self.database = database

    @contextmanager
    def lock_task(self, task_id: str, *, notification_id: str | None = None) -> Iterator[ExecutionTransaction]:
        """读取、校验、变更在同一 immediate 事务内，异常时全部回滚。"""
        with self.database.transaction(immediate=True) as session:
            now = database_now(session)
            task = session.get(TaskRecord, task_id)
            notification = session.get(NotificationRecord, notification_id) if notification_id else None
            yield ExecutionTransaction(session=session, now=now, task=task, notification=notification)
