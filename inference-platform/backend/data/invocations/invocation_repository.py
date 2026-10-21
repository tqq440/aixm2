"""公共账本 SQL；held 与 uncertain 均计额，释放不删除历史预约项。"""

from uuid import uuid4

from sqlalchemy import exists, func, or_, select, text

from backend.data.asr.target_models import AsrCallPermit
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.catalog.catalog_models import InferenceService, InferenceServiceRevision
from backend.data.invocations.invocation_models import InferenceInvocation, InferenceReservationItem, InferenceTaskEvent
from backend.data.llm.online_models import OnlineRequestRecord
from backend.data.tasks.task_models import TaskRecord


class InvocationRepository:
    @staticmethod
    def available(session):
        # 旧 0014 库无需迁移即可运行；关闭开关也不能绕过已经存在的公共占位。
        return bool(session.scalar(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inference_invocations'")))

    @staticmethod
    def for_owner(session, owner_kind, owner_id):
        column = InferenceInvocation.task_id if owner_kind == "task" else InferenceInvocation.online_request_id
        return session.scalar(select(InferenceInvocation).where(column == owner_id))

    @staticmethod
    def waiting(session, limit):
        return list(session.scalars(select(InferenceInvocation).where(InferenceInvocation.state == "waiting",
            InferenceInvocation.kind != "llm").order_by(InferenceInvocation.accepted_at, InferenceInvocation.invocation_id).limit(limit)))

    @staticmethod
    def occupied(session, key):
        return int(session.scalar(select(func.count()).select_from(InferenceReservationItem)
            .join(InferenceInvocation).where(InferenceReservationItem.resource_key == key,
                InferenceInvocation.permit_state.in_(("held", "uncertain")))) or 0)

    @staticmethod
    def active_definitions(session):
        return list(session.scalars(select(InferenceServiceRevision.config).join(InferenceService,
            InferenceService.active_revision == InferenceServiceRevision.revision).where(InferenceService.enabled.is_(True))))

    @staticmethod
    def legacy_occupied(session):
        asr = session.scalar(select(func.count()).select_from(AsrCallPermit).where(AsrCallPermit.state.in_(("held", "uncertain"))))
        remote = session.scalar(select(func.count()).select_from(RemoteAsrCall).where(RemoteAsrCall.state.in_(("held", "uncertain"))))
        local = session.scalar(select(func.count()).select_from(TaskRecord).where(
            TaskRecord.status.in_(("running", "cancel_requested")),
            ~exists(select(InferenceInvocation.invocation_id).where(InferenceInvocation.task_id == TaskRecord.task_id))))
        online = session.scalar(select(func.count()).select_from(OnlineRequestRecord).where(
            OnlineRequestRecord.permit_state.in_(("held", "uncertain")),
            ~exists(select(InferenceInvocation.invocation_id).where(
                InferenceInvocation.online_request_id == OnlineRequestRecord.online_request_id))))
        return bool(asr or remote or local or online)

    @staticmethod
    def common_occupied(session):
        return bool(session.scalar(select(func.count()).select_from(InferenceInvocation).where(
            InferenceInvocation.permit_state.in_(("held", "uncertain")))))

    @staticmethod
    def event(session, invocation, event_type, now, details=None, *, source="platform"):
        session.add(InferenceTaskEvent(event_id=uuid4().hex, invocation_id=invocation.invocation_id,
            task_id=invocation.task_id, online_request_id=invocation.online_request_id,
            application_id=invocation.application_id, event_type=event_type, source=source,
            details={"attempt": invocation.attempt, "serviceRevision": invocation.service_revision,
                     "targetId": invocation.selected_target_id, **(details or {})}, occurred_at=now))

    @staticmethod
    def due(session, now, limit):
        return list(session.scalars(select(InferenceInvocation).where(InferenceInvocation.state != "finished", or_(
            InferenceInvocation.deadline_at <= now,
            (InferenceInvocation.state == "waiting") & (InferenceInvocation.queue_deadline_at <= now),
            (InferenceInvocation.state.in_(("reserved", "sent"))) & (InferenceInvocation.lease_expires_at <= now)
        )).order_by(InferenceInvocation.accepted_at).limit(limit)))

    @staticmethod
    def events(session, owner_kind, owner_id, after, limit):
        column = InferenceTaskEvent.task_id if owner_kind == "task" else InferenceTaskEvent.online_request_id
        return list(session.scalars(select(InferenceTaskEvent).where(column == owner_id,
            InferenceTaskEvent.sequence > after).order_by(InferenceTaskEvent.sequence).limit(limit + 1)))
