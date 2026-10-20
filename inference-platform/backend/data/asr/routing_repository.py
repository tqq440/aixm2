"""路由持久化操作；调用方负责原任务事务、策略校验与执行许可。"""

from sqlalchemy import exists, select

from backend.data.asr.catalog_models import AsrCatalogRevision
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.asr.routing_models import AsrRoutingRevision, AsrTaskRoute
from backend.data.tasks.task_models import TaskRecord
from backend.utils.json.json_utils import json_values_equal


class AsrRoutingRepository:
    @staticmethod
    def add_revision(session, **fields):
        row = AsrRoutingRevision(**fields)
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def get_revision(session, routing_revision_id):
        return session.get(AsrRoutingRevision, routing_revision_id)

    @staticmethod
    def add_task_route(session, **fields):
        row = AsrTaskRoute(**fields)
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def get_task_route(session, task_id):
        return session.get(AsrTaskRoute, task_id)

    @staticmethod
    def get_task_routes(session, ids):
        if not ids:
            return {}
        rows = session.scalars(select(AsrTaskRoute).where(AsrTaskRoute.task_id.in_(ids)))
        return {row.task_id: row for row in rows}

    @staticmethod
    def select_target(session, task_id, catalog_revision_id, decision_snapshot, selected_at):
        """在原 immediate 领取事务内选择一次；不自行提交或获得执行权。"""
        row = session.get(AsrTaskRoute, task_id)
        if row is None:
            raise ValueError("Task routing record does not exist")
        if row.selected_catalog_revision_id is not None:
            if (row.selected_catalog_revision_id != catalog_revision_id
                    or not json_values_equal(row.decision_snapshot, decision_snapshot)):
                raise ValueError("Task routing selection cannot be replaced")
            return row
        row.selected_catalog_revision_id = catalog_revision_id
        row.decision_snapshot = decision_snapshot
        row.selected_at = selected_at
        session.flush()
        return row

    @staticmethod
    def get_many_catalogs(session, ids):
        if not ids:
            return {}
        rows = session.scalars(select(AsrCatalogRevision).where(
            AsrCatalogRevision.catalog_revision_id.in_(ids)))
        return {row.catalog_revision_id: row for row in rows}

    @staticmethod
    def has_legacy_waiting(session):
        routed = exists(select(AsrTaskRoute.task_id).where(AsrTaskRoute.task_id == TaskRecord.task_id))
        waiting = select(RemoteAsrCall.task_id).join(TaskRecord, TaskRecord.task_id == RemoteAsrCall.task_id).where(
            RemoteAsrCall.state == "queued", TaskRecord.status.in_(("created", "queued", "retry_wait")), ~routed)
        return bool(session.scalar(select(exists(waiting))))

    @staticmethod
    def has_other_route_waiting(session, routing_revision_id):
        waiting = select(RemoteAsrCall.task_id).join(
            TaskRecord, TaskRecord.task_id == RemoteAsrCall.task_id).join(
            AsrTaskRoute, AsrTaskRoute.task_id == TaskRecord.task_id).where(
                RemoteAsrCall.state == "queued", TaskRecord.status.in_(("created", "queued", "retry_wait")),
                AsrTaskRoute.routing_revision_id != routing_revision_id)
        return bool(session.scalar(select(exists(waiting))))
