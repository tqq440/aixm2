"""ASR-2A：只登记和观测，不启动、接管或终止任何进程。"""

from contextlib import contextmanager
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.data.database.database import Database, database_now
from backend.data.agent.agent_repository import AgentRepository
from backend.data.models.model_repository import ModelRepository
from backend.data.nodes.local_probe import probe_local_node
from backend.data.nodes.node_models import ComputeNode, InstanceSession, ServiceInstance
from backend.data.nodes.node_repository import NodeRepository
from backend.schema.models.model_schema import ModelRegistration, RegistryListQuery
from backend.schema.nodes.node_schema import (
    InstanceDetail, InstanceListQuery, InstancePage, InstancePath, InstanceView,
    LocalNodeSnapshot, LocalRegistration, NodePage, NodePath, NodeView,
    ObservationReceipt, SessionPage, SessionView,
)
from backend.service.asr.adapter_catalog import configured_asr_plugin
from backend.utils.json.json_utils import json_values_equal
from backend.utils.time.time_utils import as_utc


class NodeRegistryService:
    def __init__(self, database: Database, settings: AppSettings, *, probe=None):
        self.database = database
        self.settings = settings
        self.repository = NodeRepository()
        self.probe = probe if probe is not None else probe_local_node

    @contextmanager
    def _transaction(self, *, write=False):
        try:
            with self.database.transaction(immediate=write) as session:
                yield session
        except SQLAlchemyError as exc:
            raise AppError("NODE_REGISTRY_UNAVAILABLE", "节点登记库暂不可用，请检查数据库及迁移。", 503) from exc

    def _local_snapshot(self):
        try:
            return LocalNodeSnapshot.model_validate(self.probe())
        except (OSError, ValueError, ValidationError) as exc:
            raise AppError("NODE_PROBE_FAILED", "无法取得本机稳定身份或系统信息，请检查本机读取权限。", 503) from exc

    def _node_view(self, row, session=None):
        agent_status = 'not_started'
        if session is not None:
            control = AgentRepository.get_local_control(session)
            instance = self.repository.get_instance(session, control.instance_id) if control else None
            if instance and instance.node_id == row.node_id:
                age = (as_utc(database_now(session)) - as_utc(control.heartbeat_at)).total_seconds()
                agent_status = ('offline' if control.shutdown_confirmed_at else 'online'
                                if 0 <= age <= self.settings.runtime_stale_seconds else 'stale')
        return NodeView(node_id=row.node_id, hostname=row.hostname, os_name=row.os_name,
                        os_version=row.os_version, architecture=row.architecture, cpu_count=row.cpu_count,
                        registered_at=row.registered_at, metadata_observed_at=row.updated_at, agent_status=agent_status)

    @staticmethod
    def _instance_view(row, session=None):
        values = row.to_dict()
        if session is not None and AgentRepository.get_control(session, row.instance_id):
            values['management_mode'] = 'node_agent'
        return InstanceView.model_validate(values)

    def _require_node(self, session, node_id):
        row = self.repository.get_node(session, node_id)
        if row is None:
            raise AppError("NODE_NOT_FOUND", "未找到该节点。", 404)
        return row

    def _require_instance(self, session, instance_id):
        row = self.repository.get_instance(session, instance_id)
        if row is None:
            raise AppError("INSTANCE_NOT_FOUND", "未找到该 ASR 实例。", 404)
        return row

    def _check_model(self, session):
        expected = configured_asr_plugin(self.settings).model_spec()
        row = ModelRepository().get_version(session, expected.model_id, expected.model_version)
        if row is None:
            raise AppError("MODEL_VERSION_NOT_FOUND", "请先登记当前 Whisper 模型的准确版本，再登记实例。", 404)
        actual = ModelRegistration.model_validate(row.spec_json)
        if not json_values_equal(actual.model_dump(), expected.model_dump()):
            raise AppError("INSTANCE_BINDING_CONFLICT", "模型规范与本机 ASR 插件不匹配，不能覆盖实例绑定。", 409)
        return expected

    @staticmethod
    def _check_binding(instance, spec):
        if (instance.model_id, instance.model_version, instance.adapter_key, instance.execution_device) != (
                spec.model_id, spec.model_version, spec.adapter_key, "cpu"):
            raise AppError("INSTANCE_BINDING_CONFLICT", "已登记实例的模型版本不能被覆盖。", 409)

    def register_local(self) -> LocalRegistration:
        snapshot = self._local_snapshot()
        with self._transaction(write=True) as session:
            spec = self._check_model(session)
            node = self.repository.get_node_by_identity(session, snapshot.local_identity)
            node_created = node is None
            if node is None:
                _, total = self.repository.list_nodes(session, page=1, page_size=1)
                if total:
                    raise AppError("LOCAL_NODE_CONFLICT", "当前数据库已绑定另一台机器，本阶段不登记第二个节点。", 409)
                now = database_now(session)
                node = ComputeNode(node_id=uuid5(NAMESPACE_URL, "inference-node:" + snapshot.local_identity).hex,
                                   **snapshot.model_dump(), registered_at=now, updated_at=now)
                self.repository.add_node(session, node)
            else:
                self.repository.update_node_metadata(node, **snapshot.model_dump(exclude={"local_identity"}),
                                                     now=database_now(session))
            instance = self.repository.get_local_instance(session, node.node_id, "asr-default")
            instance_created = instance is None
            if instance is None:
                instance = ServiceInstance(
                    instance_id=uuid5(NAMESPACE_URL, "inference-asr:" + node.node_id).hex,
                    node_id=node.node_id, instance_key="asr-default", model_id=spec.model_id,
                    model_version=spec.model_version, adapter_key=spec.adapter_key,
                    execution_device="cpu", registered_at=database_now(session),
                )
                self.repository.add_instance(session, instance)
            else:
                self._check_binding(instance, spec)
            return LocalRegistration(node=self._node_view(node, session), instance=self._instance_view(instance, session),
                                     node_created=node_created, instance_created=instance_created)

    def _session_view(self, row, now, latest_id):
        if row is None:
            return None
        age = (as_utc(now) - as_utc(row.heartbeat_at)).total_seconds()
        freshness = ("superseded" if row.session_id != latest_id else "clock_skew" if age < 0
                     else "stale" if age > self.settings.runtime_stale_seconds else "fresh")
        return SessionView(**row.to_dict(), freshness=freshness, heartbeat_age_seconds=age if age >= 0 else None)

    def observe_local(self) -> ObservationReceipt:
        """显式归档本库现有 Worker 报告；不把历史 PID 用作当前进程控制依据。"""
        snapshot = self._local_snapshot()
        with self._transaction(write=True) as session:
            node = self.repository.get_node_by_identity(session, snapshot.local_identity)
            if node is None:
                raise AppError("LOCAL_NODE_NOT_REGISTERED", "请先在这台机器执行 register-local。", 409)
            instance = self.repository.get_local_instance(session, node.node_id, "asr-default")
            if instance is None:
                raise AppError("LOCAL_INSTANCE_NOT_REGISTERED", "请先登记本机 ASR 实例。", 409)
            self._check_binding(instance, self._check_model(session))
            if AgentRepository.get_control(session, instance.instance_id) is not None:
                raise AppError('INSTANCE_MANAGED_BY_AGENT', '当前实例由 Agent 管理，请查询 management/sessions；旧 Worker 观测历史仍保留。', 409)
            report = self.repository.get_worker_report(session)
            if report is None:
                return ObservationReceipt(instance_id=instance.instance_id, recorded=False, reason="no_worker_report")
            if as_utc(report.started_at) < as_utc(instance.registered_at):
                return ObservationReceipt(instance_id=instance.instance_id, recorded=False, reason="pre_registration_report")
            now = database_now(session)
            if (as_utc(report.heartbeat_at) > as_utc(now)
                    or as_utc(report.started_at) > as_utc(report.heartbeat_at)):
                raise AppError("INVALID_WORKER_REPORT", "Worker 报告时间异常，不能据此登记会话。", 409)
            row = self.repository.get_session(session, instance.instance_id, report.session_id)
            fields = dict(child_pid=report.child_pid, reported_state=report.state,
                          reported_model_ready=report.model_ready, active_task_id=report.active_task_id,
                          heartbeat_at=report.heartbeat_at, observed_at=now, last_error=report.last_error)
            reason = "recorded"
            if row is None:
                row = InstanceSession(
                    session_id=uuid5(NAMESPACE_URL, instance.instance_id + ":" + report.session_id).hex,
                    instance_id=instance.instance_id, source="legacy_worker_observation",
                    worker_session_id=report.session_id, worker_pid=report.pid,
                    worker_started_at=report.started_at, **fields,
                )
                # 校验报告再落库；没有报告时永不生成随机启动会话。
                self._session_view(row, now, row.session_id)
                self.repository.add_session(session, row)
            elif row.worker_pid != report.pid or as_utc(row.worker_started_at) != as_utc(report.started_at):
                raise AppError("WORKER_SESSION_CONFLICT", "同一 Worker 会话的启动身份不一致。", 409)
            elif as_utc(report.heartbeat_at) < as_utc(row.heartbeat_at):
                reason = "older_report"
            elif (as_utc(report.heartbeat_at) == as_utc(row.heartbeat_at)
                  and all(getattr(row, key) == value for key, value in fields.items() if key != "observed_at")):
                reason = "unchanged"
            else:
                # SQLite 时间只有毫秒；同毫秒内 idle→stopped 仍是当前真实报告的变化。
                self.repository.update_session(row, **fields)
            session.flush()
            latest = self.repository.latest_session(session, instance.instance_id)
            return ObservationReceipt(instance_id=instance.instance_id, recorded=reason == "recorded", reason=reason,
                                      session=self._session_view(row, now, latest.session_id))

    def list_nodes(self, query: RegistryListQuery) -> NodePage:
        query = RegistryListQuery.model_validate(query)
        with self._transaction() as session:
            rows, total = self.repository.list_nodes(session, **query.model_dump())
            return NodePage(items=[self._node_view(row, session) for row in rows], total=total, **query.model_dump())

    def get_node(self, node_id: str) -> NodeView:
        node_id = NodePath(node_id=node_id).node_id
        with self._transaction() as session:
            return self._node_view(self._require_node(session, node_id), session)

    def list_instances(self, query: InstanceListQuery) -> InstancePage:
        query = InstanceListQuery.model_validate(query)
        with self._transaction() as session:
            if query.node_id is not None:
                self._require_node(session, query.node_id)
            rows, total = self.repository.list_instances(session, **query.model_dump())
            return InstancePage(items=[self._instance_view(row, session) for row in rows], total=total,
                                page=query.page, page_size=query.page_size)

    def get_instance(self, instance_id: str) -> InstanceDetail:
        instance_id = InstancePath(instance_id=instance_id).instance_id
        with self._transaction() as session:
            instance = self._require_instance(session, instance_id)
            latest = self.repository.latest_session(session, instance_id)
            return InstanceDetail(instance=self._instance_view(instance, session), latest_observation=self._session_view(
                latest, database_now(session), latest.session_id if latest else None))

    def list_sessions(self, instance_id: str, query: RegistryListQuery) -> SessionPage:
        instance_id = InstancePath(instance_id=instance_id).instance_id
        query = RegistryListQuery.model_validate(query)
        with self._transaction() as session:
            self._require_instance(session, instance_id)
            rows, total = self.repository.list_sessions(session, instance_id, **query.model_dump())
            latest = self.repository.latest_session(session, instance_id)
            now = database_now(session)
            return SessionPage(items=[self._session_view(row, now, latest.session_id) for row in rows], total=total,
                               evaluated_at=now, **query.model_dump())
