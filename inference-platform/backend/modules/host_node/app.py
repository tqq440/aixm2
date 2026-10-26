"""独立 Node 应用工厂，不加载平台应用或模型。"""

from flask import Flask
from backend.service.host_node.monitor_budget_client import MonitorBudgetClient

from backend.common.logging.app_logging import close_logging, configure_logging
from backend.common.middleware.http_hooks import register_http_hooks
from backend.controller.host_node.node_controller import node_blueprint
from backend.data.host_node.node_database import NodeDatabase
from backend.service.host_node.node_service import NodeService
from backend.service.host_node.control_service import HostControlService
from backend.service.host_node.resource_service import ResourceService
from backend.service.host_node.execution_service import NodeExecutionService
from backend.service.host_node.http_adapter import NodeHttpAdapter
from backend.service.host_node.monitor_service import NodeMonitorService
from backend.service.host_node.ingress_service import NodeIngress
from backend.controller.host_node.ingress_controller import ingress_blueprint


def create_node_app(settings, *, clock=None, docker=None, business=None, sampler=None, execution_adapter=None, monitor_budget_client=None):
    database = NodeDatabase(settings)
    service = NodeService(database, settings, clock=clock)
    logger = None
    try:
        service.start()
        logger = configure_logging(settings.log_dir, settings.log_level, "host-node.log", process_scoped=True)
        app = Flask(__name__)
        app.json.ensure_ascii = False
        app.config.update(MAX_CONTENT_LENGTH=16_384, MAX_FORM_MEMORY_SIZE=16_384, MAX_FORM_PARTS=1)
        app.extensions["app_logger"] = logger
        app.extensions["host_node_service"] = service
        control = HostControlService(service, docker=docker, business=business)
        control.recover()
        app.extensions["host_node_control"] = control
        adapter=execution_adapter if execution_adapter is not None else NodeHttpAdapter(settings) if settings.business_enabled else None
        executions = NodeExecutionService(service, adapter=adapter)
        executions.recover()
        app.extensions["host_node_executions"] = executions
        app.extensions["host_node_monitor"] = NodeMonitorService(executions, budget_client=monitor_budget_client or MonitorBudgetClient(settings))
        app.extensions["host_node_resources"] = ResourceService(service, sampler=sampler)
        register_http_hooks(app)
        app.register_blueprint(node_blueprint)
        app.extensions["node_ingress"] = NodeIngress(settings)
        app.register_blueprint(ingress_blueprint)
        return app
    except Exception:
        service.close()
        if logger:
            close_logging(logger)
        raise


def close_node_app(app):
    try:
        try:app.extensions["host_node_monitor"].close()
        finally:app.extensions["host_node_service"].close()
    finally:
        close_logging(app.extensions["app_logger"])
