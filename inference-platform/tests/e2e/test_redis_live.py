"""真实Redis/Lua/进程故障验收。没有明确安装路径时跳过，绝不替身冒充。"""

from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from uuid import uuid4

from pydantic import SecretStr
import pytest
from sqlalchemy import select
from backend.common.exceptions.app_error import AppError
from backend.common.queue.redis_streams import RedisStreams
from backend.config.queue.redis_config import create_redis_client
from backend.config.settings.app_settings import AppSettings,PROJECT_ROOT
from backend.data.invocations.invocation_repository import InvocationRepository
from backend.data.database.database import database_now
from backend.data.queue.queue_models import InferenceQueueOutbox
from backend.data.tasks.task_models import TaskRecord
from backend.service.queue.queue_service import QueueService
from tests.e2e.redis_live_support import OwnedRedis
from tests.integration.test_common_execution import case,asr_result

pytestmark=pytest.mark.skipif(not os.environ.get("INFERENCE_REDIS_TEST_SERVER"),reason="未配置独立Linux redis-server，真实Redis/Lua/重启未验")


def due_now(case):
    with case.db.transaction(immediate=True) as session:
        for row in session.scalars(select(InferenceQueueOutbox)):
            row.next_attempt_at=database_now(session)-timedelta(seconds=1)
            row.publisher_expires_at=None


@pytest.fixture
def owned_redis(tmp_path):
    binary=os.environ.get("INFERENCE_REDIS_TEST_SERVER")
    if not binary:pytest.skip("未提供独立Linux redis-server路径，真实Redis/重启验收未执行")
    if platform.system()!="Linux":pytest.fail("此验收入口要求真实Linux；不会改用Windows替身")
    pytest.importorskip("redis",reason="缺少可选redis-py客户端，真实验收未执行")
    def client_factory(url):
        return create_redis_client(AppSettings(common_execution_enabled=True,common_queue_backend="redis",redis_url=url,redis_timeout_seconds=.3))
    owned=OwnedRedis(binary,tmp_path/"owned-redis",client_factory)
    try:
        owned.start();yield owned
    finally:owned.close()


@pytest.fixture
def live_case(case,owned_redis):
    settings=AppSettings.model_validate({**case.settings.model_dump(),"common_queue_backend":"redis",
        "redis_url":SecretStr(owned_redis.url),"redis_timeout_seconds":.3,"redis_republish_seconds":1,
        "notification_visibility_seconds":1,"renew_interval_seconds":.1,"poll_interval_seconds":.1})
    queue=QueueService(case.db,settings,consumer="first-worker")
    case.settings=settings;case.core=queue.core
    case.group();case.service();case.budget(limit=3)
    try:yield case,queue,owned_redis
    finally:queue.close()


def test_real_lua_identity_guard_and_missing_entry_cleanup(owned_redis):
    client=owned_redis.client;stream="module6b:{"+uuid4().hex+"}:lua"
    queue=RedisStreams(client,stream,"lua-test",1);queue.ensure()
    fields={"version":"1","eventId":uuid4().hex,"invocationId":uuid4().hex,"serviceRevision":"a"*64,"queueKey":"common."+"b"*64}
    first=client.xadd(stream,fields);queue.receive()
    assert not queue.acknowledge(first,fields|{"eventId":uuid4().hex})
    assert client.xlen(stream)==1 and client.xpending(stream,queue.group)["pending"]==1
    # XADD可重复字段名：数量相同、值都相符仍不能冒充完整身份。
    pairs=list(fields.items());ambiguous=[part for pair in [pairs[0],pairs[0],*pairs[2:]] for part in pair]
    second=client.execute_command("XADD",stream,"*",*ambiguous)
    assert not queue.acknowledge(second,fields)
    assert client.xrange(stream,second,second)
    assert queue.acknowledge(first,fields)
    third=client.xadd(stream,fields);queue.receive();client.xdel(stream,third)
    assert not queue.acknowledge(third,fields)
    assert client.xpending(stream,queue.group)["pending"]==1  # 仅重复字段的第二条仍未确认。


def test_duplicate_notifications_and_terminal_release_are_idempotent(live_case):
    case,queue,redis=live_case;task,invocation=case.task();queue.publish()
    stream=queue.gate.streams.stream;fields=redis.client.xrange(stream)[0][1]
    redis.client.xadd(stream,fields);queue.collect()
    calls=[];lease=case.core.claim_round("worker")[0]
    assert case.core.dispatch(lease,lambda *_:calls.append(1) or asr_result())
    assert case.core.complete(lease,asr_result())
    queue.acknowledge_finished();time.sleep(1.05);queue.collect();queue.acknowledge_finished()
    assert calls==[1] and not case.core.claim_round("again")
    with case.db.transaction() as session:
        assert session.get(TaskRecord,task).result_json["text"]==asr_result().text
        assert InvocationRepository.occupied(session,"group:shared")==0
    assert redis.client.xlen(stream)==0 and redis.client.xpending(stream,queue.gate.streams.group)["pending"]==0
    assert sum(e["eventType"]=="invocation.capacity_released" for e in case.events(task))==1


def test_killed_consumer_reclaims_without_reexecuting_unknown(live_case,tmp_path):
    case,queue,redis=live_case;task,invocation=case.task();queue.publish()
    # 真子进程已领取Redis通知并在SQLite标记发出，然后直接退出，模拟执行器崩溃。
    settings_file=tmp_path/"worker-settings.json"
    payload=case.settings.model_dump(mode="json");payload["redis_url"]=redis.url
    settings_file.write_text(json.dumps(payload),encoding="utf-8");settings_file.chmod(0o600)
    code="""import json,os,sys
from backend.config.settings.app_settings import AppSettings
from backend.data.database.database import Database
from backend.service.queue.queue_service import QueueService
s=AppSettings.model_validate(json.load(open(sys.argv[1])))
d=Database(s);q=QueueService(d,s,consumer='crashed-worker')
q.collect();lease=q.core.claim_round('crashed-worker')[0]
assert q.core.mark_sent(lease)
os._exit(23)
"""
    env={k:v for k,v in os.environ.items() if not k.startswith(("INFERENCE_","NODE_"))}
    env["PYTHONPATH"]=str(PROJECT_ROOT)
    result=subprocess.run([sys.executable,"-c",code,str(settings_file)],env=env,cwd=PROJECT_ROOT,
        capture_output=True,timeout=15)
    assert result.returncode==23,"隔离执行器未到达预期崩溃点；检查本地私有环境"
    time.sleep(1.05);queue.collect();case.expire(invocation);case.core.recover()
    assert not case.core.claim_round("new-worker") and queue.publish()==0
    assert case.core.get(invocation)["permitState"]=="uncertain"
    with case.db.transaction() as session:assert InvocationRepository.occupied(session,"group:shared")==1
    assert redis.client.xpending(queue.gate.streams.stream,queue.gate.streams.group)["pending"]==1
    assert queue.acknowledge_finished()==0
    for _ in range(2):case.core.resolve_unknown(invocation,"local-user",outcome="failed",evidence="isolated explicit terminal fixture")
    queue.acknowledge_finished()
    assert sum(e["eventType"]=="invocation.capacity_released" for e in case.events(task))==1
    assert redis.client.xpending(queue.gate.streams.stream,queue.gate.streams.group)["pending"]==0


@pytest.mark.parametrize("crash",[False,True])
def test_real_redis_restart_preserves_stream_group_and_pending(live_case,crash):
    case,queue,redis=live_case;case.task();queue.tick();stream=queue.gate.streams.stream
    before=redis.client.xrange(stream);assert redis.client.xpending(stream,queue.gate.streams.group)["pending"]==1
    redis.stop(crash=crash)
    with pytest.raises(AppError,match="Redis"):case.core.claim_round("offline")
    redis.start();queue.gate.check()
    assert redis.client.xrange(stream)==before
    assert redis.client.xpending(stream,queue.gate.streams.group)["pending"]==1
    calls=[]
    assert queue.run_once("after-restart",lambda *_:calls.append(1) or asr_result())
    # 停机超过补投间隔时可能产生重复通知；保留正常可见期后清理剩余终态副本。
    time.sleep(1.05);queue.collect()
    assert calls==[1] and redis.client.xlen(stream)==0


def test_lost_stream_republished_from_sqlite_without_duplicate_inference(live_case):
    case,queue,redis=live_case;_,invocation=case.task();queue.tick()
    redis.client.delete(queue.gate.streams.stream)  # 仅本次新隔离DB的键，不清库。
    due_now(case);calls=[]
    assert queue.run_once("recovery",lambda *_:calls.append(1) or asr_result())
    assert calls==[1] and case.core.get(invocation)["outcome"]=="succeeded"


def test_inflight_result_persists_while_redis_down_then_ack_after_restart(live_case):
    case,queue,redis=live_case;task,invocation=case.task();queue.tick();lease=case.core.claim_round("active")[0]
    assert case.core.mark_sent(lease);redis.stop()
    waiting,_=case.task()
    assert case.core.complete(lease,asr_result())
    with pytest.raises(AppError):queue.acknowledge_finished()
    with case.db.transaction() as session:
        assert session.get(TaskRecord,task).status=="succeeded"
        assert session.get(TaskRecord,waiting).status=="queued"
        assert InvocationRepository.occupied(session,"group:shared")==0
    redis.start();queue.tick()
    assert case.core.get(invocation)["outcome"]=="succeeded"
    assert not case.core.complete(lease,asr_result().model_copy(update={"text":"DIFFERENT_RESULT"}))


def test_xadd_committed_but_sql_receipt_lost_replays_safely(live_case,monkeypatch):
    case,queue,redis=live_case;case.task()
    from backend.data.queue.queue_repository import QueueRepository
    with monkeypatch.context() as patch:
        def fail(*args,**kwargs):raise RuntimeError("injected after Redis commit")
        patch.setattr(QueueRepository,"record_publication",fail)
        with pytest.raises(RuntimeError):queue.publish()
    assert redis.client.xlen(queue.gate.streams.stream)==1
    due_now(case);calls=[]
    assert queue.run_once("replay",lambda *_:calls.append(1) or asr_result())
    time.sleep(1.05);queue.collect()
    assert calls==[1] and redis.client.xlen(queue.gate.streams.stream)==0


def test_three_capabilities_share_quota_with_real_redis(live_case):
    case,queue,redis=live_case
    case.service("vision-main","vision");case.service("llm-main","llm");case.budget(version=1,limit=3)
    case.task();case.task("vision-main");online,_=case.online()
    assert queue.tick().published==2
    with ThreadPoolExecutor(max_workers=2) as pool:
        offline=pool.submit(case.core.claim_round,"offline")
        llm=pool.submit(case.core.claim_online,online["invocationId"],online["workerId"])
        assert len(offline.result())+int(llm.result() is not None)==1
    with case.db.transaction() as session:assert InvocationRepository.occupied(session,"group:shared")==1
    assert all(fields["invocationId"]!=online["invocationId"] for _,fields in redis.client.xrange(queue.gate.streams.stream))
    redis.stop()
    with pytest.raises(AppError):case.online()
