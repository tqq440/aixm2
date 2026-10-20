"""用数据库时钟判断组件报告是否过期；缺报告不等同于就绪。"""

from sqlalchemy.exc import SQLAlchemyError

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.schema.runtime.runtime_schema import ComponentView, RuntimeView
from backend.utils.time.time_utils import as_utc
from backend.service.asr.adapter_catalog import remote_asr_configured


class RuntimeService:
    def __init__(self, repository: RuntimeRepository, settings: AppSettings):
        self.repository = repository
        self.settings = settings

    def get_status(self) -> RuntimeView:
        try:
            records = {row.component_id: row for row in self.repository.list_components()}
            control, launch, managed = self.repository.asr_management()
            now = self.repository.now()
        except SQLAlchemyError as exc:
            raise AppError("RUNTIME_STORAGE_UNAVAILABLE", "无法读取运行报告，请检查数据库和迁移。", 503) from exc

        def view(component_id):
            row = records.get(component_id)
            if row is None:
                return ComponentView(component_id=component_id, availability="not_started")
            age = max(0, (as_utc(now) - as_utc(row.heartbeat_at)).total_seconds())
            if row.state == "stopped":
                availability = "stopped"
            elif age > self.settings.runtime_stale_seconds:
                availability = "stale"
            elif row.state == "error":
                availability = "error"
            else:
                availability = "online"
            ready = availability == "online" and row.state in {"idle", "ready"}
            if component_id in {"worker", "vision_worker", "remote_asr_worker"}:
                ready = ready and row.model_ready
            return ComponentView(
                component_id=component_id, reported_state=row.state, availability=availability,
                ready=ready, model_ready=bool(row.model_ready) and availability == "online",
                pid=row.pid, child_pid=row.child_pid, active_task_id=row.active_task_id,
                heartbeat_at=row.heartbeat_at, heartbeat_age_seconds=age, last_error=row.last_error,
            )

        worker = self._managed_worker(records.get("worker"), control, launch, now) if managed else view("worker")
        external = remote_asr_configured(self.settings)
        return RuntimeView(observed_at=now, worker=worker, vision_worker=view("vision_worker"), dispatcher=view("dispatcher"),
                           remote_asr_worker=view("remote_asr_worker") if external or "remote_asr_worker" in records else None,
                           external_call_concurrency=self.repository.external_call_limit() if external else 0)

    def _managed_worker(self, worker, control, launch, now):
        """管理意图限制旧组件就绪报告；模型仍在排空时保留已加载这一事实。"""
        if control is None:
            return ComponentView(component_id="worker", reported_state="awaiting_agent", availability="not_started")
        age = (as_utc(now) - as_utc(control.heartbeat_at)).total_seconds()
        current = (worker is not None and worker.session_id == control.worker_session_id
                   and worker.pid == control.agent_pid)
        exit_confirmed = (control.worker_session_id is None or
                          (launch is not None and launch.instance_id == control.instance_id
                           and launch.exit_confirmed_at is not None))
        fields = dict(component_id="worker", ready=False, model_ready=False,
                      pid=control.agent_pid if control.worker_session_id else None,
                      child_pid=control.child_pid, heartbeat_at=control.heartbeat_at,
                      heartbeat_age_seconds=max(0, age), last_error=control.last_error)
        if control.state == "stopped" and exit_confirmed:
            fields["child_pid"] = None
            return ComponentView(**fields, reported_state="stopped", availability="stopped")
        if control.state in {"failed", "stop_unconfirmed"}:
            return ComponentView(**fields, reported_state=control.state, availability="error")
        if (control.shutdown_confirmed_at is not None or age < 0
                or age > self.settings.runtime_stale_seconds):
            return ComponentView(**fields, reported_state="unknown", availability="stale")
        if control.state == "starting":
            return ComponentView(**fields, reported_state="model_loading", availability="online")
        if not current:
            fields["last_error"] = control.last_error or "ASR_RUNTIME_SESSION_MISMATCH"
            return ComponentView(**fields, reported_state="unknown", availability="error")
        worker_age = (as_utc(now) - as_utc(worker.heartbeat_at)).total_seconds()
        if worker_age < 0 or worker_age > self.settings.runtime_stale_seconds:
            return ComponentView(**fields, reported_state="unknown", availability="stale")
        fields.update(child_pid=worker.child_pid, active_task_id=worker.active_task_id,
                      model_ready=bool(control.model_ready and worker.model_ready),
                      last_error=control.last_error or worker.last_error)
        stopping = control.desired_state == "stopped" or control.state == "stopping"
        if stopping:
            return ComponentView(**fields, reported_state="draining", availability="online")
        if worker.state in {"error", "stopped"}:
            fields["model_ready"] = False
            return ComponentView(**fields, reported_state=worker.state,
                                 availability="error" if worker.state == "error" else "stopped")
        fields["ready"] = bool(control.state == "ready" and worker.state == "idle" and fields["model_ready"])
        reported = "busy" if control.state == "busy" or worker.state == "busy" else worker.state
        return ComponentView(**fields, reported_state=reported, availability="online")
