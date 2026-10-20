"""投递进程生命周期服务：实际状态报告和持久化投递服务的装配。"""

import logging
import os
from time import monotonic
from uuid import uuid4

from filelock import FileLock, Timeout
from sqlalchemy.exc import SQLAlchemyError

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.data.database.database import Database
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.service.dispatch.dispatch_service import DispatchService


class DispatcherRuntime:
    def __init__(self, settings: AppSettings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger
        self.session_id = uuid4().hex
        self.database = Database(settings)
        self.reports = RuntimeRepository(self.database)
        self.dispatcher = DispatchService(self.database, settings, logger)
        self.lock = FileLock(str(settings.database_path) + ".dispatcher.lock", timeout=0)
        self._locked = False
        self._last_report = 0.0

    def start(self) -> None:
        try:
            self.lock.acquire()
            self._locked = True
        except Timeout as exc:
            raise AppError("DISPATCHER_ALREADY_RUNNING", "此数据库已有投递器运行，请勿重复启动。", 409) from exc
        self._report("idle")

    def _report(self, state: str, error: str | None = None) -> None:
        self.reports.report(component_id="dispatcher", session_id=self.session_id, pid=os.getpid(),
                            state=state, last_error=error)
        self._last_report = monotonic()

    def tick(self) -> int:
        try:
            published = self.dispatcher.tick()
            if published or monotonic() - self._last_report >= self.settings.runtime_heartbeat_seconds:
                failure = self.dispatcher.pending_error()
                self._report("error" if failure else "idle", failure)
            return published
        except SQLAlchemyError:
            self.logger.exception("投递暂不可用，将继续检查未投递事件")
            try:
                self._report("error", "投递存储暂不可用，请检查 dispatcher.log。")
            except SQLAlchemyError:
                pass  # 不伪造新心跳；API 将把最后一条报告判断为 stale。
            return 0

    def close(self) -> None:
        try:
            if self._locked:
                try:
                    self._report("stopped")
                except SQLAlchemyError:
                    self.logger.exception("投递器停止报告未能保存")
        finally:
            self.database.dispose()
            if self._locked:
                self.lock.release()
                self._locked = False
