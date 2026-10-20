"""2B独立审查回归：临时SQLite和假HTTP进程，不访问真实上游。"""

import logging
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from filelock import FileLock, Timeout
import pytest
from sqlalchemy import select

from backend.common.exceptions.app_error import AppError
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.asr.target_models import (
    AsrCapacityGroup, AsrComputeTarget, AsrTargetRevision, AsrTargetWorkerSession,
)
from backend.data.database.database import database_now
from backend.data.execution.attempt_models import ExecutionAttempt
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.data.tasks.task_models import TaskRecord
from backend.modules.api.app import close_app, create_app
from backend.service.asr.remote_service import RemoteAsrService
from backend.service.asr.target_service import TargetAdmissionService
from backend.service.worker.worker_service import WorkerService
from tests.integration.test_asr_target_admission import (
    target_case, grant_target, permits, race_grants, ready_target, seed_queued, task_snapshot,
)
from tests.unit.test_worker_service import FakeRunner


def capacity(case, limit):
    """直接设置隔离夹具容量；不是日常维护入口或真实容量证明。"""
    with case.db.transaction(immediate=True) as session:
        session.get(AsrComputeTarget, "remote-default").target_limit = limit
        session.get(AsrCapacityGroup, "remote-asr-shared").capacity_limit = limit


def call_from_permit(case, task_id):
    with case.db.transaction(immediate=True) as session:
        task, call = session.get(TaskRecord, task_id), session.get(RemoteAsrCall, task_id)
        call.state, call.worker_id, call.lease_token = "held", task.lease_worker_id, task.lease_token
        call.reserved_at = database_now(session)


def pooled_worker(case, runner, remote):
    broker = Mock()
    broker.receive.return_value = None
    return WorkerService(case.db, case.settings, logging.getLogger("2b-independent-review"),
        runner=runner, broker=broker, remote_service=remote, asr_profile="remote", remote_pooled=True)


def test_one_session_cannot_take_two_concurrent_leases_when_group_has_spare_capacity(target_case):
    case = target_case
    owner = ready_target(case)
    capacity(case, 2)
    tasks = [seed_queued(case), seed_queued(case)]
    before = {task: task_snapshot(case, task) for task in tasks}
    tokens = race_grants(case, [(owner, task) for task in tasks])
    assert sum(token is not None for token in tokens) == 1
    assert len(permits(case)) == 1
    with case.db.session_factory() as session:
        assert len(list(session.scalars(select(ExecutionAttempt)))) == 1
    for task, token in zip(tasks, tokens):
        if token is None:
            assert task_snapshot(case, task) == before[task]


def test_unknown_permit_survives_session_restart_and_alias_when_capacity_one(target_case):
    case = target_case
    owner = ready_target(case)
    task = seed_queued(case)
    assert grant_target(case, owner, task)
    call_from_permit(case, task)
    remote = RemoteAsrService(case.db, case.settings, pooled=True)
    remote.confirm_local_exit(owner.session_id)
    owner.service.end_worker(owner.session_id)
    preserved = permits(case)[task]
    restarted = uuid4().hex
    owner.service.bind_worker(restarted, 123, slot_owned=True)
    owner.service.report_worker(restarted, "ready", True)
    alias = ready_target(case, "another-name-same-endpoint")
    candidate = seed_queued(case)
    with case.db.session_factory() as session:
        now = database_now(session)
        assert not owner.service.can_receive(session, restarted, now)
        assert not alias.service.can_receive(session, alias.session_id, now)
        assert session.get(RemoteAsrCall, task).state == "uncertain"
    assert grant_target(case, alias, candidate) is None
    assert permits(case)[task] == preserved and preserved["state"] == "uncertain"
    assert task_snapshot(case, candidate)["attempt"] == 0


def test_global_drain_committed_before_late_slot_binding_cannot_admit_work(target_case):
    case = target_case
    remote = RemoteAsrService(case.db, case.settings, pooled=True)
    remote.start_pool(uuid4().hex, 123)
    remote.begin_drain()
    session_id = uuid4().hex
    with pytest.raises(AppError, match="停止接单"):
        remote.begin_worker(session_id)
    with case.db.session_factory() as session:
        assert session.get(AsrTargetWorkerSession, session_id) is None
        assert not remote.can_receive(session, session_id)


def test_stop_between_slot_binding_and_spawn_does_not_create_http_child(target_case, monkeypatch):
    case = target_case
    remote = RemoteAsrService(case.db, case.settings, pooled=True)
    remote.start_pool(uuid4().hex, 123)
    original_guard = remote.spawn_guard

    def stop_before_guard(session_id):
        remote.begin_drain()
        return original_guard(session_id)

    monkeypatch.setattr(remote, "spawn_guard", stop_before_guard)
    runner = FakeRunner()
    worker = pooled_worker(case, runner, remote)
    try:
        worker.start()
        assert runner.starts == 0 and not runner.is_alive()
        assert worker.draining
        with case.db.session_factory() as session:
            assert not remote.can_receive(session, worker.session_id)
    finally:
        assert worker.close()


def test_failed_child_start_confirms_bound_session_before_releasing_slot(target_case):
    case = target_case
    remote = RemoteAsrService(case.db, case.settings, pooled=True)
    remote.start_pool(uuid4().hex, 123)
    runner = FakeRunner()
    runner.start = Mock(side_effect=OSError("isolated start failure"))
    worker = pooled_worker(case, runner, remote)
    try:
        with pytest.raises(OSError):
            worker.start()
        assert worker.close()
        with case.db.session_factory() as session:
            row = session.get(AsrTargetWorkerSession, worker.session_id)
            assert row.state == "stopped" and row.active_slot_key is None
            assert row.ended_at is not None
        with FileLock(worker._target_lock.path, timeout=0):
            pass
    finally:
        assert worker.close()


def test_failed_stopped_persistence_retains_slot_lock_after_child_start_failure(target_case, monkeypatch):
    case = target_case
    remote = RemoteAsrService(case.db, case.settings, pooled=True)
    remote.start_pool(uuid4().hex, 123)
    original_report = remote.report_worker

    def refuse_stopped(session_id, state, ready, **kwargs):
        return False if state == "stopped" else original_report(session_id, state, ready, **kwargs)

    monkeypatch.setattr(remote, "report_worker", refuse_stopped)
    runner = FakeRunner()
    runner.start = Mock(side_effect=OSError("isolated start failure"))
    worker = pooled_worker(case, runner, remote)
    try:
        with pytest.raises(OSError):
            worker.start()
        assert not worker.close() and worker._owns_lock
        with pytest.raises(Timeout):
            with FileLock(worker._target_lock.path, timeout=0):
                pass
    finally:
        monkeypatch.setattr(remote, "report_worker", original_report)
        assert worker.close()


def test_shutdown_checks_all_calls_and_all_target_sessions_not_first_only(target_case):
    case = target_case
    first = ready_target(case)
    capacity(case, 2)
    second_id = uuid4().hex
    first.service.bind_worker(second_id, pid=123, slot_index=1)
    first.service.report_worker(second_id, "ready", True)
    second = SimpleNamespace(service=first.service, session_id=second_id)
    tasks = [seed_queued(case), seed_queued(case)]
    for owner, task in zip((first, second), tasks):
        assert grant_target(case, owner, task)
        call_from_permit(case, task)
    remote = RemoteAsrService(case.db, case.settings, pooled=True)
    remote.confirm_local_exit(first.session_id)
    first.service.end_worker(first.session_id)
    RuntimeRepository(case.db).report(component_id="remote_asr_worker", session_id=uuid4().hex,
                                     pid=123, state="stopped")
    assert not remote.local_shutdown_confirmed(123, None)
    remote.confirm_local_exit(second_id)
    assert not remote.local_shutdown_confirmed(123, None)
    first.service.end_worker(second_id)
    assert remote.local_shutdown_confirmed(123, None)
    assert {value["state"] for value in permits(case).values()} == {"uncertain"}
    assert all(task_snapshot(case, task)["attempt"] == 1 for task in tasks)


def test_readonly_capacity_never_exposes_config_and_spare_quota_is_not_availability(target_case, monkeypatch):
    case = target_case
    service = TargetAdmissionService(case.db, case.settings)
    registered = service.register_current()
    with case.db.transaction(immediate=True) as session:
        row = session.get(AsrTargetRevision, registered["revision_id"])
        row.config_snapshot = {**row.config_snapshot, "token": "fixture-private-token",
                               "caFile": "C:/fixture-private-ca.pem"}
    before = service.inventory()

    def no_probe(*_args, **_kwargs):
        raise AssertionError("Read-only capacity or unavailable pool must not probe")

    monkeypatch.setattr("backend.service.asr.remote_service.probe_remote_asr", no_probe)
    app = create_app(case.settings)
    try:
        with app.test_client() as client:
            response = client.get("/api/asr/external/capacity")
            assert response.status_code == 200
            result = response.json["data"]
            assert result["remainingCapacity"] == 1
            assert result["poolState"] == "not_started"
            assert all(slot["state"] == "not_started" for slot in result["slots"])
            content = response.get_data(as_text=True)
            for secret in ("fixture-private-token", "fixture-private-ca", "target-fixture.invalid", "configSnapshot"):
                assert secret not in content
            status = client.get("/api/asr/external/status")
            assert status.status_code == 200
            assert status.json["data"]["available"] is False
            assert status.json["data"]["state"] == "unavailable"
        assert service.inventory() == before
    finally:
        close_app(app)
