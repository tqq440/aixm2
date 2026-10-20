"""真实Windows调用子进程与两个回环HTTPS替身；不证明实验室GPU或CPU性能。"""

from datetime import timezone
from io import BytesIO
import os
import time
from types import SimpleNamespace
import wave

from filelock import FileLock
import pytest
from sqlalchemy import select

from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.asr.routing_models import AsrTaskRoute
from backend.data.asr.target_models import AsrCallPermit, AsrComputeTarget, AsrTargetWorkerSession
from backend.data.database.database import database_now
from backend.data.tasks.task_models import TaskRecord
from backend.modules.api.app import close_app, create_app
from backend.schema.asr.catalog_schema import CatalogRegistration, ResourceObservationInput
from backend.schema.asr.routing_schema import RoutingRegistration
from backend.service.asr.catalog_service import AsrCatalogService
from backend.service.asr.routing_service import AsrRoutingService
from backend.service.dispatch.dispatch_service import DispatchService
from backend.service.worker.remote_pool_service import RemotePoolService
from test_remote_asr_processes import https_asr, remote_process_case


pytestmark = pytest.mark.skipif(os.name != "nt", reason="真实受管调用子进程使用Windows Job Object")


@pytest.fixture
def cpu_https_asr(tmp_path):
    folder = tmp_path / "cpu-loopback"
    folder.mkdir()
    # 复用既有仅监听127.0.0.1的TLS协议替身，在独立端口生成独立证书。
    generator = https_asr.__wrapped__(folder)
    try:
        yield next(generator)
    finally:
        generator.close()


@pytest.fixture
def routed_process_case(remote_process_case, cpu_https_asr):
    base = remote_process_case
    database = base.database
    settings = base.settings.model_copy(update={"remote_asr_routing_isolation": True})
    catalog = AsrCatalogService(database, settings)
    servers = {"gpu": base.server, "cpu": cpu_https_asr}
    candidates, records, pools = [], {}, []
    for kind, server in servers.items():
        target_id = f"isolated-{kind}-service"
        group_id = "remote-asr-shared" if kind == "gpu" else "isolated-cpu-group"
        receipt = catalog.register(CatalogRegistration.model_validate({
            "targetId": target_id,
            "connection": {"baseUrl": server.url, "modelId": settings.remote_asr_model_id,
                           "caFile": str(server.certificate)},
            "capacityGroupId": group_id,
            "independenceEvidence": None if kind == "gpu" else "isolated independent HTTP server",
            "deviceKind": kind, "nodeId": "isolated-node", "deviceId": f"fixture-{kind}",
            "deviceEvidence": "protocol fixture label only, no hardware evidence",
            "compatibilityKey": "isolated-same-model",
            "compatibilityEvidence": "identical isolated response contract",
            "evidenceKind": "isolated",
        }))
        revision_id = receipt.catalog_revision_id
        with database.session_factory() as session:
            observed_at = database_now(session).replace(tzinfo=timezone.utc)
        for scope in ("target", "capacity_group"):
            catalog.observe(ResourceObservationInput.model_validate({
                "catalogRevisionId": revision_id, "scope": scope, "source": "isolated-local-fixture",
                "evidenceKind": "isolated", "evidence": "one controlled HTTP slot, not measured hardware",
                "observedAt": observed_at.isoformat(), "ttlSeconds": 300,
                "healthState": "ready", "availableConcurrency": 1,
            }))
        candidate = {"catalogRevisionId": revision_id}
        if kind == "cpu":
            candidate.update(cpuSupported=True, cpuSupportEvidence="isolated CPU response fixture", cpuProfile={
                "targetRevision": revision_id, "evidenceKind": "isolated",
                "evidenceReference": "controlled timing fixture, not laboratory measurement",
                "measuredAt": observed_at.isoformat(), "validForSeconds": 300.0, "sampleCount": 3,
                "minAudioSeconds": 0.0, "maxAudioSeconds": 1.0, "maxTestedConcurrency": 1,
                "fixedOverheadSeconds": 0.1, "secondsPerAudioSecond": 1.0, "safetyFactor": 2.0,
                "queueWaitUpperBoundSeconds": 0.1,
            })
        candidates.append(candidate)
        records[kind] = SimpleNamespace(target_id=target_id, revision_id=revision_id, group_id=group_id)
    route_id = AsrRoutingService(database, settings).register(RoutingRegistration.model_validate({
        "modelId": settings.remote_asr_model_id, "modelVersion": settings.remote_asr_model_version,
        "compatibilityKey": "isolated-same-model", "gpuConfiguration": "configured",
        "evidenceKind": "isolated", "evidence": "two local HTTPS fixture endpoints only",
        "cpuBudgetSeconds": 4.0, "candidates": candidates,
    })).routing_revision_id
    settings = settings.model_copy(update={"remote_asr_routing_revision": route_id})
    with database.transaction(immediate=True) as session:
        for record in records.values():
            session.get(AsrComputeTarget, record.target_id).enabled = True
    app = create_app(settings)
    client = app.test_client()
    dispatcher = DispatchService(database, settings, app.extensions["app_logger"])
    audio = BytesIO()
    with wave.open(audio, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 1600)

    def pump(condition, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for pool in pools:
                pool.tick()
            if condition():
                return
            time.sleep(.03)
        pytest.fail("隔离路由未按时完成: " + str([
            [(worker.state, worker.last_error, worker.runner.pid, bool(worker.lease)) for worker in pool.workers]
            for pool in pools]))

    def start():
        pool = RemotePoolService(database, settings, app.extensions["app_logger"])
        pools.append(pool)
        pool.start()
        pump(lambda: all(worker.state == "ready" for worker in pool.workers)
             and client.get("/api/asr/external/status").get_json()["data"]["available"])
        assert len(pool.workers) == 2 and len({worker.runner.pid for worker in pool.workers}) == 2
        return pool

    def submit(key):
        response = client.post("/api/tasks/asr", headers={"Idempotency-Key": key}, data={
            "modelId": settings.remote_asr_model_id, "modelVersion": settings.remote_asr_model_version,
            "language": "auto", "file": (BytesIO(audio.getvalue()), "isolated-routing.wav")})
        assert response.status_code in {200, 201}, response.get_json()
        return response.get_json()["data"]["task"]["taskId"], response.status_code

    def task(task_id):
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        return response.get_json()["data"]

    result = SimpleNamespace(database=database, settings=settings, servers=servers, records=records,
        pools=pools, client=client, dispatcher=dispatcher, pump=pump, start=start, submit=submit,
        task=task, route_id=route_id)
    try:
        yield result
    finally:
        for server in servers.values():
            server.release.set()
            for release in server.release_by_request.values():
                release.set()
        for pool in pools:
            pool.request_drain()
        pump(lambda: all(pool.drained for pool in pools), timeout=30)
        pump(lambda: all(pool.close() for pool in pools), timeout=15)
        assert all(not worker.runner.is_alive() for pool in pools for worker in pool.workers)
        for pool in pools:
            with FileLock(pool._lock.lock_file, timeout=0):
                pass
        close_app(app)


def assert_selection(case, task_id, kind):
    with case.database.session_factory() as session:
        task = session.get(TaskRecord, task_id)
        route = session.get(AsrTaskRoute, task_id)
        permit = session.get(AsrCallPermit, task_id)
        assert (task.model_id, task.model_version) == (
            case.settings.remote_asr_model_id, case.settings.remote_asr_model_version)
        assert route.selected_catalog_revision_id == case.records[kind].revision_id
        assert permit.capacity_group_id == case.records[kind].group_id
        assert session.get(RemoteAsrCall, task_id).config_fingerprint == case.route_id


def test_two_actual_children_use_gpu_then_qualified_cpu_and_drain_without_losing_queue(routed_process_case):
    case = routed_process_case
    gpu, cpu = case.servers["gpu"], case.servers["cpu"]
    pool = case.start()
    gpu.mode = "hold"
    gpu.release.clear()
    first, _ = case.submit("gpu-first")
    gpu.response_texts[first] = "GPU标签替身结果"
    assert case.dispatcher.tick() == 1
    case.pump(lambda: len(gpu.posts) == 1)
    assert not cpu.posts
    assert case.task(first)["status"] == "running"
    assert_selection(case, first, "gpu")
    second, _ = case.submit("cpu-second")
    cpu.response_texts[second] = "CPU标签替身结果"
    assert case.dispatcher.tick() == 1
    case.pump(lambda: case.task(second)["status"] == "succeeded")
    assert case.task(first)["status"] == "running"
    assert len(gpu.posts) == len(cpu.posts) == 1
    assert_selection(case, second, "cpu")
    third, _ = case.submit("queued-before-drain")
    assert case.dispatcher.tick() == 1
    original_deadlines = {key: case.task(third)[key] for key in ("queueDeadlineAt", "totalDeadlineAt")}
    pool.request_drain()
    assert not pool.close()
    gpu.release.set()
    case.pump(lambda: pool.drained)
    assert case.task(first)["status"] == "succeeded"
    assert case.task(third)["status"] == "queued" and case.task(third)["attempt"] == 0
    assert {key: case.task(third)[key] for key in original_deadlines} == original_deadlines
    case.pump(pool.close)
    assert all(not worker.runner.is_alive() for worker in pool.workers)
    for task_id, expected in ((first, "GPU标签替身结果"), (second, "CPU标签替身结果")):
        response = case.client.get(f"/api/tasks/{task_id}/result")
        assert response.status_code == 200 and response.get_json()["data"]["output"]["text"] == expected
    gpu.mode = "ok"
    case.start()
    case.pump(lambda: case.task(third)["status"] == "succeeded")
    assert_selection(case, third, "gpu")
    assert len(gpu.posts) == 2 and len(cpu.posts) == 1
    assert case.submit("gpu-first") == (first, 200)
    assert len(gpu.posts) == 2 and len(cpu.posts) == 1


def test_unknown_gpu_request_is_never_reposted_to_cpu_after_pool_restart(routed_process_case):
    case = routed_process_case
    gpu, cpu = case.servers["gpu"], case.servers["cpu"]
    pool = case.start()
    gpu.mode = "disconnect"
    first, _ = case.submit("unknown-on-gpu")
    case.dispatcher.tick()
    case.pump(lambda: case.task(first)["status"] == "failed")
    assert case.task(first)["remoteOutcome"] == "unknown" and case.task(first)["attempt"] == 1
    assert len(gpu.posts) == 1 and not cpu.posts
    assert_selection(case, first, "gpu")
    case.pump(pool.close)
    with case.database.session_factory() as session:
        permit = session.get(AsrCallPermit, first)
        assert permit.state == "uncertain"
        identity = (permit.revision_id, permit.worker_session_id, permit.lease_token)
    gpu.mode = "ok"
    restarted = case.start()
    second, _ = case.submit("new-independent-cpu")
    case.dispatcher.tick()
    case.pump(lambda: case.task(second)["status"] == "succeeded")
    assert_selection(case, second, "cpu")
    assert case.submit("unknown-on-gpu") == (first, 200)
    assert len(gpu.posts) == len(cpu.posts) == 1
    assert gpu.posts[0]["request_id"].decode() == first
    assert cpu.posts[0]["request_id"].decode() == second
    assert case.task(first)["attempt"] == 1 and case.task(first)["status"] == "failed"
    with case.database.session_factory() as session:
        permit = session.get(AsrCallPermit, first)
        assert (permit.revision_id, permit.worker_session_id, permit.lease_token) == identity
        assert permit.state == session.get(RemoteAsrCall, first).state == "uncertain"
        assert session.get(AsrCallPermit, second).state == "released"
    case.pump(restarted.close)
    with case.database.session_factory() as session:
        sessions = session.scalars(select(AsrTargetWorkerSession)).all()
        assert len(sessions) == 4
        assert all(row.state == "stopped" and row.active_slot_key is None for row in sessions)
