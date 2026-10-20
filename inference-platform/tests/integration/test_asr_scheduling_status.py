"""隔离库内的只读调度解释；无网络、无真实设备/模型能力结论。"""

from datetime import timedelta
import json
import sqlite3

import pytest
from sqlalchemy import event, select

from backend.data.asr.catalog_models import AsrResourceObservation
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.asr.routing_models import AsrTaskRoute
from backend.data.asr.target_models import AsrCallPermit, AsrComputeTarget, AsrTargetWorkerSession
from backend.data.runtime.runtime_models import RuntimeComponent
from backend.data.tasks.task_models import TaskRecord
from backend.modules.api.app import close_app, create_app
from backend.schema.asr.catalog_schema import CatalogRegistration
from backend.schema.asr.remote_schema import RemoteAsrRelease
from backend.schema.execution.execution_schema import FailureInfo
from backend.service.asr.device_policy_service import DevicePolicyService
from backend.service.asr.routing_service import AsrRoutingService
from tests.integration.test_asr_routing_execution import (
    acquire, fixture_result, no_network, observe, queued, routing_case, routing_factory, worker,
)
from tests.integration.test_asr_target_admission import seed_queued, target_case


def reject_operation(*args, **kwargs):
    raise AssertionError("只读调度查询不得探测上游或触发执行")


def snapshot(case):
    """仅测试的临时库；所有业务表与SQLite结构均逐项保留。"""
    with sqlite3.connect(case.settings.database_path) as connection:
        names = connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {name: sorted(connection.execute('SELECT * FROM "' + name + '"').fetchall(), key=repr)
                for (name,) in names}


@pytest.fixture
def app_factory(monkeypatch):
    apps = []
    monkeypatch.setattr("backend.service.asr.remote_v2_adapter.probe_remote_asr", reject_operation)
    monkeypatch.setattr("backend.service.asr.remote_service.RemoteAsrService.status", reject_operation)
    monkeypatch.setattr("backend.service.asr.remote_service.RemoteAsrService.ensure_available", reject_operation)

    def build(case, settings=None):
        if hasattr(case, "clock"):
            monkeypatch.setattr("backend.service.asr.scheduling_service.database_now", lambda session: case.clock[0])
        app = create_app(settings or getattr(case, "root_settings", case.settings))
        def prohibit_writes(_connection, _cursor, statement, _params, _context, _executemany):
            normalized = statement.lstrip().upper()
            if normalized.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER",
                                      "VACUUM", "BEGIN IMMEDIATE")):
                raise AssertionError("GET 调度诊断不能取得写锁或执行写语句")
        event.listen(app.extensions["database"].engine, "before_cursor_execute", prohibit_writes)
        apps.append(app)
        return app

    yield build
    for app in apps:
        close_app(app)


def detail(client, task_id):
    response = client.get(f"/api/tasks/{task_id}")
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def candidates(value):
    return {row["deviceKind"]: row for row in value["candidates"]}


def refresh_workers(case):
    with case.db.transaction(immediate=True) as session:
        session.get(RuntimeComponent, "remote_asr_worker").heartbeat_at = case.clock[0]
        for row in session.scalars(select(AsrTargetWorkerSession)):
            row.heartbeat_at = case.clock[0]


def test_legacy_scheduling_is_local_readonly_and_does_not_claim_gpu(target_case, app_factory):
    app = app_factory(target_case)
    before = snapshot(target_case)
    client = app.test_client()
    response = client.get("/api/asr/external/scheduling")
    assert response.status_code == 200
    value = response.get_json()["data"]
    assert value["mode"] == value["executionScope"] == "legacy"
    assert value["reasonCode"] == "LEGACY_FIXED_TARGET" and not value["routingConfigured"]
    assert value["candidateCount"] == 0 and value["message"]
    assert "available" not in value and "baseUrl" not in value
    assert client.get("/api/asr/external/scheduling?targetId=arbitrary").status_code == 422
    assert client.get("/api/asr/external/scheduling?mode=gpu&mode=cpu").status_code == 422
    assert client.post("/api/asr/external/scheduling", json={"enable": True}).status_code == 405
    assert snapshot(target_case) == before


def test_isolated_scheduling_reports_scope_and_count_without_private_connection(routing_case, app_factory):
    case = routing_case
    app = app_factory(case)
    before = snapshot(case)
    value = app.test_client().get("/api/asr/external/scheduling").get_json()["data"]
    assert value["mode"] == value["executionScope"] == "isolated"
    assert value["routingConfigured"] and value["candidateCount"] == 2
    assert value["reasonCode"] == "ISOLATED_ROUTING"
    assert snapshot(case) == before
    encoded = json.dumps(value)
    assert all(word not in encoded for word in (".invalid", "baseUrl", "caFile", "insecureTlsOrigin", "deviceEvidence"))


@pytest.mark.parametrize("changes", [
    {"remote_asr_routing_isolation": False}, {"remote_asr_routing_revision": "f" * 64},
])
def test_invalid_or_unapproved_routing_is_blocked_without_falling_back(routing_case, app_factory, changes):
    case = routing_case
    app = app_factory(case, case.root_settings.model_copy(update=changes))
    value = app.test_client().get("/api/asr/external/scheduling").get_json()["data"]
    assert value["mode"] == "blocked" and value["executionScope"] == "disabled"
    assert value["routingConfigured"] and value["reasonCode"] != "LEGACY_FIXED_TARGET"


def test_gpu_detail_explains_candidate_without_freezing_choice_or_granting_permit(routing_case, app_factory, monkeypatch):
    case = routing_case
    task_id = queued(case)
    app = app_factory(case)
    monkeypatch.setattr(AsrRoutingService, "choose", reject_operation)
    monkeypatch.setattr(AsrRoutingService, "reserve", reject_operation)
    before = snapshot(case)
    value = detail(app.test_client(), task_id)["routing"]
    assert value["selectionState"] == "waiting" and value["reasonCategory"] == "waiting"
    assert value["reasonCode"] == "GPU_PREFERRED"
    assert candidates(value)["gpu"]["eligible"]
    assert value["catalogRevisionId"] is None and value["targetId"] is None
    assert value["selectedAt"] is None and value["message"]
    assert snapshot(case) == before


def test_confirmed_gpu_full_explains_eligible_cpu_without_executing(routing_case, app_factory):
    case = routing_case
    observe(case, worker(case, "gpu"), available=0)
    task_id = queued(case)
    value = detail(app_factory(case).test_client(), task_id)["routing"]
    assert value["selectionState"] == "waiting" and value["reasonCode"] == "CPU_FALLBACK_CONFIRMED_GPU_FULL"
    assert candidates(value)["gpu"]["reasonCode"] == "CAPACITY_FULL"
    assert candidates(value)["cpu"]["eligible"]
    with case.db.session_factory() as session:
        assert session.get(AsrTaskRoute, task_id).selected_catalog_revision_id is None
        assert session.get(AsrCallPermit, task_id) is None


def test_unknown_capacity_does_not_become_idle_or_cpu_fallback(routing_case, app_factory):
    case = routing_case
    observe(case, worker(case, "gpu"), available=None, gpuMemoryFreeMiB=100_000,
            gpuUtilizationPercent=0.0)
    task_id = queued(case)
    value = detail(app_factory(case).test_client(), task_id)["routing"]
    assert value["reasonCode"] == "GPU_STATE_UNCONFIRMED"
    assert not candidates(value)["gpu"]["eligible"]
    assert candidates(value)["gpu"]["reasonCode"] == "CAPACITY_UNCONFIRMED"
    assert value["targetId"] is None


@pytest.mark.parametrize("offset", [60, 61, -1])
def test_resource_exact_expiry_and_clock_rollback_are_not_capacity(routing_case, app_factory, offset):
    case = routing_case
    task_id = queued(case)
    case.clock[0] += timedelta(seconds=offset)
    refresh_workers(case)
    value = detail(app_factory(case).test_client(), task_id)["routing"]
    assert value["reasonCode"] == "GPU_STATE_UNCONFIRMED"
    assert not candidates(value)["gpu"]["eligible"]
    assert value["catalogRevisionId"] is None


@pytest.mark.parametrize("cpu_changes,reason", [
    ({"cpuProfile": None}, "CPU_PROFILE_MISSING"),
    ({"cpuSupported": False}, "CPU_SUPPORT_UNCONFIRMED"),
    ({"maxAudioSeconds": 0.05}, "CPU_AUDIO_OUTSIDE_MEASURED_RANGE"),
])
def test_cpu_rejection_explains_actual_missing_condition(routing_factory, app_factory, cpu_changes, reason):
    case = routing_factory(cpu_changes=cpu_changes)
    observe(case, worker(case, "gpu"), available=0)
    task_id = queued(case)
    value = detail(app_factory(case).test_client(), task_id)["routing"]
    assert value["selectionState"] == "waiting"
    assert candidates(value)["cpu"]["reasonCode"] == reason
    assert not candidates(value)["cpu"]["eligible"]


def test_cpu_profile_exact_expiry_is_rejected_with_fresh_resource_reports(routing_factory, app_factory):
    case = routing_factory(cpu_changes={"validForSeconds": 60.0})
    task_id = queued(case)
    case.clock[0] += timedelta(seconds=60)
    refresh_workers(case)
    for row in case.records.values():
        observe(case, row, available=0 if row.kind == "gpu" else 1)
    value = detail(app_factory(case).test_client(), task_id)["routing"]
    assert candidates(value)["cpu"]["reasonCode"] == "CPU_PROFILE_EXPIRED_OR_FUTURE"
    assert value["targetId"] is None


@pytest.mark.parametrize("state", ["stopping", "draining"])
def test_pool_stop_intent_prevents_displaying_candidate_as_dispatchable(routing_case, app_factory, state):
    case = routing_case
    task_id = queued(case)
    with case.db.transaction(immediate=True) as session:
        session.get(RuntimeComponent, "remote_asr_worker").state = state
    app = app_factory(case)
    before = snapshot(case)
    value = detail(app.test_client(), task_id)["routing"]
    assert value["reasonCode"] == "POOL_STOPPING"
    assert value["reasonCategory"] == "waiting" and value["candidates"] == []
    assert snapshot(case) == before


def test_stale_pool_heartbeat_is_unconfirmed_not_stopped_or_ready(routing_case, app_factory):
    case = routing_case
    task_id = queued(case)
    with case.db.transaction(immediate=True) as session:
        session.get(RuntimeComponent, "remote_asr_worker").heartbeat_at -= timedelta(seconds=61)
    value = detail(app_factory(case).test_client(), task_id)["routing"]
    assert value["reasonCode"] == "POOL_OBSERVATION_UNCONFIRMED"
    assert value["reasonCategory"] == "waiting" and value["candidates"] == []


def test_disabled_candidate_stays_ineligible_without_replacing_frozen_route(routing_case, app_factory):
    case = routing_case
    task_id = queued(case)
    with case.db.transaction(immediate=True) as session:
        session.get(AsrComputeTarget, worker(case, "gpu").name).enabled = False
    value = detail(app_factory(case).test_client(), task_id)["routing"]
    assert value["reasonCode"] == "GPU_STATE_UNCONFIRMED"
    assert not candidates(value)["gpu"]["eligible"]
    assert candidates(value)["gpu"]["reasonCode"] == "TARGET_DISABLED"
    assert value["routingRevisionId"] == case.route_id and value["targetId"] is None


def test_new_catalog_metadata_does_not_silently_rebind_old_waiting_task(routing_case, app_factory):
    case = routing_case
    task_id = queued(case)
    gpu = worker(case, "gpu")
    with case.db.transaction(immediate=True) as session:
        old = case.catalog.repository.get_catalog(session, gpu.revision_id)
        metadata = dict(old.metadata_snapshot)
        session.get(AsrComputeTarget, gpu.name).enabled = False
    metadata.update(deviceEvidence="isolated updated mapping requires a new routing revision",
        connection={"baseUrl": f"https://{gpu.name}.invalid:8003", "modelId": "fixture-compatible-asr"})
    new = case.catalog.register(CatalogRegistration.model_validate(metadata))
    assert new.catalog_revision_id != gpu.revision_id
    with case.db.transaction(immediate=True) as session:
        # 隔离夹具显式启用，以单独验证目录修订漂移，非真实目标授权。
        session.get(AsrComputeTarget, gpu.name).enabled = True
    app = app_factory(case)
    before = snapshot(case)
    value = detail(app.test_client(), task_id)["routing"]
    assert value["reasonCode"] == "GPU_STATE_UNCONFIRMED"
    assert candidates(value)["gpu"]["reasonCode"] == "CATALOG_REVISION_CHANGED"
    assert not candidates(value)["gpu"]["eligible"]
    assert value["routingRevisionId"] == case.route_id and value["targetId"] is None
    assert snapshot(case) == before


def test_future_recording_time_is_unconfirmed_even_when_sampling_time_is_fresh(routing_case, app_factory):
    case = routing_case
    task_id = queued(case)
    gpu = worker(case, "gpu")
    with case.db.transaction(immediate=True) as session:
        for row in session.scalars(select(AsrResourceObservation).where(
                AsrResourceObservation.catalog_revision_id == gpu.revision_id)):
            row.recorded_at = case.clock[0] + timedelta(seconds=1)
    value = detail(app_factory(case).test_client(), task_id)["routing"]
    assert value["reasonCode"] == "GPU_STATE_UNCONFIRMED"
    assert not candidates(value)["gpu"]["eligible"] and value["targetId"] is None


def test_deadline_expiry_is_explained_without_mutating_queued_task(routing_case, app_factory):
    case = routing_case
    task_id = queued(case)
    with case.db.transaction(immediate=True) as session:
        session.get(TaskRecord, task_id).queue_deadline_at = case.clock[0]
    app = app_factory(case)
    before = snapshot(case)
    task = detail(app.test_client(), task_id)
    assert task["status"] == "queued"
    assert task["routing"]["reasonCode"] == "TASK_DEADLINE_EXPIRED"
    assert task["routing"]["candidates"] == []
    assert snapshot(case) == before


@pytest.mark.parametrize("state", ["cancelled", "failed"])
def test_unselected_terminal_task_never_recommends_a_new_candidate(routing_case, app_factory, state):
    case = routing_case
    task_id = queued(case)
    with case.db.transaction(immediate=True) as session:
        session.get(TaskRecord, task_id).status = state
    value = detail(app_factory(case).test_client(), task_id)["routing"]
    assert value["reasonCategory"] == "terminal" and value["candidates"] == []
    assert value["targetId"] is None


@pytest.mark.parametrize("unknown", [False, True])
def test_selected_history_keeps_reason_when_configuration_is_off_and_never_reevaluates(
        routing_case, app_factory, monkeypatch, unknown):
    case = routing_case
    gpu = worker(case, "gpu")
    task_id = queued(case)
    _, accepted = acquire(case, gpu, task_id)
    assert accepted.decision == "acquired"
    if unknown:
        assert gpu.executor.commit_failure(accepted.lease, FailureInfo(code="REMOTE_ASR_UNKNOWN", message="隔离断线"))
    else:
        assert gpu.executor.commit_result(accepted.lease, fixture_result())
    app = app_factory(case, case.root_settings.model_copy(update={
        "remote_asr_routing_revision": None, "remote_asr_routing_isolation": False}))
    monkeypatch.setattr(AsrRoutingService, "_load", reject_operation)
    monkeypatch.setattr(DevicePolicyService, "evaluate", reject_operation)
    before = snapshot(case)
    value = detail(app.test_client(), task_id)["routing"]
    assert value["selectionState"] == "selected" and value["reasonCode"] == "GPU_PREFERRED"
    assert value["targetId"] == gpu.name and value["catalogRevisionId"] == gpu.revision_id
    assert value["candidates"] == []
    assert value["reasonCategory"] == ("outcome_unknown" if unknown else "terminal")
    if unknown:
        assert value["statusCode"] in {"REMOTE_ASR_UNKNOWN", "REMOTE_ASR_RESULT_UNKNOWN"}
    assert snapshot(case) == before


def test_uncertain_call_without_task_error_still_explains_unknown_outcome(routing_case, app_factory):
    case = routing_case
    task_id = queued(case)
    _, acquired = acquire(case, worker(case, "gpu"), task_id)
    assert acquired.decision == "acquired"
    with case.db.transaction(immediate=True) as session:
        session.get(RemoteAsrCall, task_id).state = "uncertain"
    value = detail(app_factory(case).test_client(), task_id)["routing"]
    assert value["reasonCode"] == "GPU_PREFERRED" and value["reasonCategory"] == "outcome_unknown"


def test_manual_capacity_release_keeps_unknown_result_without_claiming_it_is_still_held(routing_case, app_factory):
    case = routing_case
    gpu = worker(case, "gpu")
    task_id = queued(case)
    _, accepted = acquire(case, gpu, task_id)
    assert accepted.decision == "acquired"
    assert gpu.executor.commit_failure(accepted.lease,
        FailureInfo(code="REMOTE_ASR_UNKNOWN", message="隔离断线，结果尚未确认"))
    with case.db.session_factory() as session:
        error_before = session.get(TaskRecord, task_id).error_json
        decision_before = session.get(AsrTaskRoute, task_id).decision_snapshot
        assert session.get(AsrCallPermit, task_id).state == "uncertain"
    receipt = case.remote.release_uncertain(RemoteAsrRelease(task_id=task_id,
        confirm_request_ended=True, note="隔离夹具模拟人工核对请求已结束，不代表真实远端确认"))
    assert receipt["released"] and not receipt["replayed"]
    with case.db.session_factory() as session:
        assert session.get(RemoteAsrCall, task_id).state == "released"
        assert session.get(AsrCallPermit, task_id).state == "released"
        assert session.get(TaskRecord, task_id).error_json == error_before
        assert session.get(AsrTaskRoute, task_id).decision_snapshot == decision_before
    app = app_factory(case)
    before = snapshot(case)
    task = detail(app.test_client(), task_id)
    value = task["routing"]
    assert task["status"] == "failed" and task["remoteOutcome"] == "unknown"
    assert value["selectionState"] == "selected" and value["reasonCode"] == "GPU_PREFERRED"
    assert value["targetId"] == gpu.name and value["catalogRevisionId"] == gpu.revision_id
    assert value["reasonCategory"] == "outcome_unknown" and value["candidates"] == []
    assert "保留原目标及占用" not in value["message"]
    assert "容量占用请查看调用记录" in value["message"]
    assert "不能自动重发" in value["message"]
    assert snapshot(case) == before


def test_legacy_tasks_keep_no_routing_and_diagnostics_expose_no_raw_evidence(routing_case, app_factory):
    case = routing_case
    legacy_id = seed_queued(case)
    routed_id = queued(case)
    client = app_factory(case).test_client()
    assert "routing" not in detail(client, legacy_id)
    value = detail(client, routed_id)["routing"]
    encoded = json.dumps(value, ensure_ascii=False)
    assert all(word not in encoded for word in ("https://", ".invalid", "baseUrl", "caFile",
        "insecureTlsOrigin", "compatibilityEvidence", "deviceEvidence", "configFingerprint",
        "evidenceReference", "isolated observation only", "isolated same-model contract only"))


def test_task_list_is_batched_snapshot_not_one_live_policy_evaluation_per_task(routing_case, app_factory, monkeypatch):
    case = routing_case
    for _ in range(12):
        queued(case)
    app = app_factory(case)
    client = app.test_client()
    monkeypatch.setattr(DevicePolicyService, "evaluate", reject_operation)
    queries = []

    def track(_connection, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(app.extensions["database"].engine, "before_cursor_execute", track)
    try:
        one = client.get("/api/tasks?pageSize=1")
        assert one.status_code == 200
        first_count = len(queries)
        queries.clear()
        all_rows = client.get("/api/tasks?pageSize=20")
        assert all_rows.status_code == 200
        items = all_rows.get_json()["data"]["items"]
        assert len(items) == 12
        assert all(item["routing"]["candidates"] == [] for item in items)
        assert len(queries) <= first_count + 2, (first_count, len(queries), queries)
    finally:
        event.remove(app.extensions["database"].engine, "before_cursor_execute", track)
