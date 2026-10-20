"""在线请求元数据仓储；不存储提示词、回答或上游凭据。"""

from sqlalchemy import func, select

from backend.data.llm.online_models import OnlineRequestRecord
from backend.data.tasks.task_models import TaskRecord

ACTIVE_STATES = ("accepted", "waiting_instance", "streaming")


class OnlineRepository:
    @staticmethod
    def update(row, **values):
        """由 Service 提供已裁决字段，持久化赋值只在仓储层执行。"""
        for name, value in values.items():
            setattr(row, name, value)

    @staticmethod
    def get(session, request_id, caller_id=None):
        query = select(OnlineRequestRecord).where(OnlineRequestRecord.online_request_id == request_id)
        if caller_id is not None:
            query = query.where(OnlineRequestRecord.caller_id == caller_id)
        return session.scalar(query)

    @staticmethod
    def add(session, row):
        session.add(row)
        session.flush()

    @staticmethod
    def counts(session, instance_id=None):
        active = session.scalar(select(func.count()).select_from(OnlineRequestRecord).where(
            OnlineRequestRecord.state.in_(ACTIVE_STATES))) or 0
        query = select(func.count()).select_from(OnlineRequestRecord).where(
            OnlineRequestRecord.permit_state.in_(("held", "uncertain")))
        if instance_id is not None:
            query = query.where(OnlineRequestRecord.instance_id == instance_id)
        occupied = session.scalar(query) or 0
        uncertain = session.scalar(select(func.count()).select_from(OnlineRequestRecord).where(
            OnlineRequestRecord.permit_state == "uncertain")) or 0
        return active, occupied, uncertain

    @staticmethod
    def inflight(session):
        offline = session.scalar(select(func.count()).select_from(TaskRecord).where(
            TaskRecord.status.in_(("created", "queued", "running", "retry_wait", "cancel_requested")))) or 0
        return offline + OnlineRepository.counts(session)[0]

    @staticmethod
    def expired(session, now):
        return session.scalars(select(OnlineRequestRecord).where(
            OnlineRequestRecord.state.in_(ACTIVE_STATES), OnlineRequestRecord.lease_expires_at <= now)).all()

    @staticmethod
    def page(session, caller_id, page, page_size):
        query = select(OnlineRequestRecord).where(OnlineRequestRecord.caller_id == caller_id)
        total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = session.scalars(query.order_by(OnlineRequestRecord.accepted_at.desc(),
                                             OnlineRequestRecord.online_request_id.desc())
                               .offset((page - 1) * page_size).limit(page_size)).all()
        return rows, total
