"""外部 ASR 调用记录；所有写操作由调用方持有同一任务事务。"""

from sqlalchemy import select

from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.runtime.runtime_models import RuntimeComponent


class RemoteAsrRepository:
    @staticmethod
    def get(session, task_id):
        return session.get(RemoteAsrCall, task_id)

    @staticmethod
    def active(session):
        """历史只读调用兼容；并发裁决必须使用active_calls逐项处理。"""
        return session.scalar(select(RemoteAsrCall).where(
            RemoteAsrCall.state.in_(("held", "uncertain"))).limit(1))

    @staticmethod
    def active_calls(session, worker_id=None):
        query = select(RemoteAsrCall).where(RemoteAsrCall.state.in_(("held", "uncertain")))
        if worker_id is not None:
            query = query.where(RemoteAsrCall.worker_id == worker_id)
        return list(session.scalars(query.order_by(RemoteAsrCall.created_at, RemoteAsrCall.task_id)))

    @staticmethod
    def add(session, **values):
        row = RemoteAsrCall(**values)
        session.add(row)
        return row

    @staticmethod
    def reserve(row, task, now):
        row.state, row.worker_id, row.lease_token = "held", task.lease_worker_id, task.lease_token
        row.reserved_at = now

    @staticmethod
    def finish(row, now, *, confirmed):
        row.state = "released" if confirmed else "uncertain"
        row.ended_at = now

    @staticmethod
    def resolve(row, now, note):
        row.state, row.resolved_at, row.resolution_note = "released", now, note

    @staticmethod
    def runtime(session):
        return session.get(RuntimeComponent, "remote_asr_worker")

    @staticmethod
    def drain(session, session_id=None):
        row = RemoteAsrRepository.runtime(session)
        if row is not None and row.state != "stopped" and (session_id is None or row.session_id == session_id):
            row.state = "stopping"

    @staticmethod
    def begin_worker(session, session_id, pid, now):
        row = RemoteAsrRepository.runtime(session)
        if row is None:
            row = RuntimeComponent(component_id="remote_asr_worker")
            session.add(row)
        if row.session_id != session_id:
            row.state, row.model_ready = "starting", False
            row.session_id, row.pid = session_id, pid
            row.started_at = row.heartbeat_at = now
            row.child_pid = row.active_task_id = row.last_error = None

