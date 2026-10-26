"""验收进程管理器的隔离边界。替身验证，不代表Redis/Lua通过。"""

from pathlib import Path,PurePosixPath
from types import SimpleNamespace
import subprocess
import pytest
from tests.e2e.redis_live_support import OwnedRedis,redis_config


@pytest.mark.parametrize("directory",['/tmp/bad"path','/tmp/bad\npath','/tmp/bad\\path'])
def test_redis_config_rejects_injected_directives(directory):
    with pytest.raises(ValueError):redis_config(Path(directory),16379,"a"*64)


def test_config_only_binds_loopback_and_keeps_aof(tmp_path):
    # 用Linux样例路径，不把Windows路径当作Redis Linux配置验证。
    content=redis_config(PurePosixPath('/home/user/test-redis'),16379,'a'*64)
    assert 'bind 127.0.0.1\n' in content and 'daemonize no\n' in content
    assert 'appendonly yes\nappendfsync always\n' in content
    assert 'maxmemory-policy noeviction' in content and 'requirepass '+('a'*64) in content
    assert 'replicaof' not in content and 'include ' not in content


def test_windows_owned_process_is_rejected_before_file_or_process_operations(tmp_path,monkeypatch):
    monkeypatch.setattr('tests.e2e.redis_live_support.platform.system',lambda:'Windows')
    with pytest.raises(ValueError,match='Linux'):OwnedRedis('/usr/bin/redis-server',tmp_path/'none',None)
    assert not (tmp_path/'none').exists()


class Child:
    pid=123
    def __init__(self):self.calls=[];self.exited=False
    def poll(self):return 0 if self.exited else None
    def terminate(self):self.calls.append('terminate');self.exited=True
    def kill(self):self.calls.append('kill');self.exited=True
    def wait(self,timeout):self.calls.append(('wait',timeout));return 0


def shell(owned_child):
    value=OwnedRedis.__new__(OwnedRedis);value.process=owned_child
    return value


@pytest.mark.parametrize('crash,signal',[(False,'terminate'),(True,'kill')])
def test_only_direct_child_process_is_signalled(crash,signal):
    child=Child();value=shell(child);value.stop(crash=crash);value.stop(crash=crash)
    assert child.calls==[signal,('wait',10)]


def test_timeout_cleanup_only_kills_the_same_child():
    child=Child();value=shell(child)
    def wait(timeout):
        child.calls.append(('wait',timeout))
        if timeout==10:raise subprocess.TimeoutExpired('own test child',10)
        return 0
    child.wait=wait;value.stop()
    assert child.calls==['terminate',('wait',10),'kill',('wait',5)]


def test_process_identity_mismatch_stops_only_own_child(tmp_path,monkeypatch):
    child=Child();value=shell(None)
    value.binary=Path('/usr/bin/redis-server');value.config=tmp_path/'redis.conf';value.folder=tmp_path
    value.client=SimpleNamespace(info=lambda *_:{'process_id':999,'redis_version':'7.2.0'})
    calls=[]
    def popen(argv,**kwargs):calls.append((argv,kwargs));return child
    monkeypatch.setattr('tests.e2e.redis_live_support.subprocess.Popen',popen)
    with pytest.raises(RuntimeError,match='PID'):value.start()
    assert child.calls[0]=='terminate'
    assert calls[0][1]['shell'] is False and calls[0][1]['start_new_session'] is True
    assert calls[0][0]==[str(value.binary),str(value.config)]


def test_existing_live_process_cannot_start_another():
    value=shell(Child())
    with pytest.raises(RuntimeError,match='already running'):value.start()
