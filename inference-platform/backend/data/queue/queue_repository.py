"""通知补投资料操作，不在这里访问 Redis。"""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select, text

from backend.data.database.database import database_now
from backend.data.invocations.invocation_models import InferenceInvocation
from backend.data.queue.queue_models import InferenceQueueOutbox, InferenceQueueState
from backend.schema.queue.queue_schema import QueueNotice


class QueueRepository:
    @staticmethod
    def available(session):
        return bool(session.scalar(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inference_queue_outboxes'")))

    @staticmethod
    def for_invocation(session, invocation_id):
        return session.scalar(select(InferenceQueueOutbox).where(InferenceQueueOutbox.invocation_id == invocation_id))

    @staticmethod
    def add(session, invocation, *, required):
        now = database_now(session)
        session.add(InferenceQueueOutbox(event_id=uuid4().hex, invocation_id=invocation.invocation_id,
            redis_required=required, state="online" if invocation.kind == "llm" else "pending",
            publish_attempts=0, created_at=now, next_attempt_at=now))

    @staticmethod
    def due(session, now, limit=20):
        return list(session.scalars(select(InferenceQueueOutbox.event_id).join(InferenceInvocation).where(
            InferenceInvocation.kind != "llm", InferenceInvocation.state == "waiting",
            InferenceQueueOutbox.state != "settled", InferenceQueueOutbox.next_attempt_at <= now
        ).order_by(InferenceInvocation.accepted_at, InferenceQueueOutbox.event_id).limit(limit)))

    @staticmethod
    def notice(outbox, invocation):
        return QueueNotice(event_id=outbox.event_id, invocation_id=invocation.invocation_id,
                           service_revision=invocation.service_revision, queue_key=invocation.queue_key)

    @staticmethod
    def matches(outbox, invocation, notice):
        return (outbox is not None and invocation is not None and invocation.kind != "llm"
                and outbox.invocation_id == notice.invocation_id
                and invocation.service_revision == notice.service_revision and invocation.queue_key == notice.queue_key)

    @staticmethod
    def publication(session, event_id):
        row = session.get(InferenceQueueOutbox, event_id)
        if row is None:
            return None
        invocation = session.get(InferenceInvocation, row.invocation_id)
        now = database_now(session)
        if (invocation.kind == "llm" or invocation.state != "waiting" or row.state == "settled"
                or row.next_attempt_at > now or (row.publisher_expires_at and row.publisher_expires_at > now)):
            return None
        row.publisher_token = uuid4().hex
        row.publisher_expires_at = now + timedelta(seconds=30)
        row.publish_attempts += 1
        row.redis_required = True
        return row.publisher_token, QueueRepository.notice(row, invocation)

    @staticmethod
    def record_publication(session, event_id, token, *, message_id=None, error=None, retry_seconds):
        row = session.get(InferenceQueueOutbox, event_id)
        if row is None or row.publisher_token != token:
            return False
        now = database_now(session)
        row.publisher_token, row.publisher_expires_at = None, None
        row.next_attempt_at = now + timedelta(seconds=retry_seconds)
        row.last_error_code = error
        if message_id:
            row.message_id, row.published_at = message_id, now
            if row.state not in {"received", "settled"}:
                row.state = "published"
        return True

    @staticmethod
    def terminal_outbox(session, limit=20):
        return list(session.scalars(select(InferenceQueueOutbox.event_id).join(InferenceInvocation).where(
            InferenceInvocation.state == "finished", InferenceInvocation.permit_state != "uncertain",
            InferenceQueueOutbox.state != "settled"
        ).order_by(InferenceQueueOutbox.created_at).limit(limit)))

    @staticmethod
    def counts(session):
        return dict(session.execute(select(InferenceQueueOutbox.state, func.count()).group_by(InferenceQueueOutbox.state)).all())
