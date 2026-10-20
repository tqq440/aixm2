"""隔离目录、真实SQLite与原领取事务；不访问服务器，不代表真实GPU/CPU能力。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
import logging
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from filelock import FileLock
import pytest
from sqlalchemy import select

from backend.common.exceptions.app_error import AppError
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.asr.routing_models import AsrTaskRoute
from backend.data.asr.target_models import AsrCallPermit, AsrComputeTarget, AsrTargetWorkerSession
from backend.data.broker.broker_models import NotificationRecord
from backend.data.database.database import database_now
from backend.data.execution.attempt_models import ExecutionAttempt
from backend.data.runtime.runtime_repository import RuntimeRepository
from backend.data.tasks.task_models import TaskRecord
from backend.schema.asr.asr_schema import AsrEngineMetadata, TranscriptionResult
from backend.schema.asr.catalog_schema import CatalogRegistration, ResourceObservationInput
from backend.schema.asr.routing_schema import RoutingRegistration
from backend.schema.execution.execution_schema import FailureInfo
from backend.service.asr.catalog_service import AsrCatalogService
from backend.service.asr.remote_catalog import REMOTE_QUEUE_KEY, config_fingerprint
from backend.service.asr.remote_service import RemoteAsrService
from backend.service.asr.routing_service import AsrRoutingService
from backend.service.asr.target_service import TargetAdmissionService
from backend.service.execution.execution_service import ExecutionService
from backend.service.worker.remote_pool_service import RemotePoolService
from tests.integration.test_asr_target_admission import target_case, seed_queued, task_snapshot


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def reject(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("路由事务验证禁止网络访问")

    monkeypatch.setattr("socket.socket.connect", reject)
    monkeypatch.setattr("socket.socket.connect_ex", reject)
    monkeypatch.setattr("socket.getaddrinfo", reject)
    yield
    assert not attempts, "拦截的网络尝试不能记为无网络通过"


@pytest.fixture
def routing_factory(target_case, monkeypatch):
    case = target_case
    with case.db.session_factory() as session:
        clock = [database_now(session)]
    for module in (
        "backend.service.asr.catalog_service", "backend.service.asr.routing_service",
        "backend.service.asr.remote_service", "backend.service.asr.target_service",
        "backend.data.execution.execution_repository", "backend.data.runtime.runtime_repository",
        "backend.data.broker.broker_repository", "tests.integration.test_asr_target_admission",
    ):
        monkeypatch.setattr(module + ".database_now", lambda session: clock[0])

    def build(*, kinds=("gpu", "cpu"), shared=False, cpu_changes=None,
              gpu_configuration=None, enabled=True):
        base = case.settings.model_copy(update={"remote_asr_routing_isolation": True})
        catalog = AsrCatalogService(case.db, base)
        records = {}
        candidates = []
        for index, kind in enumerate(kinds):
            name = f"fixture-{kind}-{index}"
            group = "remote-asr-shared" if shared or index == 0 else f"fixture-group-{index}"
            command = CatalogRegistration.model_validate({
                "targetId": name,
                "connection": {"baseUrl": f"https://{name}.invalid:8003", "modelId": "fixture-compatible-asr"},
                "capacityGroupId": group,
                "independenceEvidence": None if group == "remote-asr-shared" else "isolated independent fixture",
                "deviceKind": kind, "nodeId": "fixture-node", "deviceId": name,
                "deviceEvidence": "isolated device mapping, no hardware claim",
                "compatibilityKey": "fixture-compatible-model",
                "compatibilityEvidence": "isolated same-model contract only",
                "evidenceKind": "isolated",
            })
            revision_id = catalog.register(command).catalog_revision_id
            item = {"catalogRevisionId": revision_id}
            if kind == "cpu":
                item.update(cpuSupported=True, cpuSupportEvidence="isolated CPU support", cpuProfile={
                    "targetRevision": revision_id, "evidenceKind": "isolated",
                    "evidenceReference": "isolated timing fixture, not measured laboratory performance",
                    "measuredAt": clock[0].replace(tzinfo=timezone.utc).isoformat(), "validForSeconds": 3600.0,
                    "sampleCount": 3, "minAudioSeconds": 0.0, "maxAudioSeconds": 10.0,
                    "maxTestedConcurrency": 1, "fixedOverheadSeconds": 0.1,
                    "secondsPerAudioSecond": 1.0, "safetyFactor": 2.0,
                    "queueWaitUpperBoundSeconds": 0.1,
                })
                for key, value in (cpu_changes or {}).items():
                    if key in {"cpuSupported", "cpuSupportEvidence", "cpuProfile"}:
                        item[key] = value
                    else:
                        item["cpuProfile"][key] = value
            candidates.append(item)
            records[name] = SimpleNamespace(name=name, kind=kind, revision_id=revision_id, group=group)
        routing = AsrRoutingService(case.db, base)
        registration = RoutingRegistration.model_validate({
            "modelId": base.remote_asr_model_id, "modelVersion": base.remote_asr_model_version,
            "compatibilityKey": "fixture-compatible-model",
            "gpuConfiguration": gpu_configuration or ("configured" if "gpu" in kinds else "none"),
            "evidenceKind": "isolated", "evidence": "isolated transaction verification only",
            "cpuBudgetSeconds": 30.0, "candidates": candidates,
        })
        revision_id = routing.register(registration).routing_revision_id
        settings = base.model_copy(update={"remote_asr_routing_revision": revision_id})
        with case.db.transaction(immediate=True) as session:
            # 仅本测试临时库显式授权执行；正式目录登记仍保持disabled。
            for name in records:
                session.get(AsrComputeTarget, name).enabled = enabled
        result = SimpleNamespace(**vars(case), clock=clock, catalog=catalog, records=records,
            route_id=revision_id, routing=AsrRoutingService(case.db, settings), root_settings=settings,
            remote=RemoteAsrService(case.db, settings, pooled=True), candidates=candidates)
        for row in records.values():
            row.settings = settings.model_copy(update={
                "remote_asr_base_url": f"https://{row.name}.invalid:8003",
                "remote_asr_model_id": "fixture-compatible-asr", "remote_asr_model_version": None,
            })
            row.session_id = uuid4().hex
            row.target = TargetAdmissionService(case.db, row.settings, target_id=row.name)
            row.executor = ExecutionService(case.db, row.settings, asr_profile="remote",
                remote_target_id=row.name, remote_pooled=True)
            if enabled:
                row.target.bind_worker(row.session_id, pid=123)
                row.target.report_worker(row.session_id, "ready", True, now=clock[0])
            observe(result, row)
        if enabled:
            result.remote.start_pool(uuid4().hex, 123)
            RuntimeRepository(case.db).report(component_id="remote_asr_worker", session_id=uuid4().hex,
                pid=123, state="ready", model_ready=True)
        return result

    return build


@pytest.fixture
def routing_case(routing_factory):
    return routing_factory()


def observe(case, target, *, scopes=("target", "capacity_group"), observed_at=None,
            ttl=60, available=1, health="ready", **metrics):
    for scope in scopes:
        case.catalog.observe(ResourceObservationInput.model_validate({
            "catalogRevisionId": target.revision_id, "scope": scope, "source": "isolated-fixture",
            "evidenceKind": "isolated", "evidence": "isolated observation only",
            "observedAt": (observed_at or case.clock[0]).replace(tzinfo=timezone.utc).isoformat(),
            "ttlSeconds": ttl, "healthState": health, "availableConcurrency": available, **metrics,
        }))


def queued(case, *, route_id=None):
    task_id = seed_queued(case)
    with case.db.transaction(immediate=True) as session:
        session.delete(session.get(RemoteAsrCall, task_id))
        session.flush()
        task = session.get(TaskRecord, task_id)
        remote = case.remote if route_id is None else RemoteAsrService(case.db,
            case.root_settings.model_copy(update={"remote_asr_routing_revision": route_id}), pooled=True)
        remote.record_submission(session, task)
    return task_id


def worker(case, kind):
    return next(row for row in case.records.values() if row.kind == kind)


def delivery(case, target, task_id, *, filtered=False):
    return case.broker.receive(worker_id=target.session_id, queue_key=REMOTE_QUEUE_KEY,
        visibility_seconds=30,
        candidate_check=lambda session, item: item.task_id == task_id and (not filtered or
            target.executor.remote_service.can_receive_task(session, item.task_id, target.session_id)))


def acquire(case, target, task_id):
    item = delivery(case, target, task_id)
    assert item is not None
    return item, target.executor.acquire(item)


def route_snapshot(case, task_id):
    with case.db.session_factory() as session:
        route = session.get(AsrTaskRoute, task_id)
        return route.selected_catalog_revision_id, route.selected_at, route.decision_snapshot


def assert_unclaimed(case, task_id, original):
    assert task_snapshot(case, task_id) == original
    assert route_snapshot(case, task_id) == (None, None, None)
    with case.db.session_factory() as session:
        assert session.get(AsrCallPermit, task_id) is None
        assert not session.scalars(select(ExecutionAttempt).where(ExecutionAttempt.task_id == task_id)).all()


def fixture_result(**changes):
    values = dict(text="隔离路由结果", segments=[], audio_duration_seconds=0.1,
        engine_metadata=AsrEngineMetadata(adapter_key="remote-v2-sensevoice"))
    values.update(changes)
    return TranscriptionResult(**values)


def test_gpu_priority_survives_cpu_first_receive_and_keeps_logical_task_identity(routing_case):
    case = routing_case
    gpu, cpu = worker(case, "gpu"), worker(case, "cpu")
    task_id = queued(case)
    original = task_snapshot(case, task_id)
    assert delivery(case, cpu, task_id, filtered=True) is None
    item, rejected = acquire(case, cpu, task_id)
    assert rejected.decision == "busy"
    assert_unclaimed(case, task_id, original)
    assert case.broker.release(item)
    _, accepted = acquire(case, gpu, task_id)
    assert accepted.decision == "acquired"
    assert accepted.input.model_id == gpu.executor.asr_spec.model_id
    assert accepted.input.model_version == gpu.executor.asr_spec.model_version
    assert accepted.input.model_version != original["model_version"]
    after = task_snapshot(case, task_id)
    assert (after["model_id"], after["model_version"]) == (original["model_id"], original["model_version"])
    assert route_snapshot(case, task_id)[0] == gpu.revision_id
    with case.db.session_factory() as session:
        assert session.get(RemoteAsrCall, task_id).config_fingerprint == case.route_id
        assert session.get(AsrCallPermit, task_id).worker_session_id == gpu.session_id
    assert not cpu.executor.commit_result(accepted.lease, fixture_result())
    assert gpu.executor.commit_result(accepted.lease, fixture_result())
    assert gpu.executor.commit_result(accepted.lease, fixture_result())
    assert not cpu.executor.commit_result(accepted.lease, fixture_result())
    assert not cpu.executor.commit_result(accepted.lease, fixture_result(text="另一个候选的结果"))


@pytest.mark.parametrize("uncertain", [False, True])
def test_confirmed_gpu_full_can_assign_new_task_to_qualified_independent_cpu(routing_case, uncertain):
    case = routing_case
    gpu, cpu = worker(case, "gpu"), worker(case, "cpu")
    first = queued(case)
    _, selected = acquire(case, gpu, first)
    assert selected.decision == "acquired"
    if uncertain:
        assert gpu.executor.commit_failure(selected.lease, FailureInfo(code="REMOTE_ASR_UNKNOWN", message="隔离断线"))
    second = queued(case)
    _, fallback = acquire(case, cpu, second)
    assert fallback.decision == "acquired"
    assert route_snapshot(case, second)[0] == cpu.revision_id
    assert route_snapshot(case, first)[0] == gpu.revision_id
    with case.db.session_factory() as session:
        assert session.get(AsrCallPermit, first).state == ("uncertain" if uncertain else "held")
        assert session.get(AsrCallPermit, second).capacity_group_id != session.get(AsrCallPermit, first).capacity_group_id


def test_explicit_zero_gpu_resource_allows_qualified_cpu_without_fabricating_gpu_permit(routing_case):
    case = routing_case
    gpu, cpu = worker(case, "gpu"), worker(case, "cpu")
    observe(case, gpu, available=0)
    task_id = queued(case)
    _, decision = acquire(case, cpu, task_id)
    assert decision.decision == "acquired"
    assert route_snapshot(case, task_id)[0] == cpu.revision_id
    with case.db.session_factory() as session:
        permits = session.scalars(select(AsrCallPermit)).all()
        assert len(permits) == 1 and permits[0].worker_session_id == cpu.session_id


def test_unknown_gpu_resource_is_not_replaced_by_high_free_memory_or_low_utilization(routing_case):
    case = routing_case
    gpu = worker(case, "gpu")
    observe(case, gpu, available=None, gpuUtilizationPercent=0.0, gpuMemoryFreeMiB=100_000)
    task_id = queued(case)
    original = task_snapshot(case, task_id)
    _, decision = acquire(case, gpu, task_id)
    assert decision.decision == "busy"
    assert_unclaimed(case, task_id, original)


@pytest.mark.parametrize("gpu_configuration,expected", [("none", "acquired"), ("unknown", "busy")])
def test_cpu_only_requires_explicit_no_gpu_configuration(routing_factory, gpu_configuration, expected):
    case = routing_factory(kinds=("cpu",), gpu_configuration=gpu_configuration)
    cpu = worker(case, "cpu")
    task_id = queued(case)
    original = task_snapshot(case, task_id)
    _, decision = acquire(case, cpu, task_id)
    assert decision.decision == expected
    if expected == "busy":
        assert_unclaimed(case, task_id, original)
    else:
        assert route_snapshot(case, task_id)[0] == cpu.revision_id


@pytest.mark.parametrize("health,available", [("unknown", 1), ("unavailable", 1), ("ready", None)])
def test_unconfirmed_gpu_is_not_full_and_does_not_authorize_cpu(routing_case, health, available):
    case = routing_case
    gpu, cpu = worker(case, "gpu"), worker(case, "cpu")
    observe(case, gpu, health=health, available=available)
    task_id = queued(case)
    before = task_snapshot(case, task_id)
    _, decision = acquire(case, cpu, task_id)
    assert decision.decision == "busy"
    assert_unclaimed(case, task_id, before)


@pytest.mark.parametrize("scope", ["target", "capacity_group"])
def test_observation_exact_expiry_blocks_claim_without_attempt_or_selection(routing_case, scope):
    case = routing_case
    gpu = worker(case, "gpu")
    # 只令指定范围的最新采样到达TTL边界；其他样本和Worker心跳均新鲜。
    case.clock[0] += timedelta(seconds=60)
    for row in case.records.values():
        row.target.report_worker(row.session_id, "ready", True, now=case.clock[0])
        scopes = tuple(value for value in ("target", "capacity_group") if row != gpu or value != scope)
        observe(case, row, scopes=scopes)
    task_id = queued(case)
    before = task_snapshot(case, task_id)
    _, decision = acquire(case, gpu, task_id)
    assert decision.decision == "busy"
    assert_unclaimed(case, task_id, before)


def test_delayed_observation_clock_rollback_is_unknown_at_claim(routing_case):
    case = routing_case
    gpu = worker(case, "gpu")
    sampled = case.clock[0]
    case.clock[0] += timedelta(seconds=20)
    observe(case, gpu, observed_at=sampled + timedelta(seconds=1))
    case.clock[0] = sampled + timedelta(seconds=10)
    task_id = queued(case)
    before = task_snapshot(case, task_id)
    _, decision = acquire(case, gpu, task_id)
    assert decision.decision == "busy"
    assert_unclaimed(case, task_id, before)


@pytest.mark.parametrize("changes", [
    {"cpuSupported": False}, {"cpuSupportEvidence": None}, {"cpuProfile": None},
    {"minAudioSeconds": 1.0},
    {"fixedOverheadSeconds": 30.0},
])
def test_gpu_full_does_not_bypass_cpu_support_and_performance_contract(routing_factory, changes):
    case = routing_factory(cpu_changes=changes)
    gpu, cpu = worker(case, "gpu"), worker(case, "cpu")
    _, first = acquire(case, gpu, queued(case))
    assert first.decision == "acquired"
    task_id = queued(case)
    original = task_snapshot(case, task_id)
    _, second = acquire(case, cpu, task_id)
    assert second.decision == "busy"
    assert_unclaimed(case, task_id, original)


def test_shared_group_competition_keeps_one_attempt_and_cpu_cannot_bypass(routing_factory):
    case = routing_factory(shared=True)
    gpu, cpu = worker(case, "gpu"), worker(case, "cpu")
    ids = [queued(case), queued(case)]
    before = [task_snapshot(case, task_id) for task_id in ids]
    messages = [delivery(case, gpu, task_id) for task_id in ids]
    barrier = Barrier(2)

    def claim(item):
        barrier.wait(timeout=5)
        return gpu.executor.acquire(item)

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(claim, messages))
    assert sorted(item.decision for item in decisions) == ["acquired", "busy"]
    losing = next(index for index, item in enumerate(decisions) if item.decision == "busy")
    assert_unclaimed(case, ids[losing], before[losing])
    assert case.broker.release(messages[losing])
    _, denied = acquire(case, cpu, ids[losing])
    assert denied.decision == "busy"
    assert_unclaimed(case, ids[losing], before[losing])
    with case.db.session_factory() as session:
        assert len(session.scalars(select(ExecutionAttempt)).all()) == 1
        assert len(session.scalars(select(AsrCallPermit)).all()) == 1


def test_distinct_fixed_gpu_workers_compete_atomically_for_same_group(routing_factory):
    case = routing_factory(kinds=("gpu", "gpu"), shared=True)
    targets = list(case.records.values())
    ids = []
    for candidate in case.candidates:
        registration = RoutingRegistration.model_validate({
            "modelId": case.settings.remote_asr_model_id, "modelVersion": case.settings.remote_asr_model_version,
            "compatibilityKey": "fixture-compatible-model", "gpuConfiguration": "configured",
            "evidenceKind": "isolated", "evidence": "isolated one-candidate route",
            "candidates": [candidate],
        })
        revision = case.routing.register(registration).routing_revision_id
        ids.append(queued(case, route_id=revision))
    before = [task_snapshot(case, task_id) for task_id in ids]
    messages = [delivery(case, target, task_id, filtered=True) for target, task_id in zip(targets, ids)]
    assert all(messages)  # 两个独立固定目标各自在容量尚未占用时通过预筛。
    barrier = Barrier(2)

    def claim(pair):
        target, message = pair
        barrier.wait(timeout=5)
        return target.executor.acquire(message)

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(claim, zip(targets, messages)))
    assert sorted(item.decision for item in decisions) == ["acquired", "busy"]
    losing = next(index for index, item in enumerate(decisions) if item.decision == "busy")
    assert_unclaimed(case, ids[losing], before[losing])
    with case.db.session_factory() as session:
        assert len(session.scalars(select(ExecutionAttempt)).all()) == 1
        assert len(session.scalars(select(AsrCallPermit)).all()) == 1


@pytest.mark.parametrize("change", ["expired_report", "draining", "worker_stale"])
def test_claim_rechecks_changes_after_broker_candidate_passes(routing_case, change):
    case = routing_case
    gpu = worker(case, "gpu")
    task_id = queued(case)
    original = task_snapshot(case, task_id)
    item = delivery(case, gpu, task_id, filtered=True)
    assert item is not None
    if change == "draining":
        case.remote.begin_drain()
    elif change == "worker_stale":
        with case.db.transaction(immediate=True) as session:
            session.get(AsrTargetWorkerSession, gpu.session_id).heartbeat_at = (
                case.clock[0] - timedelta(seconds=case.settings.runtime_stale_seconds + 1))
    else:
        case.clock[0] += timedelta(seconds=60)
        # 保留消息领取权，单独验证资源到期而非通知可见期拒绝。
        with case.db.transaction(immediate=True) as session:
            session.get(NotificationRecord, item.notification_id).visible_at = case.clock[0] + timedelta(seconds=30)
        gpu.target.report_worker(gpu.session_id, "ready", True, now=case.clock[0])
    assert gpu.executor.acquire(item).decision == "busy"
    assert_unclaimed(case, task_id, original)


def test_preparation_refusal_cannot_start_attempt_or_reserve(routing_case, monkeypatch):
    case = routing_case
    gpu = worker(case, "gpu")
    task_id = queued(case)
    original = task_snapshot(case, task_id)
    calls = []
    can_claim = gpu.executor.remote_service.can_claim

    def permitted(tx, worker_id):
        assert can_claim(tx, worker_id)
        calls.append("can_claim")
        return True

    def refused(tx, worker_id):
        assert calls == ["can_claim"]
        assert tx.task.attempt == 0 and tx.task.lease_token is None
        calls.append("prepare_claim")
        return False

    monkeypatch.setattr(gpu.executor.remote_service, "can_claim", permitted)
    monkeypatch.setattr(gpu.executor.remote_service, "prepare_claim", refused)
    _, decision = acquire(case, gpu, task_id)
    assert decision.decision == "busy" and calls == ["can_claim", "prepare_claim"]
    assert_unclaimed(case, task_id, original)


def test_reserve_failure_rolls_back_route_attempt_and_call_together(routing_case, monkeypatch):
    case = routing_case
    gpu = worker(case, "gpu")
    task_id = queued(case)
    before = task_snapshot(case, task_id)

    def fail_after_start(tx):
        assert tx.task.attempt == 1 and tx.task.lease_token
        assert tx.session.get(AsrTaskRoute, task_id).selected_catalog_revision_id == gpu.revision_id
        raise AppError("ISOLATED_CAPACITY_RACE", "隔离许可失败", 409)

    monkeypatch.setattr(gpu.executor.remote_service, "reserve", fail_after_start)
    with pytest.raises(AppError, match="隔离许可失败"):
        acquire(case, gpu, task_id)
    assert_unclaimed(case, task_id, before)
    with case.db.session_factory() as session:
        assert session.get(RemoteAsrCall, task_id).state == "queued"


def test_cancel_before_claim_preserves_unselected_route_and_creates_no_attempt(routing_case):
    case = routing_case
    gpu = worker(case, "gpu")
    task_id = queued(case)
    item = delivery(case, gpu, task_id)
    assert gpu.executor.request_cancel(task_id, "local-user") == "cancelled"
    assert gpu.executor.acquire(item).decision == "terminal_or_stale"
    assert route_snapshot(case, task_id) == (None, None, None)
    with case.db.session_factory() as session:
        assert session.get(TaskRecord, task_id).attempt == 0
        assert session.get(AsrCallPermit, task_id) is None
        assert session.get(RemoteAsrCall, task_id).state == "released"


def test_cancel_and_routed_claim_share_original_transaction_boundary(routing_case):
    case = routing_case
    gpu = worker(case, "gpu")
    task_id = queued(case)
    item = delivery(case, gpu, task_id)
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=5)
        return gpu.executor.acquire(item).decision

    def cancel():
        barrier.wait(timeout=5)
        try:
            return gpu.executor.request_cancel(task_id, "local-user")
        except AppError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_claim, future_cancel = pool.submit(claim), pool.submit(cancel)
        outcomes = future_claim.result(), future_cancel.result()
    assert outcomes in {("acquired", "REMOTE_ASR_CANCEL_UNSUPPORTED"), ("terminal_or_stale", "cancelled")}
    with case.db.session_factory() as session:
        task = session.get(TaskRecord, task_id)
        call = session.get(RemoteAsrCall, task_id)
        route = session.get(AsrTaskRoute, task_id)
        assert (task.status, task.attempt, call.state) in {("running", 1, "held"), ("cancelled", 0, "released")}
        assert (route.selected_catalog_revision_id == gpu.revision_id) == (task.status == "running")


def test_waiting_deadline_is_not_extended_and_expiry_creates_no_attempt(routing_case):
    case = routing_case
    gpu = worker(case, "gpu")
    task_id = queued(case)
    with case.db.transaction(immediate=True) as session:
        session.get(TaskRecord, task_id).queue_deadline_at = case.clock[0] + timedelta(seconds=5)
    original = task_snapshot(case, task_id)
    item = delivery(case, gpu, task_id)
    case.clock[0] += timedelta(seconds=5)
    assert gpu.executor.acquire(item).decision == "terminal_or_stale"
    after = task_snapshot(case, task_id)
    assert after["queue_deadline_at"] == original["queue_deadline_at"]
    assert after["total_deadline_at"] == original["total_deadline_at"]
    assert after["status"] == "failed" and after["attempt"] == 0
    assert route_snapshot(case, task_id) == (None, None, None)
    with case.db.session_factory() as session:
        assert session.get(RemoteAsrCall, task_id).state == "released"
        assert session.get(AsrCallPermit, task_id) is None


def test_unknown_result_survives_new_executor_without_cpu_reexecution(routing_case):
    case = routing_case
    gpu, cpu = worker(case, "gpu"), worker(case, "cpu")
    task_id = queued(case)
    item, first = acquire(case, gpu, task_id)
    assert first.decision == "acquired"
    with pytest.raises(AppError) as caught:
        gpu.executor.request_cancel(task_id, "local-user")
    assert caught.value.code == "REMOTE_ASR_CANCEL_UNSUPPORTED"
    assert gpu.executor.commit_failure(first.lease, FailureInfo(code="WORKER_CHILD_EXITED", message="隔离退出"))
    route = route_snapshot(case, task_id)
    before = task_snapshot(case, task_id)
    assert before["attempt"] == 1 and before["next_retry_at"] is None
    case.db.dispose()
    restarted = ExecutionService(case.db, cpu.settings, asr_profile="remote",
        remote_target_id=cpu.name, remote_pooled=True)
    assert restarted.acquire(item).decision == "terminal_or_stale"
    assert not restarted.commit_result(first.lease, fixture_result())
    assert task_snapshot(case, task_id) == before
    assert route_snapshot(case, task_id) == route
    with case.db.session_factory() as session:
        assert session.get(RemoteAsrCall, task_id).state == "uncertain"
        permit = session.get(AsrCallPermit, task_id)
        assert permit.state == "uncertain" and permit.worker_session_id == gpu.session_id
        assert len(session.scalars(select(ExecutionAttempt)).all()) == 1


@pytest.mark.parametrize("change", [
    {"audio_duration_seconds": 0.2}, {"language_requested": "zh"},
    {"engine_metadata": AsrEngineMetadata(adapter_key="whisper-local")},
])
def test_routed_result_retains_audio_language_and_plugin_validation(routing_case, change):
    case = routing_case
    gpu = worker(case, "gpu")
    task_id = queued(case)
    _, selected = acquire(case, gpu, task_id)
    assert selected.decision == "acquired"
    assert not gpu.executor.commit_result(selected.lease, fixture_result(**change))
    with case.db.session_factory() as session:
        assert session.get(TaskRecord, task_id).status == "running"
        assert session.get(RemoteAsrCall, task_id).state == "held"


def test_legacy_queued_fingerprint_is_not_turned_into_a_routed_task(routing_case):
    case = routing_case
    task_id = seed_queued(case)
    before = task_snapshot(case, task_id)
    gpu = worker(case, "gpu")
    _, decision = acquire(case, gpu, task_id)
    assert decision.decision == "busy"
    assert task_snapshot(case, task_id) == before
    with case.db.session_factory() as session:
        assert session.get(AsrTaskRoute, task_id) is None
        assert session.get(RemoteAsrCall, task_id).config_fingerprint == config_fingerprint(case.settings)
        assert session.get(AsrCallPermit, task_id) is None


class PlannedSlot:
    """只验证池分派与排空接线，不启动模型子进程。"""

    def __init__(self, database, settings, logger, **arguments):
        self.settings, self.arguments = settings, arguments
        self.session_id = uuid4().hex
        self._owns_lock = self.draining = self.model_ready = False
        self.state, self.lease = "starting", None
        self.tick = Mock()

    def start(self):
        self._owns_lock = True

    @property
    def drained(self):
        return self.draining and self.lease is None

    def request_drain(self):
        self.draining = True

    def close(self):
        self._owns_lock = False
        return True


def test_pool_uses_frozen_candidate_settings_and_drains_every_target(routing_case):
    case = routing_case
    remote, runtime = Mock(), Mock()
    pool = RemotePoolService(case.db, case.root_settings, logging.getLogger("isolated-routing-pool"),
        worker_factory=PlannedSlot, remote_service=remote, runtime=runtime)
    try:
        pool.start()
        assert len(pool.workers) == 2
        assert {item.arguments["remote_target_id"] for item in pool.workers} == set(case.records)
        assert {item.arguments["remote_slot_index"] for item in pool.workers} == {0}
        for item in pool.workers:
            target = case.records[item.arguments["remote_target_id"]]
            assert item.settings.remote_asr_base_url == target.settings.remote_asr_base_url
            assert item.settings.remote_asr_model_version is None
            assert item.settings.remote_asr_routing_revision == case.route_id
            assert item.arguments["remote_pooled"] is True
        remote.pool_limit.assert_not_called()
        pool.workers[0].lease = object()
        pool.request_drain()
        assert all(item.draining for item in pool.workers)
        assert not pool.close()
        assert all(item._owns_lock for item in pool.workers)
        pool.workers[0].lease = None
        assert pool.close()
        with FileLock(pool._lock.lock_file, timeout=0):
            pass
    finally:
        for item in pool.workers:
            item.lease = None
        assert pool.close()


@pytest.mark.parametrize("count", [0, 9])
def test_pool_rejects_out_of_range_routed_total_before_creating_any_slot(routing_case, monkeypatch, count):
    case = routing_case
    plans = [dict(settings=case.root_settings, target_id="fixture", slot_index=index) for index in range(count)]
    monkeypatch.setattr(AsrRoutingService, "worker_plans", lambda self: plans)
    factory = Mock()
    pool = RemotePoolService(case.db, case.root_settings, logging.getLogger("isolated-routing-bound"),
        worker_factory=factory, remote_service=Mock(), runtime=Mock())
    try:
        with pytest.raises(AppError) as caught:
            pool.start()
        assert caught.value.code == "ASR_POOL_LIMIT_INVALID"
        factory.assert_not_called()
    finally:
        assert pool.close()
