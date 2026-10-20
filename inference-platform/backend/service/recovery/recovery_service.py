"""M08 到期恢复；逻辑执行权失效不代表本机子进程已经结束。"""

from backend.config.settings.app_settings import AppSettings
from backend.data.database.database import Database
from backend.data.execution.execution_repository import ExecutionRepository
from backend.data.recovery.recovery_repository import RecoveryRepository
from backend.schema.execution.execution_schema import FailureInfo
from backend.service.execution.execution_service import failure_fingerprint
from backend.service.recovery.failure_policy import FailurePolicy, TERMINAL_STATES, WAITING_STATES, waiting_timeout
from backend.service.asr.remote_service import RemoteAsrService


class RecoveryService:
    def __init__(self, database: Database, settings: AppSettings):
        self.repository = RecoveryRepository(database)
        self.execution = ExecutionRepository(database)
        self.policy = FailurePolicy(settings)
        self.remote = RemoteAsrService(database, settings)

    def tick(self) -> int:
        return sum(self.recover_task(task_id) for task_id in self.repository.due_ids(limit=20))

    def recover_task(self, task_id: str) -> bool:
        # SQLite 写锁内重新读取状态/令牌/版本/时间；并发扫描只有首个有效转换能成功。
        with self.execution.lock_task(task_id) as tx:
            task = tx.task
            if task is None or task.status in TERMINAL_STATES:
                return False
            expired_lease = task.lease_expires_at is not None and task.lease_expires_at <= tx.now
            expired_attempt = task.attempt_deadline_at is not None and task.attempt_deadline_at <= tx.now
            total_error = waiting_timeout(tx)
            if task.status == "cancel_requested":
                if not (expired_lease or expired_attempt or total_error):
                    return False
                tx.finish_attempt(outcome="cancelled")
                tx.finish(status="cancelled", result=None, error=None, fingerprint=None)
                return True
            if task.status in WAITING_STATES:
                if total_error is None:
                    return False
                tx.finish(status="failed", result=None, error=total_error.model_dump(), fingerprint=None)
                if self.remote.is_remote(task):
                    self.remote.finish(tx, confirmed=True)
                return True
            if task.status != "running":
                return False
            error = total_error
            if error is None and expired_attempt:
                error = FailureInfo(code="ATTEMPT_TIMEOUT", message="本次执行超过单次处理期限。")
            if error is None and expired_lease:
                error = FailureInfo(code="LEASE_EXPIRED", message="执行租约已到期，原 Worker 失去提交权。")
            if error is None:
                return False
            self.policy.apply(tx, error, failure_fingerprint(error))
            if self.remote.is_remote(task):
                self.remote.finish(tx, confirmed=False)
                task.error_json = FailureInfo(code="REMOTE_ASR_RESULT_UNKNOWN",
                    message=f"远端请求结果未确认（{error.code}）；不会自动重发，须人工核对。").model_dump()
            return True
