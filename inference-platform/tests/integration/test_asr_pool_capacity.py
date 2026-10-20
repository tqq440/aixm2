"""真实SQLite的双槽准入与容量维护；不访问外部服务或日常数据库。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

from alembic import command
from alembic.config import Config
from filelock import FileLock
from pydantic import ValidationError
import pytest
from sqlalchemy import select

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.asr.target_models import AsrCallPermit, AsrTargetWorkerSession
from backend.data.database.database import Database, database_now
from backend.data.execution.execution_repository import ExecutionRepository
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.data.tasks.task_models import TaskRecord
from backend.schema.asr.remote_schema import RemoteAsrRelease
from backend.schema.asr.target_schema import TargetCapacityCommand
from backend.service.asr.remote_catalog import config_fingerprint
from backend.service.asr.remote_service import RemoteAsrService
from backend.service.asr.target_service import TargetAdmissionService


@pytest.fixture
def pool_case(tmp_path, monkeypatch):
    settings = AppSettings(database_path=tmp_path / "pool.sqlite3", log_dir=tmp_path / "logs",
        upload_dir=tmp_path / "uploads", remote_asr_base_url="https://pool-fixture.invalid:8003",
        remote_asr_model_id="pool-fixture-asr", remote_asr_model_version="fixture-1")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    database = Database(settings)
    target = TargetAdmissionService(database, settings)
    target.register_current()
    monkeypatch.setattr("backend.service.asr.remote_service.probe_remote_asr", lambda _: SimpleNamespace(ready=True))
    try:
        yield SimpleNamespace(db=database, settings=settings, target=target,
                              remote=RemoteAsrService(database, settings), config=config)
    finally:
        database.dispose()


def configure(case, count=2):
    return case.target.configure_capacity(TargetCapacityCommand(target_limit=count, group_limit=count,
        evidence="fixture-only: isolated simulated service permits; not real GPU capacity"))


def start_slots(case, count=2):
    pool_id = uuid4().hex
    case.remote.start_pool(pool_id, os.getpid())
    RuntimeRepository(case.db).report(component_id="remote_asr_worker", session_id=pool_id,
                                     pid=os.getpid(), state="ready", model_ready=True)
    workers = []
    for index in range(count):
        remote = RemoteAsrService(case.db, case.settings, slot_index=index, pooled=True)
        session_id = uuid4().hex
        remote.begin_worker(session_id)
        remote.report_worker(session_id, "ready", True, child_pid=100 + index)
        workers.append(SimpleNamespace(remote=remote, session_id=session_id))
    return pool_id, workers


def queued(case):
    task_id = uuid4().hex
    with case.db.transaction(immediate=True) as session:
        now = database_now(session)
        session.add(TaskRecord(task_id=task_id, caller_id="local-user", task_type="asr",
            model_id=case.settings.remote_asr_model_id, model_version=case.settings.remote_asr_model_version,
            parameters={"language": None}, input_ref=uuid4().hex, input_filename="fixture.wav",
            input_sha256="a" * 64, input_size_bytes=100, audio_duration_seconds=0.5,
            queue_key="asr.remote-v2-sensevoice", status="queued", created_at=now, updated_at=now))
        session.flush()
        session.add(RemoteAsrCall(task_id=task_id, profile_id="remote-v2-sensevoice",
            config_fingerprint=config_fingerprint(case.settings), request_id=task_id, state="queued", created_at=now))
    return task_id


def acquire(case, slot, task_id):
    with ExecutionRepository(case.db).lock_task(task_id) as tx:
        if not slot.remote.can_claim(tx, slot.session_id):
            return False
        tx.start(worker_id=slot.session_id, lease_token=uuid4().hex,
                 expires_at=tx.now + timedelta(seconds=60), attempt_deadline_at=tx.now + timedelta(seconds=60))
        slot.remote.reserve(tx)
        return True


def test_capacity_maintenance_is_audited_and_repeated_command_is_unchanged(pool_case):
    case = pool_case
    assert case.target.pool_limit() == 1
    first = configure(case)
    assert first["changed"] and len(first["change_id"]) == 32
    assert case.target.pool_limit() == 2
    assert configure(case)["changed"] is False
    audit = case.target.inventory()["capacity_changes"]
    assert len(audit) == 1
    assert (audit[0]["previous_target_limit"], audit[0]["previous_group_limit"],
            audit[0]["target_limit"], audit[0]["group_limit"]) == (1, 1, 2, 2)
    assert "fixture-only" in audit[0]["evidence"]
    command.check(case.config)


@pytest.mark.parametrize("value", [0, 9, True, "2", 2.5])
def test_capacity_is_strictly_bounded_at_service_input(pool_case, value):
    with pytest.raises(ValidationError):
        pool_case.target.configure_capacity({"target_limit": value, "group_limit": 2, "evidence": "fixture-only"})
    assert pool_case.target.pool_limit() == 1
    assert pool_case.target.inventory()["capacity_changes"] == []


def test_capacity_change_requires_evidence_and_stopped_global_lock(pool_case):
    case = pool_case
    with pytest.raises(ValidationError):
        case.target.configure_capacity({"target_limit": 2, "group_limit": 2, "evidence": "  "})
    path = case.settings.database_path
    with FileLock(path.with_name(path.name + ".remote-asr-worker.lock"), timeout=0):
        with pytest.raises(AppError) as error:
            configure(case)
        assert error.value.code == "ASR_POOL_MUST_BE_STOPPED"
    assert case.target.pool_limit() == 1


def test_shared_alias_fresh_worker_and_unknown_block_capacity_change(pool_case):
    case = pool_case
    alias = TargetAdmissionService(case.db, case.settings, target_id="shared-alias")
    alias.register_current()
    owner = uuid4().hex
    alias.bind_worker(owner, pid=os.getpid())
    alias.report_worker(owner, "ready", True)
    with pytest.raises(AppError) as error:
        configure(case)
    assert error.value.code == "ASR_POOL_MUST_BE_STOPPED"
    task_id = queued(case)
    with ExecutionRepository(case.db).lock_task(task_id) as tx:
        tx.start(worker_id=owner, lease_token=uuid4().hex, expires_at=tx.now + timedelta(seconds=60),
                 attempt_deadline_at=tx.now + timedelta(seconds=60))
        alias.reserve(tx)
        case.remote.repository.reserve(case.remote.repository.get(tx.session, task_id), tx.task, tx.now)
        tx.finish(status="failed", result=None, error={"code": "fixture"}, fingerprint=None)
        case.remote.finish(tx, confirmed=False)
    alias.end_worker(owner)
    with pytest.raises(AppError) as error:
        configure(case)
    assert error.value.code == "ASR_CAPACITY_OCCUPIED"
    assert case.target.pool_limit() == 1


def test_two_slots_hold_same_profile_atomically_and_one_slot_cannot_hold_twice(pool_case):
    case = pool_case
    configure(case)
    _, slots = start_slots(case)
    tasks = [queued(case) for _ in range(3)]
    barrier = Barrier(2)
    def run(index):
        barrier.wait(timeout=5)
        return acquire(case, slots[index], tasks[index])
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(run, range(2))) == [True, True]
    assert not acquire(case, slots[0], tasks[2])
    with case.db.session_factory() as session:
        calls = list(session.scalars(select(RemoteAsrCall).where(RemoteAsrCall.state == "held")))
        permits = list(session.scalars(select(AsrCallPermit).where(AsrCallPermit.state == "held")))
        assert len(calls) == len(permits) == 2
        assert len({call.worker_id for call in calls}) == 2
        assert session.get(TaskRecord, tasks[2]).status == "queued"
        assert session.get(TaskRecord, tasks[2]).attempt == 0
    status = case.remote.status()
    assert status.available and status.state == "running"  # 满held仍可排队。
    assert status.capacity.group_held == 2 and status.capacity.remaining_capacity == 0


def test_single_slot_held_blocks_duplicate_even_when_group_has_space(pool_case):
    case = pool_case
    configure(case)
    _, slots = start_slots(case)
    assert acquire(case, slots[0], queued(case))
    second = queued(case)
    assert not acquire(case, slots[0], second)
    assert acquire(case, slots[1], second)


def test_unknown_consumes_one_group_slot_and_release_does_not_touch_other_call(pool_case):
    case = pool_case
    configure(case)
    pool_id, slots = start_slots(case)
    first, second = queued(case), queued(case)
    assert acquire(case, slots[0], first)
    slots[0].remote.confirm_local_exit(slots[0].session_id)
    status = case.remote.status()
    assert status.available and status.uncertain_task_id == first
    assert status.capacity.group_uncertain == 1 and status.capacity.remaining_capacity == 1
    assert acquire(case, slots[1], second)
    assert case.remote.has_uncertain()
    release = RemoteAsrRelease(task_id=first, confirm_request_ended=True, note="fixture independently ended")
    assert case.remote.release_uncertain(release)["released"]
    with case.db.session_factory() as session:
        assert session.get(AsrCallPermit, first).state == "released"
        assert session.get(AsrCallPermit, second).state == "held"
        assert session.get(RemoteAsrCall, second).state == "held"
        assert case.remote.repository.runtime(session).session_id == pool_id
    assert case.remote.release_uncertain(release)["replayed"]


def test_unknown_fills_group_and_restart_cannot_clear_or_replay_it(pool_case):
    case = pool_case
    configure(case)
    _, slots = start_slots(case)
    tasks = [queued(case) for _ in range(2)]
    for slot, task_id in zip(slots, tasks):
        assert acquire(case, slot, task_id)
        slot.remote.confirm_local_exit(slot.session_id)
        slot.remote.report_worker(slot.session_id, "stopped", False)
    assert case.remote.status().state == "uncertain"
    _, replacements = start_slots(case)
    queued_id = queued(case)
    assert not acquire(case, replacements[0], queued_id)
    assert not case.remote.status().available
    with case.db.session_factory() as session:
        assert all(session.get(AsrCallPermit, task_id).state == "uncertain" for task_id in tasks)
        assert session.get(TaskRecord, queued_id).attempt == 0


def test_all_slot_drain_and_exit_confirmation_use_every_session(pool_case):
    case = pool_case
    configure(case)
    pool_id, slots = start_slots(case)
    first, second, waiting = [queued(case) for _ in range(3)]
    assert acquire(case, slots[0], first) and acquire(case, slots[1], second)
    case.remote.begin_drain(None)
    assert all(slot.remote.draining(slot.session_id) for slot in slots)
    assert not acquire(case, slots[0], waiting)
    case.remote.confirm_local_exit(slots[0].session_id)
    slots[0].remote.report_worker(slots[0].session_id, "stopped", False)
    RuntimeRepository(case.db).report(component_id="remote_asr_worker", session_id=pool_id,
                                     pid=os.getpid(), state="stopped", model_ready=False)
    assert not case.remote.local_shutdown_confirmed(os.getpid(), started_after=None)
    case.remote.confirm_local_exit(slots[1].session_id)
    slots[1].remote.report_worker(slots[1].session_id, "stopped", False)
    assert case.remote.local_shutdown_confirmed(os.getpid(), started_after=None)
    assert case.remote.has_uncertain()
    with case.db.session_factory() as session:
        assert session.get(TaskRecord, waiting).status == "queued"
        assert session.get(TaskRecord, waiting).attempt == 0


def test_release_rejects_slot_still_reporting_the_unknown_task(pool_case):
    case = pool_case
    _, slots = start_slots(case, 1)
    slot, task_id = slots[0], queued(case)
    assert acquire(case, slot, task_id)
    slot.remote.report_worker(slot.session_id, "running", True, active_task_id=task_id, child_pid=101)
    slot.remote.confirm_local_exit(slot.session_id)
    release = RemoteAsrRelease(task_id=task_id, confirm_request_ended=True, note="fixture ended remotely")
    with pytest.raises(AppError) as error:
        case.remote.release_uncertain(release)
    assert error.value.code == "REMOTE_ASR_RELEASE_CONFLICT"
    slot.remote.report_worker(slot.session_id, "stopped", False)
    assert case.remote.release_uncertain(release)["released"]
