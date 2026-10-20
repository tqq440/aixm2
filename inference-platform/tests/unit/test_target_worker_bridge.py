"""原 Worker 与目标槽锁衔接；复用假子进程，仅验证控制时序。"""

import logging
from contextlib import nullcontext
from unittest.mock import Mock

from filelock import FileLock, Timeout
import pytest

from backend.common.exceptions.app_error import AppError
from backend.schema.execution.execution_schema import AcquireDecision, Delivery
from backend.schema.worker.worker_schema import ReadyEvent
from backend.service.asr.target_binding import TargetSlotLock
from backend.service.worker.worker_service import WorkerService
from test_worker_service import FakeRunner, begin, remote_rig


def assert_both_locks_held(rig):
    with pytest.raises(Timeout):
        with FileLock(rig.worker._lock.lock_file, timeout=0):
            pass
    contender = TargetSlotLock(rig.settings.database_path, "remote-default", 0)
    try:
        assert not contender.acquire()
    finally:
        contender.release()


def assert_both_locks_released(rig):
    with FileLock(rig.worker._lock.lock_file, timeout=0):
        pass
    contender = TargetSlotLock(rig.settings.database_path, "remote-default", 0)
    try:
        assert contender.acquire()
    finally:
        contender.release()


def test_occupied_target_slot_rejects_start_without_leaking_legacy_lock(remote_rig):
    rig = remote_rig
    assert rig.worker.close()
    owner = TargetSlotLock(rig.settings.database_path, "remote-default", 0)
    runner, remote = FakeRunner(), Mock()
    candidate = WorkerService(None, rig.settings, logging.getLogger("target-bridge-fixture"),
        runner=runner, broker=Mock(), execution=Mock(), runtime=Mock(), remote_service=remote,
        asr_profile="remote")
    try:
        assert owner.acquire()
        with pytest.raises(AppError) as error:
            candidate.start()
        assert error.value.code == "ASR_TARGET_SLOT_IN_USE"
        assert runner.starts == 0 and not runner.is_alive()
        remote.begin_worker.assert_not_called()
        assert not candidate._owns_lock
        with FileLock(candidate._lock.lock_file, timeout=0):
            pass
        other = TargetSlotLock(rig.settings.database_path, "remote-default", 0)
        try:
            assert not other.acquire()
        finally:
            other.release()
    finally:
        candidate.close()
        owner.release()


def test_busy_acquisition_returns_owned_notification_without_acknowledging_or_running(remote_rig):
    rig = remote_rig
    delivery = Delivery(notification_id="1" * 32, task_id="2" * 32,
        worker_id=rig.worker.session_id, delivery_token="fixture-delivery",
        dispatch_version=1, execution_plan_version=1, queue_key=rig.worker.queue_key)
    rig.broker.receive.return_value = delivery
    rig.execution.acquire.return_value = AcquireDecision(decision="busy")
    rig.runner.emit(ReadyEvent(model_load_seconds=0.1))
    rig.worker.tick()
    rig.broker.release.assert_called_once_with(delivery)
    rig.broker.acknowledge.assert_not_called()
    assert rig.worker.lease is None and [item.type for item in rig.runner.sent] == ["start"]
    callbacks = rig.broker.receive.call_args.kwargs
    session, notification = object(), Mock(task_id=delivery.task_id)
    callbacks["candidate_check"](session, notification)
    rig.remote.can_receive_task.assert_called_once_with(session, delivery.task_id, rig.worker.session_id)
    assert_both_locks_held(rig)


def test_unconfirmed_child_stop_keeps_both_locks_and_does_not_report_stopped(remote_rig):
    rig = remote_rig
    begin(rig)
    rig.runner.stop_ok = False
    rig.remote.report_worker.reset_mock()
    assert not rig.worker.close()
    assert_both_locks_held(rig)
    rig.remote.confirm_local_exit.assert_not_called()
    assert all(call.args[1] != "stopped" for call in rig.remote.report_worker.call_args_list)
    rig.runner.stop_ok = True
    assert rig.worker.close()
    assert_both_locks_released(rig)


def test_failed_target_stop_report_keeps_both_locks_until_persisted(remote_rig):
    rig = remote_rig
    rig.remote.report_worker.side_effect = OSError("isolated target report failure")
    try:
        assert not rig.worker.close()
        assert not rig.runner.is_alive()
        assert_both_locks_held(rig)
    finally:
        rig.remote.report_worker.side_effect = None
    assert rig.worker.close()
    rig.remote.report_worker.assert_called_with(rig.worker.session_id, "stopped", False,
        active_task_id=None, child_pid=rig.runner.pid, last_error=rig.worker.last_error)
    assert_both_locks_released(rig)


def test_stop_report_is_recorded_before_releasing_either_lock(remote_rig):
    rig = remote_rig
    rig.runner.emit(ReadyEvent(model_load_seconds=0.1))
    rig.worker.tick()
    rig.remote.report_worker.assert_called_with(rig.worker.session_id, "ready", True,
        active_task_id=None, child_pid=rig.runner.pid, last_error=None)
    recorded = []

    def report(session_id, state, model_ready, **details):
        if state == "stopped":
            assert not rig.runner.is_alive()
            assert_both_locks_held(rig)
            recorded.append((session_id, state, model_ready))
        return True

    rig.remote.report_worker.side_effect = report
    try:
        assert rig.worker.close()
    finally:
        rig.remote.report_worker.side_effect = None
    assert recorded == [(rig.worker.session_id, "stopped", False)]
    assert_both_locks_released(rig)


def test_rejected_stop_report_retains_both_locks_until_session_is_persisted(remote_rig):
    rig = remote_rig
    rig.remote.report_worker.return_value = False
    assert not rig.worker.close()
    assert_both_locks_held(rig)
    rig.remote.report_worker.return_value = True
    assert rig.worker.close()
    assert_both_locks_released(rig)


def test_atomic_spawn_guard_denial_prevents_restart_after_earlier_drain_check(remote_rig):
    rig = remote_rig
    assert rig.runner.stop(0)
    rig.worker._stop_confirmed = True
    rig.remote.draining.return_value = False  # 较早观测尚未看到停止。
    rig.remote.spawn_guard.side_effect = lambda _session: nullcontext(False)
    rig.worker._restart_child()
    assert rig.worker.drained and rig.worker.state == "draining"
    assert rig.runner.starts == 1 and not rig.runner.is_alive()
    rig.remote.begin_drain.assert_called_once_with(session_id=rig.worker.session_id)
    assert_both_locks_held(rig)
