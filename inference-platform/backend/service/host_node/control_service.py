"""持久主机控制流程；所有 Docker/HTTP 在写事务外，未知动作不自动重发。"""

from copy import deepcopy
from datetime import datetime, timedelta
from threading import Lock
from uuid import uuid4

from sqlalchemy import func, select

from backend.common.containers.docker_engine import DockerEngine, DockerFailure
from backend.common.exceptions.app_error import AppError
from backend.data.host_node.node_models import HostNodeInstance, HostNodeOperation, HostNodeRuntimeRevision, HostNodeService
from backend.data.host_node.node_repository import NodeRepository
from backend.data.host_node.execution_models import has_unresolved_executions
from backend.schema.host_node.control_schema import (
    AbortControlCommand, ControlCommand, OperationPath, ReconcileCommand, SaveRuntimeConfig,
)
from backend.schema.host_node.node_schema import NodeListQuery
from backend.service.host_node.business_control_client import BusinessControlClient
from backend.utils.json.json_utils import canonical_json_sha256
from backend.utils.time.iso_time import iso_utc
from backend.utils.time.time_utils import as_utc


TERMINAL = {"succeeded", "failed", "rolled_back", "aborted"}


class HostControlService:
    def __init__(self, node, *, docker=None, business=None):
        self.node = node
        self.docker = docker or DockerEngine(node.settings)
        self.business = business or BusinessControlClient(node.settings)
        self.work_lock = Lock()
        self.cursor = self.observe_cursor = 0
        self.last_observation = {}

    def recover(self):
        with self.node.database.transaction(immediate=True) as session:
            self.node._require_owner(session)
            for row in session.scalars(select(HostNodeService).where(HostNodeService.runtime_revision.is_not(None))):
                control = deepcopy(row.control_json)
                if control.get("activeOperationId"):
                    op = session.get(HostNodeOperation, control["activeOperationId"])
                    if op and op.status not in TERMINAL:
                        op.status, op.error_code = "unknown", "NODE_RESTART_RECONCILE_REQUIRED"
                        op.updated_at = self.node._now(session)
                        control["gateClosed"] = True
                        row.control_json = control
                        NodeRepository.event(session, op.updated_at, "CONTROL_RECOVERY_REQUIRED", service=row,
                            details={"operationId": op.operation_id, "phase": op.phase})

    @staticmethod
    def _profile(session, row, revision=None):
        record = session.scalar(select(HostNodeRuntimeRevision).where(
            HostNodeRuntimeRevision.service_id == row.service_id,
            HostNodeRuntimeRevision.revision == (revision or row.runtime_revision)))
        if record is None:
            raise AppError("NODE_RUNTIME_NOT_CONFIGURED", "服务尚未登记受控容器。", 409)
        return deepcopy(record.config_json)

    def save_runtime(self, service_id, command):
        command = SaveRuntimeConfig.model_validate(command)
        profile = command.config.model_dump(mode="json", by_alias=True)
        digest = canonical_json_sha256(profile)
        with self.node.database.transaction(immediate=True) as session:
            self.node._require_owner(session)
            row = self.node._service(session, service_id)
            if row.control_version != command.expected_control_version:
                raise AppError("NODE_CONTROL_STALE", "控制版本已变化，请刷新。", 409)
            if row.runtime_revision == digest:
                return self._runtime_view(row, profile)
            if has_unresolved_executions(session, service_id):
                raise AppError("NODE_EXECUTIONS_UNCONFIRMED", "服务仍有未确认调用，不能修改容器资料。", 409)
            control = row.control_json or {}
            instance = session.get(HostNodeInstance, row.current_instance_id) if row.current_instance_id else None
            if control.get("activeOperationId") or (instance and instance.exit_confirmed_at is None and
                    (row.runtime_revision is not None or instance.reported_state != "stopped")):
                raise AppError("NODE_INSTANCE_UNCONFIRMED", "受管实例未确认退出，不能修改容器资料。", 409)
            if row.config_json["reportProtocol"] != "lifecycle-v1":
                raise AppError("NODE_REPORT_UNSUPPORTED", "受管启动需要生命周期上报。", 409)
            if profile["drainPath"] and not row.config_json.get("internalBaseUrl"):
                raise AppError("NODE_BUSINESS_ADDRESS_REQUIRED", "排空/恢复需要明确业务地址。", 409)
            ids = {v["containerId"] for v in profile["versions"]}
            for other in session.scalars(select(HostNodeService).where(HostNodeService.service_id != service_id,
                                                                      HostNodeService.runtime_revision.is_not(None))):
                if ids & {v["containerId"] for v in self._profile(session, other)["versions"]}:
                    raise AppError("NODE_CONTAINER_ALIAS", "容器已经登记到其他服务。", 409)
            existing = session.scalar(select(HostNodeRuntimeRevision).where(
                HostNodeRuntimeRevision.service_id == service_id, HostNodeRuntimeRevision.revision == digest))
            now = self.node._now(session)
            if existing is None:
                session.add(HostNodeRuntimeRevision(runtime_id=str(uuid4()), service_id=service_id,
                    revision=digest, config_json=profile, created_at=now))
            row.runtime_revision, row.control_version = digest, row.control_version + 1
            row.control_json = {"gateClosed": True, "activeOperationId": None, "currentVersion": None}
            NodeRepository.event(session, now, "RUNTIME_CONFIGURED", service=row,
                details={"runtimeRevision": digest, "controlVersion": row.control_version})
            return self._runtime_view(row, profile)

    @staticmethod
    def _runtime_view(row, profile):
        return {"serviceId": row.service_id, "runtimeRevision": row.runtime_revision,
                "controlVersion": row.control_version, "config": profile, "control": deepcopy(row.control_json)}

    def runtime(self, service_id):
        with self.node.database.transaction() as session:
            row = self.node._service(session, service_id)
            return self._runtime_view(row, self._profile(session, row))

    @staticmethod
    def _view(op):
        return {"operationId": op.operation_id, "serviceId": op.service_id, "action": op.action,
            "status": op.status, "phase": op.phase, "errorCode": op.error_code,
            "runtimeRevision": op.runtime_revision, "configRevision": op.config_revision,
            "requestedVersion": op.payload.get("requestedVersion"), "effectiveVersion": op.payload.get("effectiveVersion"),
            "rollingBack": op.payload.get("rollingBack", False), "controlVersion": op.payload["controlVersion"],
            "createdAt": iso_utc(op.created_at), "updatedAt": iso_utc(op.updated_at)}

    def submit(self, service_id, command):
        command = ControlCommand.model_validate(command)
        digest = canonical_json_sha256(command.model_dump(mode="json", by_alias=True))
        with self.node.database.transaction(immediate=True) as session:
            self.node._require_owner(session)
            row = self.node._service(session, service_id)
            old = session.get(HostNodeOperation, str(command.operation_id))
            if old:
                if old.service_id != service_id or old.request_hash != digest:
                    raise AppError("NODE_OPERATION_CONFLICT", "操作编号内容冲突。", 409)
                return self._view(old)
            if not self.node.settings.docker_enabled:
                raise AppError("NODE_DOCKER_DISABLED", "尚未显式启用 Docker 控制。", 409)
            if row.control_version != command.expected_control_version or row.config_revision != command.config_revision:
                raise AppError("NODE_CONTROL_STALE", "配置或控制版本已变化。", 409)
            profile = self._profile(session, row)
            control = deepcopy(row.control_json)
            if control.get("activeOperationId"):
                raise AppError("NODE_OPERATION_ACTIVE", "该服务存在尚未确认的控制操作。", 409)
            if command.version_id and command.version_id not in {v["versionId"] for v in profile["versions"]}:
                raise AppError("NODE_VERSION_NOT_REGISTERED", "目标版本未登记。", 409)
            if command.action in {"stop", "upgrade"} and not control.get("currentVersion"):
                raise AppError("NODE_NO_MANAGED_INSTANCE", "没有受管运行版本。", 409)
            if command.action == "upgrade" and command.version_id == control.get("currentVersion"):
                raise AppError("NODE_VERSION_ALREADY_CURRENT", "当前已经是该版本。", 409)
            row.control_version += 1
            now = self.node._now(session)
            op = HostNodeOperation(operation_id=str(command.operation_id), service_id=service_id, action=command.action,
                request_hash=digest, runtime_revision=row.runtime_revision, config_revision=row.config_revision,
                status="pending", phase="preflight", payload={"controlVersion": row.control_version,
                    "requestedVersion": command.version_id, "effectiveVersion": command.version_id or control.get("currentVersion"),
                    "previousVersion": control.get("currentVersion"), "rollingBack": False}, created_at=now, updated_at=now)
            session.add(op)
            control.update(gateClosed=True, activeOperationId=op.operation_id)
            row.control_json = control
            NodeRepository.event(session, now, "CONTROL_ACCEPTED", service=row,
                details={"operationId": op.operation_id, "action": op.action, "controlVersion": row.control_version})
            return self._view(op)

    def get_operation(self, operation_id):
        identifier = str(OperationPath(operation_id=operation_id).operation_id)
        with self.node.database.transaction() as session:
            op = session.get(HostNodeOperation, identifier)
            if op is None:
                raise AppError("NODE_OPERATION_NOT_FOUND", "操作不存在。", 404)
            return self._view(op)

    def operations(self, service_id, query):
        query = NodeListQuery.model_validate(query)
        with self.node.database.transaction() as session:
            self.node._service(session, service_id)
            condition = HostNodeOperation.service_id == service_id
            total = session.scalar(select(func.count()).select_from(HostNodeOperation).where(condition))
            rows = session.scalars(select(HostNodeOperation).where(condition).order_by(HostNodeOperation.created_at,
                HostNodeOperation.operation_id).offset((query.page - 1) * query.page_size).limit(query.page_size)).all()
            return {"items": [self._view(op) for op in rows], "total": total, "page": query.page, "pageSize": query.page_size}

    def assignment(self, service_id):
        with self.node.database.transaction() as session:
            row = self.node._service(session, service_id)
            instance = session.get(HostNodeInstance, row.current_instance_id) if row.current_instance_id else None
            if instance is None:
                raise AppError("NODE_INSTANCE_NOT_BOUND", "当前没有可用的启动身份。", 409)
            return {"serviceId": service_id, "instanceId": instance.instance_id, "configRevision": instance.config_revision,
                "generation": instance.generation, "sequence": instance.sequence,
                "startPaused": bool(row.runtime_revision and self._profile(session, row)["resumePath"]),
                "operationId": (row.control_json or {}).get("activeOperationId")}

    def _load(self, identifier):
        with self.node.database.transaction() as session:
            op = session.get(HostNodeOperation, identifier)
            if op is None:
                raise AppError("NODE_OPERATION_NOT_FOUND", "操作不存在。", 404)
            row = self.node._service(session, op.service_id)
            instance = session.get(HostNodeInstance, row.current_instance_id) if row.current_instance_id else None
            return op.to_dict(), row.to_dict(), instance.to_dict() if instance else None, self._profile(session, row, op.runtime_revision), self.node._now(session)

    def _change(self, identifier, *, phase=None, status=None, error=None, payload=None, gate=None, confirm_exit=False):
        with self.node.database.transaction(immediate=True) as session:
            self.node._require_owner(session)
            op = session.get(HostNodeOperation, identifier)
            row = self.node._service(session, op.service_id)
            if (row.control_json or {}).get("activeOperationId") != identifier or row.config_revision != op.config_revision:
                raise AppError("NODE_OPERATION_STALE", "操作已失去当前实例控制权。", 409)
            previous = (op.phase, op.status, op.error_code)
            op.phase = phase or op.phase
            op.status = status or op.status
            op.error_code = error
            op.payload = {**op.payload, **(payload or {})}
            op.updated_at = self.node._now(session)
            control = deepcopy(row.control_json)
            if confirm_exit and row.current_instance_id:
                instance = session.get(HostNodeInstance, row.current_instance_id)
                if instance.exit_confirmed_at is None:
                    instance.exit_confirmed_at = op.updated_at
            if gate is not None:
                control["gateClosed"] = gate
            if op.status in TERMINAL:
                control["activeOperationId"] = None
            row.control_json = control
            if previous != (op.phase, op.status, op.error_code):
                NodeRepository.event(session, op.updated_at, "CONTROL_CHANGED", service=row,
                    details={"operationId": identifier, "phase": op.phase, "status": op.status, "errorCode": error,
                             **({"evidenceHash": payload["evidenceHash"]} if payload and "evidenceHash" in payload else {})})
            return self._view(op)

    @staticmethod
    def _version(profile, identifier):
        for version in profile["versions"]:
            if version["versionId"] == identifier:
                return version
        raise DockerFailure("VERSION_NOT_REGISTERED")

    def _inspect(self, service_id, version):
        observed = self.docker.inspect(version["containerId"])
        if observed.get("restartPolicy") not in {"", "no"} or observed.get("autoRemove") is not False:
            raise DockerFailure("CONTAINER_LIFECYCLE_NOT_EXCLUSIVE")
        if (observed["containerId"] != version["containerId"] or observed["imageId"] != version["imageId"] or
            observed["labels"] != {"inference.node_id": self.node.settings.node_id,
                "inference.service_id": service_id, "inference.version_id": version["versionId"]}):
            raise DockerFailure("CONTAINER_OWNERSHIP_MISMATCH")
        return observed

    def _observe(self, service_id, version):
        observed = self._inspect(service_id, version)
        with self.node.database.transaction(immediate=True) as session:
            self.node._require_owner(session)
            row = self.node._service(session, service_id)
            control = deepcopy(row.control_json)
            if control.get("containerId") == version["containerId"]:
                previous = (control.get("containerState"), control.get("containerError"))
                now = self.node._now(session)
                control.update(containerState=observed["state"], containerObservedAt=iso_utc(now),
                    containerObservedEpoch=self.node.epoch_id, containerError=None)
                row.control_json = control
                exit_changed = False
                if observed["state"] == "exited" and row.current_instance_id:
                    instance = session.get(HostNodeInstance, row.current_instance_id)
                    if instance.exit_confirmed_at is None:
                        instance.exit_confirmed_at = now
                        exit_changed = True
                elif observed["running"] and row.current_instance_id:
                    session.get(HostNodeInstance, row.current_instance_id).exit_confirmed_at = None
                if previous != (observed["state"], None) or exit_changed:
                    NodeRepository.event(session, now, "CONTAINER_OBSERVED", service=row,
                        details={"containerId": version["containerId"], "state": observed["state"]})
        return observed

    def _fresh(self, instance, now):
        if not instance or not instance["received_at"] or instance["node_epoch_id"] != self.node.epoch_id:
            return False
        age = (now - instance["received_at"]).total_seconds()
        return 0 <= age < self.node.settings.stale_seconds

    def _zero(self, instance, now):
        return bool(self._fresh(instance, now) and not instance["accepting_requests"]
                    and instance["queued_tasks"] == 0 and instance["active_tasks"] == 0)

    def _deadline(self, now, seconds):
        return iso_utc(now + timedelta(seconds=seconds))

    @staticmethod
    def _expired(op, now):
        stamp = op["payload"].get("deadline")
        return bool(stamp and now >= as_utc(datetime.fromisoformat(stamp)).replace(tzinfo=None))

    def _business_action(self, op, row, instance, profile, action):
        if instance is None:
            raise DockerFailure("BUSINESS_INSTANCE_UNKNOWN")
        self._change(op["operation_id"], phase=action + "_issued", status="running",
            payload={"beforeSequence": instance["sequence"], **({"resumeIssued": True} if action == "resume" else {})})
        self.business.send(row["config_json"], profile, action, {"operationId": op["operation_id"],
            "instanceId": instance["instance_id"], "configRevision": op["config_revision"], "action": action})
        self._change(op["operation_id"], phase="wait_drain" if action == "drain" else "wait_accept",
            payload={"deadline": self._deadline(self._load(op["operation_id"])[-1],
                profile["drainTimeoutSeconds"] if action == "drain" else profile["readyTimeoutSeconds"])})

    def _correlated(self, op, instance, action):
        return bool(instance and instance["sequence"] > op["payload"].get("beforeSequence", -1) and
            instance["control_report_json"] == {"operationId": op["operation_id"], "action": action})

    def _bind_and_start(self, op, row, profile):
        version = self._version(profile, op["payload"]["effectiveVersion"])
        observed = self._inspect(row["service_id"], version)
        if observed["state"] not in {"created", "exited"} or observed["running"]:
            raise DockerFailure("TARGET_CONTAINER_NOT_STOPPED")
        with self.node.database.transaction(immediate=True) as session:
            self.node._require_owner(session)
            current = self.node._service(session, row["service_id"])
            operation = session.get(HostNodeOperation, op["operation_id"])
            if current.control_json.get("activeOperationId") != operation.operation_id or operation.phase != "bind_target":
                raise AppError("NODE_OPERATION_STALE", "操作阶段已经变化。", 409)
            old = session.get(HostNodeInstance, current.current_instance_id) if current.current_instance_id else None
            if old and old.exit_confirmed_at is None:
                raise DockerFailure("PREVIOUS_EXIT_UNCONFIRMED")
            now = self.node._now(session)
            instance_id = str(uuid4())
            current.generation += 1
            current.current_instance_id = instance_id
            instance = HostNodeInstance(instance_id=instance_id, service_id=current.service_id,
                config_revision=current.config_revision, generation=current.generation, sequence=0,
                reported_state="unknown", model_ready=False, accepting_requests=False, health="unknown", created_at=now,
                may_have_accepted=not bool(profile["resumePath"]))
            session.add(instance)
            current.control_json = {**current.control_json, "containerId": version["containerId"],
                "currentVersion": version["versionId"], "containerState": observed["state"],
                "containerObservedAt": iso_utc(now), "containerObservedEpoch": self.node.epoch_id,
                "containerError": None, "gateClosed": True}
            operation.phase, operation.status = "starting_issued", "running"
            operation.payload = {**operation.payload, "instanceId": instance_id, "resumeIssued": False}
            operation.updated_at = now
            NodeRepository.event(session, now, "INSTANCE_BOUND", service=current, instance=instance,
                details={"operationId": operation.operation_id, "versionId": version["versionId"], "startsContainer": True})
        self.docker.start(version["containerId"])
        self._change(op["operation_id"], phase="wait_ready",
                     payload={"deadline": self._deadline(self._load(op["operation_id"])[-1], profile["readyTimeoutSeconds"])})

    def _step(self, identifier):
        op, row, instance, profile, now = self._load(identifier)
        with self.node.database.transaction() as session:
            unresolved = has_unresolved_executions(session, row["service_id"])
        if unresolved:
            self._change(identifier, status="blocked", error="NODE_EXECUTIONS_UNCONFIRMED")
            return
        phase, control = op["phase"], row["control_json"]
        target = self._version(profile, op["payload"]["effectiveVersion"])
        if phase == "preflight":
            observed = {v["versionId"]: self._inspect(row["service_id"], v) for v in profile["versions"]}
            running = [key for key, value in observed.items() if value["running"] or value["state"] not in {"created", "exited"}]
            current = control.get("currentVersion")
            if any(v != current for v in running):
                raise DockerFailure("UNEXPECTED_RUNNING_CONTAINER")
            if op["action"] == "start":
                if running:
                    if current != target["versionId"] or not instance or control.get("containerId") != target["containerId"]:
                        raise DockerFailure("CURRENT_CONTAINER_CONFLICT")
                    self._change(identifier, phase="wait_ready", status="running",
                                 payload={"deadline": self._deadline(now, profile["readyTimeoutSeconds"]), "alreadyRunning": True})
                else:
                    if current:
                        self._observe(row["service_id"], self._version(profile, current))
                    self._change(identifier, phase="bind_target", status="running")
            else:
                previous = self._version(profile, current)
                current_observation = self._observe(row["service_id"], previous)
                if current_observation["state"] == "exited":
                    if op["action"] == "stop":
                        self._change(identifier, phase="finished", status="succeeded", gate=True)
                    else:
                        raise DockerFailure("UPGRADE_REQUIRES_LIVE_DRAIN")
                elif not profile["drainPath"]:
                    self._change(identifier, status="blocked", error="BUSINESS_DRAIN_UNSUPPORTED")
                else:
                    self._business_action(op, row, instance, profile, "drain")
        elif phase == "bind_target":
            self._bind_and_start(op, row, profile)
        elif phase == "wait_drain":
            if self._zero(instance, now) and self._correlated(op, instance, "drain") and instance["reported_state"] in {"draining", "stopped"}:
                previous = self._version(profile, control["currentVersion"])
                observed = self._observe(row["service_id"], previous)
                if observed["state"] == "exited":
                    self._change(identifier, phase="wait_exit")
                elif observed["state"] == "running":
                    self._change(identifier, phase="term_issued", payload={"deadline": self._deadline(now, profile["exitTimeoutSeconds"])})
                    self.docker.terminate(previous["containerId"])
                    self._change(identifier, phase="wait_exit")
                else:
                    raise DockerFailure("CONTAINER_STATE_UNSAFE")
            elif self._expired(op, now):
                self._change(identifier, status="blocked", error="DRAIN_UNCONFIRMED")
        elif phase in {"wait_exit", "wait_rollback_exit"}:
            current = self._version(profile, control["currentVersion"])
            observed = self._observe(row["service_id"], current)
            if observed["state"] == "exited":
                if op["action"] == "stop":
                    self._change(identifier, phase="finished", status="succeeded", gate=True)
                else:
                    updates = {"effectiveVersion": op["payload"]["previousVersion"], "rollingBack": True} if phase == "wait_rollback_exit" else {}
                    self._change(identifier, phase="bind_target", payload=updates)
            elif self._expired(op, now):
                self._change(identifier, status="blocked", error="CONTAINER_EXIT_UNCONFIRMED")
        elif phase == "wait_ready":
            observed = self._observe(row["service_id"], target)
            if observed["state"] == "running" and self._fresh(instance, now) and instance["model_ready"] and instance["health"] == "healthy":
                if instance["accepting_requests"]:
                    if profile["resumePath"] and not op["payload"].get("alreadyRunning"):
                        self._change(identifier, status="blocked", error="SERVICE_DID_NOT_START_PAUSED")
                    else:
                        self._change(identifier, phase="finished", status="rolled_back" if op["payload"]["rollingBack"] else "succeeded", gate=False)
                elif profile["resumePath"]:
                    self._business_action(op, row, instance, profile, "resume")
            elif self._expired(op, now):
                can_rollback = (op["action"] == "upgrade" and profile["autoRollback"] and not op["payload"]["rollingBack"]
                    and not op["payload"].get("resumeIssued") and self._zero(instance, now))
                can_rollback = bool(can_rollback and not instance["may_have_accepted"])
                if can_rollback and observed["state"] in {"running", "exited"}:
                    if observed["state"] == "running":
                        self._change(identifier, phase="rollback_term_issued", payload={"deadline": self._deadline(now, profile["exitTimeoutSeconds"])})
                        self.docker.terminate(target["containerId"])
                    self._change(identifier, phase="wait_rollback_exit")
                else:
                    self._change(identifier, status="blocked", error="READINESS_UNCONFIRMED")
        elif phase == "wait_accept":
            observed = self._observe(row["service_id"], target)
            if observed["state"] == "running" and self._fresh(instance, now) and instance["model_ready"] and instance["health"] == "healthy" and instance["accepting_requests"] and self._correlated(op, instance, "resume"):
                self._change(identifier, phase="finished", status="rolled_back" if op["payload"]["rollingBack"] else "succeeded", gate=False)
            elif self._expired(op, now):
                self._change(identifier, status="blocked", error="RESUME_UNCONFIRMED")
        else:
            self._change(identifier, status="unknown", error="CONTROL_ACTION_RECONCILE_REQUIRED")

    def tick(self):
        if not self.node.settings.docker_enabled or not self.work_lock.acquire(blocking=False):
            return
        try:
            with self.node.database.transaction() as session:
                identifiers = session.scalars(select(HostNodeOperation.operation_id).where(
                    HostNodeOperation.status.in_(["pending", "running"])).order_by(HostNodeOperation.created_at)).all()
            if identifiers:
                identifier = identifiers[self.cursor % len(identifiers)]; self.cursor += 1
                try:
                    self._step(identifier)
                except DockerFailure as exc:
                    phase = self._load(identifier)[0]["phase"]
                    self._change(identifier, status="failed" if phase in {"preflight", "bind_target"} else "unknown", error=exc.code)
            self._background_observe()
        finally:
            self.work_lock.release()

    def _background_observe(self):
        with self.node.database.transaction() as session:
            now = self.node._now(session)
            candidates = session.scalars(select(HostNodeService).where(HostNodeService.runtime_revision.is_not(None))
                .order_by(HostNodeService.service_id)).all()
            candidates = [row for row in candidates if row.control_json.get("currentVersion") and
                (row.service_id not in self.last_observation or not 0 <=
                 (now - self.last_observation[row.service_id]).total_seconds() < 5)]
            if not candidates:
                return
            row = candidates[self.observe_cursor % len(candidates)]; self.observe_cursor += 1
            control = row.control_json or {}
            if not control.get("currentVersion"):
                return
            version = self._version(self._profile(session, row), control["currentVersion"])
            service_id = row.service_id
        self.last_observation[service_id] = now
        try:
            self._observe(service_id, version)
        except DockerFailure as exc:
            with self.node.database.transaction(immediate=True) as session:
                self.node._require_owner(session)
                row = self.node._service(session, service_id)
                previous = row.control_json.get("containerError")
                row.control_json = {**row.control_json, "containerError": exc.code}
                if previous != exc.code:
                    NodeRepository.event(session, self.node._now(session), "CONTAINER_QUERY_FAILED", service=row, details={"errorCode": exc.code})

    def reconcile(self, operation_id, command):
        command = ReconcileCommand.model_validate(command)
        identifier = str(OperationPath(operation_id=operation_id).operation_id)
        if not self.work_lock.acquire(blocking=False):
            raise AppError("NODE_CONTROL_BUSY", "控制执行中，请稍后核对。", 409)
        try:
            op, row, instance, profile, now = self._load(identifier)
            if row["control_version"] != command.expected_control_version:
                raise AppError("NODE_CONTROL_STALE", "控制版本已变化。", 409)
            if op["status"] in TERMINAL:
                return self._view_object(op)
            phase = op["phase"]
            deadline = self._deadline(now, max(profile["drainTimeoutSeconds"], profile["readyTimeoutSeconds"], profile["exitTimeoutSeconds"]))
            try:
                if phase in {"starting_issued", "term_issued", "rollback_term_issued"}:
                    version = self._version(profile, row["control_json"]["currentVersion"])
                    observed = self._observe(row["service_id"], version)
                    expected = "running" if phase == "starting_issued" else "exited"
                    if observed["state"] != expected:
                        return self._change(identifier, status="unknown", error="ACTION_OUTCOME_UNCONFIRMED")
                    phase = {"starting_issued": "wait_ready", "term_issued": "wait_exit", "rollback_term_issued": "wait_rollback_exit"}[phase]
                elif phase in {"drain_issued", "resume_issued"}:
                    action = "drain" if phase == "drain_issued" else "resume"
                    if not self._fresh(instance, now) or not self._correlated(op, instance, action):
                        return self._change(identifier, status="unknown", error="ACTION_OUTCOME_UNCONFIRMED")
                    phase = "wait_drain" if action == "drain" else "wait_accept"
                return self._change(identifier, phase=phase, status="running", payload={"deadline": deadline})
            except DockerFailure as exc:
                return self._change(identifier, status="unknown", error=exc.code)
        finally:
            self.work_lock.release()

    def _view_object(self, values):
        return self.get_operation(values["operation_id"])

    def abort(self, operation_id, command):
        command = AbortControlCommand.model_validate(command)
        identifier = str(OperationPath(operation_id=operation_id).operation_id)
        if not self.work_lock.acquire(blocking=False):
            raise AppError("NODE_CONTROL_BUSY", "控制执行中。", 409)
        try:
            op, row, _instance, profile, _now = self._load(identifier)
            if row["control_version"] != command.expected_control_version or op["status"] not in {"unknown", "blocked"}:
                raise AppError("NODE_CONTROL_STALE", "仅允许显式终结当前受阻或未知操作。", 409)
            for version in profile["versions"]:
                observed = self._observe(row["service_id"], version)
                if observed["running"] or observed["state"] not in {"created", "exited"}:
                    raise AppError("NODE_EXIT_UNCONFIRMED", "必须确认所有登记容器均未运行，并人工核实未决命令已经结束。", 409)
            return self._change(identifier, phase="finished", status="aborted", gate=True, confirm_exit=True,
                error="EXPLICIT_CONTROL_ABORT", payload={"evidenceHash": canonical_json_sha256(command.evidence)})
        except DockerFailure as exc:
            raise AppError("NODE_EXIT_UNCONFIRMED", "无法核对容器状态，保留未知操作。", 409) from None
        finally:
            self.work_lock.release()
