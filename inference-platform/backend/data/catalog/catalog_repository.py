"""SQL 目录访问；所有写操作由上层的短事务统一提交。"""

from uuid import uuid4

from sqlalchemy import func, select

from backend.data.catalog.catalog_models import (
    InferenceCatalogEvent, InferenceResourceGroup, InferenceService, InferenceServiceRevision, InferenceTargetCheck,
)


class CatalogRepository:
    @staticmethod
    def list_services(session, page, page_size):
        total = session.scalar(select(func.count()).select_from(InferenceService))
        rows = list(session.scalars(select(InferenceService).order_by(InferenceService.service_id)
                                   .offset((page - 1) * page_size).limit(page_size)))
        ids = {value for row in rows for value in (row.draft_revision, row.active_revision) if value}
        revisions = {row.revision: row for row in session.scalars(
            select(InferenceServiceRevision).where(InferenceServiceRevision.revision.in_(ids)))} if ids else {}
        checks = list(session.scalars(select(InferenceTargetCheck).where(InferenceTargetCheck.revision.in_(ids)))) if ids else []
        return rows, revisions, checks, total

    @staticmethod
    def get(session, service_id):
        return session.get(InferenceService, service_id)

    @staticmethod
    def revision(session, revision):
        return session.get(InferenceServiceRevision, revision)

    @staticmethod
    def checks(session, revision):
        return list(session.scalars(select(InferenceTargetCheck).where(InferenceTargetCheck.revision == revision)))

    @staticmethod
    def group_ids(session):
        return set(session.scalars(select(InferenceResourceGroup.group_id)))

    @staticmethod
    def event(session, service_id, event_type, revision, now):
        session.add(InferenceCatalogEvent(event_id=uuid4().hex, service_id=service_id, event_type=event_type,
                                          revision=revision, created_at=now))

    @staticmethod
    def upsert_check(session, revision, target_id, result, now):
        row = session.scalar(select(InferenceTargetCheck).where(
            InferenceTargetCheck.revision == revision, InferenceTargetCheck.target_id == target_id))
        if row is None:
            row = InferenceTargetCheck(check_id=uuid4().hex, revision=revision, target_id=target_id)
            session.add(row)
        row.success, row.error_code = result["success"], result["errorCode"]
        row.duration_ms, row.checked_at = result["durationMs"], now

    @staticmethod
    def list_groups(session, page, page_size):
        total = session.scalar(select(func.count()).select_from(InferenceResourceGroup))
        rows = list(session.scalars(select(InferenceResourceGroup).order_by(InferenceResourceGroup.group_id)
                                   .offset((page - 1) * page_size).limit(page_size)))
        return rows, total
