"""调用池控制故障测试；假槽不作为实际并行或模型证据。"""

import logging
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

from filelock import FileLock, Timeout
import pytest

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.schema.worker.worker_schema import ReadyEvent
from backend.service.worker.remote_pool_service import RemotePoolService
from backend.service.worker.worker_service import WorkerService
from test_worker_service import FakeRunner


class Slot:
    def __init__(self, *_args, **kwargs):
        self.arguments = kwargs
        self.session_id = str(kwargs["remote_slot_index"]) * 32
        self._owns_lock = self.draining = self.model_ready = False
        self.state = "starting"
        self.lease = None
        self.tick = Mock()
        self.close_ok = True
        self.closed = 0

    def start(self):
        self._owns_lock = True

    @property
    def drained(self):
        return self.draining and self.lease is None

    def request_drain(self):
        self.draining = True

    def close(self):
        self.closed += 1
        if self.close_ok:
            self._owns_lock = False
        return self.close_ok


@pytest.fixture
def pool_rig(tmp_path):
    settings = AppSettings(database_path=tmp_path / "pool.sqlite3",
        log_dir=tmp_path / "logs", runtime_heartbeat_seconds=.1, poll_interval_seconds=.05)
    remote, runtime = Mock(), Mock()
    remote.pool_limit.return_value = 2
    pool = RemotePoolService(None, settings, logging.getLogger("pool-fixture"),
        worker_factory=Slot, remote_service=remote, runtime=runtime)
    yield SimpleNamespace(pool=pool, settings=settings, remote=remote, runtime=runtime)
    runtime.report.side_effect = None
    remote.begin_drain.side_effect = None
    for slot in pool.workers:
        slot.lease, slot.close_ok = None, True
        slot.tick.side_effect = None
    assert pool.close()


def assert_global_held(pool):
    with pytest.raises(Timeout):
        with FileLock(pool._lock.lock_file, timeout=0):
            pass


def assert_global_free(pool):
    with FileLock(pool._lock.lock_file, timeout=0):
        pass


def test_pool_binds_unique_slots_and_excludes_legacy_direct_worker(pool_rig):
    rig = pool_rig
    rig.pool.start()
    assert [slot.arguments["remote_slot_index"] for slot in rig.pool.workers] == [0, 1]
    assert all(slot.arguments["remote_pooled"] and
        slot.arguments["remote_target_id"] == "remote-default" for slot in rig.pool.workers)
    rig.remote.start_pool.assert_called_once()
    assert_global_held(rig.pool)
    # 同一旧全局锁名仍阻止另一池或旧版单 Worker，不能绕过容量。
    other = RemotePoolService(None, rig.settings, logging.getLogger("other-pool"),
        worker_factory=Slot, remote_service=Mock(), runtime=Mock())
    with pytest.raises(AppError, match="已有外部"):
        other.start()
    assert other.close()
    assert_global_held(rig.pool)


def test_slot_error_drains_every_slot_but_still_ticks_other_inflight(pool_rig):
    rig = pool_rig
    rig.pool.start()
    broken, busy = rig.pool.workers
    broken.tick.side_effect = RuntimeError("fixture control failure")
    busy.lease = object()
    rig.pool.tick()
    assert rig.pool.draining and not rig.pool.drained
    assert broken.draining and busy.draining
    busy.tick.assert_called_once()
    assert busy.closed == 0 and busy.lease is not None
    assert rig.pool.last_error == "REMOTE_ASR_POOL_SLOT_ERROR"
    assert not rig.pool.close()
    assert_global_held(rig.pool)


def test_close_waits_for_task_completion_then_all_children_and_stopped_report(pool_rig):
    rig = pool_rig
    rig.pool.start()
    first, second = rig.pool.workers
    first.lease = object()
    assert not rig.pool.close()
    assert first.closed == second.closed == 0
    first.lease = None
    first.close_ok = False
    assert not rig.pool.close()
    assert first.closed == second.closed == 1  # 不因第一个未确认而跳过第二个。
    assert_global_held(rig.pool)
    first.close_ok = True
    rig.runtime.report.side_effect = OSError("fixture persistence failure")
    assert not rig.pool.close()
    assert not any(slot._owns_lock for slot in rig.pool.workers)
    assert_global_held(rig.pool)
    rig.runtime.report.side_effect = None
    assert rig.pool.close()
    assert rig.runtime.report.call_args.kwargs["state"] == "stopped"
    assert_global_free(rig.pool)


def test_database_stop_intent_seen_by_one_slot_drains_all_before_next_claim(pool_rig):
    rig = pool_rig
    rig.pool.start()
    first, second = rig.pool.workers
    first.tick.side_effect = first.request_drain

    def second_tick():
        assert second.draining

    second.tick.side_effect = second_tick
    rig.pool.tick()
    assert rig.pool.draining
    rig.remote.begin_drain.assert_called_once()


def test_failed_pool_registration_releases_only_its_own_global_lock(pool_rig):
    rig = pool_rig
    rig.remote.start_pool.side_effect = AppError("FIXTURE", "fixture rollback", 409)
    with pytest.raises(AppError):
        rig.pool.start()
    assert rig.pool.close()
    rig.remote.begin_drain.assert_not_called()
    rig.runtime.report.assert_not_called()
    assert_global_free(rig.pool)


def test_partial_slot_start_failure_keeps_pool_guard_until_previous_slots_exit(pool_rig):
    rig = pool_rig

    def factory(*args, **kwargs):
        slot = Slot(*args, **kwargs)
        if kwargs["remote_slot_index"] == 1:
            slot.start = Mock(side_effect=AppError("FIXTURE", "fixture rejected before start", 409))
        return slot

    rig.pool.worker_factory = factory
    with pytest.raises(AppError):
        rig.pool.start()
    assert len(rig.pool.workers) == 2 and rig.pool.workers[0]._owns_lock
    rig.pool.workers[0].close_ok = False
    assert not rig.pool.close()
    assert_global_held(rig.pool)
    rig.pool.workers[0].close_ok = True
    assert rig.pool.close()
    assert_global_free(rig.pool)


def test_pooled_worker_reports_slot_only_and_stop_does_not_block_other_slots(tmp_path):
    settings = AppSettings(database_path=tmp_path / "isolated.sqlite3", log_dir=tmp_path / "logs",
        remote_asr_base_url="https://example.invalid", remote_asr_model_id="fixture",
        remote_asr_model_version="fixture-v1")
    runner, broker, execution, runtime, remote = FakeRunner(), Mock(), Mock(), Mock(), Mock()
    broker.receive.return_value = None
    remote.draining.return_value = False
    remote.spawn_guard.side_effect = lambda _session: nullcontext(True)
    stop = Mock(wraps=runner.stop)
    runner.stop = stop
    worker = WorkerService(None, settings, logging.getLogger("pooled-slot"),
        runner=runner, broker=broker, execution=execution, runtime=runtime, remote_service=remote,
        asr_profile="remote", remote_pooled=True, remote_slot_index=1)
    global_owner = FileLock(worker._lock.lock_file, timeout=0)
    try:
        global_owner.acquire()
        worker.start()
        runner.emit(ReadyEvent(model_load_seconds=.01))
        worker.tick()
        assert worker.state == "ready"
        runtime.report.assert_not_called()
        assert remote.report_worker.call_args.kwargs["child_pid"] == runner.pid
        worker.request_drain()
        assert worker.close()
        stop.assert_called_with(0)
        runtime.report.assert_not_called()
        with pytest.raises(Timeout):
            with FileLock(global_owner.lock_file, timeout=0):
                pass
    finally:
        assert worker.close()
        global_owner.release()
