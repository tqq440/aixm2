"""专属数据库和进程锁；应用启动只检查结构，不建表。"""

from contextlib import contextmanager

from filelock import FileLock, Timeout
from sqlalchemy import URL, create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from backend.common.exceptions.app_error import StartupError


NODE_REVISION = "20261021_node_0004"


def node_database_url(settings):
    return URL.create("sqlite+pysqlite", database=str(settings.database_path))


def check_node_database(connection, *, initialized):
    tables = set(inspect(connection).get_table_names())
    expected = {"host_node_identities", "host_node_services", "host_node_service_revisions",
                "host_node_instances", "host_node_events", "host_node_alembic_versions",
                "host_node_runtime_revisions", "host_node_operations", "host_node_resource_snapshots", "host_node_executions"}
    if tables - expected:
        raise StartupError("NODE_DATABASE_NOT_ISOLATED：指定数据库包含非 Node 数据表。")
    if initialized:
        if tables != expected or connection.scalar(text("SELECT version_num FROM host_node_alembic_versions")) != NODE_REVISION:
            raise StartupError("NODE_MIGRATION_REQUIRED：请先显式执行 Node init-db。")


class NodeDatabase:
    def __init__(self, settings):
        if not settings.database_path.is_file():
            raise StartupError("NODE_MIGRATION_REQUIRED：Node 数据库尚未初始化。")
        self.path = settings.database_path
        # HTTP 工作线程共享同一进程所有权；不能使用默认线程本地锁上下文。
        self.lock = FileLock(str(self.path) + ".host-node.lock", timeout=0, thread_local=False)
        self.engine = create_engine(node_database_url(settings), connect_args={"check_same_thread": False, "timeout": 5})
        event.listen(self.engine, "connect", self._connect)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        try:
            with self.engine.connect() as connection:
                check_node_database(connection, initialized=True)
        except Exception:
            self.engine.dispose()
            raise

    @staticmethod
    def _connect(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    def acquire(self):
        try:
            self.lock.acquire(timeout=0)
        except Timeout:
            raise StartupError("NODE_ALREADY_RUNNING：同一 Node 状态库已有控制进程。") from None

    @contextmanager
    def transaction(self, *, immediate=False):
        with self.session_factory.begin() as session:
            if immediate:
                session.execute(text("BEGIN IMMEDIATE"))
            yield session

    def dispose(self):
        self.engine.dispose()
        self.lock.release()
