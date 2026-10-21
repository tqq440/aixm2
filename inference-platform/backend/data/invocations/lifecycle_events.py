"""Session 级事务事件：只跟踪状态/许可转换，不记录输入、结果或租约令牌。"""

from uuid import uuid4

from sqlalchemy import inspect, select

from backend.data.database.database import database_now
from backend.data.invocations.invocation_models import InferenceInvocation, InferenceTaskEvent
from backend.data.llm.online_models import OnlineRequestRecord
from backend.data.tasks.task_models import TaskRecord


def record_lifecycle_events(session, _context, _instances):
    for row in list(session.new) + list(session.dirty):
        if not isinstance(row, (TaskRecord, OnlineRequestRecord)):
            continue
        state = inspect(row)
        name = "status" if isinstance(row, TaskRecord) else "state"
        fields = [name] if isinstance(row, TaskRecord) else [name, "permit_state"]
        changed = {field: getattr(row, field) for field in fields if state.attrs[field].history.has_changes()}
        if not changed and row not in session.new:
            continue
        # 新对象常由数据库默认值补齐，此处只记录调用方已明确设置的字段。
        changed = {key: value for key, value in changed.items() if value is not None}
        if row in session.new:
            changed[name] = getattr(row, name) or ("created" if name == "status" else "accepted")
        if not changed:
            continue
        online = isinstance(row, OnlineRequestRecord)
        invocation = None
        if row not in session.new:
            column = InferenceInvocation.online_request_id if online else InferenceInvocation.task_id
            invocation = session.scalar(select(InferenceInvocation).where(column == (
                row.online_request_id if online else row.task_id)))
        context = {"attempt": getattr(row, "attempt", 0) or 0}
        if invocation:
            context.update(attempt=invocation.attempt, serviceRevision=invocation.service_revision,
                           targetId=invocation.selected_target_id)
        session.info.setdefault("pending_owner_events", []).append(dict(event_id=uuid4().hex,
            task_id=None if online else row.task_id,
            online_request_id=row.online_request_id if online else None,
            invocation_id=invocation.invocation_id if invocation else None, application_id=row.caller_id,
            event_type=("online." if online else "task.") + ("created" if row in session.new else "state_changed"),
            source="platform", details={**changed, **context},
            occurred_at=database_now(session)))


def persist_lifecycle_events(session, _context):
    # 父记录已 INSERT 后再加入事件，避免无 ORM relationship 时外键插入顺序不确定。
    for values in session.info.pop("pending_owner_events", []):
        session.add(InferenceTaskEvent(**values))


def discard_lifecycle_events(session):
    session.info.pop("pending_owner_events", None)
