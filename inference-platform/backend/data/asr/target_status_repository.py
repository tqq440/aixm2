"""有界、只读的容量视图查询；业务状态由Service解释。"""

from sqlalchemy import exists, func, select

from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.asr.target_models import AsrCallPermit, AsrTargetRevision, AsrTargetWorkerSession
from backend.data.tasks.task_models import TaskRecord


class TargetStatusRepository:
    @staticmethod
    def counts(session, target_id, group_id):
        rows = session.execute(select(AsrCallPermit.state, AsrTargetRevision.target_id, func.count())
            .join(AsrTargetRevision, AsrTargetRevision.revision_id == AsrCallPermit.revision_id)
            .where(AsrCallPermit.capacity_group_id == group_id,
                   AsrCallPermit.state.in_(("held", "uncertain")))
            .group_by(AsrCallPermit.state, AsrTargetRevision.target_id))
        target, group = {"held": 0, "uncertain": 0}, {"held": 0, "uncertain": 0}
        for state, identifier, count in rows:
            group[state] += count
            if identifier == target_id:
                target[state] += count
        return target, group

    @staticmethod
    def latest_slots(session, target_id, limit):
        ranked = select(AsrTargetWorkerSession.session_id,
            func.row_number().over(partition_by=AsrTargetWorkerSession.slot_index,
                order_by=(AsrTargetWorkerSession.active_slot_key.is_not(None).desc(),
                          AsrTargetWorkerSession.started_at.desc(),
                          AsrTargetWorkerSession.session_id.desc())).label("position"))
        ranked = ranked.join(AsrTargetRevision).where(
            AsrTargetRevision.target_id == target_id,
            AsrTargetWorkerSession.slot_index < limit).subquery()
        return list(session.scalars(select(AsrTargetWorkerSession).join(ranked,
            ranked.c.session_id == AsrTargetWorkerSession.session_id)
            .where(ranked.c.position == 1).order_by(AsrTargetWorkerSession.slot_index).limit(8)))

    @staticmethod
    def active_calls(session, group_id):
        return list(session.execute(select(AsrCallPermit, AsrTargetRevision.target_id, AsrTargetWorkerSession)
            .join(AsrTargetRevision, AsrTargetRevision.revision_id == AsrCallPermit.revision_id)
            .join(AsrTargetWorkerSession, AsrTargetWorkerSession.session_id == AsrCallPermit.worker_session_id)
            .where(AsrCallPermit.capacity_group_id == group_id,
                   AsrCallPermit.state.in_(("held", "uncertain")))
            .order_by(AsrCallPermit.created_at, AsrCallPermit.task_id).limit(50)))

    @staticmethod
    def queued(session, fingerprint):
        rows = session.execute(select(RemoteAsrCall.config_fingerprint, func.count())
            .join(TaskRecord, TaskRecord.task_id == RemoteAsrCall.task_id)
            .where(TaskRecord.status == "queued", RemoteAsrCall.state == "queued")
            .group_by(RemoteAsrCall.config_fingerprint))
        compatible, incompatible = 0, 0
        for value, count in rows:
            if fingerprint is not None and value == fingerprint:
                compatible += count
            else:
                incompatible += count
        return compatible, incompatible

    @staticmethod
    def legacy_unknown_count(session):
        tracked = exists(select(AsrCallPermit.task_id).where(
            AsrCallPermit.task_id == RemoteAsrCall.task_id,
            AsrCallPermit.state.in_(("held", "uncertain"))))
        return int(session.scalar(select(func.count()).select_from(RemoteAsrCall)
            .where(RemoteAsrCall.state.in_(("held", "uncertain")), ~tracked)))

