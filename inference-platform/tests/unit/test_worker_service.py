"""Worker 控制链路单元测试：假模型只验证状态与时序，不作为真实转写证据。"""

from datetime import datetime, timezone
from contextlib import nullcontext
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.config.vision.model_catalog import MODEL_SHA256, MODEL_VERSION
from backend.schema.asr.asr_schema import TranscriptionResult
from backend.schema.execution.execution_schema import AcquireDecision, Delivery, ExecutionInput, ExecutionLease, FailureInfo, VisionExecutionInput
from backend.schema.vision.detection_schema import DetectionResult
from backend.schema.worker.worker_schema import COMMAND_ADAPTER, FailedEvent, FatalEvent, ReadyEvent, ResultEvent
from backend.service.worker.worker_service import WorkerService


class FakeRunner:
    def __init__(self):
        self.pid = 321
        self.alive = False
        self.events = []
        self.sent = []
        self.stop_ok = True
        self.stops = 0
        self.starts = 0

    def start(self):
        assert not self.alive
        self.alive = True
        self.starts += 1

    def send(self, payload):
        self.sent.append(COMMAND_ADAPTER.validate_json(payload))

    def poll(self):
        return self.events.pop(0) if self.events else None

    def emit(self, event):
        self.events.append(event.model_dump_json().encode())

    def is_alive(self):
        return self.alive

    def stop(self, timeout):
        self.stops += 1
        if self.stop_ok:
            self.alive = False
            self.events.clear()
        return self.stop_ok


@pytest.fixture(params=["asr", "vision"])
def rig(tmp_path, request):
    settings = AppSettings(database_path=tmp_path / "platform.sqlite3", upload_dir=tmp_path / "uploads", log_dir=tmp_path / "logs")
    clock = SimpleNamespace(now=0.0)
    runner = FakeRunner()
    broker = Mock()
    broker.receive.return_value = None
    broker.extend.return_value = True
    broker.acknowledge.return_value = True
    execution = Mock()
    execution.renew.return_value = True
    execution.control.return_value = "continue"
    execution.finish_cancel.return_value = True
    execution.commit_result.return_value = True
    execution.commit_failure.return_value = True
    runtime = Mock()
    worker = WorkerService(None, settings, logging.getLogger("worker-test"), runner=runner, broker=broker, execution=execution, runtime=runtime, clock=lambda: clock.now, task_type=request.param)
    result = SimpleNamespace(worker=worker, settings=settings, clock=clock, runner=runner, broker=broker, execution=execution, runtime=runtime)
    worker.start()
    yield result
    runner.stop_ok = True
    worker.close()


def begin(rig, *, attempt_budget_seconds=None, acquire_delay=0):
    delivery = Delivery(notification_id="1" * 32, task_id="2" * 32, worker_id=rig.worker.session_id, delivery_token="delivery", dispatch_version=1, execution_plan_version=1, queue_key=rig.worker.queue_key)
    lease = ExecutionLease(task_id=delivery.task_id, worker_id=delivery.worker_id, lease_token="lease", attempt=1, dispatch_version=1, execution_plan_version=1, lease_expires_at=datetime.now(timezone.utc), attempt_budget_seconds=attempt_budget_seconds)
    task_input = ExecutionInput(task_id=delivery.task_id, input_ref="3" * 32, input_sha256="4" * 64, input_size_bytes=1, model_id="whisper-small", model_version=rig.settings.asr_model_revision, language="zh")
    if rig.worker.asr_profile == "remote":
        task_input = task_input.model_copy(update={"model_id": rig.settings.remote_asr_model_id,
                                                  "model_version": rig.settings.remote_asr_model_version,
                                                  "language": None})
    if rig.worker.task_type == "vision":
        task_input = VisionExecutionInput(task_id=delivery.task_id, input_ref="3" * 32, input_sha256="4" * 64, input_size_bytes=1, model_id="yolo-general", model_version=MODEL_VERSION, image_width=200, image_height=100, confidence_threshold=.25, iou_threshold=.45, max_detections=100)
    rig.broker.receive.side_effect = [delivery, None, None, None]
    rig.execution.acquire.return_value = AcquireDecision(decision="acquired", lease=lease, input=task_input)
    if acquire_delay:
        def acquire(_):
            rig.clock.now += acquire_delay
            return rig.execution.acquire.return_value
        rig.execution.acquire.side_effect = acquire
    rig.runner.emit(ReadyEvent(model_load_seconds=0.1))
    rig.worker.tick()
    return lease


def sample_result(settings, task_type="asr"):
    if task_type == "vision":
        return DetectionResult(model_sha256=MODEL_SHA256, image_width=200, image_height=100, detections=[], runtime_version="test", load_seconds=0, inference_seconds=1, total_seconds=1)
    return TranscriptionResult(text="测试", segments=[], language="zh", language_requested="zh", language_probability=0.9, audio_duration_seconds=1, model_repo=settings.asr_model_repo, model_revision=settings.asr_model_revision, device="cpu", compute_type="int8", cpu_threads=4, model_load_seconds=0, transcription_seconds=1, total_seconds=1, faster_whisper_version="test", ctranslate2_version="test")


def test_worker_does_not_claim_until_child_reports_model_ready(rig):
    rig.worker.tick()
    assert rig.worker.state == "starting"
    rig.broker.receive.assert_not_called()
    assert rig.runner.sent[0].type == "start"
    assert rig.runner.sent[0].task_type == rig.worker.task_type
    rig.runner.emit(ReadyEvent(model_load_seconds=2))
    rig.worker.tick()
    assert rig.worker.state == "ready"
    rig.broker.receive.assert_called_once()
    assert rig.broker.receive.call_args.kwargs["queue_key"] == rig.worker.queue_key
    assert rig.runtime.report.call_args.kwargs["component_id"] == rig.worker.component_id


def test_busy_child_does_not_block_renewal_and_runtime_heartbeat(rig):
    lease = begin(rig)
    assert rig.worker.state == "running"
    rig.clock.now = 10
    rig.worker.tick()
    rig.execution.renew.assert_called_once_with(lease)
    rig.broker.extend.assert_called_once()
    assert rig.broker.receive.call_count == 1
    assert rig.runtime.report.call_args.kwargs["active_task_id"] == lease.task_id


def test_lost_lease_stops_child_before_slot_release_and_rejects_late_result(rig):
    lease = begin(rig)
    rig.runner.emit(ResultEvent(task_id=lease.task_id, result=sample_result(rig.settings, rig.worker.task_type)))
    rig.clock.now = 10
    rig.execution.renew.return_value = False
    rig.worker.tick()
    assert rig.runner.stops == 1
    assert rig.runner.starts == 2
    assert rig.worker.lease is None
    assert rig.worker.state == "starting"
    rig.execution.commit_result.assert_not_called()
    rig.broker.acknowledge.assert_not_called()


def test_unconfirmed_stop_keeps_slot_and_lock_and_never_claims(rig):
    lease = begin(rig)
    rig.runner.stop_ok = False
    rig.clock.now = 30
    rig.worker.tick()
    assert rig.worker.lease == lease
    assert rig.worker._lock.is_locked
    assert not rig.worker.can_exit
    assert rig.worker.state == "error"
    rig.worker.tick()
    assert rig.broker.receive.call_count == 1
    assert not rig.worker.close()
    rig.execution.commit_failure.assert_not_called()


def test_temporary_result_save_failure_retains_result_and_retries_without_inference(rig):
    lease = begin(rig)
    result = sample_result(rig.settings, rig.worker.task_type)
    rig.runner.emit(ResultEvent(task_id=lease.task_id, result=result))
    rig.execution.commit_result.side_effect = [OSError("disk"), True]
    rig.worker.tick()
    assert rig.worker.state == "committing"
    rig.broker.acknowledge.assert_not_called()
    rig.clock.now = 10
    rig.worker.tick()
    assert rig.execution.renew.call_count == 0
    assert rig.execution.commit_result.call_count == 2
    rig.broker.acknowledge.assert_called_once()
    assert [item.type for item in rig.runner.sent] == ["start", "run"]


def test_result_save_rejection_never_acknowledges(rig):
    lease = begin(rig)
    rig.runner.emit(ResultEvent(task_id=lease.task_id, result=sample_result(rig.settings, rig.worker.task_type)))
    rig.execution.commit_result.return_value = False
    rig.worker.tick()
    assert rig.worker.state == "starting"
    assert rig.runner.starts == 2
    rig.broker.acknowledge.assert_not_called()


def test_failed_transcription_is_persisted_before_ack(rig):
    lease = begin(rig)
    calls = []
    rig.execution.commit_failure.side_effect = lambda *_: calls.append("save") or True
    rig.broker.acknowledge.side_effect = lambda *_: calls.append("ack") or True
    rig.runner.emit(FailedEvent(task_id=lease.task_id, failure=FailureInfo(code="TRANSCRIPTION_FAILED", message="failed")))
    rig.worker.tick()
    assert calls == ["save", "ack"]
    assert rig.worker.state == "ready"


def test_terminal_notification_is_acknowledged_without_running_child(rig):
    begin(rig)
    rig.worker.lease = None
    rig.worker.delivery = None
    rig.worker.state = "ready"
    delivery = rig.execution.acquire.call_args.args[0]
    rig.broker.receive.side_effect = None
    rig.broker.receive.return_value = delivery
    rig.execution.acquire.return_value = AcquireDecision(decision="terminal_or_stale")
    rig.worker.tick()
    rig.broker.acknowledge.assert_called_once_with(delivery)
    assert [item.type for item in rig.runner.sent] == ["start", "run"]


def test_busy_notification_is_not_acknowledged(rig):
    rig.runner.emit(ReadyEvent(model_load_seconds=0))
    rig.broker.receive.return_value = Mock()
    rig.execution.acquire.return_value = AcquireDecision(decision="busy")
    rig.worker.tick()
    assert rig.worker.state == "ready"
    rig.broker.acknowledge.assert_not_called()
    assert [item.type for item in rig.runner.sent] == ["start"]


def test_child_model_start_failure_never_claims(rig):
    rig.runner.emit(FatalEvent(failure=FailureInfo(code="MODEL_MISSING", message="missing")))
    rig.worker.tick()
    assert rig.worker.can_exit
    rig.broker.receive.assert_not_called()


def test_child_crash_records_failure(rig):
    lease = begin(rig)
    rig.runner.alive = False
    rig.worker.tick()
    assert rig.execution.commit_failure.call_args.args[0] == lease
    assert rig.execution.commit_failure.call_args.args[1].code == "WORKER_CHILD_EXITED"
    rig.broker.acknowledge.assert_called_once()
    assert rig.worker.state == "starting"


def test_renewal_failures_eventually_stop_child_without_false_commit(rig):
    begin(rig)
    rig.execution.renew.side_effect = OSError("unavailable")
    rig.clock.now = 10
    rig.worker.tick()
    assert rig.runner.alive
    rig.clock.now = 30
    rig.worker.tick()
    assert rig.runner.starts == 2
    assert rig.worker.state == "starting"
    rig.execution.commit_result.assert_not_called()
    rig.broker.acknowledge.assert_not_called()


def test_graceful_stop_confirms_child_exit_before_failure_commit(rig):
    lease = begin(rig)
    def commit(received_lease, failure):
        assert not rig.runner.alive
        assert received_lease == lease
        assert failure.code == "WORKER_STOPPED"
        return True
    rig.execution.commit_failure.side_effect = commit
    assert rig.worker.close()
    rig.broker.acknowledge.assert_called_once()
    assert not rig.worker._lock.is_locked


def test_second_worker_cannot_claim_same_database(rig):
    other = WorkerService(None, rig.settings, logging.getLogger("test"), runner=FakeRunner(), broker=Mock(), execution=Mock(), runtime=Mock(), task_type=rig.worker.task_type)
    with pytest.raises(AppError, match="已有 Worker"):
        other.start()
    assert other.close()


def test_unknown_ipc_event_stops_child_and_does_not_claim(rig):
    rig.runner.events.append(b'{"type":"ready","model_load_seconds":1,"unexpected":true}')
    rig.worker.tick()
    assert rig.worker.last_error == "WORKER_PROTOCOL_ERROR"
    rig.broker.receive.assert_not_called()


def test_model_load_timeout_never_claims(rig):
    rig.clock.now = 120
    rig.worker.tick()
    assert rig.worker.last_error == "MODEL_START_TIMEOUT"
    assert not rig.runner.alive
    rig.broker.receive.assert_not_called()


def test_unknown_commit_success_is_rechecked_before_renewal(rig):
    lease = begin(rig)
    rig.runner.emit(ResultEvent(task_id=lease.task_id, result=sample_result(rig.settings, rig.worker.task_type)))
    rig.execution.commit_result.side_effect = [OSError("response lost"), True]
    rig.execution.renew.return_value = False  # 已终态，数据库不再续租。
    rig.worker.tick()
    rig.clock.now = 10
    rig.worker.tick()
    assert rig.worker.state == "ready"
    assert rig.runner.alive
    rig.broker.acknowledge.assert_called_once()
    rig.execution.renew.assert_not_called()


def test_active_child_protocol_error_records_failure_after_stop(rig):
    begin(rig)
    rig.runner.events.append(b'{"type":"invalid"}')
    def commit(lease, failure):
        assert not rig.runner.alive
        assert failure.code == "WORKER_PROTOCOL_ERROR"
        return True
    rig.execution.commit_failure.side_effect = commit
    rig.worker.tick()
    rig.broker.acknowledge.assert_called_once()
    assert rig.worker.state == "starting"


def test_error_state_remains_visible_after_close(rig):
    rig.runner.emit(FatalEvent(failure=FailureInfo(code="MODEL_MISSING", message="missing")))
    rig.worker.tick()
    rig.worker.close()
    assert rig.runtime.report.call_args.kwargs["state"] == "error"
    assert rig.runtime.report.call_args.kwargs["last_error"] == "MODEL_MISSING"


def test_cancel_is_polled_before_renew_interval_and_stops_before_finishing(rig):
    lease = begin(rig)
    rig.clock.now = 0.25
    rig.execution.control.return_value = "cancel"
    def finish(received_lease):
        assert received_lease == lease
        assert not rig.runner.alive
        assert rig.worker._lock.is_locked
        return True
    rig.execution.finish_cancel.side_effect = finish
    rig.worker.tick()
    assert not rig.runner.alive
    assert rig.worker.lease == lease
    rig.execution.renew.assert_not_called()
    rig.execution.finish_cancel.assert_not_called()
    rig.worker.tick()
    rig.execution.finish_cancel.assert_called_once_with(lease)
    rig.broker.acknowledge.assert_called_once()
    assert rig.worker.state == "starting"
    assert rig.runner.starts == 2
    assert rig.broker.receive.call_count == 1
    rig.broker.receive.side_effect = None
    rig.broker.receive.return_value = None
    rig.runner.emit(ReadyEvent(model_load_seconds=0.2))
    rig.worker.tick()
    assert rig.worker.state == "ready"
    assert rig.broker.receive.call_count == 2


def test_unconfirmed_cancel_stop_keeps_intent_slot_and_lock_until_confirmed(rig):
    lease = begin(rig)
    rig.execution.control.return_value = "cancel"
    rig.runner.stop_ok = False
    rig.worker.tick()
    rig.worker.tick()
    assert rig.worker.lease == lease
    assert rig.worker.last_error == "WORKER_STOP_UNCONFIRMED"
    assert rig.worker._lock.is_locked
    assert not rig.worker.can_exit
    assert rig.runner.starts == 1
    rig.execution.finish_cancel.assert_not_called()
    rig.broker.acknowledge.assert_not_called()
    rig.runner.stop_ok = True
    rig.worker.tick()
    assert not rig.runner.alive
    rig.worker.tick()
    rig.execution.finish_cancel.assert_called_once_with(lease)
    assert rig.runner.starts == 2
    assert rig.worker.lease is None


def test_cancel_winning_result_commit_race_never_publishes_success(rig):
    lease = begin(rig)
    rig.runner.emit(ResultEvent(task_id=lease.task_id, result=sample_result(rig.settings, rig.worker.task_type)))
    rig.execution.control.side_effect = ["continue", "cancel"]
    rig.execution.commit_result.return_value = False
    rig.worker.tick()
    assert not rig.runner.alive
    rig.execution.commit_result.assert_called_once()
    rig.broker.acknowledge.assert_not_called()
    rig.worker.tick()
    rig.execution.finish_cancel.assert_called_once_with(lease)
    assert rig.worker.state == "starting"
    rig.broker.acknowledge.assert_called_once()


@pytest.mark.parametrize("control,code", [("attempt_timeout", "ATTEMPT_TIMEOUT"), ("total_timeout", "TOTAL_TIMEOUT")])
def test_deadlines_stop_compute_before_recording_timeout(rig, control, code):
    lease = begin(rig)
    rig.execution.control.return_value = control
    rig.clock.now = 30  # 与本地租约到期同一刻，优先保存数据库裁决的期限原因。
    def commit(received_lease, failure):
        assert received_lease == lease
        assert failure.code == code
        assert not rig.runner.alive
        return True
    rig.execution.commit_failure.side_effect = commit
    rig.worker.tick()
    assert not rig.runner.alive
    rig.worker.tick()
    rig.execution.commit_failure.assert_called_once()
    rig.broker.acknowledge.assert_called_once()
    assert rig.runner.starts == 2


def test_unknown_failure_commit_is_replayed_before_stale_control(rig):
    lease = begin(rig)
    rig.runner.alive = False
    rig.execution.commit_failure.side_effect = [OSError("response lost"), True]
    rig.worker.tick()
    assert rig.worker.state == "committing"
    rig.execution.control.return_value = "expired"  # DB 已持久化 retry_wait 和下次投递版本。
    rig.clock.now = 0.25
    rig.worker.tick()
    assert rig.execution.commit_failure.call_count == 2
    assert all(call.args[0] == lease for call in rig.execution.commit_failure.call_args_list)
    assert rig.worker.state == "starting"
    rig.broker.acknowledge.assert_called_once()


def test_result_saved_but_ack_lost_redelivery_does_not_run_model_twice(rig):
    lease = begin(rig)
    delivery = rig.execution.acquire.call_args.args[0]
    rig.runner.emit(ResultEvent(task_id=lease.task_id, result=sample_result(rig.settings, rig.worker.task_type)))
    rig.broker.acknowledge.side_effect = [OSError("ack lost"), True]
    rig.worker.tick()
    assert rig.worker.state == "ready"
    rig.broker.receive.side_effect = None
    rig.broker.receive.return_value = delivery
    rig.execution.acquire.return_value = AcquireDecision(decision="terminal_or_stale")
    rig.worker.tick()
    assert [item.type for item in rig.runner.sent] == ["start", "run"]
    assert rig.broker.acknowledge.call_count == 2
    rig.execution.commit_result.assert_called_once()


def test_control_database_failure_cannot_extend_local_execution_permission(rig):
    begin(rig)
    rig.execution.control.side_effect = OSError("database unavailable")
    rig.execution.renew.side_effect = OSError("database unavailable")
    rig.clock.now = 30
    rig.worker.tick()
    assert rig.runner.starts == 2
    assert rig.worker.lease is None
    rig.execution.commit_result.assert_not_called()
    rig.execution.commit_failure.assert_not_called()
    rig.broker.acknowledge.assert_not_called()


def test_shutdown_finishes_existing_cancel_after_confirming_child_exit(rig):
    lease = begin(rig)
    rig.execution.commit_failure.return_value = False
    rig.execution.control.return_value = "cancel"
    def finish(received_lease):
        assert received_lease == lease
        assert not rig.runner.alive
        return True
    rig.execution.finish_cancel.side_effect = finish
    assert rig.worker.close()
    rig.execution.finish_cancel.assert_called_once_with(lease)
    assert rig.runner.starts == 1
    rig.broker.acknowledge.assert_called_once()


def test_rejected_timeout_failure_does_not_loop_on_earlier_expired_lease(rig):
    begin(rig)
    rig.execution.control.return_value = "attempt_timeout"
    rig.execution.commit_failure.return_value = False
    rig.worker.tick()
    assert not rig.runner.alive
    rig.worker.tick()
    assert rig.worker.lease is None
    assert rig.worker.state == "starting"
    rig.execution.commit_failure.assert_called_once()
    rig.broker.acknowledge.assert_not_called()


def test_short_granted_budget_stops_during_control_and_renew_database_outage(rig):
    begin(rig, attempt_budget_seconds=12)
    rig.execution.control.side_effect = OSError("database unavailable")
    rig.execution.renew.side_effect = OSError("database unavailable")
    rig.clock.now = 10
    rig.worker.tick()
    assert rig.runner.starts == 1 and rig.runner.alive
    rig.execution.renew.assert_called_once()
    rig.clock.now = 12
    rig.worker.tick()
    assert rig.worker.lease is None
    assert rig.runner.starts == 2  # 默认30秒租约尚未到期，12秒有效预算已强制结束旧进程。
    assert rig.worker.state == "starting"
    rig.broker.acknowledge.assert_not_called()


def test_renewal_never_refreshes_the_attempt_budget(rig):
    begin(rig, attempt_budget_seconds=25)
    for now in (10, 20):
        rig.clock.now = now
        rig.worker.tick()
    assert rig.execution.renew.call_count == 2
    assert rig.worker._deadline == 25
    rig.execution.control.side_effect = OSError("database unavailable")
    rig.clock.now = 25
    rig.worker.tick()
    assert rig.runner.starts == 2
    assert rig.worker.lease is None


def test_acquire_response_after_budget_expiry_never_starts_inference(rig):
    begin(rig, attempt_budget_seconds=1, acquire_delay=2)
    assert [item.type for item in rig.runner.sent] == ["start", "start"]
    assert rig.worker.lease is None
    rig.broker.acknowledge.assert_not_called()


def test_slow_successful_renewal_cannot_extend_past_fixed_attempt_limit(rig):
    begin(rig, attempt_budget_seconds=12)
    def renew(_):
        rig.clock.now = 13
        return True
    rig.execution.renew.side_effect = renew
    rig.clock.now = 10
    rig.worker.tick()
    assert rig.runner.starts == 2
    assert rig.worker.lease is None
    rig.broker.extend.assert_not_called()


def test_expired_local_budget_still_replays_unknown_success_before_control(rig):
    lease = begin(rig, attempt_budget_seconds=1)
    rig.runner.emit(ResultEvent(task_id=lease.task_id, result=sample_result(rig.settings, rig.worker.task_type)))
    rig.execution.commit_result.side_effect = [OSError("response lost"), True]
    rig.worker.tick()
    rig.execution.control.reset_mock()
    rig.execution.control.return_value = "expired"
    rig.clock.now = 2
    rig.worker.tick()
    assert rig.execution.commit_result.call_count == 2
    rig.execution.control.assert_not_called()
    rig.broker.acknowledge.assert_called_once()
    assert rig.runner.starts == 1


def test_other_profile_has_independent_lock_and_is_not_stopped_with_this_worker(rig):
    task_type = "vision" if rig.worker.task_type == "asr" else "asr"
    runner = FakeRunner()
    other = WorkerService(None, rig.settings, logging.getLogger("test"), runner=runner, broker=Mock(), execution=Mock(), runtime=Mock(), task_type=task_type)
    try:
        other.start()
        assert other._lock.is_locked and rig.worker._lock.is_locked
        assert other.component_id != rig.worker.component_id
        assert other.session_id != rig.worker.session_id
        assert rig.worker.close()
        assert runner.alive and other._lock.is_locked
    finally:
        other.close()


@pytest.mark.parametrize("wrong_model", [False, True])
def test_worker_rejects_cross_profile_or_wrong_model_result_before_success_commit(rig, wrong_model):
    lease = begin(rig)
    if wrong_model:
        result = sample_result(rig.settings, rig.worker.task_type)
        field = "model_revision" if rig.worker.task_type == "asr" else "model_sha256"
        result = result.model_copy(update={field: "0" * (40 if field == "model_revision" else 64)})
    else:
        result = sample_result(rig.settings, "vision" if rig.worker.task_type == "asr" else "asr")
    rig.runner.emit(ResultEvent(task_id=lease.task_id, result=result))
    def save_failure(*_):
        assert not rig.runner.alive
        return True
    rig.execution.commit_failure.side_effect = save_failure
    rig.worker.tick()
    rig.execution.commit_result.assert_not_called()
    assert rig.execution.commit_failure.call_args.args[1].code == "WORKER_PROTOCOL_ERROR"
    assert rig.runner.stops >= 1
    assert rig.worker.state == "starting"


def test_old_asr_run_json_without_task_type_remains_compatible():
    task_input = ExecutionInput(task_id="a" * 32, input_ref="b" * 32, input_sha256="c" * 64, input_size_bytes=1, model_id="whisper-small", model_version="revision", language=None)
    payload = {"type": "run", "input": task_input.model_dump(exclude={"task_type"}, by_alias=True)}
    command = COMMAND_ADAPTER.validate_python(payload)
    assert isinstance(command.input, ExecutionInput)
    assert command.input.task_type == "asr"


def test_drain_finishes_current_result_without_claiming_next_notification(rig):
    lease = begin(rig)
    rig.worker.request_drain()
    rig.worker.request_drain()
    assert rig.worker.draining and not rig.worker.drained
    assert rig.runner.alive and rig.worker.model_ready
    rig.clock.now = 10
    rig.worker.tick()
    rig.execution.renew.assert_called_once_with(lease)
    assert rig.runner.stops == 0
    assert rig.runtime.report.call_args.kwargs["state"] == "draining"
    rig.runner.emit(ResultEvent(task_id=lease.task_id, result=sample_result(rig.settings, rig.worker.task_type)))
    rig.worker.tick()
    assert rig.worker.drained and rig.runner.alive
    assert rig.broker.receive.call_count == 1
    rig.execution.commit_result.assert_called_once()
    rig.execution.commit_failure.assert_not_called()
    rig.worker.tick()
    assert rig.broker.receive.call_count == 1
    assert rig.worker.close()
    assert not rig.runner.alive and not rig.worker._lock.is_locked


def test_drain_during_model_load_does_not_claim_or_report_exit_before_close(rig):
    rig.worker.request_drain()
    assert rig.worker.drained
    assert rig.runner.alive and rig.worker._lock.is_locked
    rig.runner.emit(ReadyEvent(model_load_seconds=1))
    rig.worker.tick()
    rig.broker.receive.assert_not_called()
    assert rig.runner.starts == 1 and rig.runner.stops == 0
    assert rig.worker.close()
    assert not rig.runner.alive


@pytest.mark.parametrize("outcome", ["cancel", "attempt_timeout", "total_timeout", "expired"])
def test_drain_preserves_control_rules_without_rebuilding_model(rig, outcome):
    lease = begin(rig)
    rig.worker.request_drain()
    rig.execution.control.return_value = outcome
    rig.worker.tick()
    rig.worker.tick()
    assert rig.worker.drained
    assert not rig.runner.alive and rig.runner.starts == 1
    assert rig.broker.receive.call_count == 1
    if outcome == "cancel":
        rig.execution.finish_cancel.assert_called_once_with(lease)
    elif outcome.endswith("timeout"):
        rig.execution.commit_failure.assert_called_once()
    else:
        rig.execution.commit_failure.assert_not_called()
        rig.broker.acknowledge.assert_not_called()


def test_drain_retries_pending_result_without_forcing_failure(rig):
    lease = begin(rig)
    rig.worker.request_drain()
    rig.runner.emit(ResultEvent(task_id=lease.task_id, result=sample_result(rig.settings, rig.worker.task_type)))
    rig.execution.commit_result.side_effect = [OSError("temporary storage error"), True]
    rig.worker.tick()
    assert not rig.worker.drained
    assert rig.runner.stops == 0
    rig.clock.now = 1
    rig.worker.tick()
    assert rig.worker.drained
    assert rig.execution.commit_result.call_count == 2
    rig.execution.commit_failure.assert_not_called()
    assert rig.broker.receive.call_count == 1


def test_drain_child_crash_persists_failure_without_rebuild(rig):
    begin(rig)
    rig.worker.request_drain()
    rig.runner.alive = False
    rig.worker.tick()
    assert rig.worker.drained
    rig.execution.commit_failure.assert_called_once()
    assert rig.runner.starts == 1


def test_drain_with_unconfirmed_cancel_keeps_lease_and_lock(rig):
    lease = begin(rig)
    rig.worker.request_drain()
    rig.execution.control.return_value = "cancel"
    rig.runner.stop_ok = False
    rig.worker.tick()
    assert not rig.worker.drained and not rig.worker.can_exit
    assert rig.worker.lease == lease and rig.worker._lock.is_locked
    rig.execution.finish_cancel.assert_not_called()
    rig.runner.stop_ok = True
    rig.worker.tick()
    rig.worker.tick()
    assert rig.worker.drained
    assert rig.runner.starts == 1


@pytest.fixture
def remote_rig(tmp_path):
    settings = AppSettings(database_path=tmp_path / "remote.sqlite3", upload_dir=tmp_path / "uploads",
                           log_dir=tmp_path / "logs", remote_asr_base_url="https://isolated.invalid:8003",
                           remote_asr_model_id="test-sensevoice", remote_asr_model_version="fixture-v1")
    clock = SimpleNamespace(now=0.0)
    runner = FakeRunner()
    broker, execution, runtime, remote = Mock(), Mock(), Mock(), Mock()
    broker.receive.return_value = None
    broker.extend.return_value = execution.renew.return_value = True
    execution.control.return_value = "continue"
    execution.commit_failure.return_value = execution.commit_result.return_value = True
    remote.draining.return_value = False
    remote.spawn_guard.side_effect = lambda _session: nullcontext(True)
    worker = WorkerService(None, settings, logging.getLogger("remote-worker-test"), runner=runner,
                           broker=broker, execution=execution, runtime=runtime, remote_service=remote,
                           asr_profile="remote", clock=lambda: clock.now)
    worker.start()
    yield SimpleNamespace(worker=worker, settings=settings, clock=clock, runner=runner, broker=broker,
                          execution=execution, runtime=runtime, remote=remote)
    runner.stop_ok = True
    remote.confirm_local_exit.side_effect = None
    runtime.report.side_effect = None
    worker.close()


def test_remote_worker_has_fixed_distinct_queue_lock_profile_and_no_agent_gate(remote_rig):
    rig = remote_rig
    assert rig.worker.component_id == "remote_asr_worker"
    assert rig.worker.queue_key == "asr.remote-v2-sensevoice"
    assert rig.worker._lock.lock_file.endswith(".remote-asr-worker.lock")
    assert rig.worker.gate is None
    assert rig.runner.sent[0].asr_profile == "remote"
    rig.remote.begin_worker.assert_called_once_with(rig.worker.session_id)


def test_remote_health_failure_keeps_local_loop_alive_and_bounded_rechecks(remote_rig):
    rig = remote_rig
    rig.runner.emit(FatalEvent(failure=FailureInfo(code="REMOTE_ASR_UNAVAILABLE", message="未就绪")))
    rig.worker.tick()
    assert rig.worker.state == "unavailable"
    assert not rig.worker.model_ready and not rig.worker.can_exit
    rig.broker.receive.assert_not_called()
    rig.worker.tick()
    assert rig.runner.starts == 1
    rig.clock.now = rig.settings.runtime_stale_seconds
    rig.worker.tick()
    assert rig.runner.starts == 2
    assert rig.worker.state == "starting"
    rig.runner.emit(ReadyEvent(model_load_seconds=.1))
    rig.worker.tick()
    assert rig.worker.state == "ready"


def test_remote_drain_during_unavailable_never_recreates_http_child(remote_rig):
    rig = remote_rig
    rig.runner.emit(FatalEvent(failure=FailureInfo(code="REMOTE_ASR_UNAVAILABLE", message="未就绪")))
    rig.worker.tick()
    rig.worker.request_drain()
    rig.worker.request_drain()
    rig.clock.now = 200
    rig.worker.tick()
    assert rig.worker.drained and rig.runner.starts == 1
    rig.remote.begin_drain.assert_called_once_with(session_id=rig.worker.session_id)


def test_remote_drain_allows_current_result_and_stops_further_claims(remote_rig):
    rig = remote_rig
    lease = begin(rig)
    rig.worker.request_drain()
    rig.clock.now = 10
    rig.worker.tick()
    assert rig.runner.stops == 0
    rig.execution.renew.assert_called_once_with(lease)
    result = TranscriptionResult(text="远端测试", segments=[], audio_duration_seconds=1,
                                  engine_metadata={"adapter_key": "remote-v2-sensevoice"})
    rig.runner.emit(ResultEvent(task_id=lease.task_id, result=result))
    rig.worker.tick()
    assert rig.worker.drained
    assert rig.broker.receive.call_count == 1
    rig.execution.commit_result.assert_called_once_with(lease, result)
    assert rig.worker.close()
    rig.remote.confirm_local_exit.assert_called_once_with(rig.worker.session_id)
    assert rig.runtime.report.call_args.kwargs["state"] == "stopped"


def test_remote_lease_loss_records_unknown_after_local_exit_before_restarting(remote_rig):
    rig = remote_rig
    begin(rig)
    rig.clock.now = 31
    rig.worker.tick()
    rig.remote.confirm_local_exit.assert_called_once_with(rig.worker.session_id)
    assert rig.runner.stops == 1
    assert rig.worker.lease is None
    rig.broker.acknowledge.assert_not_called()


def test_remote_unknown_save_failure_retains_lease_and_exclusive_lock(remote_rig):
    rig = remote_rig
    lease = begin(rig)
    rig.remote.confirm_local_exit.side_effect = OSError("database unavailable")
    rig.clock.now = 31
    rig.worker.tick()
    assert rig.worker.lease == lease and rig.worker._lock.is_locked
    assert rig.runner.starts == 1
    assert not rig.worker.can_exit
    rig.remote.confirm_local_exit.side_effect = None
    rig.worker.tick()
    assert rig.worker.lease is None


def test_remote_close_retains_lock_when_call_state_or_stop_report_cannot_be_saved(remote_rig):
    rig = remote_rig
    rig.remote.confirm_local_exit.side_effect = OSError("database unavailable")
    assert not rig.worker.close()
    assert rig.worker._lock.is_locked
    rig.remote.confirm_local_exit.side_effect = None
    rig.runtime.report.side_effect = OSError("report unavailable")
    assert not rig.worker.close()
    assert rig.worker._lock.is_locked
    rig.runtime.report.side_effect = None
    assert rig.worker.close()
    assert not rig.worker._lock.is_locked


def test_remote_database_drain_signal_prevents_the_next_claim(remote_rig):
    rig = remote_rig
    rig.remote.draining.return_value = True
    rig.worker.tick()
    assert rig.worker.drained
    rig.broker.receive.assert_not_called()
    assert rig.runner.starts == 1


def test_remote_failure_uses_existing_failure_commit_and_does_not_resend(remote_rig):
    rig = remote_rig
    lease = begin(rig)
    failure = FailureInfo(code="REMOTE_ASR_UNKNOWN", message="结果未知")
    rig.runner.emit(FailedEvent(task_id=lease.task_id, failure=failure))
    rig.worker.tick()
    rig.execution.commit_failure.assert_called_once_with(lease, failure)
    assert [command.type for command in rig.runner.sent] == ["start", "run"]


def test_remote_profile_is_rejected_for_vision_before_any_process(tmp_path):
    settings = AppSettings(database_path=tmp_path / "data.sqlite3")
    with pytest.raises(ValueError, match="无效"):
        WorkerService(None, settings, logging.getLogger("test"), task_type="vision", asr_profile="remote")
