"""SQLite 原子预约全平台四个、同服务一个查询名额。"""

from ipaddress import ip_address
from urllib.parse import urlsplit
from sqlalchemy import func, select
from backend.common.exceptions.app_error import AppError
from backend.data.database.database import database_now
from backend.data.catalog.catalog_models import InferenceServiceRevision
from backend.data.invocations.node_dispatch_models import InferenceNodeDispatch
from backend.data.invocations.invocation_models import InferenceInvocation
from backend.data.monitoring.budget_models import MonitoringPermit
from backend.service.catalog.catalog_service import ServiceCatalog
from backend.schema.monitoring.budget_schema import MonitorPermitCommand, MonitorPermitReply
from backend.utils.json.json_utils import canonical_json_sha256


class MonitorBudgetService:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def _identity(session, request):
        payload = request.command.model_dump(mode="json", by_alias=True)
        binding = session.get(InferenceNodeDispatch, request.command.invocation_id)
        invocation = session.get(InferenceInvocation, request.command.invocation_id)
        command_hash=canonical_json_sha256(payload)
        if (binding is None or binding.command_json != payload or binding.request_hash != command_hash
                or invocation is None or invocation.sent_at is None):
            raise AppError("MONITOR_CALL_MISMATCH", "查询身份与中心已派发调用不匹配。", 409)
        revision = session.get(InferenceServiceRevision, request.command.service_revision)
        definition = ServiceCatalog._definition(revision)
        target = next(t for t in definition.targets if t.target_id == request.command.target_id)
        if target.node is None or (target.node.node_id,target.node.service_id,target.node.config_revision) != (
                request.command.node_id,request.command.service_id,request.command.config_revision):
            raise AppError("MONITOR_CALL_MISMATCH", "Node 绑定与调用不匹配。", 409)
        group = target.monitoring.budget_group
        host = urlsplit(target.base_url).hostname
        try: local = ip_address(host).is_loopback
        except ValueError: local = host == "localhost"
        endpoint_key = canonical_json_sha256({"origin": target.base_url, "path": target.monitoring.status_path,
                                              "node": request.command.node_id if local else None})
        return command_hash, canonical_json_sha256({"group":group}) if group else endpoint_key, endpoint_key

    def change(self, value, *, release=False):
        request = MonitorPermitCommand.model_validate(value)
        with self.database.transaction(immediate=True) as session:
            command_hash, service_key, endpoint_key = self._identity(session, request)
            row = session.get(MonitoringPermit, request.poll_id)
            if row and row.command_hash != command_hash:
                raise AppError("MONITOR_PERMIT_CONFLICT", "查询许可编号已绑定其他调用。", 409)
            if row:
                if release and row.state != "released":
                    row.state, row.released_at = "released", database_now(session)
                return MonitorPermitReply(poll_id=request.poll_id, state=row.state)
            now = database_now(session)
            if not release:
                held = select(func.count()).select_from(MonitoringPermit).where(MonitoringPermit.state == "held")
                # 同一调用即便配置修订变化也不得重叠查询。
                same = session.scalar(held.where((MonitoringPermit.service_key == service_key) |
                                                 (MonitoringPermit.endpoint_key == endpoint_key) |
                                                 (MonitoringPermit.invocation_id == request.command.invocation_id)))
                reason = "SERVICE_QUERY_LIMIT" if same else "GLOBAL_QUERY_LIMIT" if session.scalar(held) >= 4 else None
                if reason:
                    return MonitorPermitReply(poll_id=request.poll_id, state="denied", reason=reason)
            # release 在 acquire 之前到达时留下墓碑，迟到 acquire 不得重新激活。
            state = "released" if release else "held"
            session.add(MonitoringPermit(poll_id=request.poll_id, command_hash=command_hash,
                node_id=request.command.node_id, invocation_id=request.command.invocation_id,
                service_key=service_key, endpoint_key=endpoint_key, state=state, created_at=now, released_at=now if release else None))
            return MonitorPermitReply(poll_id=request.poll_id, state=state)
