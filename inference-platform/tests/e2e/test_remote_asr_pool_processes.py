"""真实两个受管HTTP子进程并行；仅回环HTTPS协议替身，不测课题组吞吐/GPU。"""

import os
import threading
import time

from filelock import FileLock, Timeout
import pytest
from sqlalchemy import select

from backend.common.exceptions.app_error import AppError
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.asr.target_models import AsrCallPermit, AsrTargetWorkerSession
from backend.data.runtime.runtime_models import RuntimeComponent
from backend.schema.asr.target_schema import TargetCapacityCommand
from backend.service.asr.target_service import TargetAdmissionService
from backend.service.worker.remote_pool_service import RemotePoolService
from test_remote_asr_processes import https_asr, remote_process_case

pytestmark = pytest.mark.skipif(os.name != "nt", reason="真实受管子进程使用Windows Job Object")


@pytest.fixture
def pool_case(remote_process_case):
    case, pools = remote_process_case, []
    targets = TargetAdmissionService(case.database, case.settings)
    targets.register_current()
    targets.configure_capacity(TargetCapacityCommand(target_limit=2, group_limit=2,
        evidence="fixture-only: isolated threaded loopback server, no GPU capacity claim"))

    def pump(condition, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for pool in pools:
                pool.tick()
            if condition():
                return
            time.sleep(.03)
        pytest.fail("隔离调用池未按时完成: " + str([
            [(worker.state, worker.last_error, bool(worker.lease)) for worker in pool.workers]
            for pool in pools]))

    def start():
        pool = RemotePoolService(case.database, case.settings, case.client.application.extensions["app_logger"])
        pools.append(pool)
        pool.start()
        pump(lambda: all(worker.state == "ready" for worker in pool.workers))
        return pool

    case.pump_pool, case.start_pool, case.targets = pump, start, targets
    try:
        yield case
    finally:
        case.server.release.set()
        for release in case.server.release_by_request.values():
            release.set()
        for pool in pools:
            pool.request_drain()
        pump(lambda: all(pool.drained for pool in pools), timeout=30)
        pump(lambda: all(pool.close() for pool in pools), timeout=15)
        assert all(not worker.runner.is_alive() for pool in pools for worker in pool.workers)


def task_ids(*responses):
    assert all(response.status_code == 201 for response in responses), [response.get_json() for response in responses]
    return [response.get_json()["data"]["task"]["taskId"] for response in responses]


def test_two_real_children_overlap_and_drain_keeps_third_queued_then_restart(pool_case):
    case = pool_case
    pool = case.start_pool()
    first, second, third = task_ids(case.submit("parallel-a"), case.submit("parallel-b"), case.submit("queued-c"))
    deadline = {key: case.task(third)[key] for key in ("queueDeadlineAt", "totalDeadlineAt")}
    first_release, second_release = threading.Event(), threading.Event()
    case.server.release_by_request.update({first: first_release, second: second_release})
    case.server.response_texts.update({first: "第一个隔离结果", second: "第二个隔离结果"})
    case.server.mode = "hold"
    case.server.release.clear()
    assert case.dispatcher.tick() == 3
    case.pump_pool(lambda: len(case.server.posts) == 2)
    assert {post["request_id"].decode() for post in case.server.posts} == {first, second}
    assert case.task(first)["status"] == case.task(second)["status"] == "running"
    assert case.task(third)["status"] == "queued"
    pids = {worker.runner.pid for worker in pool.workers}
    assert len(pids) == 2 and None not in pids
    with case.database.session_factory() as session:
        permits = session.scalars(select(AsrCallPermit)).all()
        assert len(permits) == 2 and all(permit.state == "held" for permit in permits)
        assert len({permit.worker_session_id for permit in permits}) == 2
        assert len({permit.lease_token for permit in permits}) == 2
        assert session.get(RuntimeComponent, "remote_asr_worker").session_id == pool.session_id
        assert pool.session_id not in {permit.worker_session_id for permit in permits}
    pool.request_drain()
    assert not pool.close()
    assert case.submit("after-stop").status_code == 503
    # 后提交任务先返回，必须仍落到自己的task/result/permit，不串线。
    second_release.set()
    case.pump_pool(lambda: case.task(second)["status"] == "succeeded")
    assert case.task(first)["status"] == "running"
    assert not pool.drained and all(worker.runner.is_alive() for worker in pool.workers)
    first_release.set()
    case.pump_pool(lambda: pool.drained)
    assert case.task(first)["status"] == "succeeded"
    assert case.task(third)["status"] == "queued"
    assert {key: case.task(third)[key] for key in deadline} == deadline
    assert len(case.server.posts) == 2
    for task_id, text in ((first, "第一个隔离结果"), (second, "第二个隔离结果")):
        assert case.client.get(f"/api/tasks/{task_id}/result").get_json()["data"]["output"]["text"] == text
    case.pump_pool(pool.close)
    assert all(not worker.runner.is_alive() for worker in pool.workers)
    with FileLock(pool._lock.lock_file, timeout=0):
        pass
    case.server.mode = "ok"
    restarted = case.start_pool()
    case.pump_pool(lambda: case.task(third)["status"] == "succeeded")
    assert len(case.server.posts) == 3
    assert case.submit("parallel-a").status_code == 200
    assert len(case.server.posts) == 3
    assert all(worker.session_id not in {item.session_id for item in pool.workers} for worker in restarted.workers)
    with case.database.session_factory() as session:
        assert all(permit.state == "released" for permit in session.scalars(select(AsrCallPermit)))
        assert all(session.get(AsrTargetWorkerSession, worker.session_id).active_slot_key is None for worker in pool.workers)


def test_unknown_occupies_one_of_two_slots_across_restart_without_resend(pool_case):
    case = pool_case
    pool = case.start_pool()
    unknown, successful = task_ids(case.submit("unknown-a"), case.submit("normal-b"))
    case.server.post_modes[unknown] = "disconnect"
    case.dispatcher.tick()
    case.pump_pool(lambda: case.task(unknown)["status"] == "failed" and case.task(successful)["status"] == "succeeded")
    assert case.task(unknown)["remoteOutcome"] == "unknown"
    assert case.task(unknown)["attempt"] == 1
    case.pump_pool(pool.close)
    with case.database.session_factory() as session:
        permit = session.get(AsrCallPermit, unknown)
        identity = (permit.worker_session_id, permit.lease_token, permit.revision_id)
        assert permit.state == "uncertain"
        assert session.get(RemoteAsrCall, unknown).state == "uncertain"
    with pytest.raises(AppError) as error:
        case.targets.configure_capacity(TargetCapacityCommand(target_limit=3, group_limit=3, evidence="fixture-only"))
    assert error.value.code == "ASR_CAPACITY_OCCUPIED"
    restarted = case.start_pool()
    third, fourth = task_ids(case.submit("held-c"), case.submit("queued-d"))
    case.server.mode = "hold"
    case.server.release.clear()
    case.dispatcher.tick()
    case.pump_pool(lambda: len(case.server.posts) == 3)
    assert case.task(third)["status"] == "running"
    for _ in range(3):
        restarted.tick()
    assert case.task(fourth)["status"] == "queued" and case.task(fourth)["attempt"] == 0
    with case.database.session_factory() as session:
        permit = session.get(AsrCallPermit, unknown)
        assert (permit.worker_session_id, permit.lease_token, permit.revision_id) == identity
        assert permit.state == "uncertain"
        assert session.get(AsrCallPermit, third).state == "held"
        assert session.get(AsrCallPermit, fourth) is None
    assert [post["request_id"].decode() for post in case.server.posts].count(unknown) == 1
    restarted.request_drain()
    assert not restarted.close()
    case.server.release.set()
    case.pump_pool(lambda: restarted.drained)
    case.pump_pool(restarted.close)
    assert case.task(third)["status"] == "succeeded" and case.task(fourth)["status"] == "queued"
    assert case.submit("unknown-a").status_code == 200
    assert len(case.server.posts) == 3
