"""应用工厂只组装基础组件与入口，不包含任务业务规则。"""

from flask import Flask
from backend.common.storage.recovery_guard import require_active_database
from backend.controller.monitoring.budget_controller import monitor_budget_blueprint
from backend.service.monitoring.budget_service import MonitorBudgetService
from sqlalchemy.exc import SQLAlchemyError

from backend.common.exceptions.app_error import StartupError
from backend.common.logging.app_logging import close_logging, configure_logging
from backend.common.middleware.http_hooks import register_http_hooks
from backend.config.settings.app_settings import AppSettings, load_settings
from backend.controller.health.health_controller import health_blueprint
from backend.controller.deployment.validation_controller import validation_blueprint
from backend.service.deployment.validation_issue_service import ValidationIssueLog
from backend.controller.tasks.task_controller import task_blueprint
from backend.controller.runtime.runtime_controller import runtime_blueprint
from backend.controller.llm.online_controller import online_blueprint
from backend.controller.nodes.node_controller import node_blueprint
from backend.controller.agent.agent_controller import agent_blueprint
from backend.controller.asr.remote_controller import remote_asr_blueprint
from backend.controller.asr.service_config_controller import asr_services_blueprint
from backend.controller.catalog.catalog_controller import catalog_blueprint
from backend.service.catalog.catalog_service import ServiceCatalog
from backend.service.invocations.invocation_service import InvocationService
from backend.service.invocations.node_bridge import NodeInvocationBridge
from backend.service.host_node.input_service import CentralMaterialSource
from backend.service.gateway.key_service import GatewayKeys
from backend.service.gateway.request_service import GatewayRequests
from backend.service.gateway.admin_service import GatewayAdministration
from backend.controller.gateway.gateway_controller import public_blueprint, gateway_admin_blueprint, protect_legacy
from backend.service.queue.queue_service import QueueService
from backend.controller.invocations.invocation_controller import invocation_blueprint
from backend.service.asr.service_config_service import AsrServiceConfiguration
from backend.service.asr.remote_service import RemoteAsrService
from backend.data.database.database import Database
from backend.data.files.upload_store import LocalUploadStore
from backend.data.tasks.task_repository import TaskRepository
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.service.health.health_service import HealthService
from backend.service.tasks.model_service import ModelService
from backend.service.tasks.task_service import TaskService
from backend.service.runtime.runtime_service import RuntimeService
from backend.service.llm.online_service import OnlineService
from backend.service.nodes.node_registry_service import NodeRegistryService
from backend.service.agent.agent_command_service import AgentCommandService


def create_app(settings: AppSettings | None = None) -> Flask:
    environment_settings = settings if settings is not None else load_settings(include_service_selection=False)
    settings = settings if settings is not None else load_settings()
    require_active_database(settings.database_path)
    app = Flask(__name__, static_folder=None)
    app.json.ensure_ascii = False
    app.config.update(
        DEBUG=False, APP_SETTINGS=settings,
        MAX_CONTENT_LENGTH=max(settings.asr_max_file_bytes, settings.vision_max_file_bytes) + 64 * 1024,
        # 解析器每次读 64 KiB，还需保留上一块的边界尾部；不能把内存上限设为一块。
        MAX_FORM_MEMORY_SIZE=128 * 1024, MAX_FORM_PARTS=8,
    )
    try:
        logger = configure_logging(settings.log_dir, settings.log_level, process_scoped=settings.common_execution_enabled)
    except OSError as exc:
        raise StartupError("日志目录不可写，请检查 INFERENCE_LOG_DIR 和目录权限。") from exc
    app.extensions["app_logger"] = logger
    database = None
    try:
        database = Database(settings)
        database.check_connection()
        if settings.public_api_enabled:
            from sqlalchemy import text
            with database.engine.connect() as connection:
                if connection.scalar(text("SELECT version_num FROM alembic_version")) not in {"20261021_0020", "20261022_0021"}:
                    raise StartupError("公共 API 需要显式完成 0020 迁移，不自动升级数据库。")
        if settings.monitor_token:
            from sqlalchemy import text
            with database.engine.connect() as connection:
                if connection.scalar(text("SELECT version_num FROM alembic_version")) != "20261022_0021":
                    raise StartupError("查询预算需要显式完成0021迁移，不自动升级数据库。")
    except StartupError:
        if database is not None:database.dispose()
        close_logging(logger)
        raise
    except (OSError, SQLAlchemyError) as exc:
        logger.exception("数据库初始化失败")
        if database is not None:
            database.dispose()
        close_logging(logger)
        raise StartupError("数据库无法连接，请检查 INFERENCE_DATABASE_PATH 和目录权限。") from exc
    app.extensions["monitor_budget"] = MonitorBudgetService(database)
    app.register_blueprint(monitor_budget_blueprint)
    app.extensions["database"] = database
    app.extensions["validation_issue_log_factory"] = lambda: ValidationIssueLog(settings.log_dir/"validation")
    app.extensions["service_catalog"] = ServiceCatalog(database, settings)
    app.extensions["queue_service"] = QueueService(database, settings, logger=logger)
    app.extensions["invocation_service"] = app.extensions["queue_service"].core
    app.extensions["node_invocation_bridge"] = NodeInvocationBridge(app.extensions["invocation_service"],
        material_source=CentralMaterialSource(app.extensions["invocation_service"]))
    configuration = AsrServiceConfiguration(database, settings, environment_settings)
    try:
        configuration.register_running()
    except Exception:
        database.dispose()
        close_logging(logger)
        raise
    app.extensions["asr_service_configuration"] = configuration
    app.extensions["health_service"] = HealthService(database)
    models = ModelService(settings, database)
    app.extensions["model_service"] = models
    app.extensions["online_service"] = OnlineService(settings, database, models, logger)
    app.extensions["task_service"] = TaskService(
        settings, TaskRepository(database), LocalUploadStore(settings.upload_dir), models, logger,
    )
    app.extensions["runtime_service"] = RuntimeService(RuntimeRepository(database), settings)
    app.extensions["node_registry_service"] = NodeRegistryService(database, settings)
    app.extensions["agent_service"] = AgentCommandService(database, settings)
    app.extensions["remote_asr_service"] = RemoteAsrService(database, settings)
    register_http_hooks(app)
    app.extensions["gateway_keys"] = GatewayKeys(app.extensions["invocation_service"])
    app.extensions["gateway_requests"] = GatewayRequests(app.extensions["invocation_service"], app.extensions["node_invocation_bridge"])
    app.extensions["gateway_administration"] = GatewayAdministration(app.extensions["invocation_service"], app.extensions["node_invocation_bridge"])
    app.before_request(protect_legacy)
    app.register_blueprint(public_blueprint)
    app.register_blueprint(gateway_admin_blueprint)
    app.register_blueprint(health_blueprint)
    app.register_blueprint(validation_blueprint)
    app.register_blueprint(task_blueprint)
    app.register_blueprint(runtime_blueprint)
    app.register_blueprint(online_blueprint)
    app.register_blueprint(node_blueprint)
    app.register_blueprint(agent_blueprint)
    app.register_blueprint(remote_asr_blueprint)
    app.register_blueprint(asr_services_blueprint)
    app.register_blueprint(catalog_blueprint)
    app.register_blueprint(invocation_blueprint)
    logger.info("应用初始化完成（上传、任务结果与管理状态；ASR 由本机 Agent 控制）")
    return app


def close_app(app: Flask) -> None:
    """运行入口退出时释放连接及日志句柄；不在每个请求结束时销毁连接池。"""
    app.extensions["online_service"].close()
    app.extensions["queue_service"].close()
    app.extensions["database"].dispose()
    logger = app.extensions["app_logger"]
    logger.info("应用资源已释放")
    close_logging(logger)
