"""本机脚本只操作所属实例，并按真实退出确认处理启动失败。"""

import logging
from datetime import datetime, timezone
import socket
import sys
import time
from types import SimpleNamespace

from filelock import FileLock
import pytest

from backend.common.process.platform_control import port_available
from backend.config.settings.app_settings import AppSettings
from backend.data.platform.platform_repository import ControlFiles
from backend.schema.platform.platform_schema import PlatformOptions, PlatformState
from backend.service.platform.platform_client import PlatformClient
from backend.service.platform.platform_service import PlatformService


@pytest.fixture
def platform(tmp_path):
    settings = AppSettings(database_path=tmp_path / "db.sqlite3", log_dir=tmp_path / "logs")
    options = PlatformOptions(node_path=sys.executable)
    control = ControlFiles(tmp_path / "control")
    return PlatformService(settings, options, "a" * 32, control, logging.getLogger("test-platform"))


def test_stale_stop_request_cannot_stop_new_instance(platform):
    platform.control.write(platform.control.stop_path, {"instance_id": "b" * 32})
    assert not platform.stop_requested()
    platform.control.write(platform.control.stop_path, {"instance_id": "a" * 32})
    assert platform.stop_requested()


def test_port_conflict_rejected_without_starting_or_stopping_existing_process(platform):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        platform.settings = platform.settings.model_copy(update={"port": listener.getsockname()[1]})
        with pytest.raises(RuntimeError, match="已占用"):
            platform.preflight()
        assert not platform.processes
        assert not port_available(listener.getsockname()[1])


@pytest.mark.parametrize("name,suffix", [("worker", ".worker.lock"), ("vision_worker", ".vision-worker.lock"), ("agent", ".agent.lock")])
def test_manual_worker_lock_is_not_taken_over(platform, monkeypatch, name, suffix):
    # 此例验证 Worker 锁，不让用户正在运行的 API/前端端口抢先触发另一条拒绝规则。
    monkeypatch.setattr("backend.service.platform.platform_service.port_available", lambda _port: True)
    with FileLock(str(platform.settings.database_path) + suffix):
        with pytest.raises(RuntimeError, match=f"已有 {name}"):
            platform.preflight()
    assert not platform.processes


def test_failed_preflight_does_not_spawn_services(platform, monkeypatch):
    # 端口冲突由独立测试覆盖；这里固定前置条件，只检查环境失败后的启动处理。
    monkeypatch.setattr("backend.service.platform.platform_service.port_available", lambda _port: True)
    monkeypatch.setattr("backend.service.platform.platform_service.check_environment", lambda *_a, **_k: SimpleNamespace(
        passed=False, checks=[SimpleNamespace(passed=False, message="请先迁移数据库")],
    ))
    assert platform.run() == 2
    state = PlatformState.model_validate(platform.control.read(platform.control.state_path))
    assert state.status == "failed"
    assert "请先迁移数据库" in state.message
    assert state.processes == {}
    assert not platform.control.manager_locked()


def test_partial_start_failure_rolls_back_only_owned_services(platform, monkeypatch):
    stopped = []

    def begin():
        platform.publish()
        for name in ("api", "dispatcher"):
            platform.processes[name] = SimpleNamespace(
                pid=100 if name == "api" else 101, poll=lambda: 0,
                stop=lambda _timeout, name=name: stopped.append(name) or True,
                close_log=lambda: None, process=SimpleNamespace(wait=lambda **_k: 0),
            )
        raise RuntimeError("故障注入：Worker 启动失败")

    monkeypatch.setattr(platform, "start", begin)
    assert platform.run() == 2
    assert stopped == ["dispatcher", "api"]
    assert platform.state.status == "failed"
    assert not platform.control.manager_locked()


def test_unconfirmed_job_exit_retains_job_and_stopping_state(platform):
    terminated = []
    platform.job = SimpleNamespace(wait_empty=lambda _timeout: False, terminate=lambda: terminated.append(True))
    assert not platform.stop()
    assert terminated == [True]
    assert platform.job is not None
    assert platform.state.status == "stopping"
    assert "尚未确认" in platform.state.message


def test_status_disk_error_cannot_prevent_owned_process_cleanup(platform, monkeypatch):
    stopped = []
    platform.processes["vision_worker"] = SimpleNamespace(
        pid=100, poll=lambda: 0, stop=lambda _timeout: stopped.append(True) or True,
        close_log=lambda: None, process=SimpleNamespace(wait=lambda **_k: 0),
    )
    def disk_error(*_a):
        raise OSError("故障注入：磁盘写入失败")
    monkeypatch.setattr(platform.control, "write", disk_error)
    assert platform.stop()
    assert stopped == [True]


def test_repeated_start_uses_existing_instance_without_spawning(platform, monkeypatch):
    platform.publish(status="running")
    client = PlatformClient(platform.control)
    monkeypatch.setattr(client, "launch_manager", lambda *_a: pytest.fail("must not spawn twice"))
    monkeypatch.setattr("backend.service.platform.platform_client.local_http", lambda *_a, **_k: True)
    with FileLock(platform.control.lock_path):
        code, result = client.start(platform.settings, platform.options)
    assert code == 0
    assert "未重复启动" in result["message"]


def test_duplicate_start_does_not_claim_readiness_if_probe_fails(platform, monkeypatch):
    platform.publish(status="running")
    client = PlatformClient(platform.control)
    monkeypatch.setattr(client, "launch_manager", lambda *_a: pytest.fail("must not spawn twice"))
    monkeypatch.setattr("backend.service.platform.platform_client.local_http", lambda *_a, **_k: None)
    with FileLock(platform.control.lock_path):
        code, result = client.start(platform.settings, platform.options)
    assert code == 2
    assert "未创建第二个实例" in result["message"]


def test_stop_can_signal_while_start_client_is_waiting(platform, monkeypatch):
    platform.publish(status="starting")
    client = PlatformClient(platform.control)
    observed = []
    monkeypatch.setattr(client, "wait_stopped", lambda instance, _timeout: observed.append(instance) or True)
    monkeypatch.setattr(client, "status", lambda: {})
    with FileLock(platform.control.client_lock_path), FileLock(platform.control.lock_path):
        code, _result = client.stop(1)
    assert code == 0
    assert observed == [platform.state.instance_id]
    assert platform.stop_requested()


def test_start_interrupted_before_first_state_still_sends_instance_stop(platform, monkeypatch):
    client = PlatformClient(platform.control)
    started = []
    def launch(_settings, _options, instance):
        started.append(instance)
        return SimpleNamespace(poll=lambda: None)
    def interrupted_read():
        raise KeyboardInterrupt
    monkeypatch.setattr(client, "launch_manager", launch)
    monkeypatch.setattr(client, "read_state", interrupted_read)
    monkeypatch.setattr(client, "wait_stopped", lambda *_a: False)
    code, result = client.start(platform.settings, platform.options)
    assert code == 2
    assert platform.control.read(platform.control.stop_path) == {"instance_id": started[0]}
    assert "已请求停止" in result["message"]


def test_status_never_treats_saved_pid_as_live_manager(platform, monkeypatch):
    platform.publish(status="running")
    client = PlatformClient(platform.control)
    monkeypatch.setattr("backend.service.platform.platform_client.local_http", lambda *_a, **_k: None)
    code, result = client.stop(1)
    assert code == 2
    assert result["managerRunning"] is False
    assert "不是正常停止" in result["message"]
    assert not platform.control.stop_path.exists()


def test_readiness_requires_owned_agent_vision_and_dispatcher(platform, monkeypatch):
    platform.processes = {"agent": SimpleNamespace(pid=11), "dispatcher": SimpleNamespace(pid=22), "vision_worker": SimpleNamespace(pid=33)}
    platform._agent_started_at = datetime(2026, 10, 14, 1, 0, 0)
    data = {"worker": {"pid": None, "availability": "not_started", "modelReady": False},
            "dispatcher": {"pid": 22, "availability": "online"},
            "visionWorker": {"pid": 33, "availability": "online", "modelReady": True}}
    monkeypatch.setattr("backend.service.platform.platform_service.local_http", lambda *_a, **_k: {"data": data})
    monkeypatch.setattr("backend.service.platform.platform_service.agent_is_ready", lambda _settings, _pid, **_kwargs: False)
    assert not platform.services_ready()
    monkeypatch.setattr("backend.service.platform.platform_service.agent_is_ready", lambda _settings, pid, **kwargs: pid == 11 and kwargs["started_after"] == datetime(2026, 10, 14, 1, 0, 0))
    assert platform.services_ready()
    data["visionWorker"]["modelReady"] = False
    assert not platform.services_ready()
    data["visionWorker"]["modelReady"] = True
    data["visionWorker"]["pid"] = 999
    assert not platform.services_ready()
    data["visionWorker"]["pid"] = 33
    data["visionWorker"]["availability"] = "stale"
    assert not platform.services_ready()


def test_platform_starts_agent_without_asr_and_stops_agent_before_other_services(platform, monkeypatch):
    started, stopped = [], []
    exited = set()
    monkeypatch.setattr(platform, "preflight", lambda: None)
    monkeypatch.setattr(platform, "services_ready", lambda: True)
    monkeypatch.setattr("backend.service.platform.platform_service.ProcessTreeJob", lambda: SimpleNamespace(wait_empty=lambda _: True, close=lambda: None))
    monkeypatch.setattr("backend.service.platform.platform_service.agent_shutdown_confirmed", lambda _settings, _pid, **_kwargs: True)

    def spawn(name):
        started.append(name)
        def stop(timeout):
            stopped.append((name, timeout))
            exited.add(name)
            return True
        platform.processes[name] = SimpleNamespace(pid=100 + len(started), poll=lambda: 0 if name in exited else None, stop=stop, close_log=lambda: None, process=SimpleNamespace(wait=lambda **_: 0))

    monkeypatch.setattr(platform, "spawn", spawn)
    assert platform.start()
    assert started == ["api", "dispatcher", "agent", "vision_worker", "frontend"]
    assert platform.stop()
    assert [name for name, _ in stopped] == ["agent", "vision_worker", "dispatcher", "api", "frontend"]
    assert stopped[0][1] == 0
    assert stopped[1][1] == platform.settings.worker_stop_timeout_seconds + 3


def test_status_records_completed_processes_before_releasing_manager(platform):
    platform.publish(status="stopped", message="均已停止")
    result = PlatformClient(platform.control).status()
    assert result["managerRunning"] is False
    assert result["state"]["status"] == "stopped"


def test_agent_spawn_records_launch_boundary_before_process_creation(platform, monkeypatch):
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    observed = []
    def process(name, command, **_):
        observed.append((name, command, platform._agent_started_at))
        return SimpleNamespace(pid=123, poll=lambda: None)
    monkeypatch.setattr("backend.service.platform.platform_service.OwnedProcess", process)
    platform.spawn("agent")
    assert observed[0][0] == "agent"
    assert observed[0][1][-1] == "agent"
    assert before <= observed[0][2] <= datetime.now(timezone.utc).replace(tzinfo=None)


def test_agent_drain_does_not_stop_other_services_or_terminate_job(platform, monkeypatch):
    signals = []
    platform.processes = {
        "agent": SimpleNamespace(pid=100, poll=lambda: None, stop=lambda timeout: signals.append(timeout) or False),
        "api": SimpleNamespace(pid=101, poll=lambda: None, stop=lambda _: pytest.fail("API must remain available")),
        "vision_worker": SimpleNamespace(pid=102, poll=lambda: None, stop=lambda _: pytest.fail("Vision must remain available")),
    }
    platform.job = SimpleNamespace(
        wait_empty=lambda _: pytest.fail("must not begin Job cleanup while draining"),
        terminate=lambda: pytest.fail("must not terminate draining ASR"),
    )
    monkeypatch.setattr("backend.service.platform.platform_service.agent_shutdown_confirmed", lambda *_args, **_kwargs: pytest.fail("live Agent cannot prove process exit"))
    with FileLock(platform.control.lock_path):
        assert not platform.stop()
        assert platform.control.manager_locked()
        assert not platform.stop()
        assert platform.control.manager_locked()
    assert signals == [0]
    assert platform.state.status == "stopping"
    assert platform.job is not None


@pytest.mark.parametrize("exit_code", [0, 2])
def test_agent_exit_without_shutdown_evidence_keeps_other_services(platform, monkeypatch, exit_code):
    platform.processes = {
        "agent": SimpleNamespace(pid=100, poll=lambda: exit_code, stop=lambda _: True),
        "api": SimpleNamespace(pid=101, poll=lambda: None, stop=lambda _: pytest.fail("unconfirmed ASR must retain API")),
    }
    platform.job = SimpleNamespace(terminate=lambda: pytest.fail("exit code is not ASR exit evidence"))
    monkeypatch.setattr("backend.service.platform.platform_service.agent_shutdown_confirmed", lambda _settings, pid, **_kwargs: pid != 100)
    assert not platform.stop()
    assert "尚未确认" in platform.state.message
    assert platform.job is not None


def test_failed_agent_start_with_explicit_exit_evidence_allows_cleanup(platform, monkeypatch):
    stopped = []
    platform.processes = {
        "agent": SimpleNamespace(pid=100, poll=lambda: 2, stop=lambda _: True,
                                 process=SimpleNamespace(wait=lambda **_: 2), close_log=lambda: None),
        "api": SimpleNamespace(pid=101, poll=lambda: 0, stop=lambda _: stopped.append("api") or True,
                               process=SimpleNamespace(wait=lambda **_: 0), close_log=lambda: None),
    }
    monkeypatch.setattr("backend.service.platform.platform_service.agent_shutdown_confirmed", lambda _settings, pid, **_kwargs: pid == 100)
    assert platform.stop()
    assert stopped == ["api"]


def test_agent_state_read_failure_retains_live_services_and_owned_job(platform, monkeypatch):
    platform.processes = {"agent": SimpleNamespace(pid=100, poll=lambda: 0, stop=lambda _: True)}
    platform.job = SimpleNamespace(terminate=lambda: pytest.fail("database errors cannot authorize kill"))
    def unreadable(*_args, **_kwargs):
        raise OSError("agent state unavailable")
    monkeypatch.setattr("backend.service.platform.platform_service.agent_shutdown_confirmed", unreadable)
    with pytest.raises(OSError, match="unavailable"):
        platform.stop()
    assert platform.state.status == "stopping"
    assert platform.job is not None


def test_readiness_checks_frontend_proxy_points_to_current_runtime(platform, monkeypatch):
    platform.processes = {"agent": SimpleNamespace(pid=11), "dispatcher": SimpleNamespace(pid=22), "vision_worker": SimpleNamespace(pid=33)}
    data = {"dispatcher": {"pid": 22, "availability": "online"},
            "visionWorker": {"pid": 33, "availability": "online", "modelReady": True}}
    monkeypatch.setattr("backend.service.platform.platform_service.agent_is_ready", lambda *_args, **_kwargs: True)
    def response(port, path, **_):
        if port == platform.options.frontend_port and path == "/api/runtime":
            return {"data": {"visionWorker": {"pid": 999}, "dispatcher": {"pid": 22}}}
        return {"data": data}
    monkeypatch.setattr("backend.service.platform.platform_service.local_http", response)
    assert not platform.services_ready()


def test_asr_reported_stopped_does_not_fail_platform_tick(platform):
    platform.processes = {name: SimpleNamespace(pid=100 + index, poll=lambda: None)
                          for index, name in enumerate(("api", "dispatcher", "agent", "vision_worker", "frontend"))}
    platform.tick()
    assert "worker" not in platform.state.processes
    assert platform.state.processes["agent"] == 102


def test_stop_client_timeout_leaves_request_for_background_drain(platform, monkeypatch):
    platform.publish(status="running")
    client = PlatformClient(platform.control)
    monkeypatch.setattr(client, "wait_stopped", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(client, "status", lambda: {"managerRunning": True})
    with FileLock(platform.control.lock_path):
        code, result = client.stop(1)
        assert platform.control.manager_locked()
    assert code == 2
    assert platform.stop_requested()
    assert "继续等待" in result["message"]


def test_manager_retains_lock_and_services_until_agent_finishes_drain(platform, monkeypatch):
    finished, signals, other_stops = [], [], []
    def begin():
        platform.processes = {
            "agent": SimpleNamespace(pid=100, poll=lambda: 0 if finished else None,
                                     stop=lambda timeout: signals.append(timeout) or False,
                                     process=SimpleNamespace(wait=lambda **_: 0), close_log=lambda: None),
            "api": SimpleNamespace(pid=101, poll=lambda: 0 if other_stops else None,
                                   stop=lambda _: other_stops.append(True) or True,
                                   process=SimpleNamespace(wait=lambda **_: 0), close_log=lambda: None),
        }
        return False
    def finish_during_wait(_):
        assert platform.control.manager_locked()
        assert platform.state.status == "stopping"
        assert not other_stops
        finished.append(True)
    monkeypatch.setattr(platform, "start", begin)
    monkeypatch.setattr("backend.service.platform.platform_service.time.sleep", finish_during_wait)
    monkeypatch.setattr("backend.service.platform.platform_service.agent_shutdown_confirmed", lambda *_args, **_kwargs: bool(finished))
    assert platform.run() == 0
    assert signals == [0]
    assert other_stops == [True]
    assert platform.state.status == "stopped"
    assert not platform.control.manager_locked()


@pytest.mark.parametrize("saved_status", ["starting", "running", "stopping"])
def test_stop_without_manager_does_not_report_old_live_state_as_stopped(platform, monkeypatch, saved_status):
    platform.publish(status=saved_status)
    monkeypatch.setattr("backend.service.platform.platform_client.local_http", lambda *_a, **_k: None)
    code, result = PlatformClient(platform.control).stop(1)
    assert code == 2
    assert result["managerRunning"] is False
    assert "尚未确认 ASR 退出" in result["message"]
    assert not platform.control.stop_path.exists()


def test_configured_remote_worker_is_optional_sixth_entry(platform, monkeypatch):
    platform.settings = platform.settings.model_copy(update={"remote_asr_base_url": "https://isolated.invalid:8003",
        "remote_asr_model_id": "test-sensevoice", "remote_asr_model_version": "fixture-v1"})
    started = []
    monkeypatch.setattr(platform, "preflight", lambda: None)
    monkeypatch.setattr(platform, "services_ready", lambda: True)
    monkeypatch.setattr("backend.service.platform.platform_service.ProcessTreeJob", lambda: object())
    monkeypatch.setattr(platform, "spawn", lambda name: started.append(name))
    assert platform.start()
    assert started == ["api", "dispatcher", "agent", "vision_worker", "remote_asr_worker", "frontend"]


def test_remote_upstream_unavailable_does_not_fail_local_platform_readiness(platform, monkeypatch):
    platform.settings = platform.settings.model_copy(update={"remote_asr_base_url": "https://isolated.invalid:8003",
        "remote_asr_model_id": "test-sensevoice", "remote_asr_model_version": "fixture-v1"})
    platform.processes = {"agent": SimpleNamespace(pid=11), "dispatcher": SimpleNamespace(pid=22),
                          "vision_worker": SimpleNamespace(pid=33), "remote_asr_worker": SimpleNamespace(pid=44)}
    data = {"dispatcher": {"pid": 22, "availability": "online"},
            "visionWorker": {"pid": 33, "availability": "online", "modelReady": True},
            "remoteAsrWorker": {"pid": 44, "availability": "online", "reportedState": "unavailable", "modelReady": False}}
    monkeypatch.setattr("backend.service.platform.platform_service.agent_is_ready", lambda *_a, **_k: True)
    monkeypatch.setattr("backend.service.platform.platform_service.local_http", lambda *_a, **_k: {"data": data})
    assert platform.services_ready()
    data["remoteAsrWorker"]["availability"] = "stale"
    assert not platform.services_ready()


def test_remote_drain_is_persisted_and_signaled_even_while_local_asr_is_draining(platform, monkeypatch):
    actions = []
    platform.processes = {
        "agent": SimpleNamespace(pid=10, poll=lambda: None, stop=lambda _: actions.append("agent-signal")),
        "remote_asr_worker": SimpleNamespace(pid=20, poll=lambda: None, stop=lambda _: actions.append("remote-signal")),
        "api": SimpleNamespace(pid=30, poll=lambda: None, stop=lambda _: pytest.fail("draining must retain API")),
    }
    monkeypatch.setattr(platform, "_begin_remote_drain", lambda: actions.append("persist-drain"))
    assert not platform.stop()
    assert not platform.stop()
    assert actions == ["persist-drain", "remote-signal", "agent-signal"]


def test_remote_exited_process_requires_saved_call_state_before_platform_cleanup(platform, monkeypatch):
    platform.processes = {
        "remote_asr_worker": SimpleNamespace(pid=20, poll=lambda: 0, stop=lambda _: True),
        "api": SimpleNamespace(pid=30, poll=lambda: None, stop=lambda _: pytest.fail("must retain API")),
    }
    monkeypatch.setattr(platform, "_begin_remote_drain", lambda: None)
    monkeypatch.setattr(platform, "_remote_shutdown_confirmed", lambda _pid: False)
    assert not platform.stop()
    assert "调用记录尚未确认保存" in platform.state.message


def test_remote_drain_storage_failure_does_not_signal_or_cleanup(platform, monkeypatch):
    platform.processes = {"remote_asr_worker": SimpleNamespace(pid=20, poll=lambda: None,
        stop=lambda _: pytest.fail("must persist drain first"))}
    def fail():
        raise OSError("database unavailable")
    monkeypatch.setattr(platform, "_begin_remote_drain", fail)
    with pytest.raises(OSError):
        platform.stop()
    assert not platform._remote_stop_requested


def test_remote_unknown_fact_remains_visible_after_local_platform_exit(platform, monkeypatch):
    def stopped():
        platform._remote_uncertain = True
        return True
    monkeypatch.setattr(platform, "start", lambda: False)
    monkeypatch.setattr(platform, "stop", stopped)
    assert platform.run() == 0
    assert platform.state.status == "stopped"
    assert "远端请求停止未确认" in platform.state.message


def test_manual_remote_worker_lock_is_not_taken_over_even_if_config_removed(platform, monkeypatch):
    monkeypatch.setattr("backend.service.platform.platform_service.port_available", lambda _port: True)
    with FileLock(str(platform.settings.database_path) + ".remote-asr-worker.lock"):
        with pytest.raises(RuntimeError, match="已有 remote_asr_worker"):
            platform.preflight()
