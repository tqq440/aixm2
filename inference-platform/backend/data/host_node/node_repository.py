"""Node 查询与追加事件；不连接网络或改变业务进程。"""

from datetime import datetime

from sqlalchemy import func, select, text

from backend.data.host_node.node_models import HostNodeEvent, HostNodeInstance, HostNodeService


class NodeRepository:
    @staticmethod
    def now(session):
        return datetime.fromisoformat(session.scalar(text("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')")))

    @staticmethod
    def event(session, now, event_type, *, source="node", service=None, instance=None, details=None):
        session.add(HostNodeEvent(service_id=service.service_id if service else None,
            instance_id=instance.instance_id if instance else None,
            config_revision=instance.config_revision if instance else (service.config_revision if service else None),
            event_type=event_type, source=source, details=details or {}, created_at=now))

    @staticmethod
    def services(session, query):
        total = session.scalar(select(func.count()).select_from(HostNodeService))
        rows = session.execute(select(HostNodeService, HostNodeInstance).outerjoin(
            HostNodeInstance, HostNodeService.current_instance_id == HostNodeInstance.instance_id
        ).order_by(HostNodeService.service_id).offset((query.page - 1) * query.page_size).limit(query.page_size)).all()
        return rows, total

    @staticmethod
    def events(session, service_id, query):
        statement = select(HostNodeEvent).where(HostNodeEvent.event_id > query.after_sequence)
        if service_id:
            statement = statement.where(HostNodeEvent.service_id == service_id)
        return session.scalars(statement.order_by(HostNodeEvent.event_id).limit(query.limit + 1)).all()
