"""调用方控制事务；这里只读写登记和报告，不推断就绪或执行进程控制。"""

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.data.nodes.node_models import ComputeNode, InstanceSession, ServiceInstance
from backend.data.runtime.runtime_models import RuntimeComponent


class NodeRepository:
    @staticmethod
    def get_node(session: Session, node_id: str) -> ComputeNode | None:
        return session.get(ComputeNode, node_id)

    @staticmethod
    def get_node_by_identity(session: Session, local_identity: str) -> ComputeNode | None:
        return session.scalar(select(ComputeNode).where(ComputeNode.local_identity == local_identity))

    @staticmethod
    def add_node(session: Session, row: ComputeNode) -> None:
        session.add(row)
        session.flush()

    @staticmethod
    def update_node_metadata(
        row: ComputeNode, *, hostname: str, os_name: str, os_version: str,
        architecture: str, cpu_count: int | None, now: datetime,
    ) -> None:
        row.hostname = hostname
        row.os_name = os_name
        row.os_version = os_version
        row.architecture = architecture
        row.cpu_count = cpu_count
        row.updated_at = now

    @staticmethod
    def list_nodes(session: Session, *, page: int, page_size: int):
        total = session.scalar(select(func.count()).select_from(ComputeNode)) or 0
        rows = session.scalars(
            select(ComputeNode).order_by(ComputeNode.registered_at, ComputeNode.node_id)
            .offset((page - 1) * page_size).limit(page_size)
        ).all()
        return rows, total

    @staticmethod
    def get_instance(session: Session, instance_id: str) -> ServiceInstance | None:
        return session.get(ServiceInstance, instance_id)

    @staticmethod
    def get_local_instance(session: Session, node_id: str, instance_key: str) -> ServiceInstance | None:
        return session.scalar(select(ServiceInstance).where(
            ServiceInstance.node_id == node_id, ServiceInstance.instance_key == instance_key,
        ))

    @staticmethod
    def add_instance(session: Session, row: ServiceInstance) -> None:
        session.add(row)
        session.flush()

    @staticmethod
    def list_instances(session: Session, *, node_id: str | None = None, page: int, page_size: int):
        query = select(ServiceInstance)
        if node_id is not None:
            query = query.where(ServiceInstance.node_id == node_id)
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = session.scalars(
            query.order_by(ServiceInstance.registered_at, ServiceInstance.instance_id)
            .offset((page - 1) * page_size).limit(page_size)
        ).all()
        return rows, total

    @staticmethod
    def get_session(session: Session, instance_id: str, worker_session_id: str) -> InstanceSession | None:
        return session.scalar(select(InstanceSession).where(
            InstanceSession.instance_id == instance_id,
            InstanceSession.worker_session_id == worker_session_id,
        ))

    @staticmethod
    def add_session(session: Session, row: InstanceSession) -> None:
        session.add(row)
        session.flush()

    @staticmethod
    def update_session(row: InstanceSession, **values: Any) -> None:
        """只接受 Service 已裁决的报告字段；不覆盖会话身份或启动时间。"""
        allowed = {
            "child_pid", "reported_state", "reported_model_ready", "active_task_id",
            "heartbeat_at", "observed_at", "last_error",
        }
        if values.keys() - allowed:
            raise ValueError("不能修改实例会话身份或未定义字段。")
        for key, value in values.items():
            setattr(row, key, value)

    @staticmethod
    def latest_session(session: Session, instance_id: str) -> InstanceSession | None:
        # 按真实报告中的启动时间排序，迟到观测不能把旧会话变为最新会话。
        return session.scalar(select(InstanceSession).where(InstanceSession.instance_id == instance_id)
                              .order_by(InstanceSession.worker_started_at.desc(),
                                        InstanceSession.session_id.desc()).limit(1))

    @staticmethod
    def list_sessions(session: Session, instance_id: str, *, page: int, page_size: int):
        query = select(InstanceSession).where(InstanceSession.instance_id == instance_id)
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = session.scalars(query.order_by(InstanceSession.worker_started_at.desc(),
                                             InstanceSession.session_id.desc())
                               .offset((page - 1) * page_size).limit(page_size)).all()
        return rows, total

    @staticmethod
    def get_worker_report(session: Session) -> RuntimeComponent | None:
        return session.get(RuntimeComponent, "worker")
