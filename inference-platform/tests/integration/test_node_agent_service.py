"""Agent 生命周期使用真实 SQLite 与进程替身，不作为真实模型退出验收。"""

from datetime import timedelta
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from filelock import FileLock
import pytest

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import PROJECT_ROOT
from backend.data.agent.agent_models import AgentControl, ManagedInstanceSession
from backend.data.agent.agent_repository import AgentRepository
from backend.data.database.database import database_now
from backend.schema.agent.agent_schema import ManagementCommand
from backend.schema.models.model_schema import RegistryListQuery
from backend.service.agent.agent_command_service import AgentCommandService
from backend.service.agent.agent_status_service import agent_shutdown_confirmed
from backend.service.agent.node_agent_service import NodeAgentService


class FakeWorker:
    def __init__(self, database, settings, logger, *, agent_session_id, worker_session_id):
        self.database = database
        self.session_id, self.agent_session_id = worker_session_id, agent_session_id
        self.state = "starting"
        self.model_ready = False
        self.last_error = None
        self.lease = self.pending = None
        self.draining = False
        self.runner = SimpleNamespace(pid=12345)
        self.start_calls = self.close_calls = self.tick_calls = 0
        self.close_ok = True
        self.complete_current = False
        self.fail_start = False

    @property
    def drained(self):
        return self.draining and self.lease is None and self.pending is None

    @property
    def can_exit(self):
        return self.state == "error"

    def start(self):
        # 启动会话必须在触发启动动作前真实提交，不能仅存在于调用者事务内。
        with self.database.session_factory() as session:
            row = AgentRepository.get_local_control(session)
            assert row.worker_session_id == self.session_id and row.agent_session_id == self.agent_session_id
            assert session.get(ManagedInstanceSession, self.session_id).state == "starting"
        self.start_calls += 1
        if self.fail_start:
            raise OSError("test-only start failure")

    def request_drain(self):
        self.draining = True

    def tick(self):
        self.tick_calls += 1
        if self.complete_current:
            self.lease = self.pending = None
        if self.state != "error":
            self.model_ready = True
            self.state = "running" if self.lease else "ready"

    def close(self):
        self.close_calls += 1
        self.model_ready = False
        if self.close_ok:
            self.runner.pid = None
            self.state = "stopped"
        else:
            self.state, self.last_error = "error", "WORKER_STOP_UNCONFIRMED"
        return self.close_ok


@pytest.fixture
def agent_case(app_factory, monkeypatch):
    app = app_factory()
    settings = app.config["APP_SETTINGS"]
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    database = app.extensions["database"]
    snapshot = dict(local_identity="a" * 64, hostname="test-only", os_name="Windows",
                    os_version="test-only", architecture="AMD64", cpu_count=4)
    registry = app.extensions["node_registry_service"]
    registry.probe = lambda: snapshot
    monkeypatch.setattr("backend.service.agent.agent_command_service.probe_local_node", lambda: snapshot)
    workers, agents = [], []

    def factory(*args, **kwargs):
        worker = FakeWorker(*args, **kwargs)
        workers.append(worker)
        return worker

    def new_agent(worker_factory=factory):
        agent = NodeAgentService(database, settings, app.extensions["app_logger"],
            worker_factory=worker_factory, registry=registry)
        agents.append(agent)
        return agent

    agent = new_agent()
    commands = AgentCommandService(database, settings)
    case = SimpleNamespace(app=app, settings=settings, database=database, registry=registry,
        workers=workers, agent=agent, commands=commands, new_agent=new_agent)
    yield case
    # 这里只释放测试进程的文件锁；失败/未知路径不伪造数据库退出凭据。
    for item in agents:
        if item._owns_lock:
            item._lock.release()
            item._owns_lock = False


def act(case, action):
    return case.commands.local_command(action)


def test_agent_starts_idle_without_worker_and_duplicate_start_keeps_one_launch(agent_case):
    case = agent_case
    case.agent.start()
    assert act(case, "status").state == "stopped"
    assert case.workers == []
    first = act(case, "start")
    repeated = act(case, "start")
    assert repeated.command_revision == first.command_revision
    case.agent.tick()
    assert len(case.workers) == 1 and case.workers[0].start_calls == 1
    status = act(case, "status")
    assert status.state == "ready" and status.model_ready and status.accepting_tasks
    act(case, "start")
    case.agent.tick()
    assert len(case.workers) == 1


def test_start_then_stop_before_tick_never_creates_launch(agent_case):
    case = agent_case
    case.agent.start()
    act(case, "start")
    act(case, "stop")
    case.agent.tick()
    assert case.workers == []
    assert act(case, "status").state == "stopped"
    assert case.commands.sessions(case.agent.instance_id, RegistryListQuery()).total == 0


def test_busy_drain_keeps_model_loaded_and_waits_before_exit_confirmation(agent_case):
    case = agent_case
    case.agent.start()
    act(case, "start")
    case.agent.tick()
    worker = case.workers[0]
    worker.lease = SimpleNamespace(task_id="d" * 32)
    case.agent.tick()
    busy = act(case, "status")
    assert busy.state == "busy" and busy.accepting_tasks
    stop = act(case, "stop")
    assert act(case, "stop").command_revision == stop.command_revision
    case.agent.tick()
    status = act(case, "status")
    assert status.state == "stopping" and status.model_ready and not status.accepting_tasks
    assert status.active_task_id == "d" * 32 and status.exit_confirmed_at is None
    assert worker.close_calls == 0
    case.agent.tick()
    assert worker.close_calls == 0
    worker.complete_current = True
    case.agent.tick()
    stopped = act(case, "status")
    assert worker.close_calls == 1 and case.agent.worker is None
    assert stopped.state == "stopped" and stopped.exit_confirmed_at is not None
    assert stopped.active_task_id is None and not stopped.model_ready
    first_session = stopped.worker_session_id
    act(case, "start")
    case.agent.tick()
    assert len(case.workers) == 2 and act(case, "status").worker_session_id != first_session
    assert case.commands.sessions(case.agent.instance_id, RegistryListQuery()).total == 2


def test_close_unconfirmed_preserves_owned_worker_and_blocks_restart(agent_case):
    case = agent_case
    case.agent.start()
    act(case, "start")
    case.agent.tick()
    worker = case.workers[0]
    worker.close_ok = False
    act(case, "stop")
    case.agent.tick()
    unknown = act(case, "status")
    assert unknown.state == "stop_unconfirmed" and unknown.exit_confirmed_at is None
    assert case.agent.worker is worker and case.agent._owns_lock
    with pytest.raises(AppError) as caught:
        act(case, "start")
    assert caught.value.code == "ASR_STOP_UNCONFIRMED"
    case.agent.tick()
    assert len(case.workers) == 1 and worker.close_calls == 2
    worker.close_ok = True
    case.agent.tick()
    assert act(case, "status").exit_confirmed_at is not None


@pytest.mark.parametrize("failure_stage", ["constructor", "start", "model_load"])
def test_startup_failures_record_failed_session_only_after_confirmed_close(agent_case, failure_stage):
    case = agent_case
    original_factory = case.agent.worker_factory

    def fail_factory(*args, **kwargs):
        if failure_stage == "constructor":
            raise OSError("test-only constructor failure")
        worker = original_factory(*args, **kwargs)
        worker.fail_start = failure_stage == "start"
        if failure_stage == "model_load":
            worker.state, worker.last_error = "error", "MODEL_MISSING"
        return worker

    case.agent.worker_factory = fail_factory
    case.agent.start()
    act(case, "start")
    case.agent.tick()
    status = act(case, "status")
    assert status.state == "failed" and status.exit_confirmed_at is not None
    assert not status.accepting_tasks and status.last_error
    assert case.agent.worker is None
    sessions = case.commands.sessions(case.agent.instance_id, RegistryListQuery())
    assert sessions.total == 1 and sessions.items[0].state == "failed"


def test_start_failure_with_unconfirmed_close_never_marks_failed_exit(agent_case):
    case = agent_case
    original_factory = case.agent.worker_factory

    def fail_factory(*args, **kwargs):
        worker = original_factory(*args, **kwargs)
        worker.fail_start, worker.close_ok = True, False
        return worker

    case.agent.worker_factory = fail_factory
    case.agent.start()
    act(case, "start")
    case.agent.tick()
    assert act(case, "status").state == "stop_unconfirmed"
    assert act(case, "status").exit_confirmed_at is None
    case.workers[0].close_ok = True
    case.agent.tick()
    assert act(case, "status").state == "failed"
    assert act(case, "status").exit_confirmed_at is not None


def test_old_revision_and_agent_identity_are_rejected_without_mutation(agent_case):
    case = agent_case
    case.agent.start()
    original = act(case, "status")
    act(case, "start")
    for identity, revision in [(original.agent_session_id, original.command_revision), ("e" * 32, 1)]:
        before = act(case, "status")
        with pytest.raises(AppError) as caught:
            case.commands.command(case.agent.instance_id, "stop", ManagementCommand(
                agent_session_id=identity, expected_revision=revision))
        assert caught.value.code == "AGENT_COMMAND_STALE"
        after = act(case, "status")
        assert after.desired_state == before.desired_state
        assert after.command_revision == before.command_revision


def test_start_rejected_while_current_worker_is_draining(agent_case):
    case = agent_case
    case.agent.start()
    act(case, "start")
    case.agent.tick()
    case.workers[0].lease = SimpleNamespace(task_id="d" * 32)
    act(case, "stop")
    with pytest.raises(AppError) as caught:
        act(case, "start")
    assert caught.value.code == "ASR_STOP_IN_PROGRESS"


def test_agent_restart_retains_unconfirmed_previous_session_and_refuses_spawn(agent_case):
    case = agent_case
    case.agent.start()
    act(case, "start")
    case.agent.tick()
    old = act(case, "status")
    # 模拟 Agent 丢失；不为替身进程写退出凭据。
    case.agent._lock.release()
    case.agent._owns_lock = False
    restarted = case.new_agent()
    restarted.start()
    status = act(case, "status")
    assert status.agent_session_id != old.agent_session_id
    assert status.worker_session_id == old.worker_session_id
    assert status.state == "stop_unconfirmed" and status.exit_confirmed_at is None
    restarted.tick()
    assert len(case.workers) == 1
    with pytest.raises(AppError, match="尚未确认"):
        act(case, "start")
    restarted.request_shutdown()
    restarted.tick()
    assert not restarted.shutdown_confirmed and restarted._owns_lock


def test_clean_agent_restart_keeps_history_and_requires_explicit_start(agent_case):
    case = agent_case
    case.agent.start()
    act(case, "start")
    case.agent.tick()
    case.agent.request_shutdown()
    case.agent.tick()
    assert case.agent.shutdown_confirmed
    old = act(case, "status")
    restarted = case.new_agent()
    restarted.start()
    restarted.tick()
    status = act(case, "status")
    assert status.agent_session_id != old.agent_session_id
    assert status.state == "stopped" and not status.model_ready
    assert len(case.workers) == 1
    assert case.commands.sessions(case.agent.instance_id, RegistryListQuery()).total == 1


def test_shutdown_signal_keeps_agent_lock_until_active_task_and_child_finish(agent_case):
    case = agent_case
    case.agent.start()
    act(case, "start")
    case.agent.tick()
    worker = case.workers[0]
    worker.lease = SimpleNamespace(task_id="d" * 32)
    case.agent.request_shutdown()
    case.agent.tick()
    assert not case.agent.shutdown_confirmed and case.agent._owns_lock
    assert act(case, "status").desired_state == "stopped"
    assert worker.close_calls == 0
    worker.complete_current = True
    worker.close_ok = False
    case.agent.tick()
    assert not case.agent.shutdown_confirmed and case.agent._owns_lock
    worker.close_ok = True
    case.agent.tick()
    assert case.agent.shutdown_confirmed and not case.agent._owns_lock
    status = act(case, "status")
    assert status.shutdown_confirmed_at is not None and not status.agent_available
    assert agent_shutdown_confirmed(case.settings, status.agent_pid)


def test_worker_autonomous_drain_closes_admission_and_finishes_agent_remains(agent_case):
    case = agent_case
    case.agent.start()
    act(case, "start")
    case.agent.tick()
    case.workers[0].request_drain()
    case.agent.tick()
    status = act(case, "status")
    assert status.desired_state == "stopped" and status.state == "stopped"
    assert status.agent_available and not status.accepting_tasks
    assert not case.agent.shutdown_confirmed and case.agent._owns_lock


def test_stale_heartbeat_is_unknown_and_does_not_infer_process_exit(agent_case):
    case = agent_case
    case.agent.start()
    act(case, "start")
    case.agent.tick()
    with case.database.transaction(immediate=True) as session:
        row = session.get(AgentControl, case.agent.instance_id)
        row.heartbeat_at = database_now(session) - timedelta(seconds=case.settings.runtime_stale_seconds + 1)
    status = act(case, "status")
    assert status.state == "unknown" and status.exit_confirmed_at is None
    assert not status.agent_available and not status.accepting_tasks
    with pytest.raises(AppError) as caught:
        act(case, "stop")
    assert caught.value.code == "ASR_AGENT_UNAVAILABLE"


def test_old_worker_report_and_finish_cannot_touch_new_start_session(agent_case):
    case = agent_case
    case.agent.start()
    act(case, "start")
    case.agent.tick()
    worker = case.agent.worker
    original_session = worker.session_id
    worker.session_id = "e" * 32
    try:
        with pytest.raises(AppError) as caught:
            case.agent._report_worker()
        assert caught.value.code == "AGENT_WORKER_SESSION_CHANGED"
        with pytest.raises(AppError) as caught:
            case.agent._finish(worker.session_id)
        assert caught.value.code == "AGENT_WORKER_SESSION_CHANGED"
    finally:
        worker.session_id = original_session
    assert act(case, "status").worker_session_id == original_session
    assert act(case, "status").exit_confirmed_at is None


def test_agent_unique_lock_and_existing_worker_lock_are_both_enforced(agent_case):
    case = agent_case
    case.agent.start()
    duplicate = case.new_agent()
    with pytest.raises(AppError) as caught:
        duplicate.start()
    assert caught.value.code == "AGENT_ALREADY_RUNNING"
    assert act(case, "status").agent_session_id == case.agent.session_id
    case.agent.request_shutdown()
    case.agent.tick()
    with FileLock(str(case.settings.database_path) + ".worker.lock", timeout=0):
        with pytest.raises(Exception):
            duplicate.start()
    assert not duplicate._owns_lock


def test_intent_database_failure_still_advances_active_worker_and_closes_when_drained(agent_case, monkeypatch):
    case = agent_case
    case.agent.start()
    act(case, "start")
    case.agent.tick()
    worker = case.workers[0]
    worker.lease = SimpleNamespace(task_id="d" * 32)
    original_intent = case.agent._intent

    def broken_intent():
        raise OSError("test-only control storage failure")

    monkeypatch.setattr(case.agent, "_intent", broken_intent)
    initial_ticks = worker.tick_calls
    with pytest.raises(OSError):
        case.agent.tick()
    assert worker.tick_calls == initial_ticks + 1 and worker.draining
    assert worker.close_calls == 0
    worker.complete_current = True
    with pytest.raises(OSError):
        case.agent.tick()
    assert worker.tick_calls == initial_ticks + 2
    assert worker.close_calls == 1 and case.agent.worker is None
    monkeypatch.setattr(case.agent, "_intent", original_intent)
    assert act(case, "status").exit_confirmed_at is not None


def test_report_failure_does_not_block_drain_close(agent_case, monkeypatch):
    case = agent_case
    case.agent.start()
    act(case, "start")
    case.agent.tick()
    worker = case.workers[0]
    act(case, "stop")

    def broken_report(**kwargs):
        raise OSError("test-only report failure")

    monkeypatch.setattr(case.agent, "_report_worker", broken_report)
    with pytest.raises(OSError):
        case.agent.tick()
    assert worker.close_calls == 1 and case.agent.worker is None
    assert act(case, "status").exit_confirmed_at is not None


def test_total_database_outage_keeps_pending_exit_without_ticking_closed_worker(agent_case, monkeypatch):
    case = agent_case
    case.agent.start()
    act(case, "start")
    case.agent.tick()
    worker = case.workers[0]
    original_transaction = case.database.transaction

    def unavailable(**kwargs):
        raise OSError("test-only database unavailable")

    case.agent.request_shutdown()
    monkeypatch.setattr(case.database, "transaction", unavailable)
    with pytest.raises(OSError):
        case.agent.tick()
    assert worker.close_calls == 1 and worker.runner.pid is None
    assert case.agent._pending_closed == (worker.session_id, False)
    assert case.agent.worker is worker and not case.agent.shutdown_confirmed and case.agent._owns_lock
    ticks = worker.tick_calls
    with pytest.raises(OSError):
        case.agent.tick()
    assert worker.tick_calls == ticks and worker.close_calls == 1
    monkeypatch.setattr(case.database, "transaction", original_transaction)
    case.agent.tick()
    assert case.agent.worker is None and case.agent._pending_closed is None
    assert case.agent.shutdown_confirmed and not case.agent._owns_lock
    assert act(case, "status").exit_confirmed_at is not None


def test_unknown_exit_commit_replay_keeps_first_confirmation_time(agent_case, monkeypatch):
    case = agent_case
    case.agent.start()
    act(case, "start")
    case.agent.tick()
    worker = case.workers[0]
    act(case, "stop")
    finish = case.agent._finish
    attempts = 0

    def unknown_commit(worker_id, *, failed=False):
        nonlocal attempts
        finish(worker_id, failed=failed)
        attempts += 1
        if attempts == 1:
            raise OSError("test-only response lost after commit")

    monkeypatch.setattr(case.agent, "_finish", unknown_commit)
    with pytest.raises(OSError):
        case.agent.tick()
    first_exit = act(case, "status").exit_confirmed_at
    assert first_exit is not None and case.agent._pending_closed is not None
    case.agent.tick()
    assert case.agent._pending_closed is None and attempts == 2
    assert act(case, "status").exit_confirmed_at == first_exit
    assert worker.close_calls == 1


def test_exit_commit_replay_cannot_erase_a_new_explicit_start(agent_case, monkeypatch):
    case = agent_case
    case.agent.start()
    act(case, "start")
    case.agent.tick()
    old_session = case.workers[0].session_id
    act(case, "stop")
    finish = case.agent._finish
    attempts = 0

    def unknown_commit(worker_id, *, failed=False):
        nonlocal attempts
        finish(worker_id, failed=failed)
        attempts += 1
        if attempts == 1:
            raise OSError("test-only response lost after commit")

    monkeypatch.setattr(case.agent, "_finish", unknown_commit)
    with pytest.raises(OSError):
        case.agent.tick()
    accepted = act(case, "start")
    assert accepted.desired_state == "running"
    case.agent.tick()
    status = act(case, "status")
    assert status.desired_state == "running" and status.command_revision == accepted.command_revision
    assert status.worker_session_id != old_session and status.state == "ready"
    assert len(case.workers) == 2
