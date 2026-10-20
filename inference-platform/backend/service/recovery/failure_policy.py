"""M08 的有限重试裁决；只有明确列出的暂态故障允许再次执行。"""

from datetime import timedelta

from backend.config.settings.app_settings import AppSettings
from backend.data.execution.execution_repository import ExecutionTransaction
from backend.schema.execution.execution_schema import FailureInfo


RETRYABLE_FAILURE_CODES = frozenset({
    "WORKER_CHILD_EXITED", "WORKER_COMMUNICATION_FAILED", "WORKER_STOPPED",
    "LEASE_EXPIRED", "ATTEMPT_TIMEOUT", "STORAGE_TEMPORARILY_UNAVAILABLE",
})
WAITING_STATES = frozenset({"created", "queued", "retry_wait"})
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


def waiting_timeout(tx: ExecutionTransaction) -> FailureInfo | None:
    """首次受理确定的截止时间只用于等待态；执行中的排队期限不影响结果。"""
    task = tx.task
    if task is None:
        return None
    if task.total_deadline_at is not None and tx.now >= task.total_deadline_at:
        return FailureInfo(code="TOTAL_TIMEOUT", message="任务已超过总处理期限。")
    if task.status in WAITING_STATES and task.queue_deadline_at is not None and tx.now >= task.queue_deadline_at:
        return FailureInfo(code="QUEUE_TIMEOUT", message="任务已超过从首次受理起计算的排队期限。")
    return None


class FailurePolicy:
    def __init__(self, settings: AppSettings):
        self.settings = settings

    def apply(self, tx: ExecutionTransaction, error: FailureInfo, fingerprint: str) -> None:
        """调用方已裁决执行身份；历史、任务和重试事件在同一事务落盘。"""
        task = tx.task
        if task is None:
            raise RuntimeError("失败事务缺少任务")
        payload = error.model_dump(mode="json", by_alias=True)
        can_retry = (task.queue_key != "asr.remote-v2-sensevoice"
                     and error.code in RETRYABLE_FAILURE_CODES and task.attempt < task.max_attempts)
        retry_at = tx.now + timedelta(seconds=self.settings.retry_wait_seconds)
        final_error = payload
        if can_retry and task.total_deadline_at is not None and retry_at >= task.total_deadline_at:
            can_retry = False
            final_error = FailureInfo(code="TOTAL_TIMEOUT", message="任务剩余总期限不足以等待下一次执行。").model_dump()
        if can_retry and task.queue_deadline_at is not None and retry_at >= task.queue_deadline_at:
            can_retry = False
            final_error = FailureInfo(code="QUEUE_TIMEOUT", message="首次受理确定的排队期限不足以等待下一次执行。").model_dump()
        tx.finish_attempt(outcome="retry_wait" if can_retry else "failed", error=payload, fingerprint=fingerprint)
        if can_retry:
            tx.schedule_retry(available_at=retry_at,
                              error=payload, fingerprint=fingerprint)
        else:
            tx.finish(status="failed", result=None, error=final_error, fingerprint=fingerprint)
