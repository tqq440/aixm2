"""两类执行槽位分别由真实心跳声明就绪，配置槽数不是活跃进程数。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.config.settings.app_settings import AppSettings
from backend.service.runtime.runtime_service import RuntimeService


def test_two_configured_slots_do_not_claim_missing_workers_are_running():
    repository = Mock()
    repository.now.return_value = datetime.now(timezone.utc)
    repository.list_components.return_value = []
    repository.asr_management.return_value = (None, None, False)
    status = RuntimeService(repository, AppSettings()).get_status()
    assert status.execution_concurrency == 2
    for component in (status.worker, status.vision_worker, status.dispatcher):
        assert component.availability == "not_started"
        assert not component.ready and not component.model_ready
    assert "visionWorker" in status.model_dump(by_alias=True)


@pytest.mark.parametrize("component_id", ["worker", "vision_worker"])
@pytest.mark.parametrize("state,model_ready,age,availability,ready", [
    ("model_loading", False, 0, "online", False),
    ("idle", False, 0, "online", False),
    ("idle", True, 0, "online", True),
    ("busy", True, 0, "online", False),
    ("idle", True, 3600, "stale", False),
    ("stopped", False, 0, "stopped", False),
    ("error", False, 0, "error", False),
])
def test_each_worker_readiness_depends_on_its_own_fresh_model_report(component_id, state, model_ready, age, availability, ready):
    now = datetime.now(timezone.utc)
    repository = Mock()
    repository.now.return_value = now
    repository.list_components.return_value = [SimpleNamespace(component_id=component_id, state=state, model_ready=model_ready, heartbeat_at=now - timedelta(seconds=age), pid=123, child_pid=456, active_task_id=None, last_error=None)]
    repository.asr_management.return_value = (None, None, False)
    status = RuntimeService(repository, AppSettings()).get_status()
    component = getattr(status, component_id)
    assert component.availability == availability and component.ready == ready
    assert component.model_ready == (model_ready and availability == "online")
    other = status.vision_worker if component_id == "worker" else status.worker
    assert other.availability == "not_started" and not other.ready


@pytest.fixture
def managed_runtime():
    now = datetime(2026, 10, 14, 3, 0, 0)
    worker = SimpleNamespace(component_id="worker", session_id="a" * 32, pid=123, child_pid=456,
                             state="idle", model_ready=True, heartbeat_at=now, active_task_id=None, last_error=None)
    control = SimpleNamespace(instance_id="b" * 32, agent_pid=123, worker_session_id="a" * 32,
                              state="ready", desired_state="running", model_ready=True, child_pid=456,
                              heartbeat_at=now, shutdown_confirmed_at=None, last_error=None)
    launch = SimpleNamespace(instance_id="b" * 32, exit_confirmed_at=None)
    repository = Mock()
    repository.now.return_value = now
    repository.list_components.return_value = [worker]
    repository.asr_management.return_value = (control, launch, True)
    return RuntimeService(repository, AppSettings()), repository, control, launch, worker, now


def test_registered_instance_without_agent_never_reuses_legacy_ready_report(managed_runtime):
    service, repository, *_ = managed_runtime
    repository.asr_management.return_value = (None, None, True)
    view = service.get_status().worker
    assert view.availability == "not_started" and not view.ready and not view.model_ready
    assert view.pid is None and view.child_pid is None


def test_managed_ready_requires_matching_worker_and_model_state(managed_runtime):
    service, *_ = managed_runtime
    view = service.get_status().worker
    assert view.ready and view.model_ready and view.availability == "online"


@pytest.mark.parametrize("field,value", [("state", "busy"), ("desired_state", "stopped"), ("state", "stopping")])
def test_busy_or_stop_intent_closes_readiness_without_faking_model_unloaded(managed_runtime, field, value):
    service, _, control, _, worker, *_ = managed_runtime
    setattr(control, field, value)
    worker.active_task_id = "d" * 32
    view = service.get_status().worker
    assert not view.ready and view.model_ready
    assert view.active_task_id == "d" * 32
    assert view.reported_state == ("busy" if value == "busy" else "draining")


@pytest.mark.parametrize("field,value", [("session_id", "c" * 32), ("pid", 999)])
def test_old_or_unrelated_worker_cannot_supply_agent_model_readiness(managed_runtime, field, value):
    service, _, _, _, worker, *_ = managed_runtime
    setattr(worker, field, value)
    view = service.get_status().worker
    assert not view.ready and not view.model_ready and view.availability == "error"
    assert view.last_error == "ASR_RUNTIME_SESSION_MISMATCH"


@pytest.mark.parametrize("target,seconds", [("agent", -20), ("worker", -20), ("agent", 20), ("worker", 20)])
def test_expired_or_future_report_cannot_supply_managed_readiness(managed_runtime, target, seconds):
    service, _, control, _, worker, now = managed_runtime
    row = control if target == "agent" else worker
    row.heartbeat_at = now + timedelta(seconds=seconds)
    view = service.get_status().worker
    assert view.availability == "stale" and not view.ready and not view.model_ready


@pytest.mark.parametrize("state,expected", [("stop_unconfirmed", "error"), ("failed", "error"), ("starting", "online")])
def test_agent_uncertainty_or_loading_overrides_fresh_idle_worker(managed_runtime, state, expected):
    service, _, control, *_ = managed_runtime
    control.state = state
    view = service.get_status().worker
    assert view.availability == expected and not view.ready and not view.model_ready
    assert view.reported_state == ("model_loading" if state == "starting" else state)


def test_only_current_instance_exit_evidence_allows_managed_stopped_view(managed_runtime):
    service, _, control, launch, _, now = managed_runtime
    control.state = "stopped"
    control.desired_state = "stopped"
    launch.exit_confirmed_at = now
    view = service.get_status().worker
    assert view.availability == "stopped" and not view.ready and not view.model_ready
    launch.instance_id = "c" * 32
    view = service.get_status().worker
    assert view.availability != "stopped" and not view.ready


def test_worker_busy_beats_slightly_older_agent_idle_report(managed_runtime):
    service, _, _, _, worker, *_ = managed_runtime
    worker.state = "busy"
    worker.active_task_id = "d" * 32
    view = service.get_status().worker
    assert view.reported_state == "busy" and not view.ready and view.model_ready
