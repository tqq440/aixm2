"""通过项目配置连接数据库；所有结构变更均由迁移版本管理。"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from backend.config.settings.app_settings import AppSettings, load_settings
from backend.data.database.base import Base
from backend.data.monitoring.budget_models import MonitoringPermit
from backend.data.execution.attempt_models import ExecutionAttempt
from backend.data.database.database import build_database_url
from backend.data.broker.broker_models import NotificationRecord
from backend.data.runtime.runtime_models import RuntimeComponent
from backend.data.models.model_models import ModelRecord, ModelVersionRecord
from backend.data.llm.online_models import OnlineRequestRecord
from backend.data.nodes.node_models import ComputeNode, InstanceSession, ServiceInstance
from backend.data.agent.agent_models import AgentControl, ManagedInstanceSession
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.asr.catalog_models import AsrCatalogRevision, AsrResourceObservation
from backend.data.asr.routing_models import AsrRoutingRevision, AsrTaskRoute
from backend.data.asr.target_models import (
    AsrCallPermit, AsrCapacityChange, AsrCapacityGroup, AsrComputeTarget, AsrTargetRevision, AsrTargetWorkerSession,
)
from backend.data.tasks.task_models import DispatchEvent, IdempotencyRecord, TaskRecord
from backend.data.catalog.catalog_models import (
    InferenceCatalogEvent, InferenceResourceGroup, InferenceService, InferenceServiceRevision, InferenceTargetCheck,
)
from backend.data.invocations.invocation_models import (
    InferenceApplicationBudget, InferenceCpuProfile, InferenceInvocation, InferenceReservationItem,
    InferenceSchedulerCursor, InferenceTaskEvent,
)
from backend.data.queue.queue_models import InferenceQueueOutbox, InferenceQueueState
from backend.data.invocations.node_dispatch_models import InferenceNodeDispatch
from backend.data.gateway.key_models import GatewayKey, GatewayNodeObservation

# 显式导入业务模型以注册元数据，不自动扫描、导入整个 backend 包。

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings = config.attributes.get("settings")
if settings is None:
    settings = load_settings()
if not isinstance(settings, AppSettings):
    raise TypeError("Alembic config.attributes['settings'] 必须是 AppSettings。")

database_url = build_database_url(settings)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """输出 SQL，不连接或创建数据库文件。"""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """连接配置指定的 SQLite 文件，按版本执行迁移。"""
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
