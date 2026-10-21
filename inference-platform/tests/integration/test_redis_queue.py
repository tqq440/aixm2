"""模块 3 隔离 SQLite + 明确标注的 Redis 客户端替身，无真实服务。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

from alembic import command
from pydantic import SecretStr
import pytest
from sqlalchemy import event, func, select, text

from backend.common.exceptions.app_error import AppError
from backend.data.database.database import database_now
from backend.data.invocations.invocation_models import InferenceInvocation, InferenceTaskEvent
from backend.data.invocations.invocation_repository import InvocationRepository
from backend.data.queue.queue_models import InferenceQueueOutbox, InferenceQueueState
from backend.data.tasks.task_models import TaskRecord
from backend.data.llm.online_models import OnlineRequestRecord
from backend.schema.invocations.invocation_schema import InvocationBinding, EventQuery, OnlineCompletion
from backend.service.invocations.invocation_service import InvocationService
from backend.service.queue.queue_service import QueueService
from backend.schema.queue.queue_schema import QueueNotice
from redis_stream_fake import RedisStreamFake
from test_common_execution import case, asr_result


@pytest.fixture
def queue_case(case):
    settings = case.settings.model_copy(update={"common_queue_backend": "redis", "redis_url": SecretStr("redis://127.0.0.1:6379/0")})
    client = RedisStreamFake()
    queue = QueueService(case.db, settings, client=client, consumer="consumer-one", logger=case.app.extensions["app_logger"])
    case.core, case.settings = queue.core, settings
    case.app.extensions["queue_service"] = queue
    case.app.extensions["invocation_service"] = queue.core
    case.group(); case.service(); case.budget(limit=3)
    return case, queue, client


def due_now(case):
    with case.db.transaction(immediate=True) as session:
        for row in session.scalars(select(InferenceQueueOutbox)):
            row.next_attempt_at = database_now(session) - timedelta(seconds=1)
            row.publisher_expires_at = None


def test_task_binding_outbox_and_events_are_one_transaction(queue_case):
    case, queue, fake = queue_case
    task, invocation = case.task()
    with case.db.transaction() as session:
        rows = list(session.scalars(select(InferenceQueueOutbox)))
        assert len(rows) == 1 and rows[0].redis_required and rows[0].state == "pending"
        assert rows[0].invocation_id == invocation and session.get(TaskRecord, task).status == "queued"
    def reject(_connection, _cursor, statement, *_args):
        if "INSERT INTO inference_queue_outboxes" in statement: raise RuntimeError("isolated rollback")
    event.listen(case.db.engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError): case.task()
    finally: event.remove(case.db.engine, "before_cursor_execute", reject)
    with case.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(TaskRecord)) == 1
        assert session.scalar(select(func.count()).select_from(InferenceInvocation)) == 1
        assert session.scalar(select(func.count()).select_from(InferenceQueueOutbox)) == 1
    assert not fake.commands


def test_notification_required_and_result_commit_precedes_ack(queue_case):
    case, queue, fake = queue_case
    task, invocation = case.task()
    assert not case.core.claim_round("worker")
    assert case.core.get(invocation)["reason"] == "WAITING_REDIS_NOTIFICATION"
    assert queue.publish() == 1
    assert not case.core.claim_round("worker")
    assert queue.collect() == 1
    lease = case.core.claim_round("worker")[0]
    assert queue.acknowledge_finished() == 0
    def check_ack(_):
        with case.db.transaction() as session:
            assert session.get(TaskRecord, task).result_json["text"] == "NEVER_LOG_THIS_RESULT"
            assert session.get(InferenceInvocation, invocation).permit_state == "released"
    fake.before_ack = check_ack
    assert case.core.dispatch(lease, lambda *_: asr_result())
    assert queue.acknowledge_finished() == 1
    assert not fake.pending and not fake.entries
    assert queue.acknowledge_finished() == 0


def test_xadd_reply_lost_causes_duplicate_notice_not_duplicate_inference(queue_case):
    case, queue, fake = queue_case
    task, invocation = case.task()
    def lost(_): raise ConnectionError("PRIVATE_REDIS_PASSWORD")
    fake.after_add = lost
    with pytest.raises(AppError) as error: queue.publish()
    assert error.value.code == "REDIS_UNAVAILABLE" and "PRIVATE" not in str(error.value)
    with case.db.transaction() as session:
        outbox = session.scalar(select(InferenceQueueOutbox))
        assert outbox.publish_attempts == 1 and outbox.last_error_code == "REDIS_UNAVAILABLE"
    fake.after_add = None; due_now(case)
    assert queue.publish() == 1 and len(fake.entries) == 2
    assert queue.collect() == 2
    calls = []
    def sender(*_): calls.append(1); return asr_result()
    assert queue.run_once("worker", sender)
    assert not queue.run_once("worker", sender)
    fake.clock += 31000; queue.collect()
    assert calls == [1] and not fake.pending
    assert sum(e["eventType"] == "invocation.capacity_released" for e in case.events(task)) == 1


def test_process_exits_after_xadd_before_sql_commit_replays_safely(queue_case, monkeypatch):
    case, queue, fake = queue_case
    case.task()
    from backend.data.queue.queue_repository import QueueRepository
    original = QueueRepository.record_publication
    monkeypatch.setattr(QueueRepository, "record_publication", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("isolated process loss")))
    with pytest.raises(RuntimeError): queue.publish()
    assert len(fake.entries) == 1
    monkeypatch.setattr(QueueRepository, "record_publication", original)
    due_now(case)
    assert queue.publish() == 1 and len(fake.entries) == 2
    queue.collect()
    assert len(case.core.claim_round("worker")) == 1


def test_redis_loses_stream_rebuilds_from_sqlite_waiting_outbox(queue_case):
    case, queue, fake = queue_case
    case.task(); queue.publish(); fake.lose_data(); due_now(case)
    assert queue.publish() == 1
    assert queue.collect() == 1
    assert queue.run_once("worker", lambda *_: asr_result())


@pytest.mark.parametrize("sent", [False, True])
def test_reclaimed_notice_cannot_reexecute_live_or_expired_call(queue_case, sent):
    case, queue, fake = queue_case
    task, invocation = case.task(); queue.tick()
    lease = case.core.claim_round("worker")[0]
    if sent: assert case.core.mark_sent(lease)
    fake.clock += 31000
    second = QueueService(case.db, case.settings, client=fake, consumer="consumer-two")
    assert second.collect() == 1
    assert not second.core.claim_round("another-worker")
    case.expire(invocation); assert second.core.recover() == 1
    second.acknowledge_finished()
    assert case.core.get(invocation)["permitState"] == ("uncertain" if sent else "released")
    with case.db.transaction() as session:
        assert session.get(TaskRecord, task).attempt == 1
        assert InvocationRepository.occupied(session, "group:shared") == int(sent)


def test_redis_outage_keeps_accepted_offline_and_rejects_new_online(queue_case):
    case, queue, fake = queue_case
    fake.down = True
    task, invocation = case.task()
    with pytest.raises(AppError): queue.publish()
    with pytest.raises(AppError): case.core.claim_round("worker")
    case.service("llm-main", "llm"); case.budget(version=1)
    with pytest.raises(AppError) as error: case.online()
    assert error.value.code == "REDIS_UNAVAILABLE"
    assert case.core.get(invocation)["state"] == "waiting"
    with case.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(OnlineRequestRecord)) == 0
        assert session.get(TaskRecord, task).attempt == 0
    assert queue.status()["availability"] == "unavailable"
    fake.down = False
    assert queue.run_once("worker", lambda *_: asr_result())


def test_outage_before_send_and_inflight_result_do_not_lose_capacity_or_result(queue_case):
    case, queue, fake = queue_case
    task, invocation = case.task(); queue.tick(); lease = case.core.claim_round("worker")[0]
    fake.down = True
    with pytest.raises(AppError): case.core.mark_sent(lease)
    assert case.core.get(invocation)["state"] == "reserved"
    fake.down = False; assert case.core.mark_sent(lease)
    fake.down = True
    assert case.core.renew(lease)
    assert case.core.complete(lease, asr_result())
    with pytest.raises(AppError): queue.acknowledge_finished()
    with case.db.transaction() as session:
        assert session.get(TaskRecord, task).status == "succeeded"
    fake.down = False; assert queue.acknowledge_finished() == 1


def test_failed_ack_does_not_undo_result_and_is_retried(queue_case):
    case, queue, fake = queue_case
    task, invocation = case.task(); queue.tick(); lease = case.core.claim_round("worker")[0]
    case.core.dispatch(lease, lambda *_: asr_result())
    fake.fail_ack = True
    with pytest.raises(AppError): queue.acknowledge_finished()
    assert case.core.get(invocation)["outcome"] == "succeeded"
    fake.fail_ack = False; assert queue.acknowledge_finished() == 1
    assert sum(e["eventType"] == "invocation.capacity_released" for e in case.events(task)) == 1


def test_cancelled_unpublished_job_is_never_published(queue_case):
    case, queue, fake = queue_case
    task, invocation = case.task()
    with case.db.transaction(immediate=True) as session:
        assert case.core.cancel_in_transaction(session, session.get(InferenceInvocation, invocation)) == "cancelled"
    assert queue.tick().published == 0
    assert not fake.entries


@pytest.mark.parametrize("change", ["invalid", "revision", "unknown", "queue"])
def test_invalid_or_stale_notification_never_changes_job(queue_case, change):
    case, queue, fake = queue_case
    task, invocation = case.task(); queue.gate.check()
    with case.db.transaction() as session:
        outbox = session.scalar(select(InferenceQueueOutbox))
        values = QueueNotice(event_id=outbox.event_id, invocation_id=invocation,
            service_revision=case.core.get(invocation)["serviceRevision"], queue_key=session.get(InferenceInvocation, invocation).queue_key).model_dump(by_alias=True)
    if change == "invalid": values["privateBody"] = "NEVER_LOG_THIS"
    if change == "revision": values["serviceRevision"] = "f" * 64
    if change == "unknown": values["eventId"] = uuid4().hex
    if change == "queue": values["queueKey"] = "common." + "f" * 64
    fake.xadd(queue.gate.streams.stream, values)
    before = case.events(task)
    assert queue.collect() == 0
    assert case.core.get(invocation)["state"] == "waiting" and case.events(task) == before
    assert not fake.pending


def test_switching_backend_cannot_bypass_redis_required_call(queue_case):
    case, queue, fake = queue_case
    _, invocation = case.task(); queue.tick()
    sqlite_settings = case.settings.model_copy(update={"common_queue_backend": "sqlite"})
    old = InvocationService(case.db, sqlite_settings)
    assert not old.claim_round("sqlite-worker")
    assert old.get(invocation)["reason"] == "REDIS_BACKEND_REQUIRED"
    lease = case.core.claim_round("redis-worker")[0]
    with pytest.raises(AppError): old.mark_sent(lease)


def test_two_publishers_and_consumers_do_not_overbook_shared_capacity(queue_case):
    case, queue, fake = queue_case
    case.task(); case.task()
    other = QueueService(case.db, case.settings, client=fake, consumer="consumer-two")
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(f.result() for f in [pool.submit(queue.publish), pool.submit(other.publish)]) == 2
    queue.collect(); other.collect()
    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = [x for f in [pool.submit(queue.core.claim_round, "one"), pool.submit(other.core.claim_round, "two")] for x in f.result()]
    assert len(leases) == 1


@pytest.mark.parametrize("checked", [False, True])
def test_queue_status_get_never_contacts_redis_or_writes_rows(queue_case, checked):
    case, queue, fake = queue_case
    if checked:
        queue.gate.check(); fake.commands.clear()
    fake.down = True
    with case.db.transaction() as session:
        assert session.get(InferenceQueueState, "redis").availability == ("available" if checked else "unknown")
    client = case.app.test_client()
    for _ in range(3):
        response = client.get("/api/admin/common-execution/queue")
        assert response.status_code == 200 and response.get_json()["data"]["availability"] == ("available" if checked else "unknown")
        if checked: assert response.get_json()["data"]["checkedAt"].endswith("Z")
    assert not fake.commands


def test_network_publish_and_ack_are_outside_sql_write_transactions(queue_case):
    case, queue, fake = queue_case
    case.task()
    def writes_outside(_):
        with case.db.transaction(immediate=True) as session: session.execute(text("SELECT 1"))
    fake.after_add = writes_outside; fake.before_ack = writes_outside
    assert queue.run_once("worker", lambda *_: asr_result())


def test_missing_optional_client_is_explicit_and_does_not_touch_jobs(queue_case, monkeypatch):
    case, queue, fake = queue_case
    task, invocation = case.task()
    import backend.service.queue.redis_gate as module
    def missing(_): raise AppError("REDIS_CLIENT_MISSING", "optional client missing", 503)
    monkeypatch.setattr(module, "create_redis_client", missing)
    unavailable = QueueService(case.db, case.settings)
    with pytest.raises(AppError) as error: unavailable.publish()
    assert error.value.code == "REDIS_CLIENT_MISSING"
    assert case.core.get(invocation)["state"] == "waiting"
    assert unavailable.status()["lastErrorCode"] == "REDIS_CLIENT_MISSING"


def test_version_and_group_errors_pause_new_calls(queue_case):
    case, queue, fake = queue_case
    case.task(); fake.version = "5.0.0"
    with pytest.raises(AppError) as error: queue.publish()
    assert error.value.code == "REDIS_VERSION_UNSUPPORTED"
    fake.version = "7.2.0"
    fake.xgroup_create = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("NOPERM private credentials"))
    with pytest.raises(AppError) as error: queue.publish()
    assert error.value.code == "REDIS_UNAVAILABLE" and "private" not in str(error.value)


def test_unknown_keeps_pel_until_explicit_terminal_and_never_republishes(queue_case):
    case, queue, fake = queue_case
    task, invocation = case.task(); queue.tick(); lease = case.core.claim_round("worker")[0]
    case.core.mark_sent(lease); case.expire(invocation); case.core.recover()
    assert queue.acknowledge_finished() == 0 and len(fake.pending) == 1
    due_now(case); assert queue.publish() == 0
    fake.clock += 31000; queue.collect()
    assert not case.core.claim_round("replacement")
    case.core.resolve_unknown(invocation, "local-user", outcome="failed", evidence="verified terminal fixture")
    assert queue.acknowledge_finished() == 1 and not fake.pending
    with case.db.transaction() as session:
        assert session.get(TaskRecord, task).error_json["code"] == "COMMON_UNKNOWN"


def test_completed_result_survives_redis_loss_without_new_inference(queue_case):
    case, queue, fake = queue_case
    task, invocation = case.task(); queue.tick(); lease = case.core.claim_round("worker")[0]
    case.core.mark_sent(lease); fake.lose_data()
    assert case.core.complete(lease, asr_result())
    assert queue.tick().acknowledged == 1
    assert not case.core.claim_round("replacement") and not fake.entries


def test_publication_failure_obeys_backoff_and_rebinding_is_idempotent(queue_case):
    case, queue, fake = queue_case
    task, invocation = case.task()
    bound = case.core.bind(InvocationBinding(application_id="local-user", service_id="asr-main", owner_id=task, owner_kind="task"))
    assert bound["invocationId"] == invocation
    with case.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(InferenceQueueOutbox)) == 1
    fake.after_add = lambda _: (_ for _ in ()).throw(ConnectionError("lost reply"))
    with pytest.raises(AppError): queue.publish()
    fake.after_add = None
    added = fake.commands.count("XADD")
    assert queue.publish() == 0 and fake.commands.count("XADD") == added


def test_three_kinds_share_quota_and_llm_never_enters_stream(queue_case):
    case, queue, fake = queue_case
    case.service("vision-main", "vision"); case.service("llm-main", "llm"); case.budget(version=1, limit=3)
    case.task(); case.task("vision-main"); online, request = case.online()
    assert queue.tick().published == 2
    with ThreadPoolExecutor(max_workers=2) as pool:
        offline = pool.submit(case.core.claim_round, "worker")
        llm = pool.submit(case.core.claim_online, online["invocationId"], online["workerId"])
        assert len(offline.result()) + int(llm.result() is not None) == 1
    with case.db.transaction() as session:
        assert InvocationRepository.occupied(session, "group:shared") == 1
        box = session.scalar(select(InferenceQueueOutbox).where(InferenceQueueOutbox.invocation_id == online["invocationId"]))
        assert box.state == "online" and box.message_id is None
    assert all(values["invocationId"] != online["invocationId"] for values in fake.entries.values())


def test_live_redis_contract_only_when_explicit_test_url_is_provided(case):
    import os
    url = os.environ.get("INFERENCE_REDIS_LIVE_TEST_URL")
    if not url:
        pytest.skip("未提供独立测试 Redis；真实客户端、服务与重启验收尚未执行")
    from backend.config.settings.app_settings import AppSettings
    settings = AppSettings(**{**case.settings.model_dump(), "common_queue_backend": "redis", "redis_url": url})
    queue = QueueService(case.db, settings)
    case.core = queue.core
    case.group(); case.service(); case.budget()
    task, invocation = case.task()
    try:
        queue.tick()
        # 只删除本次新建隔离库 UUID 对应的流，不清库、不访问生产键。
        queue.gate.client.delete(queue.gate.streams.stream)
        due_now(case)
        assert queue.run_once("live-contract-worker", lambda *_: asr_result())
        assert queue.core.get(invocation)["outcome"] == "succeeded"
    finally:
        if queue.gate.streams:
            queue.gate.client.delete(queue.gate.streams.stream)
        queue.close()


def test_0016_upgrade_preserves_results_unknown_permits_and_old_event_history(tmp_path):
    from alembic.config import Config
    from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
    from backend.modules.api.app import create_app, close_app
    from test_common_execution import Case
    settings = AppSettings(common_execution_enabled=True, database_path=tmp_path / "legacy16.sqlite3", log_dir=tmp_path / "logs",
                           upload_dir=tmp_path / "uploads")
    migration = Config(str(PROJECT_ROOT / "alembic.ini")); migration.attributes["settings"] = settings
    command.upgrade(migration, "20261021_0016")
    app = create_app(settings); case = Case(app, settings, migration)
    try:
        case.group(limit=2); case.service(limit=2); case.budget(limit=3)
        saved, _ = case.task(); lease = case.core.claim_round("old-worker")[0]
        assert case.core.dispatch(lease, lambda *_: asr_result())
        unknown, unknown_id = case.task(); lease = case.core.claim_round("old-worker")[0]
        case.core.mark_sent(lease); case.core.fail(lease)
        waiting, _ = case.task()
        with case.db.transaction() as session:
            before = [row.to_dict() for row in session.scalars(select(TaskRecord).order_by(TaskRecord.task_id))]
            history = session.scalar(select(func.count()).select_from(InferenceTaskEvent))
        command.upgrade(migration, "head")
        with case.db.transaction() as session:
            assert before == [row.to_dict() for row in session.scalars(select(TaskRecord).order_by(TaskRecord.task_id))]
            assert history == session.scalar(select(func.count()).select_from(InferenceTaskEvent))
            assert InvocationRepository.occupied(session, "group:shared") == 1
            assert session.scalar(select(func.count()).select_from(InferenceQueueOutbox)) == 3
            broker_id = session.get(InferenceQueueState, "redis").broker_id
        command.upgrade(migration, "head"); command.check(migration)
        with case.db.transaction() as session:
            assert session.get(InferenceQueueState, "redis").broker_id == broker_id
        queue = QueueService(case.db, settings.model_copy(update={"common_queue_backend": "redis",
            "redis_url": SecretStr("redis://127.0.0.1:6379/0")}), client=RedisStreamFake())
        assert queue.publish() == 1  # 仅补 waiting，不重发成功或未知调用。
        assert case.core.get(unknown_id)["permitState"] == "uncertain"
        with pytest.raises(RuntimeError): command.downgrade(migration, "20261021_0016")
        queue.close()
    finally: close_app(app)


def test_same_message_id_with_other_body_is_never_deleted(queue_case):
    case, queue, fake = queue_case
    case.task(); queue.tick(); lease = case.core.claim_round("worker")[0]
    case.core.dispatch(lease, lambda *_: asr_result())
    key = next(iter(fake.entries)); changed = dict(fake.entries[key]); changed["eventId"] = uuid4().hex
    fake.entries[key] = changed
    assert queue.acknowledge_finished() == 1  # 当前任务已保存，但不会误删其他身份通知。
    assert fake.entries[key] == changed


def test_repeated_publication_failure_records_one_redacted_reason_event(queue_case):
    case, queue, fake = queue_case
    task, _ = case.task()
    fake.after_add = lambda _: (_ for _ in ()).throw(ConnectionError("PRIVATE_BODY_AND_CREDENTIAL"))
    for _ in range(2):
        due_now(case)
        with pytest.raises(AppError): queue.publish()
    events = case.events(task)
    assert sum(row["eventType"] == "queue.notification_failed" for row in events) == 1
    assert "PRIVATE_BODY_AND_CREDENTIAL" not in str(events)


def test_missing_stream_entry_clears_only_terminal_pending_receipt(queue_case):
    case, queue, fake = queue_case
    case.task(); queue.tick(); lease = case.core.claim_round("worker")[0]
    case.core.dispatch(lease, lambda *_: asr_result())
    fake.entries.clear()
    assert len(fake.pending) == 1
    assert queue.acknowledge_finished() == 1 and not fake.pending
