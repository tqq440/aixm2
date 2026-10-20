"""只读解释本地记录；不采样网络、不更新路由、不获取执行权。"""

from sqlalchemy import text

from backend.common.exceptions.app_error import AppError
from backend.data.database.database import database_now
from backend.schema.asr.routing_schema import TaskRoutingView
from backend.schema.asr.scheduling_schema import SchedulingCandidateView, SchedulingStatus
from backend.service.asr.device_policy_service import DevicePolicyService
from backend.service.asr.routing_inventory import RoutingInventory
from backend.service.asr.routing_service import AsrRoutingService
from backend.service.asr.scheduling_reasons import reason_message
from backend.utils.time.time_utils import as_utc


class AsrSchedulingService:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings
        self.routing = AsrRoutingService(database, settings)

    def status(self):
        with self.database.session_factory() as session:
            session.execute(text("BEGIN"))
            self.routing.calls.runtime(session)
            now = as_utc(database_now(session))
            if self.settings.remote_asr_routing_revision is None:
                return SchedulingStatus(observed_at=now, mode="legacy", routing_configured=False,
                    execution_scope="legacy", reason_code="LEGACY_FIXED_TARGET", candidate_count=0,
                    message=reason_message("LEGACY_FIXED_TARGET"))
            count = 0
            try:
                _, policy, _ = self.routing._load(session)
                count = len(policy.candidates)
                self.routing.validate_submission(session)
            except AppError as exc:
                return SchedulingStatus(observed_at=now, mode="blocked", routing_configured=True,
                    execution_scope="disabled", reason_code=exc.code, candidate_count=count, message=exc.message)
            except (ValueError, TypeError):
                return SchedulingStatus(observed_at=now, mode="blocked", routing_configured=True,
                    execution_scope="disabled", reason_code="ROUTING_RECORD_INVALID", candidate_count=count,
                    message=reason_message("ROUTING_RECORD_INVALID"))
            return SchedulingStatus(observed_at=now, mode="isolated", routing_configured=True,
                execution_scope="isolated", reason_code="ISOLATED_ROUTING", candidate_count=count,
                message=reason_message("ISOLATED_ROUTING"))

    def task_view(self, session, task, route, detailed=False):
        frozen = route.decision_snapshot or {}
        view = TaskRoutingView(routing_revision_id=route.routing_revision_id,
            selection_state="selected" if route.selected_catalog_revision_id else "waiting",
            catalog_revision_id=route.selected_catalog_revision_id, target_id=frozen.get("targetId"),
            device_kind=frozen.get("deviceKind"), reason_code=frozen.get("reasonCode"),
            evidence_kind=frozen.get("evidenceKind"), selected_at=as_utc(route.selected_at) if route.selected_at else None)
        now = as_utc(database_now(session)) if detailed else None
        def explained(code, category, *, message=None, candidates=()):
            values = dict(observed_at=now, message=message or reason_message(code), reason_category=category,
                status_code=code, candidates=list(candidates))
            if route.selected_catalog_revision_id is None:
                values["reason_code"] = code
            return view.model_copy(update=values)

        unknown = task.error_json and task.error_json.get("code") in {"REMOTE_ASR_RESULT_UNKNOWN", "REMOTE_ASR_UNKNOWN"}
        if detailed:
            call = self.routing.calls.get(session, task.task_id)
            unknown = unknown or call is not None and call.state == "uncertain"
        if unknown:
            return explained("REMOTE_ASR_RESULT_UNKNOWN", "outcome_unknown")
        if task.status in {"succeeded", "failed", "cancelled"}:
            return explained({"succeeded": "TASK_SUCCEEDED", "failed": "TASK_FAILED", "cancelled": "TASK_CANCELLED"}[task.status], "terminal")
        if route.selected_catalog_revision_id is not None:
            return explained(view.reason_code or "TASK_ALREADY_STARTED_OR_NOT_QUEUED", "selected")
        if not detailed:
            return explained("ROUTING_AWAITING_RECHECK", "waiting")
        if task.status not in {"created", "queued"} or task.attempt != 0 or self.routing.targets.get_permit(session, task.task_id):
            return explained("TASK_ALREADY_STARTED_OR_NOT_QUEUED", "blocked")
        deadlines = [as_utc(value) for value in (task.queue_deadline_at, task.total_deadline_at) if value is not None]
        if deadlines and now >= min(deadlines):
            return explained("TASK_DEADLINE_EXPIRED", "waiting")
        if task.status == "created":
            return explained("TASK_AWAITING_DISPATCH", "waiting")
        try:
            row, policy, catalogs = self.routing._load(session, route.routing_revision_id)
            if (task.model_id, task.model_version) != (row.model_id, row.model_version):
                return explained("ASR_ROUTING_MODEL_MISMATCH", "blocked", message="任务保留的逻辑模型与路由不匹配，请核对原记录。")
            call = self.routing.calls.get(session, task.task_id)
            if call is None or call.state != "queued" or call.config_fingerprint != route.routing_revision_id:
                return explained("ROUTING_BINDING_UNCONFIRMED", "blocked")
            runtime = self.routing.calls.runtime(session)
            if runtime is not None and runtime.state in {"stopping", "draining"}:
                return explained("POOL_STOPPING", "waiting")
            if runtime is None or runtime.state == "stopped":
                return explained("POOL_NOT_RUNNING", "waiting")
            if not 0 <= (now-as_utc(runtime.heartbeat_at)).total_seconds() <= self.settings.runtime_stale_seconds:
                return explained("POOL_OBSERVATION_UNCONFIRMED", "waiting")
            if not runtime.model_ready or runtime.state not in {"ready", "running", "idle", "busy", "committing"}:
                return explained("POOL_LOADING", "waiting")
            if self.routing.targets.untracked_legacy_active(session):
                return explained("LEGACY_CALL_UNCONFIRMED", "blocked")
            request, _ = RoutingInventory(self.settings).build(session, task, policy, catalogs, now)
            request = request.model_copy(update={"routing_revision": route.routing_revision_id})
            decision = DevicePolicyService.evaluate(request)
            candidates = []
            by_target = {catalog.target_id: catalog for catalog in catalogs.values()}
            for item in decision.candidates:
                catalog = by_target[item.target_id]
                target = self.routing.targets.get_target(session, item.target_id)
                code = item.reason_code
                if not target.enabled:
                    code = "TARGET_DISABLED"
                elif self.routing.catalogs.latest_catalog(session, item.target_id).catalog_revision_id != catalog.catalog_revision_id:
                    code = "CATALOG_REVISION_CHANGED"
                candidates.append(SchedulingCandidateView(target_id=item.target_id, device_kind=item.device_kind,
                    eligible=item.eligible and code == item.reason_code, reason_code=code, message=reason_message(code)))
            message = reason_message(decision.reason_code)
            if decision.action == "select":
                message += "这只是当前候选建议，尚未领取或冻结目标，实际执行须重新准入。"
            return explained(decision.reason_code, "waiting", message=message, candidates=candidates)
        except AppError as exc:
            return explained(exc.code, "blocked", message=exc.message)
        except (ValueError, TypeError):
            return explained("ROUTING_RECORD_INVALID", "blocked")
