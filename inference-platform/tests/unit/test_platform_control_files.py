"""管理状态文件遇Windows短暂共享冲突时保持原子内容和有界失败。"""

import errno
import json
import os
import threading
from unittest.mock import Mock

import pytest

from backend.data.platform import platform_repository
from backend.data.platform.platform_repository import ControlFiles, WINDOWS_REPLACE_RETRY_DELAYS
from backend.utils.file.file_utils import atomic_write_text


def permission_error(code):
    error = PermissionError(errno.EACCES, "isolated Windows permission fixture")
    if code is not None:
        error.winerror = code
    return error


@pytest.mark.parametrize("code", [5, 32, 33])
def test_known_windows_conflict_retries_same_complete_json_then_succeeds(tmp_path, monkeypatch, code):
    control = ControlFiles(tmp_path)
    control.write(control.state_path, {"instance_id": "old", "status": "running"})
    real_write = platform_repository.atomic_write_text
    observed = []

    def transient(path, content):
        observed.append((path, content))
        if len(observed) == 1:
            assert json.loads(path.read_text()) == {"instance_id": "old", "status": "running"}
            raise permission_error(code)
        return real_write(path, content)

    wait = Mock()
    monkeypatch.setattr(platform_repository, "atomic_write_text", transient)
    monkeypatch.setattr(platform_repository, "sleep", wait)
    target = {"instance_id": "new", "status": "starting", "message": "完整中文状态"}
    control.write(control.state_path, target)
    assert observed[0] == observed[1]
    assert control.read(control.state_path) == target
    wait.assert_called_once_with(WINDOWS_REPLACE_RETRY_DELAYS[0])
    assert not list(tmp_path.glob("*.tmp"))


def test_persistent_windows_permission_denial_is_bounded_and_preserves_old_state(tmp_path, monkeypatch):
    control = ControlFiles(tmp_path)
    control.write(control.state_path, {"status": "running"})
    error = permission_error(5)
    write, wait = Mock(side_effect=error), Mock()
    monkeypatch.setattr(platform_repository, "atomic_write_text", write)
    monkeypatch.setattr(platform_repository, "sleep", wait)
    with pytest.raises(PermissionError) as raised:
        control.write(control.state_path, {"status": "stopped"})
    assert raised.value is error
    assert write.call_count == len(WINDOWS_REPLACE_RETRY_DELAYS) + 1
    assert [call.args[0] for call in wait.call_args_list] == list(WINDOWS_REPLACE_RETRY_DELAYS)
    assert sum(WINDOWS_REPLACE_RETRY_DELAYS) < 1
    assert control.read(control.state_path) == {"status": "running"}


@pytest.mark.parametrize("error", [permission_error(None), permission_error(123), OSError(errno.ENOSPC, "disk full")])
def test_other_io_errors_are_not_silently_retried(tmp_path, monkeypatch, error):
    control = ControlFiles(tmp_path)
    write, wait = Mock(side_effect=error), Mock()
    monkeypatch.setattr(platform_repository, "atomic_write_text", write)
    monkeypatch.setattr(platform_repository, "sleep", wait)
    with pytest.raises(type(error)) as raised:
        control.write(control.stop_path, {"instance_id": "one"})
    assert raised.value is error
    write.assert_called_once()
    wait.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="复现Windows不共享删除的真实打开句柄")
def test_actual_windows_open_reader_rejects_replace_then_control_write_recovers(tmp_path, monkeypatch):
    control = ControlFiles(tmp_path)
    control.write(control.state_path, {"status": "starting", "generation": 1})
    reader = control.state_path.open("rb")
    failure = None
    try:
        with pytest.raises(PermissionError) as raised:
            atomic_write_text(control.state_path, json.dumps({"generation": 999}))
        failure = raised.value
        assert failure.winerror in {5, 32, 33}
        assert control.read(control.state_path) == {"status": "starting", "generation": 1}
        assert not list(tmp_path.glob("*.tmp"))
        # 实际读句柄在第三次短暂等待中关闭，不更改权限或删目标文件。
        real_sleep = platform_repository.sleep
        waits = []

        def release_after_second_wait(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                reader.close()
            real_sleep(seconds)

        monkeypatch.setattr(platform_repository, "sleep", release_after_second_wait)
        control.write(control.state_path, {"status": "running", "generation": 2})
        assert len(waits) == 2
        assert control.read(control.state_path) == {"status": "running", "generation": 2}
        assert not list(tmp_path.glob("*.tmp"))
        (tmp_path / "windows-sharing-evidence.json").write_text(json.dumps({
            "actualWinError": failure.winerror, "oldContentPreservedOnFailure": True,
            "boundedRetryWaits": waits, "recoveredWithoutPermissionChange": True}, indent=2), encoding="utf-8")
    finally:
        reader.close()


@pytest.mark.skipif(os.name != "nt", reason="真实Windows并发读写")
def test_concurrent_control_reads_observe_whole_json_during_replacements(tmp_path):
    control = ControlFiles(tmp_path)
    control.write(control.state_path, {"sequence": 0, "copy": 0})
    finish, errors = threading.Event(), []

    def reader():
        while not finish.is_set():
            value = control.read(control.state_path)
            if value is not None and (set(value) != {"sequence", "copy"} or value["sequence"] != value["copy"]):
                errors.append(value)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(1, 41):
            control.write(control.state_path, {"sequence": index, "copy": index})
    finally:
        finish.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    assert control.read(control.state_path) == {"sequence": 40, "copy": 40}
    assert not list(tmp_path.glob("*.tmp"))
