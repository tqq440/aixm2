"""真实SQLite只读容量投影；不访问课题组、不把隔离容量标为GPU。"""

from datetime import timedelta
from uuid import uuid4

from backend.data.asr.target_models import AsrCapacityGroup, AsrComputeTarget, AsrTargetWorkerSession
from backend.data.database.database import database_now
from backend.modules.api.app import close_app, create_app
from backend.service.asr.target_status import TargetStatusService
from tests.integration.test_asr_target_admission import target_case, ready_target, seed_queued, grant_target


def test_capacity_get_is_readonly_unregistered_and_does_not_probe(target_case, monkeypatch):
    case = target_case
    monkeypatch.setattr("backend.service.asr.remote_v2_adapter.probe_remote_asr",
                        lambda *_: (_ for _ in ()).throw(AssertionError("must not probe")))
    app = create_app(case.settings)
    try:
        with app.test_client() as client:
            response = client.get("/api/asr/external/capacity")
            assert response.status_code == 200
            value = response.json["data"]
            assert value["registered"] is False and value["deviceKind"] == "unknown"
            assert value["targetLimit"] == value["remainingCapacity"] == 0
            assert value["slots"] == []
            assert client.get("/api/asr/external/capacity?targetId=arbitrary").status_code == 422
            assert client.post("/api/asr/external/capacity", json={"limit": 99}).status_code == 405
        assert TargetStatusService(case.db, case.settings).status().registered is False
    finally:
        close_app(app)


def test_two_slots_shared_counts_unknown_and_old_queue_are_distinct(target_case):
    case = target_case
    first = ready_target(case)
    with case.db.transaction(immediate=True) as session:
        session.get(AsrComputeTarget, "remote-default").target_limit = 2
        session.get(AsrCapacityGroup, "remote-asr-shared").capacity_limit = 2
    second = uuid4().hex
    first.service.bind_worker(second, pid=124, slot_index=1)
    first.service.report_worker(second, "ready", True)
    active = seed_queued(case)
    assert grant_target(case, first, active)
    with case.execution.lock_task(active) as tx:
        first.service.finish(tx, confirmed=False)
    seed_queued(case)
    seed_queued(case, settings=case.settings.model_copy(update={"remote_asr_model_version": "different"}))
    value = TargetStatusService(case.db, case.settings).status()
    assert value.device_kind == "unknown" and value.target_limit == value.group_limit == 2
    assert (value.held, value.uncertain, value.group_uncertain, value.remaining_capacity) == (0, 1, 1, 1)
    assert value.queued == value.incompatible_queued == 1
    assert len(value.slots) == 2 and all(slot.fresh for slot in value.slots)
    assert value.active_calls[0].task_id == active and value.active_calls[0].state == "uncertain"
    assert value.active_calls_total == 1 and not value.active_calls_truncated
    exposed = value.model_dump_json(by_alias=True)
    assert "target-fixture.invalid" not in exposed and "caFile" not in exposed and "insecureTlsOrigin" not in exposed
    app = create_app(case.settings)
    try:
        runtime = app.test_client().get("/api/runtime").json["data"]
        assert runtime["externalCallConcurrency"] == 2
        assert runtime["remoteAsrWorker"]["availability"] == "not_started"
    finally:
        close_app(app)


def test_stale_worker_is_not_ready_and_stopped_history_remains_visible(target_case):
    case = target_case
    first = ready_target(case)
    with case.db.transaction(immediate=True) as session:
        worker = session.get(AsrTargetWorkerSession, first.session_id)
        worker.heartbeat_at = database_now(session) - timedelta(seconds=60)
        worker.last_error = "REMOTE_ASR_RESULT_UNKNOWN"
    value = TargetStatusService(case.db, case.settings).status()
    assert value.slots[0].state == "stale" and not value.slots[0].model_ready
    assert value.slots[0].last_error == "REMOTE_ASR_RESULT_UNKNOWN"
    first.service.report_worker(first.session_id, "stopped", False)
    stopped = TargetStatusService(case.db, case.settings).status().slots[0]
    assert stopped.state == "stopped" and not stopped.fresh


def test_new_configuration_does_not_present_previous_worker_as_ready(target_case):
    case = target_case
    ready_target(case)
    changed = case.settings.model_copy(update={"remote_asr_model_version": "new-binding"})
    value = TargetStatusService(case.db, changed).status()
    assert value.revision_id is None and value.remaining_capacity == 0
    assert value.slots[0].state == "configuration_changed" and not value.slots[0].fresh


def test_pool_slot_count_respects_the_smaller_shared_group_limit(target_case):
    case = target_case
    ready_target(case)
    with case.db.transaction(immediate=True) as session:
        session.get(AsrComputeTarget, "remote-default").target_limit = 3
    value = TargetStatusService(case.db, case.settings).status()
    assert (value.target_limit, value.group_limit) == (3, 1)
    assert len(value.slots) == 1
