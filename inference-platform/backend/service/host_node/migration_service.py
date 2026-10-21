"""显式升级 Node 专属数据库；不读取或迁移平台配置。"""

from alembic import command
from alembic.config import Config
from filelock import FileLock, Timeout

from backend.common.exceptions.app_error import StartupError
from backend.config.host_node.node_settings import PROJECT_ROOT


def upgrade_node_database(settings):
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(settings.database_path) + ".host-node.lock", timeout=0):
            config = Config()
            config.set_main_option("script_location", str(PROJECT_ROOT / "node-migrations"))
            config.attributes["node_settings"] = settings
            command.upgrade(config, "head")
    except Timeout:
        raise StartupError("NODE_ALREADY_RUNNING：先正常关闭 Node，再迁移其状态库。") from None
