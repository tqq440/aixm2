"""目标容量准入使用真实 SQLite；仅隔离任务，不启动模型或访问上游。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import select

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.asr.target_models import (
    AsrCallPermit, AsrCapacityGroup, AsrComputeTarget, AsrTargetRevision, AsrTargetWorkerSession,
)
from backend.data.broker.broker_models import NotificationRecord
from backend.data.broker.broker_repository import BrokerRepository
from backend.data.database.database import Database, database_now
from backend.data.execution.attempt_models import ExecutionAttempt
from backend.data.execution.execution_repository import ExecutionRepository
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.data.tasks.task_models import DispatchEvent, TaskRecord
from backend.schema.asr.asr_schema import AsrEngineMetadata, TranscriptionResult
from backend.schema.asr.remote_schema import RemoteAsrRelease
from backend.schema.execution.execution_schema import FailureInfo
from backend.service.asr.remote_catalog import (
    REMOTE_PROFILE_ID, REMOTE_QUEUE_KEY, config_fingerprint, remote_asr_model_spec,
)
from backend.service.asr.target_service import TargetAdmissionService
from backend.service.asr.remote_service import RemoteAsrService
from backend.service.execution.execution_service import ExecutionService
from backend.service.recovery.recovery_service import RecoveryService


@pytest.fixture
def target_case(tmp_path):
    settings = AppSettings(
        database_path=tmp_path / "target-admission.sqlite3", log_dir=tmp_path / "logs",
        upload_dir=tmp_path / "uploads", remote_asr_base_url="https://target-fixture.invalid:8003",
        remote_asr_model_id="target-fixture-asr", remote_asr_model_version="isolated-release-1",
    )
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    database = Database(settings)
    try:
        yield SimpleNamespace(db=database, settings=settings, broker=BrokerRepository(database),
                              execution=ExecutionRepository(database))
    finally:
        database.dispose()


def seed_queued(case, *, settings=None):
    """在隔离库创建原通知链路；容量测试不需要音频文件或真实模型。"""
    settings = settings or case.settings
    spec = remote_asr_model_spec(settings)
    task_id, event_id, notification_id = uuid4().hex, uuid4().hex, uuid4().hex
    with case.db.transaction(immediate=True) as session:
        now = database_now(session)
        session.add(TaskRecord(
            task_id=task_id, caller_id="local-user", task_type="asr", input_media_type="audio",
            model_id=spec.model_id, model_version=spec.model_version, parameters={"language": None},
            input_ref=uuid4().hex, input_filename="isolated.wav", input_sha256="a" * 64,
            input_size_bytes=3244, audio_duration_seconds=0.1, status="queued", attempt=0,
            max_attempts=1, queue_key=REMOTE_QUEUE_KEY, execution_plan_version=1,
            dispatch_version=1, queue_deadline_at=now + timedelta(seconds=600),
            total_deadline_at=now + timedelta(seconds=1800), created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(DispatchEvent(
            event_id=event_id, task_id=task_id, event_type="task.created", queue_key=REMOTE_QUEUE_KEY,
            status="published", dispatch_version=1, execution_plan_version=1,
            available_at=now, created_at=now, published_at=now, notification_id=notification_id,
        ))
        session.add(RemoteAsrCall(
            task_id=task_id, profile_id=REMOTE_PROFILE_ID, config_fingerprint=config_fingerprint(settings),
            request_id=task_id, state="queued", created_at=now,
        ))
        session.flush()
        session.add(NotificationRecord(
            notification_id=notification_id, event_id=event_id, task_id=task_id,
            queue_key=REMOTE_QUEUE_KEY, status="ready", dispatch_version=1,
            execution_plan_version=1, visible_at=now, created_at=now,
        ))
    return task_id


def task_snapshot(case, task_id):
    with case.db.session_factory() as session:
        return session.get(TaskRecord, task_id).to_dict()


def notification_snapshot(case, notification_id):
    with case.db.session_factory() as session:
        return session.get(NotificationRecord, notification_id).to_dict()


def receive(case, worker_id):
    delivery = case.broker.receive(worker_id=worker_id, queue_key=REMOTE_QUEUE_KEY,
                                   visibility_seconds=30)
    assert delivery is not None
    return delivery


def ready_target(case, target_id="remote-default", *, settings=None,
                 group_id="remote-asr-shared", independence_evidence=None):
    settings = settings or case.settings
    service = TargetAdmissionService(case.db, settings, target_id=target_id)
    registered = service.register_current(group_id=group_id, independence_evidence=independence_evidence)
    session_id = uuid4().hex
    service.bind_worker(session_id, pid=123)
    service.report_worker(session_id, state="ready", model_ready=True)
    return SimpleNamespace(service=service, settings=settings, session_id=session_id, registered=registered)


def grant_target(case, target, task_id):
    """只验证基础准入 API；不运行正式多 Worker，也不改变旧单槽调用记录。"""
    with case.execution.lock_task(task_id) as tx:
        if not target.service.can_claim(tx, target.session_id):
            return None
        tx.start(worker_id=target.session_id, lease_token=uuid4().hex,
                 expires_at=tx.now + timedelta(seconds=30),
                 attempt_deadline_at=tx.now + timedelta(seconds=600))
        target.service.reserve(tx)
        return tx.task.lease_token


def eligible(case, target):
    with case.db.session_factory() as session:
        return target.service.can_receive(session, target.session_id, database_now(session))


def permits(case):
    with case.db.session_factory() as session:
        return {row.task_id: row.to_dict() for row in session.scalars(select(AsrCallPermit))}


def race_grants(case, pairs):
    barrier = Barrier(len(pairs))

    def claim(pair):
        target, task_id = pair
        barrier.wait(timeout=5)
        return grant_target(case, target, task_id)

    with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
        return list(pool.map(claim, pairs))


def test_two_sqlite_claimers_share_one_target_and_do_not_create_losing_attempt(target_case):
    case = target_case
    target = ready_target(case)
    tasks = [seed_queued(case), seed_queued(case)]
    before = {task_id: task_snapshot(case, task_id) for task_id in tasks}
    tokens = race_grants(case, [(target, task_id) for task_id in tasks])
    assert sum(token is not None for token in tokens) == 1
    saved_permits = permits(case)
    assert len(saved_permits) == 1
    with case.db.session_factory() as session:
        attempts = session.scalars(select(ExecutionAttempt)).all()
        assert len(attempts) == 1
        for task_id, token in zip(tasks, tokens):
            task = session.get(TaskRecord, task_id)
            if token is None:
                assert task.to_dict() == before[task_id]
            else:
                assert task.status == "running" and task.attempt == 1 and task.lease_token == token
                assert saved_permits[task_id]["lease_token"] == token
                assert saved_permits[task_id]["worker_session_id"] == target.session_id
    assert not eligible(case, target)


def test_aliases_and_model_versions_share_capacity_under_sqlite_race(target_case):
    case = target_case
    first = ready_target(case, "alias-first")
    changed = case.settings.model_copy(update={"remote_asr_model_version": "isolated-release-2"})
    second = ready_target(case, "alias-second", settings=changed)
    tasks = [seed_queued(case), seed_queued(case, settings=changed)]
    tokens = race_grants(case, list(zip((first, second), tasks)))
    assert sum(token is not None for token in tokens) == 1
    with case.db.session_factory() as session:
        targets = session.scalars(select(AsrComputeTarget)).all()
        revisions = session.scalars(select(AsrTargetRevision)).all()
        assert len(targets) == len(revisions) == 2
        assert {row.capacity_group_id for row in targets} == {"remote-asr-shared"}
        assert len({row.endpoint_identity for row in revisions}) == 1
        assert len({row.config_fingerprint for row in revisions}) == 2
        assert len(session.scalars(select(ExecutionAttempt)).all()) == 1
    assert not eligible(case, first) and not eligible(case, second)


def test_independent_fixture_groups_can_each_reserve_one_via_base_api(target_case):
    case = target_case
    first = ready_target(case, "fixture-a")
    other = case.settings.model_copy(update={"remote_asr_base_url": "https://independent-fixture.invalid:8003"})
    second = ready_target(case, "fixture-b", settings=other, group_id="fixture-only-independent",
                          independence_evidence="fixture-only: isolated groups, no real GPU claim")
    tasks = [seed_queued(case), seed_queued(case, settings=other)]
    tokens = race_grants(case, list(zip((first, second), tasks)))
    assert all(tokens)
    saved_permits = permits(case)
    assert len(saved_permits) == 2
    assert {row["capacity_group_id"] for row in saved_permits.values()} == {
        "remote-asr-shared", "fixture-only-independent",
    }
    with case.db.session_factory() as session:
        assert len(session.scalars(select(ExecutionAttempt)).all()) == 2
        # 这是基础许可层验证；原正式调用通道的全局单槽仍保留。
        assert {row.state for row in session.scalars(select(RemoteAsrCall))} == {"queued"}


def test_endpoint_alias_cannot_gain_capacity_by_changing_group_version_or_tls(target_case):
    case = target_case
    ready_target(case, "original-target")
    settings = case.settings.model_copy(update={
        "remote_asr_model_version": "another-fixture-version",
        "remote_asr_insecure_tls_origin": case.settings.remote_asr_base_url,
    })
    alias = TargetAdmissionService(case.db, settings, target_id="disguised-alias")
    with pytest.raises(AppError):
        alias.register_current(group_id="fake-extra-capacity", independence_evidence="fixture-only")
    with case.db.session_factory() as session:
        assert len(session.scalars(select(AsrComputeTarget)).all()) == 1
        assert len(session.scalars(select(AsrCapacityGroup)).all()) == 1
        assert len(session.scalars(select(AsrTargetRevision)).all()) == 1


def test_independent_group_requires_evidence_and_existing_target_cannot_move_groups(target_case):
    case = target_case
    service = TargetAdmissionService(case.db, case.settings, target_id="stable-target")
    with pytest.raises(AppError):
        service.register_current(group_id="unproven-independent")
    service.register_current()
    changed = case.settings.model_copy(update={"remote_asr_base_url": "https://new-fixture.invalid:8003"})
    moved = TargetAdmissionService(case.db, changed, target_id="stable-target")
    with pytest.raises(AppError):
        moved.register_current(group_id="different-group", independence_evidence="fixture-only")
    with case.db.session_factory() as session:
        target = session.get(AsrComputeTarget, "stable-target")
        assert target.capacity_group_id == "remote-asr-shared"
        assert len(session.scalars(select(AsrTargetRevision)).all()) == 1


def test_task_attempt_and_capacity_reservation_roll_back_together(target_case):
    case = target_case
    target = ready_target(case)
    task_id = seed_queued(case)
    before = task_snapshot(case, task_id)
    with pytest.raises(RuntimeError, match="isolated rollback"):
        with case.execution.lock_task(task_id) as tx:
            assert target.service.can_claim(tx, target.session_id)
            tx.start(worker_id=target.session_id, lease_token=uuid4().hex,
                     expires_at=tx.now + timedelta(seconds=30),
                     attempt_deadline_at=tx.now + timedelta(seconds=600))
            target.service.reserve(tx)
            raise RuntimeError("isolated rollback")
    assert task_snapshot(case, task_id) == before and permits(case) == {}
    with case.db.session_factory() as session:
        assert session.scalars(select(ExecutionAttempt)).all() == []
    assert eligible(case, target)


def test_unknown_permit_survives_restart_and_new_revision_but_not_independent_group(target_case):
    case = target_case
    first = ready_target(case, "fixture-a")
    alias = ready_target(case, "alias-a")
    other = case.settings.model_copy(update={"remote_asr_base_url": "https://independent-fixture.invalid:8003"})
    independent = ready_target(case, "fixture-b", settings=other, group_id="fixture-independent",
                               independence_evidence="fixture-only: no real hardware observation")
    first_task = seed_queued(case)
    grant_target(case, first, first_task)
    with case.execution.lock_task(first_task) as tx:
        first.service.finish(tx, confirmed=False)
        error = {"code": "REMOTE_ASR_RESULT_UNKNOWN", "message": "isolated lost response"}
        tx.finish(status="failed", result=None, error=error, fingerprint=None)
        tx.finish_attempt(outcome="failed", error=error)
    before = permits(case)[first_task]
    first.service.end_worker(first.session_id)
    case.db.dispose()
    changed = case.settings.model_copy(update={"remote_asr_model_version": "new-isolated-release"})
    restarted = ready_target(case, "fixture-a", settings=changed)
    assert not eligible(case, restarted) and not eligible(case, alias)
    pending = seed_queued(case, settings=changed)
    assert grant_target(case, restarted, pending) is None
    assert task_snapshot(case, pending)["attempt"] == 0
    assert eligible(case, independent)
    second_task = seed_queued(case, settings=other)
    assert grant_target(case, independent, second_task)
    assert permits(case)[first_task] == before
    assert before["state"] == "uncertain"
    with case.db.session_factory() as session:
        old_revision = session.get(AsrTargetRevision, before["revision_id"])
        assert old_revision.config_snapshot["modelVersion"] == "isolated-release-1"
        assert old_revision.device_kind == "unknown" and old_revision.device_evidence is None
        assert session.get(TaskRecord, first_task).attempt == 1


def test_legacy_unknown_without_snapshot_blocks_admission_without_guessing_history(target_case):
    case = target_case
    task_id = seed_queued(case)
    with case.db.transaction(immediate=True) as session:
        call = session.get(RemoteAsrCall, task_id)
        call.state = "uncertain"
        before = call.to_dict()
    changed = case.settings.model_copy(update={"remote_asr_base_url": "https://new-fixture.invalid:8003"})
    target = ready_target(case, "new-isolated-target", settings=changed, group_id="fixture-independent",
                          independence_evidence="fixture-only: old request provenance remains unknown")
    assert not eligible(case, target)
    assert permits(case) == {}
    with case.db.session_factory() as session:
        assert session.get(RemoteAsrCall, task_id).to_dict() == before


def test_worker_replacement_cannot_clear_existing_permit_or_restore_old_session(target_case):
    case = target_case
    target = ready_target(case)
    task_id = seed_queued(case)
    assert grant_target(case, target, task_id)
    before = permits(case)
    replacement = uuid4().hex
    with pytest.raises(AppError):
        target.service.bind_worker(replacement, pid=456)
    # 基础接口夹具模拟入口已经持有稳定槽锁；不表示本测试启动了进程。
    target.service.bind_worker(replacement, pid=456, slot_owned=True)
    target.service.report_worker(replacement, state="ready", model_ready=True)
    assert target.service.report_worker(target.session_id, state="ready", model_ready=True) is False
    with case.db.session_factory() as session:
        old = session.get(AsrTargetWorkerSession, target.session_id)
        current = session.get(AsrTargetWorkerSession, replacement)
        assert old.state == "lost" and old.active_slot_key is None
        assert current.active_slot_key is not None and current.state == "ready"
        now = database_now(session)
        assert not target.service.can_receive(session, target.session_id, now)
        assert not target.service.can_receive(session, replacement, now)
    assert permits(case) == before


@pytest.mark.parametrize("field", ["lease_token", "lease_worker_id"])
def test_mismatched_execution_identity_cannot_release_the_original_permit(target_case, field):
    case = target_case
    target = ready_target(case)
    task_id = seed_queued(case)
    assert grant_target(case, target, task_id)
    before = permits(case)
    with case.execution.lock_task(task_id) as tx:
        original = getattr(tx.task, field)
        setattr(tx.task, field, uuid4().hex)
        target.service.finish(tx, confirmed=True)
        setattr(tx.task, field, original)
    assert permits(case) == before and not eligible(case, target)
    with case.execution.lock_task(task_id) as tx:
        target.service.finish(tx, confirmed=True)
    assert permits(case)[task_id]["state"] == "released"


def test_config_change_cannot_redirect_queued_call_or_overwrite_old_snapshot(target_case):
    case = target_case
    original = ready_target(case)
    task_id = seed_queued(case)
    before_task = task_snapshot(case, task_id)
    with case.db.session_factory() as session:
        before_revisions = {row.revision_id: row.to_dict() for row in session.scalars(select(AsrTargetRevision))}
        before_call = session.get(RemoteAsrCall, task_id).to_dict()
    original.service.end_worker(original.session_id)
    changed = case.settings.model_copy(update={"remote_asr_base_url": "https://replacement-fixture.invalid:8003"})
    replacement = ready_target(case, settings=changed)
    assert eligible(case, replacement)
    assert grant_target(case, replacement, task_id) is None
    assert task_snapshot(case, task_id) == before_task and permits(case) == {}
    with case.db.session_factory() as session:
        revisions = {row.revision_id: row.to_dict() for row in session.scalars(select(AsrTargetRevision))}
        assert len(revisions) == 2
        assert all(revisions[key] == value for key, value in before_revisions.items())
        assert session.get(RemoteAsrCall, task_id).to_dict() == before_call
    repeat = replacement.service.register_current()
    assert repeat == replacement.registered


def test_drain_rejects_new_claim_without_resetting_waiting_deadlines(target_case):
    case = target_case
    target = ready_target(case)
    task_id = seed_queued(case)
    before = task_snapshot(case, task_id)
    target.service.begin_drain(target.session_id)
    target.service.report_worker(target.session_id, state="ready", model_ready=True)
    assert not eligible(case, target)
    assert grant_target(case, target, task_id) is None
    assert task_snapshot(case, task_id) == before and permits(case) == {}
    with case.db.session_factory() as session:
        assert session.get(AsrTargetWorkerSession, target.session_id).state == "stopping"


def test_stale_heartbeat_and_unregistered_slot_never_grant_new_execution(target_case):
    case = target_case
    target = ready_target(case)
    with case.db.transaction(immediate=True) as session:
        session.get(AsrTargetWorkerSession, target.session_id).heartbeat_at = (
            database_now(session) - timedelta(seconds=case.settings.runtime_stale_seconds + 1)
        )
    task_id = seed_queued(case)
    assert not eligible(case, target) and grant_target(case, target, task_id) is None
    with pytest.raises(AppError):
        target.service.bind_worker(uuid4().hex, pid=456, slot_index=1)
    assert task_snapshot(case, task_id)["attempt"] == 0 and permits(case) == {}


def default_execution(case):
    """使用正式单槽调用链的服务对象；只有推理结果为显式夹具。"""
    remote = RemoteAsrService(case.db, case.settings)
    worker_id = uuid4().hex
    remote.begin_worker(worker_id)
    runtime = RuntimeRepository(case.db)
    runtime.report(component_id="remote_asr_worker", session_id=worker_id,
                   pid=123, state="ready", model_ready=True)
    executor = ExecutionService(case.db, case.settings, asr_profile="remote")
    return SimpleNamespace(remote=remote, worker_id=worker_id, executor=executor, runtime=runtime)


def remote_result():
    return TranscriptionResult(text="isolated target result", segments=[], audio_duration_seconds=0.1,
                               engine_metadata=AsrEngineMetadata(adapter_key="remote-v2-sensevoice"))


@pytest.mark.parametrize("changed", [
    {"worker_id": "old-session"}, {"lease_token": "old-token"}, {"attempt": 2},
])
def test_default_execution_commits_both_permits_and_rejects_foreign_lease(target_case, changed):
    case = target_case
    execution = default_execution(case)
    task_id = seed_queued(case)
    delivery = receive(case, execution.worker_id)
    acquired = execution.executor.acquire(delivery)
    assert acquired.decision == "acquired"
    before = permits(case)[task_id]
    assert before["state"] == "held" and before["lease_token"] == acquired.lease.lease_token
    stale = acquired.lease.model_copy(update=changed)
    assert not execution.executor.renew(stale)
    assert not execution.executor.commit_result(stale, remote_result())
    assert not execution.executor.commit_failure(stale, FailureInfo(code="REMOTE_ASR_NOT_SENT", message="fixture"))
    assert permits(case)[task_id] == before
    assert execution.executor.commit_result(acquired.lease, remote_result())
    final_permit = permits(case)[task_id]
    assert final_permit["state"] == "released"
    assert execution.executor.commit_result(acquired.lease, remote_result())
    assert permits(case)[task_id] == final_permit
    with case.db.session_factory() as session:
        call = session.get(RemoteAsrCall, task_id)
        task = session.get(TaskRecord, task_id)
        attempt = session.get(ExecutionAttempt, acquired.lease.lease_token)
        assert call.state == "released" and task.status == "succeeded" and task.attempt == 1
        assert attempt.outcome == "succeeded"


def test_recovery_and_explicit_release_keep_legacy_and_target_unknown_in_sync(target_case):
    case = target_case
    execution = default_execution(case)
    task_id = seed_queued(case)
    delivery = receive(case, execution.worker_id)
    acquired = execution.executor.acquire(delivery)
    assert acquired.decision == "acquired"
    with case.db.transaction(immediate=True) as session:
        session.get(TaskRecord, task_id).lease_expires_at = database_now(session) - timedelta(seconds=1)
    assert RecoveryService(case.db, case.settings).recover_task(task_id)
    assert permits(case)[task_id]["state"] == "uncertain"
    with case.db.session_factory() as session:
        assert session.get(RemoteAsrCall, task_id).state == "uncertain"
        assert session.get(TaskRecord, task_id).error_json["code"] == "REMOTE_ASR_RESULT_UNKNOWN"
    assert not execution.executor.commit_result(acquired.lease, remote_result())
    command = RemoteAsrRelease(task_id=task_id, confirm_request_ended=True,
                               note="fixture-only: simulated request was independently checked as ended")
    assert execution.remote.release_uncertain(command)["released"]
    released = permits(case)[task_id]
    assert released["state"] == "released" and released["resolved_at"] is not None
    assert execution.remote.release_uncertain(command)["replayed"]
    assert permits(case)[task_id] == released
    with case.db.session_factory() as session:
        call = session.get(RemoteAsrCall, task_id)
        assert call.state == "released" and call.resolved_at is not None
        assert session.get(TaskRecord, task_id).attempt == 1
        assert len(session.scalars(select(DispatchEvent)).all()) == 1


def test_candidate_check_skips_incompatible_head_without_hiding_notification(target_case):
    case = target_case
    execution = default_execution(case)
    old_settings = case.settings.model_copy(update={"remote_asr_model_version": "previous-fixture-version"})
    incompatible = seed_queued(case, settings=old_settings)
    compatible = seed_queued(case)
    with case.db.transaction(immediate=True) as session:
        head = session.scalar(select(NotificationRecord).where(NotificationRecord.task_id == incompatible))
        head.visible_at = database_now(session) - timedelta(seconds=1)
        head_id = head.notification_id
    before_head = notification_snapshot(case, head_id)
    before_task = task_snapshot(case, incompatible)
    selected = case.broker.receive(
        worker_id=execution.worker_id, queue_key=REMOTE_QUEUE_KEY, visibility_seconds=30,
        receive_check=lambda session: execution.remote.can_receive(session, execution.worker_id),
        candidate_check=lambda session, row: execution.remote.can_receive_task(
            session, row.task_id, execution.worker_id),
    )
    assert selected is not None and selected.task_id == compatible
    assert notification_snapshot(case, head_id) == before_head
    assert task_snapshot(case, incompatible) == before_task
    assert case.broker.release(selected)
    execution.remote.begin_drain(execution.worker_id)
    before_compatible = task_snapshot(case, compatible)
    assert case.broker.receive(
        worker_id=execution.worker_id, queue_key=REMOTE_QUEUE_KEY, visibility_seconds=30,
        receive_check=lambda session: execution.remote.can_receive(session, execution.worker_id),
        candidate_check=lambda session, row: execution.remote.can_receive_task(
            session, row.task_id, execution.worker_id),
    ) is None
    assert task_snapshot(case, compatible) == before_compatible
    assert notification_snapshot(case, head_id) == before_head
    assert permits(case) == {}


def test_ineligible_delivery_release_is_immediately_visible_and_preserves_task(target_case):
    case = target_case
    task_id = seed_queued(case)
    before = task_snapshot(case, task_id)
    delivery = receive(case, "ineligible-worker")
    assert case.broker.release(delivery)
    released = notification_snapshot(case, delivery.notification_id)
    assert released["status"] == "ready" and released["acked_at"] is None
    assert task_snapshot(case, task_id) == before
    accepted = receive(case, "eligible-worker")
    assert accepted.task_id == task_id and accepted.delivery_token != delivery.delivery_token
    assert not case.broker.release(delivery)
    assert not case.broker.acknowledge(delivery)
    assert not case.broker.extend(delivery, visibility_seconds=30)
    assert task_snapshot(case, task_id) == before
    with case.db.session_factory() as session:
        assert session.scalars(select(ExecutionAttempt)).all() == []
        assert session.get(RemoteAsrCall, task_id).state == "queued"


@pytest.mark.parametrize("change", [{"worker_id": "other-worker"}, {"delivery_token": "other-token"}])
def test_foreign_delivery_cannot_release_another_owners_notification(target_case, change):
    case = target_case
    seed_queued(case)
    delivery = receive(case, "current-worker")
    before = notification_snapshot(case, delivery.notification_id)
    assert not case.broker.release(delivery.model_copy(update=change))
    assert notification_snapshot(case, delivery.notification_id) == before
    assert case.broker.release(delivery)


def test_expired_delivery_cannot_release_or_overwrite_reclaimed_owner(target_case):
    case = target_case
    task_id = seed_queued(case)
    first = receive(case, "old-worker")
    before_task = task_snapshot(case, task_id)
    with case.db.transaction(immediate=True) as session:
        session.get(NotificationRecord, first.notification_id).visible_at = (
            database_now(session) - timedelta(seconds=1)
        )
    before_notification = notification_snapshot(case, first.notification_id)
    assert not case.broker.release(first)
    assert notification_snapshot(case, first.notification_id) == before_notification
    second = receive(case, "new-worker")
    after_reclaim = notification_snapshot(case, first.notification_id)
    assert not case.broker.release(first)
    assert notification_snapshot(case, first.notification_id) == after_reclaim
    assert case.broker.release(second)
    assert task_snapshot(case, task_id) == before_task


def test_acked_delivery_cannot_be_made_visible_by_release(target_case):
    case = target_case
    seed_queued(case)
    delivery = receive(case, "current-worker")
    assert case.broker.acknowledge(delivery)
    before = notification_snapshot(case, delivery.notification_id)
    assert not case.broker.release(delivery)
    assert notification_snapshot(case, delivery.notification_id) == before
    assert case.broker.receive(worker_id="later-worker", queue_key=REMOTE_QUEUE_KEY,
                               visibility_seconds=30) is None
