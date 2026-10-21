"""4B 隔离故障验收；DockerFake 和 BusinessFake 不是实际容器。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from backend.common.exceptions.app_error import AppError
from backend.data.host_node.node_models import HostNodeEvent, HostNodeInstance, HostNodeOperation
from backend.data.host_node.node_repository import NodeRepository
from backend.modules.host_node.app import close_node_app, create_node_app
from tests.integration.host_control_support import (
    FIRST, SECOND, control_case, finish_start, paused_ready, runtime_config, submit,
)
from tests.integration.host_node_support import ADMIN, OBSERVER, REPORT, auth, data, report_body


@pytest.fixture
def case(tmp_path):
    value = control_case(tmp_path)
    yield value
    close_node_app(value.app)


def test_start_waits_for_warmup_and_resume_report_and_opens_gate_once(case):
    operation, command = submit(case)
    assert not case.docker.actions
    case.control.tick(); case.control.tick()
    assert case.docker.actions == [("start", FIRST)]
    assert case.service.get_service("asr")["acceptingRequests"] is False
    case.control.tick()
    assert case.control.get_operation(operation["operationId"])["status"] == "running"
    paused_ready(case)
    case.control.tick(); case.control.tick()
    snapshot = case.service.get_service("asr")
    assert snapshot["acceptingRequests"] is True and snapshot["containerState"] == "running"
    assert snapshot["executionReady"] is False
    repeated = data(case.client.post("/node/v1/services/asr/operations", headers=auth(), json=command), 202)
    assert repeated["operationId"] == operation["operationId"]
    assert case.docker.actions == [("start", FIRST)]


def test_stop_requires_correlated_drain_and_confirmed_exit(case):
    finish_start(case)
    operation, _ = submit(case, "stop")
    assert not case.service.get_service("asr")["acceptingRequests"]
    case.business.ack_only = True
    case.control.tick()
    assert len(case.business.commands) == 2
    case.control.tick()
    assert case.docker.actions == [("start", FIRST)]
    entry = case.service.get_service("asr")
    case.service.report("asr", report_body(entry, entry["sequence"] + 1, "draining", activeTasks=0, queuedTasks=0))
    case.control.tick()
    assert case.docker.actions == [("start", FIRST)]
    case.service.report("asr", report_body(entry, entry["sequence"] + 2, "draining", activeTasks=0, queuedTasks=0,
        controlOperationId=operation["operationId"], controlAction="drain"))
    case.control.tick(); case.control.tick()
    assert case.docker.actions == [("start", FIRST), ("SIGTERM", FIRST)]
    assert case.control.get_operation(operation["operationId"])["status"] == "succeeded"
    snapshot = case.service.get_service("asr")
    assert snapshot["containerState"] == "exited" and not snapshot["acceptingRequests"]


def test_busy_or_unknown_drain_times_out_without_forcing_a_stop(case):
    finish_start(case)
    operation, _ = submit(case, "stop")
    case.business.ack_only = True
    case.control.tick()
    entry = case.service.get_service("asr")
    case.service.report("asr", report_body(entry, entry["sequence"] + 1, "draining", activeTasks=1, queuedTasks=2,
        controlOperationId=operation["operationId"], controlAction="drain"))
    case.clock.advance(11); case.control.tick()
    assert case.control.get_operation(operation["operationId"])["errorCode"] == "DRAIN_UNCONFIRMED"
    assert case.docker.actions == [("start", FIRST)]


def test_successful_upgrade_stops_old_then_warms_new_before_admission(case):
    finish_start(case)
    operation, _ = submit(case, "upgrade", "v2")
    for _ in range(4):
        case.control.tick()
    assert case.docker.actions == [("start", FIRST), ("SIGTERM", FIRST), ("start", SECOND)]
    assert case.service.get_service("asr")["control"]["gateClosed"]
    paused_ready(case)
    case.control.tick(); case.control.tick()
    assert case.control.get_operation(operation["operationId"])["status"] == "succeeded"
    assert case.service.get_service("asr")["control"]["currentVersion"] == "v2"


def test_failed_candidate_rolls_back_only_before_admission_and_with_empty_queue(case):
    finish_start(case)
    operation, _ = submit(case, "upgrade", "v2")
    for _ in range(4): case.control.tick()
    paused_ready(case, state="failed")
    case.clock.advance(11); case.control.tick(); case.control.tick(); case.control.tick()
    assert case.docker.actions == [("start", FIRST), ("SIGTERM", FIRST), ("start", SECOND),
                                  ("SIGTERM", SECOND), ("start", FIRST)]
    paused_ready(case)
    case.control.tick(); case.control.tick()
    final = case.control.get_operation(operation["operationId"])
    assert final["status"] == "rolled_back" and final["requestedVersion"] == "v2" and final["effectiveVersion"] == "v1"
    assert case.service.get_service("asr")["acceptingRequests"]


def test_missing_candidate_progress_or_unknown_queue_cannot_trigger_rollback(case):
    finish_start(case)
    operation, _ = submit(case, "upgrade", "v2")
    for _ in range(4): case.control.tick()
    case.clock.advance(11); case.control.tick()
    assert case.control.get_operation(operation["operationId"])["status"] == "blocked"
    assert case.docker.actions[-1] == ("start", SECOND)


def test_past_candidate_activity_prevents_rollback_even_after_queue_becomes_empty(case):
    finish_start(case)
    operation, _ = submit(case, "upgrade", "v2")
    for _ in range(4): case.control.tick()
    entry = case.service.get_service("asr")
    case.service.report("asr", report_body(entry, 1, "busy", acceptingRequests=False, activeTasks=1, queuedTasks=0))
    paused_ready(case, state="failed")
    case.clock.advance(11); case.control.tick()
    assert case.control.get_operation(operation["operationId"])["status"] == "blocked"
    assert case.docker.actions[-1] == ("start", SECOND)


def test_resume_outcome_unknown_does_not_roll_back_a_possibly_serving_candidate(case):
    finish_start(case)
    operation, _ = submit(case, "upgrade", "v2")
    for _ in range(4): case.control.tick()
    paused_ready(case)
    case.business.fail = True
    case.control.tick()
    assert case.control.get_operation(operation["operationId"])["status"] == "unknown"
    assert len(case.docker.actions) == 3
    case.business.fail = False
    case.control.reconcile(operation["operationId"], {"expectedControlVersion": operation["controlVersion"]})
    case.control.tick()
    assert case.control.get_operation(operation["operationId"])["status"] == "succeeded"
    assert len(case.docker.actions) == 3


@pytest.mark.parametrize("change,error", [
    ({"imageId": "sha256:" + "f" * 64}, "CONTAINER_OWNERSHIP_MISMATCH"),
    ({"labels": {}}, "CONTAINER_OWNERSHIP_MISMATCH"),
    ({"restartPolicy": "always"}, "CONTAINER_LIFECYCLE_NOT_EXCLUSIVE"),
    ({"autoRemove": True}, "CONTAINER_LIFECYCLE_NOT_EXCLUSIVE"),
])
def test_unowned_or_independently_restarting_containers_are_never_modified(case, change, error):
    case.docker.containers[FIRST].update(change)
    operation, _ = submit(case)
    case.control.tick()
    assert case.control.get_operation(operation["operationId"])["errorCode"] == error
    assert case.control.get_operation(operation["operationId"])["status"] == "failed"
    assert not case.docker.actions


def test_repeated_submit_cannot_create_two_active_operations(case):
    runtime = case.control.runtime("asr")
    commands = [{"operationId": str(uuid4()), "action": "start", "versionId": "v1",
        "expectedControlVersion": runtime["controlVersion"], "configRevision": case.entry["configRevision"]} for _ in range(8)]
    def call(command):
        try: return case.control.submit("asr", command)
        except AppError as error: return error.code
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(call, commands))
    assert sum(isinstance(value, dict) for value in results) == 1


def test_start_reply_loss_is_reconciled_without_a_second_start(case):
    case.docker.start_unknown = True
    operation, _ = submit(case)
    case.control.tick(); case.control.tick()
    assert case.control.get_operation(operation["operationId"])["phase"] == "starting_issued"
    case.control.tick()
    assert case.docker.actions == [("start", FIRST)]
    case.docker.start_unknown = False
    paused_ready(case)
    case.control.reconcile(operation["operationId"], {"expectedControlVersion": operation["controlVersion"]})
    case.control.tick(); case.control.tick()
    assert case.control.get_operation(operation["operationId"])["status"] == "succeeded"
    assert case.docker.actions == [("start", FIRST)]


def test_stop_reply_loss_never_resends_term(case):
    finish_start(case)
    case.docker.term_unknown = True
    operation, _ = submit(case, "stop")
    case.control.tick(); case.control.tick()
    assert case.control.get_operation(operation["operationId"])["status"] == "unknown"
    case.docker.term_unknown = False
    case.control.reconcile(operation["operationId"], {"expectedControlVersion": operation["controlVersion"]})
    case.control.tick()
    assert case.control.get_operation(operation["operationId"])["status"] == "succeeded"
    assert case.docker.actions.count(("SIGTERM", FIRST)) == 1


def test_ignored_term_blocks_without_sigkill(case):
    finish_start(case)
    case.docker.ignore_term = True
    operation, _ = submit(case, "stop")
    case.control.tick(); case.control.tick()
    case.clock.advance(11); case.control.tick()
    assert case.control.get_operation(operation["operationId"])["errorCode"] == "CONTAINER_EXIT_UNCONFIRMED"
    assert case.docker.actions.count(("SIGTERM", FIRST)) == 1


def test_node_restart_preserves_container_and_pauses_unfinished_control(case):
    operation, _ = submit(case)
    case.control.tick(); case.control.tick()
    close_node_app(case.app)
    case.app = create_node_app(case.settings, clock=case.clock, docker=case.docker, business=case.business)
    case.client = case.app.test_client(); case.service = case.app.extensions["host_node_service"]
    case.business.node = case.service; case.control = case.app.extensions["host_node_control"]
    assert case.control.get_operation(operation["operationId"])["errorCode"] == "NODE_RESTART_RECONCILE_REQUIRED"
    case.control.tick()
    assert case.docker.actions == [("start", FIRST)]
    paused_ready(case)
    case.control.reconcile(operation["operationId"], {"expectedControlVersion": operation["controlVersion"]})
    case.control.tick(); case.control.tick()
    assert case.control.get_operation(operation["operationId"])["status"] == "succeeded"


def test_explicit_abort_requires_all_containers_stopped_and_keeps_admission_closed(case):
    case.docker.start_unknown = True
    operation, _ = submit(case)
    case.control.tick(); case.control.tick()
    command = {"expectedControlVersion": operation["controlVersion"], "evidence": "人工核对：未决Docker命令已结束，所有容器已退出。"}
    with pytest.raises(AppError, match="必须确认"):
        case.control.abort(operation["operationId"], command)
    case.docker.containers[FIRST].update(state="exited", running=False)
    result = case.control.abort(operation["operationId"], command)
    assert result["status"] == "aborted"
    assert case.service.get_service("asr")["control"]["gateClosed"]
    with case.service.database.transaction() as session:
        operation_record = session.get(HostNodeOperation, operation["operationId"])
        assert command["evidence"] not in repr(operation_record.to_dict())


def test_gets_are_readonly_and_permissions_do_not_cross(case):
    operation, _ = submit(case)
    before = (len(case.docker.actions), len(case.docker.inspections))
    with case.service.database.transaction() as session:
        events = session.scalar(select(func.count()).select_from(HostNodeEvent))
    for _ in range(3):
        for path in ("/node/v1/services/asr", "/node/v1/resources", "/node/v1/services/asr/operations",
                     "/node/v1/operations/" + operation["operationId"]):
            assert case.client.get(path, headers=auth(OBSERVER)).status_code == 200
    assert before == (len(case.docker.actions), len(case.docker.inspections))
    with case.service.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(HostNodeEvent)) == events
    assert case.client.put("/node/v1/admin/services/asr/runtime", headers=auth(REPORT), json={}).status_code == 401
    assert case.client.post("/node/v1/services/asr/operations", headers=auth(OBSERVER), json={}).status_code == 401
    assert case.client.get("/node/internal/v1/services/asr/assignment", headers=auth(ADMIN)).status_code == 401


def test_runtime_prevents_manual_instance_bypass_and_live_config_change(case):
    operation, _ = submit(case)
    case.control.tick(); case.control.tick()
    assert case.client.post("/node/v1/admin/services/asr/instances", headers=auth(), json={"instanceId": str(uuid4()),
        "configRevision": case.entry["configRevision"], "expectedGeneration": 1}).status_code == 409
    assert case.client.put("/node/v1/admin/services/asr/runtime", headers=auth(), json={
        "expectedControlVersion": operation["controlVersion"], "config": runtime_config(readyTimeoutSeconds=50)}).status_code == 409
    entry = data(case.client.get("/node/internal/v1/services/asr/assignment", headers=auth(REPORT)))
    assert entry["startPaused"] is True and entry["operationId"] == operation["operationId"]


def test_missing_drain_interface_is_recorded_without_stopping_container(case):
    profile = runtime_config(drainPath=None, resumePath=None, autoRollback=False, rollbackCompatibilityEvidence=None)
    case.control.save_runtime("asr", {"expectedControlVersion": 1, "config": profile})
    start, _ = submit(case)
    case.control.tick(); case.control.tick()
    entry = case.service.get_service("asr")
    case.service.report("asr", report_body(entry, 1, "ready", queuedTasks=0, activeTasks=0))
    case.control.tick()
    assert case.control.get_operation(start["operationId"])["status"] == "succeeded"
    stop, _ = submit(case, "stop")
    case.control.tick()
    assert case.control.get_operation(stop["operationId"])["errorCode"] == "BUSINESS_DRAIN_UNSUPPORTED"
    assert case.docker.actions == [("start", FIRST)] and not case.business.commands


def test_container_identity_cannot_be_registered_as_another_service(case):
    from tests.integration.host_node_support import register
    register(case, "vision", kind="vision")
    with pytest.raises(AppError, match="其他服务"):
        case.control.save_runtime("vision", {"expectedControlVersion": 0, "config": runtime_config()})


def test_failed_control_event_commit_cannot_start_a_container(case, monkeypatch):
    operation, _ = submit(case)
    case.control.tick()
    before = case.service.get_service("asr")["generation"]
    def broken(*_args, **_kwargs): raise RuntimeError("isolated control event write failure")
    with monkeypatch.context() as context:
        context.setattr(NodeRepository, "event", broken)
        with pytest.raises(RuntimeError): case.control.tick()
    assert case.docker.actions == []
    assert case.service.get_service("asr")["generation"] == before
    assert case.control.get_operation(operation["operationId"])["phase"] == "bind_target"
