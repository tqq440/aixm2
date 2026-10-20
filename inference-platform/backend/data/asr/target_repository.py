"""目标与许可的持久化操作；事务和准入决定均由调用方持有。"""

from sqlalchemy import exists, func, select

from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.asr.target_models import (
    AsrCallPermit, AsrCapacityChange, AsrCapacityGroup, AsrComputeTarget, AsrTargetRevision, AsrTargetWorkerSession,
)


ACTIVE_PERMIT_STATES = ("held", "uncertain")


class AsrTargetRepository:
    @staticmethod
    def _add(session, model, fields):
        row = model(**fields)
        session.add(row)
        return row

    @staticmethod
    def add_group(session, **fields):
        return AsrTargetRepository._add(session, AsrCapacityGroup, fields)

    @staticmethod
    def add_target(session, **fields):
        return AsrTargetRepository._add(session, AsrComputeTarget, fields)

    @staticmethod
    def add_revision(session, **fields):
        return AsrTargetRepository._add(session, AsrTargetRevision, fields)

    @staticmethod
    def add_worker(session, **fields):
        return AsrTargetRepository._add(session, AsrTargetWorkerSession, fields)

    @staticmethod
    def add_permit(session, **fields):
        return AsrTargetRepository._add(session, AsrCallPermit, fields)

    @staticmethod
    def add_capacity_change(session, **fields):
        return AsrTargetRepository._add(session, AsrCapacityChange, fields)

    @staticmethod
    def list_capacity_changes(session):
        return list(session.scalars(select(AsrCapacityChange).order_by(AsrCapacityChange.changed_at, AsrCapacityChange.change_id)))

    @staticmethod
    def worker_active_permits(session, session_id, *, held_only=False):
        return list(session.scalars(select(AsrCallPermit).where(
            AsrCallPermit.worker_session_id == session_id,
            AsrCallPermit.state.in_(("held",) if held_only else ACTIVE_PERMIT_STATES))))

    @staticmethod
    def active_workers(session, target_id=None):
        query = select(AsrTargetWorkerSession).where(AsrTargetWorkerSession.active_slot_key.is_not(None))
        if target_id is not None:
            query = query.join(AsrTargetRevision, AsrTargetWorkerSession.revision_id == AsrTargetRevision.revision_id).where(
                AsrTargetRevision.target_id == target_id)
        return list(session.scalars(query))

    @staticmethod
    def active_permits_with_target(session, group_id):
        return list(session.execute(select(AsrCallPermit, AsrTargetRevision.target_id).join(
            AsrTargetRevision, AsrCallPermit.revision_id == AsrTargetRevision.revision_id).where(
                AsrCallPermit.capacity_group_id == group_id, AsrCallPermit.state.in_(ACTIVE_PERMIT_STATES))))

    @staticmethod
    def workers_for_process(session, pid, started_after):
        return list(session.scalars(select(AsrTargetWorkerSession).where(
            AsrTargetWorkerSession.pid == pid, AsrTargetWorkerSession.started_at >= started_after)))

    @staticmethod
    def get_group(session, key):
        return session.get(AsrCapacityGroup, key)

    @staticmethod
    def get_target(session, key):
        return session.get(AsrComputeTarget, key)

    @staticmethod
    def get_revision(session, key):
        return session.get(AsrTargetRevision, key)

    @staticmethod
    def get_worker(session, key):
        return session.get(AsrTargetWorkerSession, key)

    @staticmethod
    def get_permit(session, key):
        return session.get(AsrCallPermit, key)

    @staticmethod
    def list_groups(session):
        return list(session.scalars(select(AsrCapacityGroup).order_by(AsrCapacityGroup.group_id)))

    @staticmethod
    def list_targets(session):
        return list(session.scalars(select(AsrComputeTarget).order_by(AsrComputeTarget.target_id)))

    @staticmethod
    def list_revisions(session):
        return list(session.scalars(select(AsrTargetRevision).order_by(AsrTargetRevision.created_at, AsrTargetRevision.revision_id)))

    @staticmethod
    def list_workers(session):
        return list(session.scalars(select(AsrTargetWorkerSession).order_by(AsrTargetWorkerSession.started_at, AsrTargetWorkerSession.session_id)))

    @staticmethod
    def list_permits(session):
        return list(session.scalars(select(AsrCallPermit).order_by(AsrCallPermit.created_at, AsrCallPermit.task_id)))

    @staticmethod
    def active_worker(session, active_slot_key):
        if active_slot_key is None:
            return None
        return session.scalar(select(AsrTargetWorkerSession).where(
            AsrTargetWorkerSession.active_slot_key == active_slot_key))

    @staticmethod
    def revision_by_config(session, target_id, fingerprint):
        return session.scalar(select(AsrTargetRevision).where(
            AsrTargetRevision.target_id == target_id,
            AsrTargetRevision.config_fingerprint == fingerprint))

    @staticmethod
    def endpoint_groups(session, endpoint_identity):
        return set(session.scalars(select(AsrComputeTarget.capacity_group_id).join(
            AsrTargetRevision, AsrTargetRevision.target_id == AsrComputeTarget.target_id).where(
                AsrTargetRevision.endpoint_identity == endpoint_identity).distinct()))

    @staticmethod
    def active_counts(session, target_id, group_id):
        target_count = select(func.count()).select_from(AsrCallPermit).join(
            AsrTargetRevision, AsrCallPermit.revision_id == AsrTargetRevision.revision_id).where(
                AsrTargetRevision.target_id == target_id,
                AsrCallPermit.state.in_(ACTIVE_PERMIT_STATES)).scalar_subquery()
        group_count = select(func.count()).select_from(AsrCallPermit).where(
            AsrCallPermit.capacity_group_id == group_id,
            AsrCallPermit.state.in_(ACTIVE_PERMIT_STATES)).scalar_subquery()
        counts = session.execute(select(target_count, group_count)).one()
        return int(counts[0]), int(counts[1])

    @staticmethod
    def untracked_legacy_active(session):
        tracked = exists(select(AsrCallPermit.task_id).where(
            AsrCallPermit.task_id == RemoteAsrCall.task_id,
            AsrCallPermit.state.in_(ACTIVE_PERMIT_STATES)))
        return bool(session.scalar(select(exists(select(RemoteAsrCall.task_id).where(
            RemoteAsrCall.state.in_(ACTIVE_PERMIT_STATES), ~tracked)))))

    @staticmethod
    def set_fields(row, **fields):
        for name, value in fields.items():
            setattr(row, name, value)
        return row
