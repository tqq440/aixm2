"""公共执行隔离验收；只有受控内存回调，不调用任何真实模型。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4
from time import monotonic
from threading import Event
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import func, select, text, event

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.config.vision.model_catalog import MODEL_SHA256
from backend.data.database.database import database_now
from backend.data.invocations.invocation_models import InferenceInvocation, InferenceTaskEvent, InferenceReservationItem
from backend.data.invocations.invocation_repository import InvocationRepository
from backend.data.catalog.catalog_models import InferenceTargetCheck
from backend.data.tasks.task_models import TaskRecord
from backend.data.llm.online_models import OnlineRequestRecord
from backend.data.asr.remote_models import RemoteAsrCall
from backend.modules.api.app import close_app, create_app
from backend.schema.asr.asr_schema import TranscriptionResult
from backend.schema.catalog.service_schema import SaveService, SaveResourceGroup, CheckTarget, ActivateService
from backend.schema.invocations.invocation_schema import ApplicationBudget, InvocationBinding, EventQuery, CpuProfileRegistration, OnlineCompletion
from backend.schema.llm.online_schema import OnlineRequestInput
from backend.schema.vision.detection_schema import DetectionResult
from backend.schema.catalog.service_schema import ServiceDefinition
from backend.service.execution.execution_service import ExecutionService
from backend.service.recovery.recovery_service import RecoveryService
from backend.utils.time.time_utils import as_utc
from backend.utils.json.json_utils import canonical_json_sha256
from backend.service.invocations.target_policy import target_identity


@pytest.fixture
def case(tmp_path):
    settings = AppSettings(database_path=tmp_path / "db/p.sqlite3", log_dir=tmp_path / "logs",
                           upload_dir=tmp_path / "uploads", common_execution_enabled=True)
    migration = Config(str(PROJECT_ROOT / "alembic.ini")); migration.attributes["settings"] = settings
    command.upgrade(migration, "head")
    app = create_app(settings)
    value = Case(app, settings, migration)
    yield value
    close_app(app)


class Case:
    def __init__(self, app, settings, migration):
        self.app, self.settings, self.migration = app, settings, migration
        self.db, self.core = app.extensions["database"], app.extensions["invocation_service"]
        self.catalog = app.extensions["service_catalog"]
        self.catalog.probe = lambda *_: {"success": True, "errorCode": None, "durationMs": 1,
            "phase": "health", "inferenceVerified": False, "actualDevice": None, "message": "隔离替身"}
        self.definitions, self.entries, self.serial = {}, {}, 0

    def group(self, name="shared", limit=1, version=None):
        return self.catalog.save_group(name, SaveResourceGroup.model_validate({"expectedVersion": version,
            "config": {"groupId": name, "name": name, "concurrencyLimit": limit, "capacityEvidence": "isolated fixture"}}))

    def service(self, name="asr-main", kind="asr", *, groups=("shared",), limit=1, policy="fixed", targets=None, version=None):
        model, model_version = ("yolo-general", "yolov5n-v7.0-onnx") if kind == "vision" else (kind + "-fixture", "fixture-v1")
        target = {"targetId": "primary", "name": "fixture", "baseUrl": "http://127.0.0.1:8010",
            "invokePath": "/" + kind, "healthPath": "/health", "upstreamModel": "fixture",
            "concurrencyLimit": limit, "capacityEvidence": "isolated fixture", "resourceGroupIds": list(groups)}
        config = {"serviceId": name, "name": name, "kind": kind, "modelId": model, "modelVersion": model_version,
            "protocol": {"asr": "remote-v2", "vision": "vision-json-v1", "llm": "chat-completions-sse"}[kind],
            "invocationMode": "stream" if kind == "llm" else "sync", "policy": policy, "targets": targets or [target], "maxFileBytes": 1024}
        entry = self.catalog.save(name, SaveService.model_validate({"config": config, "expectedVersion": version}))
        for item in entry["config"]["targets"]:
            if item["enabled"]:
                self.catalog.check(name, item["targetId"], CheckTarget(expected_version=entry["version"], revision=entry["draftRevision"]))
        entry = self.catalog.activate(name, ActivateService(expected_version=entry["version"], revision=entry["draftRevision"], enabled=True))
        self.definitions[name], self.entries[name] = config, entry
        return entry

    def budget(self, app="local-user", limit=1, services=None, version=None):
        return self.core.configure_application(ApplicationBudget(application_id=app, concurrency_limit=limit,
            allowed_services=services or list(self.definitions), expected_version=version))

    def task(self, service="asr-main", app="local-user", *, bind=True):
        config = self.definitions[service]; self.serial += 1
        task_id = uuid4().hex
        with self.db.transaction(immediate=True) as session:
            now = database_now(session)
            row = TaskRecord(task_id=task_id, caller_id=app, task_type=config["kind"], model_id=config["modelId"],
                model_version=config["modelVersion"], parameters={"language": "zh"} if config["kind"] == "asr" else {
                    "confidence_threshold": 0.25, "iou_threshold": 0.45, "max_detections": 100},
                input_ref=uuid4().hex, input_filename="never-read.wav", input_sha256="a" * 64, input_size_bytes=100,
                input_media_type="audio" if config["kind"] == "asr" else "image",
                audio_duration_seconds=5 if config["kind"] == "asr" else None,
                image_width=64 if config["kind"] == "vision" else None, image_height=64 if config["kind"] == "vision" else None,
                status="queued", attempt=0, max_attempts=2, queue_key=("common." + canonical_json_sha256({
                    "service": service, "model": config["modelId"]})) if bind else "fixture." + service,
                queue_deadline_at=now + timedelta(seconds=60), total_deadline_at=now + timedelta(seconds=120),
                created_at=now - timedelta(seconds=60) + timedelta(milliseconds=self.serial), updated_at=now)
            session.add(row); session.flush()
            if bind:
                invocation = self.core.bind_in_transaction(session, InvocationBinding(application_id=app, service_id=service,
                    owner_id=task_id, owner_kind="task"))
                return task_id, invocation.invocation_id
        return task_id, None

    def online(self, service="llm-main", app="local-user", request_id=None):
        config = self.definitions[service]
        request = OnlineRequestInput(online_request_id=request_id or uuid4().hex, model_id=config["modelId"],
            model_version=config["modelVersion"], messages=[{"role": "user", "content": "NEVER_LOG_THIS_BODY"}])
        return self.core.accept_online(app, service, request), request

    def events(self, task, app="local-user"):
        return self.core.events("task", task, EventQuery(limit=200), application_id=app)["items"]

    def expire(self, invocation_id):
        with self.db.transaction(immediate=True) as session:
            row = session.get(InferenceInvocation, invocation_id)
            row.lease_expires_at = database_now(session) - timedelta(seconds=1)


def asr_result():
    return TranscriptionResult(text="NEVER_LOG_THIS_RESULT", segments=[], audio_duration_seconds=5, language_requested="zh")


def setup_asr(case, limit=1):
    case.group(limit=limit); case.service(limit=limit); case.budget(limit=limit)


def test_atomic_binding_and_lifecycle_rollback(case):
    setup_asr(case)
    task, invocation = case.task()
    assert case.core.bind(InvocationBinding(application_id="local-user", service_id="asr-main", owner_id=task, owner_kind="task"))["invocationId"] == invocation
    before = case.events(task)
    assert {e["eventType"] for e in before} == {"task.created", "invocation.accepted"}
    with pytest.raises(RuntimeError):
        with case.db.transaction(immediate=True) as session:
            session.get(TaskRecord, task).status = "failed"
            session.flush()
            raise RuntimeError("rollback")
    assert case.events(task) == before
    assert case.core.claim_round("worker")[0].invocation_id == invocation
    assert not case.core.claim_round("worker")
    assert len(case.events(task)) > len(before)


def test_dispatch_runs_outside_transaction_once_and_saves_before_release(case):
    setup_asr(case)
    task, invocation = case.task()
    lease = case.core.claim_round("worker")[0]
    calls = []
    def sender(config, target, active):
        with case.db.transaction(immediate=True) as session:
            assert session.get(InferenceInvocation, invocation).state == "sent"
            assert InvocationRepository.occupied(session, "group:shared") == 1
        assert active == lease and config.service_id == "asr-main"
        calls.append(target.target_id)
        return asr_result()
    assert case.core.dispatch(lease, sender)
    assert not case.core.dispatch(lease, sender)
    assert case.core.complete(lease, asr_result())
    assert calls == ["primary"]
    with case.db.transaction() as session:
        row = session.get(TaskRecord, task)
        assert row.status == "succeeded" and row.result_json["text"] == "NEVER_LOG_THIS_RESULT"
        assert InvocationRepository.occupied(session, "group:shared") == 0
    events = case.events(task)
    assert sum(e["eventType"] == "invocation.capacity_released" for e in events) == 1
    assert "NEVER_LOG_THIS_RESULT" not in str(events)
    assignment = next(e["details"] for e in events if e["eventType"] == "invocation.target_selected")
    assert assignment["assignedDevice"] == "unknown" and assignment["actualDevice"] is None


@pytest.mark.parametrize("sent", [False, True])
def test_expiry_never_requeues_sent_requests_and_does_not_release_unknown(case, sent):
    setup_asr(case)
    task, invocation = case.task()
    lease = case.core.claim_round("worker")[0]
    if sent:
        assert case.core.mark_sent(lease) and not case.core.mark_sent(lease)
    case.expire(invocation)
    assert RecoveryService(case.db, case.settings).tick() == 1
    assert not case.core.complete(lease, asr_result())
    state = case.core.get(invocation)
    assert state["permitState"] == ("uncertain" if sent else "released")
    assert state["outcome"] == ("unknown" if sent else "expired")
    assert not case.core.claim_round("replacement")
    assert case.core.recover() == 0
    with case.db.transaction() as session:
        assert session.get(TaskRecord, task).attempt == 1
        assert InvocationRepository.occupied(session, "group:shared") == int(sent)


def test_late_terminal_is_explicit_idempotent_and_preserves_timeout_history(case):
    setup_asr(case)
    task, invocation = case.task(); lease = case.core.claim_round("worker")[0]
    assert case.core.mark_sent(lease); case.expire(invocation); case.core.recover()
    assert case.core.resolve_unknown(invocation, "local-user", outcome="succeeded", evidence="SECRET_DO_NOT_PERSIST")["resolvedOutcome"] == "succeeded"
    case.core.resolve_unknown(invocation, "local-user", outcome="succeeded", evidence="same terminal")
    with pytest.raises(AppError):
        case.core.resolve_unknown(invocation, "local-user", outcome="failed", evidence="conflict")
    with case.db.transaction() as session:
        owner = session.get(TaskRecord, task)
        assert owner.status == "failed" and owner.result_json is None
        assert InvocationRepository.occupied(session, "group:shared") == 0
    events = case.events(task)
    assert "SECRET_DO_NOT_PERSIST" not in str(events)
    assert sum(e["eventType"] == "invocation.capacity_released" for e in events) == 1


@pytest.mark.parametrize("sent", [False, True])
def test_cancel_uses_common_rules_in_legacy_task_api(case, sent):
    setup_asr(case)
    task, invocation = case.task(); lease = case.core.claim_round("worker")[0]
    if sent: case.core.mark_sent(lease)
    old = ExecutionService(case.db, case.settings)
    assert old.request_cancel(task, "local-user") == ("cancel_requested" if sent else "cancelled")
    assert case.core.get(invocation)["permitState"] == ("held" if sent else "released")
    if sent:
        case.expire(invocation); case.core.recover()
        assert case.core.get(invocation)["permitState"] == "uncertain"
    else:
        assert not case.core.mark_sent(lease)


def test_multi_group_atomic_admission_and_cross_kind_race(case):
    case.group("gpu", 1); case.group("host", 2)
    for name, kind in [("asr-main", "asr"), ("vision-main", "vision"), ("llm-main", "llm")]:
        case.service(name, kind, groups=("gpu", "host"))
    case.budget(limit=3)
    case.task(); case.task("vision-main"); online, _ = case.online()
    def claim_offline(): return case.core.claim_round("worker")
    def claim_online(): return case.core.claim_online(online["invocationId"], online["workerId"])
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(claim_offline), pool.submit(claim_offline), pool.submit(claim_online)]
        leases = [lease for future in futures for lease in (future.result() if isinstance(future.result(), list) else [future.result()]) if lease]
    assert len(leases) == 1
    with case.db.transaction() as session:
        assert InvocationRepository.occupied(session, "group:gpu") == 1
        assert InvocationRepository.occupied(session, "group:host") == 1
        assert session.scalar(select(func.count()).select_from(InferenceReservationItem)) == 5


def test_blocked_group_does_not_reserve_any_other_resource(case):
    case.group("shared", 1); case.group("free", 2)
    case.service(); case.service("vision-main", "vision", groups=("free", "shared")); case.budget(limit=3)
    case.task(); case.task("vision-main")
    assert len(case.core.claim_round("worker")) == 1
    with case.db.transaction() as session:
        assert InvocationRepository.occupied(session, "group:free") == 0


def test_application_limit_and_round_robin_skip_ineligible_fifo(case):
    case.group(limit=4); case.service(limit=4); case.service("vision-main", "vision", limit=4)
    case.budget("blocked", limit=1); case.budget("available", limit=4)
    first, _ = case.task(app="blocked")
    assert len(case.core.claim_round("worker")) == 1
    blocked, _ = case.task(app="blocked")
    second, _ = case.task(app="available"); third, _ = case.task(app="available")
    vision, _ = case.task("vision-main", app="available")
    leases = case.core.claim_round("worker")
    with case.db.transaction() as session:
        owners = {session.get(InferenceInvocation, lease.invocation_id).task_id for lease in leases}
    assert owners == {second, vision} and third not in owners and blocked not in owners
    assert len(case.core.claim_round("worker")) == 1


def test_revision_pinning_and_endpoint_alias_does_not_create_extra_capacity(case):
    setup_asr(case)
    _, old_id = case.task()
    entry = case.entries["asr-main"]
    config = dict(case.definitions["asr-main"])
    config["targets"] = [{**config["targets"][0], "baseUrl": "http://127.0.0.1:8020"}]
    new = case.catalog.save("asr-main", SaveService.model_validate({"config": config, "expectedVersion": entry["version"]}))
    case.catalog.check("asr-main", "primary", CheckTarget(expected_version=new["version"], revision=new["draftRevision"]))
    new = case.catalog.activate("asr-main", ActivateService(expected_version=new["version"], revision=new["draftRevision"], enabled=True))
    assert case.core.get(old_id)["serviceRevision"] == entry["activeRevision"]
    assert case.core.claim_round("worker")[0].service_revision == entry["activeRevision"]
    _, new_id = case.task(); assert case.core.get(new_id)["serviceRevision"] == new["activeRevision"]
    assert not case.core.claim_round("worker")  # 旧修订仍占同一共享组。


def test_same_endpoint_different_services_without_group_share_target_limit(case):
    case.service(groups=()); case.service("alias", groups=()); case.budget(limit=3)
    case.task(); case.task("alias")
    assert len(case.core.claim_round("worker")) == 1


def test_renewal_and_unchanged_wait_reason_do_not_spam_events(case):
    setup_asr(case); first, _ = case.task(); lease = case.core.claim_round("worker")[0]
    waiting, _ = case.task(); assert not case.core.claim_round("worker")
    first_events, waiting_events = case.events(first), case.events(waiting)
    assert case.core.renew(lease) and not case.core.claim_round("worker")
    assert case.events(first) == first_events and case.events(waiting) == waiting_events


def test_online_short_wait_metadata_only_and_shared_capacity(case):
    case.group(); case.service(); case.service("llm-main", "llm"); case.budget(limit=2)
    _, task_id = case.task(); lease = case.core.claim_round("worker")[0]
    online, request = case.online()
    assert case.core.claim_online(online["invocationId"], online["workerId"]) is None
    assert case.core.accept_online("local-user", "llm-main", request)["invocationId"] == online["invocationId"]
    assert case.core.fail(lease, confirmed_not_sent=True)
    online_lease = case.core.claim_online(online["invocationId"], online["workerId"])
    assert online_lease and case.core.mark_sent(online_lease)
    assert case.core.complete(online_lease, OnlineCompletion(output_characters=12, finish_reason="stop"))
    events = case.core.events("online", request.online_request_id, EventQuery(), application_id="local-user")
    assert "NEVER_LOG_THIS_BODY" not in str(events)
    with case.db.transaction() as session:
        row = session.get(OnlineRequestRecord, request.online_request_id)
        assert row.state == "completed" and row.output_characters == 12
        assert len(row.owner_id) == 32


@pytest.mark.parametrize("fault", ["exception", "invalid_result"])
def test_adapter_failure_after_send_is_uncertain(case, fault):
    setup_asr(case); _, invocation = case.task(); lease = case.core.claim_round("worker")[0]
    def sender(*_):
        if fault == "exception": raise RuntimeError("PRIVATE_SECRET")
        return asr_result().model_copy(update={"audio_duration_seconds": 6})
    if fault == "exception":
        with pytest.raises(AppError) as error: case.core.dispatch(lease, sender)
        assert "PRIVATE_SECRET" not in str(error.value)
    else:
        assert not case.core.dispatch(lease, sender)
    assert case.core.get(invocation)["permitState"] == "uncertain"


@pytest.mark.parametrize("kind", ["task", "remote", "online"])
def test_legacy_unmapped_occupancy_blocks_common(case, kind):
    setup_asr(case); _, invocation = case.task()
    legacy, _ = case.task(bind=False)
    with case.db.transaction(immediate=True) as session:
        now = database_now(session)
        if kind == "task": session.get(TaskRecord, legacy).status = "running"
        elif kind == "remote":
            session.add(RemoteAsrCall(task_id=legacy, profile_id="legacy", config_fingerprint="b" * 64,
                request_id=legacy, state="uncertain", created_at=now))
        else:
            session.add(OnlineRequestRecord(online_request_id=uuid4().hex, caller_id="local-user", model_id="llm",
                model_version="v1", owner_id=uuid4().hex, request_fingerprint="c" * 64,
                state="failed", permit_state="uncertain", accepted_at=now, updated_at=now,
                deadline_at=now, lease_expires_at=now, output_characters=0, cancel_requested=False, cancellation_confirmed=False))
    assert not case.core.claim_round("worker")
    assert case.core.get(invocation)["reason"] == "LEGACY_OCCUPANCY_UNMAPPED"


def test_wrong_lease_and_already_terminal_owner_are_not_overwritten(case):
    setup_asr(case); task, invocation = case.task()
    with case.db.transaction(immediate=True) as session:
        row = session.get(TaskRecord, task); row.status = "failed"; row.error_json = {"code": "ORIGINAL"}
    assert not case.core.claim_round("worker")
    with case.db.transaction() as session:
        assert session.get(TaskRecord, task).error_json == {"code": "ORIGINAL"}
    task, invocation = case.task(); lease = case.core.claim_round("worker")[0]
    stale = lease.model_copy(update={"lease_token": uuid4().hex})
    assert not case.core.mark_sent(stale) and not case.core.complete(stale, asr_result())
    assert case.core.mark_sent(lease) and case.core.complete(lease, asr_result())


def test_management_get_is_readonly_paginated_isolated_and_local(case):
    setup_asr(case); task, invocation = case.task(); case.core.claim_round("worker")
    client = case.app.test_client(); root = "/api/admin/common-execution"
    with case.db.transaction() as session:
        count = session.scalar(select(func.count()).select_from(InferenceTaskEvent))
    for _ in range(3):
        assert client.get(root + "/capacity").status_code == 200
        assert client.get(root + "/invocations/" + invocation).status_code == 200
    url = root + "/applications/local-user/tasks/" + task + "/events"
    page = client.get(url + "?limit=1").get_json()["data"]
    assert len(page["items"]) == 1 and page["hasMore"]
    following = client.get(url + "?afterSequence=" + str(page["nextSequence"])).get_json()["data"]
    assert following["items"][0]["sequence"] > page["nextSequence"]
    assert client.get(url.replace("local-user", "other-app")).status_code == 404
    assert client.get(url + "?limit=201").status_code == 422
    assert client.get(root + "/capacity", environ_overrides={"REMOTE_ADDR": "10.0.0.2"}).status_code == 403
    with case.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(InferenceTaskEvent)) == count


def test_populated_downgrade_refused_without_losing_tasks(case):
    setup_asr(case); task, _ = case.task()
    with pytest.raises(RuntimeError): command.downgrade(case.migration, "20261020_0015")
    with case.db.transaction() as session:
        assert session.get(TaskRecord, task)
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(case.migration).get_current_head()


def device_targets():
    return [{"targetId": kind, "name": kind, "baseUrl": "http://127.0.0.1:" + port,
        "invokePath": "/invoke", "healthPath": "/health", "upstreamModel": "fixture",
        "resourceGroupIds": [kind], "deviceKind": kind, "deviceId": "0" if kind == "gpu" else "cpu",
        "deviceEvidence": "isolated binding", "compatibilityEvidence": "isolated same model",
        "capacityEvidence": "isolated capacity"} for kind, port in [("gpu", "8011"), ("cpu", "8012")]]


def cpu_profile(case, *, stale=False, slow=False):
    entry = case.entries["asr-main"]
    target = next(t for t in ServiceDefinition.model_validate(entry["config"]).targets if t.target_id == "cpu")
    with case.db.transaction() as session: now = as_utc(database_now(session))
    return CpuProfileRegistration.model_validate({"serviceRevision": entry["activeRevision"], "targetId": "cpu",
        "cpuBudgetSeconds": 10, "profile": {"targetRevision": target_identity(target), "evidenceKind": "isolated",
            "evidenceReference": "fixture performance", "measuredAt": now - timedelta(hours=2) if stale else now,
            "validForSeconds": 3600.0, "sampleCount": 3, "minAudioSeconds": 1.0, "maxAudioSeconds": 20.0,
            "maxTestedConcurrency": 1, "fixedOverheadSeconds": 1.0, "secondsPerAudioSecond": 5.0 if slow else 0.2,
            "safetyFactor": 1.5, "queueWaitUpperBoundSeconds": 0.0}})


@pytest.mark.parametrize("profile_state", ["valid", "missing", "expired", "slow"])
def test_gpu_preferred_cpu_requires_valid_measured_budget(case, profile_state):
    case.group("gpu"); case.group("cpu")
    case.service(policy="gpu_preferred", targets=device_targets()); case.budget(limit=3)
    case.task(); gpu = case.core.claim_round("worker")[0]
    assert gpu.target_id == "gpu"
    if profile_state != "missing":
        profile = cpu_profile(case, stale=profile_state == "expired", slow=profile_state == "slow")
        assert case.core.register_cpu_profile(profile)["registered"]
        case.core.register_cpu_profile(profile)
        with pytest.raises(AppError):
            case.core.register_cpu_profile(profile.model_copy(update={"cpu_budget_seconds": 9}))
    _, invocation = case.task()
    leases = case.core.claim_round("worker")
    if profile_state == "valid":
        assert len(leases) == 1 and leases[0].target_id == "cpu"
    else:
        assert not leases and case.core.get(invocation)["reason"] == "CPU_PERFORMANCE_UNCONFIRMED"


@pytest.mark.parametrize("health", ["stale", "failed", "future"])
def test_unconfirmed_gpu_health_cannot_trigger_cpu_fallback(case, health):
    case.group("gpu"); case.group("cpu")
    entry = case.service(policy="gpu_preferred", targets=device_targets()); case.budget(limit=3)
    case.core.register_cpu_profile(cpu_profile(case))
    with case.db.transaction(immediate=True) as session:
        check = session.scalar(select(InferenceTargetCheck).where(InferenceTargetCheck.revision == entry["activeRevision"],
                                                                 InferenceTargetCheck.target_id == "gpu"))
        now = database_now(session)
        if health == "failed": check.success = False
        else: check.checked_at = now + timedelta(minutes=1) if health == "future" else now - timedelta(minutes=6)
    _, invocation = case.task()
    assert not case.core.claim_round("worker")
    assert case.core.get(invocation)["reason"] == "GPU_STATE_UNCONFIRMED"


@pytest.mark.parametrize("kind", ["vision", "llm"])
def test_non_asr_automatic_cpu_fallback_not_claimed(case, kind):
    case.group("gpu"); case.group("cpu")
    name = kind + "-main"
    case.service(name, kind, policy="gpu_preferred", targets=device_targets()); case.budget(limit=3)
    if kind == "vision":
        case.task(name); assert case.core.claim_round("worker")[0].target_id == "gpu"
        _, invocation = case.task(name); assert not case.core.claim_round("worker")
    else:
        first, _ = case.online(); second, _ = case.online()
        assert case.core.claim_online(first["invocationId"], first["workerId"]).target_id == "gpu"
        invocation = second["invocationId"]
        assert case.core.claim_online(invocation, second["workerId"]) is None
    assert case.core.get(invocation)["reason"] == "CPU_AUTOMATIC_FALLBACK_UNAVAILABLE"


def test_failed_event_insert_rolls_back_result_and_capacity_together(case):
    setup_asr(case); task, invocation = case.task(); lease = case.core.claim_round("worker")[0]
    case.core.mark_sent(lease)
    def reject(_connection, _cursor, statement, *_args):
        if "INSERT INTO inference_task_events" in statement: raise RuntimeError("isolated write failure")
    event.listen(case.db.engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError): case.core.complete(lease, asr_result())
    finally:
        event.remove(case.db.engine, "before_cursor_execute", reject)
    with case.db.transaction() as session:
        assert session.get(TaskRecord, task).result_json is None
        assert session.get(TaskRecord, task).status == "running"
        assert session.get(InferenceInvocation, invocation).permit_state == "held"
    assert case.core.complete(lease, asr_result())


def test_vision_result_is_checked_and_persisted_in_original_task(case):
    case.group(); case.service("vision-main", "vision"); case.budget()
    task, invocation = case.task("vision-main"); lease = case.core.claim_round("worker")[0]
    case.core.mark_sent(lease)
    result = DetectionResult(model_sha256=MODEL_SHA256, image_width=64, image_height=64, detections=[],
                             runtime_version="isolated", load_seconds=0.1, inference_seconds=0.1, total_seconds=0.2)
    assert not case.core.complete(lease, result.model_copy(update={"image_width": 32}))
    assert case.core.complete(lease, result)
    with case.db.transaction() as session:
        assert session.get(TaskRecord, task).result_json["modelId"] == "yolo-general"


@pytest.mark.parametrize("disabled", [False, True])
def test_legacy_online_gate_respects_common_permits_even_when_flag_switched_off(case, disabled):
    setup_asr(case); case.task(); case.core.claim_round("worker")
    old = case.app.extensions["online_service"]
    if disabled: old.settings = case.settings.model_copy(update={"common_execution_enabled": False})
    request_id = uuid4().hex
    with case.db.transaction(immediate=True) as session:
        now = database_now(session)
        session.add(OnlineRequestRecord(online_request_id=request_id, caller_id="local-user", model_id="llm",
            model_version="v1", owner_id=old.owner_id, request_fingerprint="c" * 64,
            state="accepted", permit_state="waiting", accepted_at=now, updated_at=now,
            deadline_at=now + timedelta(seconds=60), lease_expires_at=now + timedelta(seconds=60),
            output_characters=0, cancel_requested=False, cancellation_confirmed=False))
    run = SimpleNamespace(record=SimpleNamespace(online_request_id=request_id), deadline=monotonic() + 60, cancel=Event())
    assert old._touch(run, acquire=True) is False


def test_online_legacy_get_does_not_reap_common_request(case):
    case.group(); case.service("llm-main", "llm"); case.budget()
    online, request = case.online(); lease = case.core.claim_online(online["invocationId"], online["workerId"])
    case.core.mark_sent(lease)
    with case.db.transaction(immediate=True) as session:
        session.get(OnlineRequestRecord, request.online_request_id).lease_expires_at = database_now(session) - timedelta(seconds=1)
    assert case.app.extensions["online_service"].get(request.online_request_id).state == "streaming"
    assert case.core.get(online["invocationId"])["permitState"] == "held"


def test_online_short_wait_expires_without_calling_sender(case):
    case.group(); case.service("llm-main", "llm"); case.budget()
    online, _ = case.online()
    with case.db.transaction(immediate=True) as session:
        row = session.get(InferenceInvocation, online["invocationId"])
        assert (row.queue_deadline_at - row.accepted_at).total_seconds() == 2
        row.queue_deadline_at = database_now(session) - timedelta(milliseconds=1)
    assert case.core.wait_online(online["invocationId"], online["workerId"]) is None
    assert case.core.get(online["invocationId"])["outcome"] == "expired"


def test_disabled_feature_works_with_unmigrated_0014_database(tmp_path):
    settings = AppSettings(database_path=tmp_path / "old.sqlite3", log_dir=tmp_path / "logs")
    migration = Config(str(PROJECT_ROOT / "alembic.ini")); migration.attributes["settings"] = settings
    command.upgrade(migration, "20261020_0014")
    app = create_app(settings)
    try:
        assert app.test_client().get("/api/admin/common-execution/capacity").status_code == 503
        assert app.extensions["online_service"].status().active_requests == 0
        assert RecoveryService(app.extensions["database"], settings).tick() == 0
        with app.extensions["database"].transaction() as session:
            assert not InvocationRepository.available(session)
            assert session.scalar(text("SELECT version_num FROM alembic_version")) == "20261020_0014"
    finally: close_app(app)


def test_enabled_feature_with_missing_migration_returns_clear_storage_error(tmp_path):
    settings = AppSettings(database_path=tmp_path / "empty.sqlite3", log_dir=tmp_path / "logs", common_execution_enabled=True)
    app = create_app(settings)
    try:
        response = app.test_client().get("/api/admin/common-execution/capacity")
        assert response.status_code == 503 and response.get_json()["error"]["code"] == "COMMON_EXECUTION_STORAGE_UNAVAILABLE"
    finally: close_app(app)


def test_old_queued_task_cannot_be_rebound_to_new_endpoint(case):
    setup_asr(case); task, _ = case.task(bind=False)
    with pytest.raises(AppError) as error:
        case.core.bind(InvocationBinding(application_id="local-user", service_id="asr-main", owner_id=task, owner_kind="task"))
    assert error.value.code == "LEGACY_TASK_NOT_REBOUND"


@pytest.mark.parametrize("disabled", [False, True])
def test_legacy_worker_gate_and_common_task_ownership(case, disabled):
    from backend.data.tasks.task_models import DispatchEvent
    from backend.data.broker.broker_models import NotificationRecord
    from backend.config.vision.model_catalog import QUEUE_KEY
    from backend.schema.execution.execution_schema import Delivery
    setup_asr(case); case.task(); active = case.core.claim_round("common-worker")[0]
    case.service("vision-main", "vision")
    task, _ = case.task("vision-main", bind=False)
    event_id, notification_id = uuid4().hex, uuid4().hex
    with case.db.transaction(immediate=True) as session:
        now = database_now(session); session.get(TaskRecord, task).queue_key = QUEUE_KEY
        session.add(DispatchEvent(event_id=event_id, task_id=task, queue_key=QUEUE_KEY,
                                 status="published", available_at=now, created_at=now))
        session.flush()
        session.add(NotificationRecord(notification_id=notification_id, event_id=event_id, task_id=task,
            queue_key=QUEUE_KEY, dispatch_version=1, execution_plan_version=1, status="inflight",
            delivery_token="delivery-token", delivery_owner="old-worker", visible_at=now + timedelta(seconds=60)))
    delivery = Delivery(notification_id=notification_id, task_id=task, worker_id="old-worker",
        delivery_token="delivery-token", dispatch_version=1, execution_plan_version=1, queue_key=QUEUE_KEY)
    settings = case.settings.model_copy(update={"common_execution_enabled": not disabled})
    worker = ExecutionService(case.db, settings, task_type="vision")
    assert worker.acquire(delivery).decision == "busy"
    assert case.core.fail(active, confirmed_not_sent=True)
    assert worker.acquire(delivery).decision == "acquired"


def test_capacity_view_lists_current_limits_and_unknown_occupancy(case):
    setup_asr(case, limit=2)
    case.task(); lease = case.core.claim_round("worker")[0]; case.core.mark_sent(lease)
    assert case.core.fail(lease)
    summary = case.core.capacity()
    shared = next(row for row in summary["resources"] if row["resourceKey"] == "group:shared")
    assert shared == {"resourceKey": "group:shared", "concurrencyLimit": 2, "held": 0, "uncertain": 1}
    assert not summary["legacyOccupancyUnmapped"]
