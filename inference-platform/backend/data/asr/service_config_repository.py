"""查询切换接口可能影响的旧任务和未确认调用，不修改其状态。"""

from sqlalchemy import or_, select

from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.asr.target_models import AsrCallPermit
from backend.data.tasks.task_models import TaskRecord
from backend.data.models.model_models import ModelVersionRecord


def incompatible_asr_work(session, fingerprint: str | None) -> bool:
    active = RemoteAsrCall.state.in_(("held", "uncertain"))
    unfinished = TaskRecord.status.not_in(("succeeded", "failed", "cancelled"))
    query = select(RemoteAsrCall.task_id).join(TaskRecord, TaskRecord.task_id == RemoteAsrCall.task_id).where(
        or_(active, unfinished))
    if fingerprint is not None:
        query = query.where(RemoteAsrCall.config_fingerprint != fingerprint)
    if session.scalar(query.limit(1)) is not None:
        return True
    # 无对应调用档案的旧许可也不能通过换配置绕过。
    return session.scalar(select(AsrCallPermit.task_id).outerjoin(
        RemoteAsrCall, RemoteAsrCall.task_id == AsrCallPermit.task_id).where(
            AsrCallPermit.state.in_(("held", "uncertain")), RemoteAsrCall.task_id.is_(None)).limit(1)) is not None


def existing_service_spec(session, model_id, model_version):
    row = session.scalar(select(ModelVersionRecord).where(
        ModelVersionRecord.model_id == model_id, ModelVersionRecord.model_version == model_version))
    return row.spec_json if row is not None else None


def service_model_specs(session, model_id):
    return list(session.scalars(select(ModelVersionRecord.spec_json).where(
        ModelVersionRecord.model_id == model_id)))
