"""目录与资源记录的有界持久化查询；新鲜度和可用性由 Service 裁决。"""

from sqlalchemy import func, select

from backend.data.asr.catalog_models import AsrCatalogRevision, AsrResourceObservation
from backend.data.asr.target_models import AsrCapacityGroup, AsrComputeTarget, AsrTargetRevision


class AsrCatalogRepository:
    @staticmethod
    def get_catalog(session, catalog_revision_id):
        return session.scalar(select(AsrCatalogRevision).where(
            AsrCatalogRevision.catalog_revision_id == catalog_revision_id))

    @staticmethod
    def latest_catalog(session, target_id):
        return session.scalar(select(AsrCatalogRevision).where(
            AsrCatalogRevision.target_id == target_id).order_by(AsrCatalogRevision.sequence.desc()).limit(1))

    @staticmethod
    def add_catalog(session, **fields):
        row = AsrCatalogRevision(**fields)
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def get_observation(session, observation_id):
        return session.scalar(select(AsrResourceObservation).where(
            AsrResourceObservation.observation_id == observation_id))

    @staticmethod
    def add_observation(session, **fields):
        row = AsrResourceObservation(**fields)
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def latest_observations(session, catalog_ids):
        if not catalog_ids:
            return {}
        # 迟到样本仍保留历史，但不得凭录入时间覆盖更新的采样事实。
        ranked = select(
            AsrResourceObservation.sequence,
            func.row_number().over(
                partition_by=(AsrResourceObservation.catalog_revision_id, AsrResourceObservation.scope),
                order_by=(AsrResourceObservation.observed_at.desc(), AsrResourceObservation.sequence.desc()),
            ).label("position"),
        ).where(AsrResourceObservation.catalog_revision_id.in_(catalog_ids)).subquery()
        rows = session.scalars(select(AsrResourceObservation).join(
            ranked, ranked.c.sequence == AsrResourceObservation.sequence).where(ranked.c.position == 1))
        return {(row.catalog_revision_id, row.scope): row for row in rows}

    @staticmethod
    def page_targets(session, offset, limit):
        total = int(session.scalar(select(func.count()).select_from(AsrComputeTarget)) or 0)
        latest_catalog_sequence = select(func.max(AsrCatalogRevision.sequence)).where(
            AsrCatalogRevision.target_id == AsrComputeTarget.target_id,
        ).correlate(AsrComputeTarget).scalar_subquery()
        legacy_revision_id = select(AsrTargetRevision.revision_id).where(
            AsrTargetRevision.target_id == AsrComputeTarget.target_id,
        ).order_by(AsrTargetRevision.created_at.desc(), AsrTargetRevision.revision_id.desc()
                   ).limit(1).correlate(AsrComputeTarget).scalar_subquery()
        # 有目录时读取它固定绑定的连接修订；无目录的旧目标仍可只读展示。
        query = select(AsrComputeTarget, AsrCapacityGroup, AsrCatalogRevision, AsrTargetRevision
            ).select_from(AsrComputeTarget).join(
                AsrCapacityGroup, AsrCapacityGroup.group_id == AsrComputeTarget.capacity_group_id,
            ).outerjoin(
                AsrCatalogRevision, AsrCatalogRevision.sequence == latest_catalog_sequence,
            ).outerjoin(
                AsrTargetRevision, AsrTargetRevision.revision_id == func.coalesce(
                    AsrCatalogRevision.revision_id, legacy_revision_id),
            ).order_by(AsrComputeTarget.target_id).offset(offset).limit(limit)
        return total, list(session.execute(query))
