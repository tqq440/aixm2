"""Node 独立 Alembic 环境，无平台 AppSettings 或 Base 元数据。"""

from alembic import context
from sqlalchemy import create_engine, pool

from backend.config.host_node.node_settings import NodeSettings
from backend.data.host_node.node_database import check_node_database, node_database_url
from backend.data.host_node.node_models import HostNodeBase
from backend.data.host_node.execution_models import HostNodeExecution


settings = context.config.attributes.get("node_settings")
if not isinstance(settings, NodeSettings):
    raise TypeError("Node 迁移必须显式传入 NodeSettings。")

if context.is_offline_mode():
    raise RuntimeError("Node 首版仅提供显式本机 init-db。")

engine = create_engine(node_database_url(settings), poolclass=pool.NullPool)
try:
    with engine.connect() as connection:
        check_node_database(connection, initialized=False)
        connection.commit()
        context.configure(connection=connection, target_metadata=HostNodeBase.metadata,
                          version_table="host_node_alembic_versions", render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
