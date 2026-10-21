"""独立 Node 的目录与可信报告；容器流程由 HostControlService 管理。"""

import hmac
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select

from backend.common.exceptions.app_error import AppError, StartupError
from backend.data.host_node.node_models import (
    HostNodeIdentity, HostNodeInstance, HostNodeService, HostNodeServiceRevision,
)
from backend.data.host_node.node_repository import NodeRepository
from backend.data.host_node.execution_models import has_unresolved_executions
from backend.schema.host_node.node_schema import (
    NodeEventQuery, NodeListQuery, NodeServicePath, NodeServiceReport, OpenNodeInstance, SaveNodeService,
)
from backend.utils.json.json_utils import canonical_json_sha256
from backend.utils.time.iso_time import iso_utc
from backend.utils.time.time_utils import as_utc


class NodeService:
    def __init__(self, database, settings, *, clock=None):
        self.database = database
        self.settings = settings
        self.clock = clock
        self.epoch_id = str(uuid4())
        self.started = False

    def _now(self, session):
        return as_utc(self.clock()).replace(tzinfo=None) if self.clock else NodeRepository.now(session)

    def start(self):
        self.database.acquire()
        try:
            with self.database.transaction(immediate=True) as session:
                row = session.get(HostNodeIdentity, 1)
                if row and row.node_id != self.settings.node_id:
                    raise StartupError("NODE_ID_MISMATCH：状态库已绑定另一 Node。")
                if row is None:
                    row = HostNodeIdentity(identity_id=1, node_id=self.settings.node_id)
                    session.add(row)
                now = self._now(session)
                row.epoch_id, row.started_at, row.stopped_at = self.epoch_id, now, None
                NodeRepository.event(session, now, "NODE_STARTED", details={"epochId": self.epoch_id})
            self.started = True
        except Exception:
            self.database.lock.release()
            raise

    def close(self):
        try:
            if self.started:
                with self.database.transaction(immediate=True) as session:
                    row = session.get(HostNodeIdentity, 1)
                    if row and row.epoch_id == self.epoch_id:
                        row.stopped_at = self._now(session)
                        NodeRepository.event(session, row.stopped_at, "NODE_STOPPED", details={"epochId": self.epoch_id})
                self.started = False
        finally:
            self.database.dispose()

    @staticmethod
    def _matches(header, token):
        if token is None or not isinstance(header, str) or len(header) > 512 or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].encode("utf-8"), token.get_secret_value().encode("utf-8"))

    def authorize(self, header, *, admin=False):
        if not self.started:
            raise AppError("NODE_NOT_RUNNING", "Node 控制进程已经关闭。", 503)
        if self._matches(header, self.settings.admin_token):
            return
        if not admin and self._matches(header, self.settings.observer_token):
            return
        raise AppError("NODE_UNAUTHORIZED", "Node 凭据无效或权限不足。", 401)

    def authorize_report(self, service_id, header):
        if not self.started:
            raise AppError("NODE_NOT_RUNNING", "Node 控制进程已经关闭。", 503)
        service_id = NodeServicePath(service_id=service_id).service_id
        with self.database.transaction() as session:
            row = session.get(HostNodeService, service_id)
            token = None if row is None else self.settings.report_tokens.get(row.config_json.get("reportTokenEnv"))
            if not self._matches(header, token):
                raise AppError("NODE_UNAUTHORIZED", "Node 凭据无效或权限不足。", 401)

    @staticmethod
    def _service(session, service_id):
        service_id = NodeServicePath(service_id=service_id).service_id
        row = session.get(HostNodeService, service_id)
        if row is None:
            raise AppError("NODE_SERVICE_NOT_FOUND", "服务未登记。", 404)
        return row

    def _require_owner(self, session):
        identity = session.get(HostNodeIdentity, 1)
        if not self.started or not self.database.lock.is_locked or identity is None or identity.epoch_id != self.epoch_id:
            raise AppError("NODE_NOT_RUNNING", "当前 Node 已不持有控制会话。", 503)

    def save_service(self, service_id, command):
        service_id = NodeServicePath(service_id=service_id).service_id
        command = SaveNodeService.model_validate(command)
        config = command.config.model_dump(mode="json", by_alias=True)
        revision = canonical_json_sha256(config)
        reference = command.config.report_token_env
        if reference and reference not in self.settings.report_tokens:
            raise AppError("NODE_REPORT_CREDENTIAL_MISSING", "尚未为该服务配置独立上报凭据。", 409)
        with self.database.transaction(immediate=True) as session:
            self._require_owner(session)
            row = session.get(HostNodeService, service_id)
            if (row is None and command.expected_version is not None) or (row and row.version != command.expected_version):
                raise AppError("NODE_CONFIG_STALE", "服务配置已变化，请刷新版本。", 409)
            if row and row.config_revision == revision:
                return self._snapshot(row, session.get(HostNodeInstance, row.current_instance_id) if row.current_instance_id else None, self._now(session))
            if row and has_unresolved_executions(session, service_id):
                raise AppError("NODE_EXECUTIONS_UNCONFIRMED", "该服务仍有未确认调用，不能改写配置。", 409)
            if reference:
                for other in session.scalars(select(HostNodeService).where(HostNodeService.service_id != service_id)):
                    if other.config_json.get("reportTokenEnv") == reference:
                        raise AppError("NODE_REPORT_CREDENTIAL_SHARED", "每个服务必须使用独立上报凭据。", 409)
            instance = session.get(HostNodeInstance, row.current_instance_id) if row and row.current_instance_id else None
            if (row and row.control_json and row.control_json.get("activeOperationId")) or (
                    instance and instance.reported_state != "stopped" and instance.exit_confirmed_at is None):
                raise AppError("NODE_INSTANCE_UNCONFIRMED", "当前业务实例尚未报告停止，不能改写其配置。", 409)
            now = self._now(session)
            if row is None:
                row = HostNodeService(service_id=service_id, config_revision=revision, config_json=config,
                    version=1, generation=0, created_at=now, updated_at=now)
                session.add(row)
                session.flush()
            else:
                row.config_revision, row.config_json = revision, config
                row.version += 1
                row.updated_at = now
                row.current_instance_id = None
                if row.runtime_revision:
                    row.runtime_revision = None
                    row.control_json = {}
                    row.control_version += 1
            existing = session.scalar(select(HostNodeServiceRevision).where(
                HostNodeServiceRevision.service_id == service_id, HostNodeServiceRevision.revision == revision))
            if existing is None:
                session.add(HostNodeServiceRevision(revision_id=str(uuid4()), service_id=service_id,
                    revision=revision, config_json=config, created_at=now))
            NodeRepository.event(session, now, "SERVICE_CONFIGURED", service=row,
                                 details={"version": row.version, "kind": config["kind"]})
            return self._snapshot(row, None, now)

    def open_instance(self, service_id, command):
        command = OpenNodeInstance.model_validate(command)
        instance_id = str(command.instance_id)
        with self.database.transaction(immediate=True) as session:
            self._require_owner(session)
            row = self._service(session, service_id)
            if row.runtime_revision:
                raise AppError("NODE_CONTROL_REQUIRED", "受管容器的启动身份由控制操作创建。", 409)
            if row.config_json["reportProtocol"] != "lifecycle-v1":
                raise AppError("NODE_REPORT_UNSUPPORTED", "此服务没有声明状态上报能力。", 409)
            if row.config_revision != command.config_revision:
                raise AppError("NODE_CONFIG_STALE", "实例修订与当前配置不符。", 409)
            current = session.get(HostNodeInstance, row.current_instance_id) if row.current_instance_id else None
            if current and current.instance_id == instance_id and current.generation == command.expected_generation + 1:
                return self._snapshot(row, current, self._now(session))
            if row.generation != command.expected_generation:
                raise AppError("NODE_GENERATION_STALE", "启动代次已变化。", 409)
            if has_unresolved_executions(session, service_id):
                raise AppError("NODE_EXECUTIONS_UNCONFIRMED", "旧实例仍有未确认调用，不能替换身份。", 409)
            if current and current.reported_state != "stopped":
                raise AppError("NODE_INSTANCE_UNCONFIRMED", "旧业务实例未报告停止，不能替换启动身份。", 409)
            if session.get(HostNodeInstance, instance_id):
                raise AppError("NODE_INSTANCE_REUSED", "启动实例编号已经使用。", 409)
            now = self._now(session)
            row.generation += 1
            row.current_instance_id, row.updated_at = instance_id, now
            instance = HostNodeInstance(instance_id=instance_id, service_id=row.service_id,
                config_revision=row.config_revision, generation=row.generation, sequence=0,
                reported_state="unknown", model_ready=False, accepting_requests=False, health="unknown", created_at=now)
            session.add(instance)
            NodeRepository.event(session, now, "INSTANCE_BOUND", service=row, instance=instance,
                                 details={"generation": row.generation, "startsContainer": False})
            return self._snapshot(row, instance, now)

    def report(self, service_id, command):
        command = NodeServiceReport.model_validate(command)
        normalized = command.model_dump(mode="json", by_alias=True)
        for key in ("controlOperationId", "controlAction"):
            if normalized[key] is None:
                del normalized[key]
        digest = canonical_json_sha256(normalized)
        with self.database.transaction(immediate=True) as session:
            self._require_owner(session)
            row = self._service(session, service_id)
            instance = session.get(HostNodeInstance, row.current_instance_id) if row.current_instance_id else None
            if instance is None or instance.instance_id != str(command.instance_id) or instance.config_revision != command.config_revision:
                raise AppError("NODE_REPORT_INSTANCE_STALE", "报告不属于当前启动实例或配置修订。", 409)
            if command.sequence <= instance.sequence:
                if command.sequence == instance.sequence and digest == instance.report_hash:
                    return {"duplicate": True, "service": self._snapshot(row, instance, self._now(session))}
                raise AppError("NODE_REPORT_SEQUENCE_STALE", "报告序号过时或相同序号内容冲突。", 409)
            if instance.reported_state == "stopped" and command.state != "stopped":
                raise AppError("NODE_REPORT_TERMINAL", "已停止实例不能重新声明运行。", 409)
            previous = (instance.reported_state, instance.health, instance.model_ready, instance.accepting_requests,
                        instance.error_code, instance.control_report_json)
            now = self._now(session)
            instance.sequence, instance.report_hash = command.sequence, digest
            instance.reported_state, instance.health = command.state, command.health
            instance.model_ready, instance.accepting_requests = command.model_ready, command.accepting_requests
            instance.may_have_accepted = bool(instance.may_have_accepted or command.accepting_requests
                or (command.queued_tasks is not None and command.queued_tasks > 0)
                or (command.active_tasks is not None and command.active_tasks > 0))
            instance.queued_tasks, instance.active_tasks, instance.error_code = command.queued_tasks, command.active_tasks, command.error_code
            instance.reported_at = as_utc(command.reported_at).replace(tzinfo=None) if command.reported_at else None
            instance.received_at, instance.node_epoch_id = now, self.epoch_id
            instance.control_report_json = {"operationId": str(command.control_operation_id), "action": command.control_action} if command.control_operation_id else None
            current = (instance.reported_state, instance.health, instance.model_ready, instance.accepting_requests,
                       instance.error_code, instance.control_report_json)
            if previous != current or command.sequence == 1:
                NodeRepository.event(session, now, "SERVICE_STATE_REPORTED", source="service", service=row, instance=instance,
                    details={"state": command.state, "health": command.health, "modelReady": command.model_ready,
                             "acceptingRequests": command.accepting_requests, "sequence": command.sequence,
                             "errorCode": command.error_code, "control": instance.control_report_json})
            return {"duplicate": False, "service": self._snapshot(row, instance, now)}

    def _snapshot(self, row, instance, now):
        issues = []
        if row.config_json["reportProtocol"] == "lifecycle-v1" and row.config_json.get("reportTokenEnv") not in self.settings.report_tokens:
            issues.append("REPORT_CREDENTIAL_MISSING")
        current_epoch = bool(instance and instance.node_epoch_id == self.epoch_id)
        age = (now - instance.received_at).total_seconds() if instance and instance.received_at else None
        fresh = current_epoch and age is not None and 0 <= age < self.settings.stale_seconds
        if row.config_json["reportProtocol"] == "none":
            issues.append("LIFECYCLE_REPORT_UNSUPPORTED")
        elif instance is None:
            issues.append("INSTANCE_NOT_BOUND")
        elif instance.received_at is None:
            issues.append("REPORT_NOT_RECEIVED")
        elif not current_epoch:
            issues.append("REPORT_AFTER_NODE_RESTART_REQUIRED")
        elif not fresh:
            issues.append("REPORT_STALE")
        control = row.control_json or {}
        container_stamp = control.get("containerObservedAt")
        container_age = (now - as_utc(datetime.fromisoformat(container_stamp)).replace(tzinfo=None)).total_seconds() if container_stamp else None
        container_fresh = bool(container_age is not None and 0 <= container_age < self.settings.stale_seconds
            and control.get("containerObservedEpoch") == self.epoch_id and not control.get("containerError"))
        container_state = control.get("containerState", "unknown") if container_fresh else "unknown"
        runtime_ready = not row.runtime_revision or container_state == "running"
        accepting = bool(fresh and instance.accepting_requests and instance.model_ready and instance.health == "healthy"
                         and runtime_ready and not control.get("gateClosed", False))
        if row.runtime_revision and not container_fresh:
            issues.append("CONTAINER_OBSERVATION_REQUIRED")
        if control.get("gateClosed"):
            issues.append("NODE_ADMISSION_CLOSED")
        upstream=row.config_json.get("upstream")
        if upstream and upstream["query"]["protocol"]=="none":
            issues.extend(("PROGRESS_INTERFACE_NOT_PROVIDED","ACTUAL_DEVICE_INTERFACE_NOT_PROVIDED"))
        proxy=bool(self.settings.business_enabled and upstream and row.config_json.get("internalBaseUrl"))
        if proxy and upstream.get("tokenEnv") and upstream["tokenEnv"] not in self.settings.upstream_tokens:
            proxy=False;issues.append("UPSTREAM_CREDENTIAL_MISSING")
        if proxy and upstream["mode"]=="async" and (upstream["query"]["protocol"]=="none" or not (upstream["query"].get("resultPath") or upstream["query"].get("resultInStatus"))):
            proxy=False;issues.append("COMPLETION_UNAVAILABLE")
        return {"serviceId": row.service_id, "nodeId": self.settings.node_id, "name": row.config_json["name"],
            "kind": row.config_json["kind"], "modelId": row.config_json["modelId"], "version": row.version,
            "configRevision": row.config_revision, "generation": row.generation,
            "instanceId": instance.instance_id if instance else None, "sequence": instance.sequence if instance else 0,
            "state": ("stopped" if row.runtime_revision and container_state == "exited" else
                      (instance.reported_state if fresh and runtime_ready else "unknown")),
            "reportedState": instance.reported_state if instance else None,
            "health": instance.health if fresh else "unknown", "reportedHealth": instance.health if instance else None,
            "modelReady": bool(fresh and instance.model_ready and runtime_ready), "acceptingRequests": accepting,
            "reportedAcceptingRequests": instance.accepting_requests if instance else None,
            "queuedTasks": instance.queued_tasks if instance else None, "activeTasks": instance.active_tasks if instance else None,
            "reportFresh": fresh, "lastReceivedAt": iso_utc(instance.received_at) if instance else None,
            "reportedAt": iso_utc(instance.reported_at) if instance else None, "evaluatedAt": iso_utc(now),
            "lastErrorCode": instance.error_code if instance else None,
            "containerState": container_state, "actualExecutionDevice": None, "progress": None,
            "control": {"managed": bool(row.runtime_revision), "runtimeRevision": row.runtime_revision,
                "controlVersion": row.control_version, "gateClosed": control.get("gateClosed", False),
                "currentVersion": control.get("currentVersion"), "activeOperationId": control.get("activeOperationId"),
                "containerFresh": container_fresh, "lastContainerState": control.get("containerState"),
                "containerError": control.get("containerError")},
            "executionReady": bool(proxy and accepting), "issues": issues,
            "capabilities": {"lifecycleReports": row.config_json["reportProtocol"] == "lifecycle-v1",
                "containerControl": bool(row.runtime_revision and self.settings.docker_enabled),
                "resourceMetrics": self.settings.metrics_enabled, "taskMonitoring": bool(upstream and upstream["query"]["protocol"]!="none"), "businessProxy": proxy}}

    def list_services(self, query):
        query = NodeListQuery.model_validate(query)
        with self.database.transaction() as session:
            rows, total = NodeRepository.services(session, query)
            now = self._now(session)
            return {"items": [self._snapshot(row, instance, now) for row, instance in rows],
                    "total": total, "page": query.page, "pageSize": query.page_size}

    def get_service(self, service_id, *, configuration=False):
        with self.database.transaction() as session:
            row = self._service(session, service_id)
            if configuration:
                return {"serviceId": row.service_id, "version": row.version,
                        "configRevision": row.config_revision, "config": row.config_json}
            instance = session.get(HostNodeInstance, row.current_instance_id) if row.current_instance_id else None
            return self._snapshot(row, instance, self._now(session))

    def events(self, query, *, service_id=None):
        query = NodeEventQuery.model_validate(query)
        with self.database.transaction() as session:
            if service_id:
                self._service(session, service_id)
            rows = NodeRepository.events(session, service_id, query)
            more = len(rows) > query.limit
            rows = rows[:query.limit]
            return {"items": [{"sequence": row.event_id, "serviceId": row.service_id, "instanceId": row.instance_id,
                "configRevision": row.config_revision, "eventType": row.event_type, "source": row.source,
                "details": row.details, "createdAt": iso_utc(row.created_at)} for row in rows],
                "hasMore": more, "nextSequence": rows[-1].event_id if rows else query.after_sequence}

    def status(self):
        with self.database.transaction() as session:
            row = session.get(HostNodeIdentity, 1)
            return {"nodeId": self.settings.node_id, "epochId": self.epoch_id, "status": "online",
                "startedAt": iso_utc(row.started_at), "heartbeatSeconds": self.settings.heartbeat_seconds,
                "staleSeconds": self.settings.stale_seconds, "evaluatedAt": iso_utc(self._now(session)),
                "capabilities": {"serviceRegistry": True, "lifecycleReports": True, "inferenceQueue": False,
                    "publicGateway": False, "containerControl": self.settings.docker_enabled,
                    "resourceMetrics": self.settings.metrics_enabled}}
