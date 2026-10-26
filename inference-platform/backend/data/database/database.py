"""数据库连接和事务边界；不隐式创建或修改数据表。"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import URL, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from backend.config.settings.app_settings import AppSettings
from backend.common.storage.recovery_guard import require_active_database


def build_database_url(settings: AppSettings) -> URL:
    return URL.create("sqlite+pysqlite", database=str(settings.database_path))


def database_now(session: Session) -> datetime:
    """由 SQLite 统一裁决时间；数据库中的时间始终为无时区 UTC。"""
    value = session.scalar(text("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')"))
    return datetime.fromisoformat(value)


class Database:
    def __init__(self, settings: AppSettings) -> None:
        require_active_database(settings.database_path)
        self.common_execution_enabled = settings.common_execution_enabled
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            build_database_url(settings),
            connect_args={"check_same_thread": False, "timeout": 5},
            pool_pre_ping=True,
        )
        event.listen(self.engine, "connect", self._enable_foreign_keys)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        if self.common_execution_enabled:
            from backend.data.invocations.lifecycle_events import (
                discard_lifecycle_events, persist_lifecycle_events, record_lifecycle_events,
            )
            event.listen(self.session_factory, "before_flush", record_lifecycle_events)
            event.listen(self.session_factory, "after_flush_postexec", persist_lifecycle_events)
            event.listen(self.session_factory, "after_rollback", discard_lifecycle_events)

    @staticmethod
    def _enable_foreign_keys(connection, _record) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    def check_connection(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[Session]:
        """每次操作独立会话，成功提交，异常回滚，始终关闭。"""
        with self.session_factory.begin() as session:
            if immediate:
                # 先取得 SQLite 写锁再读取状态，避免多个进程同时根据旧状态裁决。
                session.execute(text("BEGIN IMMEDIATE"))
            yield session

    def dispose(self) -> None:
        self.engine.dispose()
