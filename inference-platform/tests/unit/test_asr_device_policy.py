"""纯离线规则验证；所有设备与性能资料均为隔离样例，不访问硬件。"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.schema.asr.device_policy_schema import DevicePolicyRequest
from backend.service.asr.device_policy_service import DevicePolicyService


NOW = datetime(2026, 10, 19, 8, tzinfo=timezone.utc)


def stamp(seconds=0):
    return (NOW + timedelta(seconds=seconds)).isoformat()


def performance(revision):
    return {"targetRevision": revision, "evidenceKind": "isolated",
        "evidenceReference": "unit fixture only", "measuredAt": stamp(), "validForSeconds": 3600,
        "sampleCount": 3, "minAudioSeconds": 1, "maxAudioSeconds": 10,
        "maxTestedConcurrency": 1, "fixedOverheadSeconds": 1,
        "secondsPerAudioSecond": 1, "safetyFactor": 2, "queueWaitUpperBoundSeconds": 3}


def target(name, kind, *, used=0, uncertain=0, limit=1, group=None):
    revision = name + "-revision"
    result = {"targetId": name, "targetRevision": revision, "compatibilityKey": "fixture-sensevoice",
        "deviceKind": kind, "deviceEvidence": "unit fixture only",
        "compatibilityEvidence": "unit fixture only", "evidenceKind": "isolated",
        "capacityGroupId": group or name + "-group", "limit": limit, "held": used,
        "uncertain": uncertain, "capacityKnown": True, "capacityObservedAt": stamp(),
        "capacityTtlSeconds": 30, "capacityEvidence": "unit fixture only",
        "healthState": "ready", "healthObservedAt": stamp(), "healthTtlSeconds": 30,
        "slotReady": True, "slotObservedAt": stamp(), "slotTtlSeconds": 30}
    if kind == "cpu":
        result.update(cpuSupported=True, cpuSupportEvidence="unit fixture only", cpuProfile=performance(revision))
    return result


def payload(*targets, configured=None):
    groups = {}
    for item in targets:
        key = item["capacityGroupId"]
        group = groups.setdefault(key, {"capacityGroupId": key, "limit": item["limit"],
            "held": 0, "uncertain": 0, "capacityKnown": True, "observedAt": stamp(),
            "ttlSeconds": 30, "capacityEvidence": "unit fixture only", "evidenceKind": "isolated"})
        group["held"] += item["held"]
        group["uncertain"] += item["uncertain"]
    return {"evaluatedAt": stamp(), "executionEnabled": False,
        "routingRevision": "fixture-route-v1", "compatibilityKey": "fixture-sensevoice",
        "gpuConfiguration": configured or ("configured" if any(item["deviceKind"] == "gpu" for item in targets) else "none"),
        "task": {"status": "queued", "attempt": 0, "hasPermit": False,
            "upstreamExecutionStarted": False, "audioSeconds": 5,
            "queueDeadlineAt": stamp(60), "totalDeadlineAt": stamp(120),
            "attemptBudgetSeconds": 60, "remoteBudgetSeconds": 60, "cpuBudgetSeconds": 30},
        "groups": list(groups.values()), "targets": list(targets)}


def evaluate(value):
    return DevicePolicyService.evaluate(DevicePolicyRequest.model_validate(value))


def test_gpu_preferred_even_when_cpu_is_available_and_fixture_cannot_claim_real_evidence():
    data = payload(target("cpu", "cpu"), target("gpu", "gpu"))
    before = deepcopy(data)
    result = evaluate(data)
    assert (result.action, result.target_id, result.device_kind) == ("select", "gpu", "gpu")
    assert result.mode == "preview" and result.execution_enabled is False
    assert result.evidence_kind == "isolated" and result.estimated_cpu_seconds is None
    assert data == before


def test_gpu_occupancy_ratio_precedes_identifier_and_ties_are_stable():
    data = payload(target("a", "gpu", used=1, limit=2), target("z", "gpu", used=1, limit=4))
    assert evaluate(data).target_id == "z"
    data = payload(target("z", "gpu"), target("a", "gpu"))
    assert evaluate(data).target_id == "a"
    data["targets"].reverse()
    assert evaluate(data).target_id == "a"


@pytest.mark.parametrize("unknown", [False, True])
def test_gpu_held_and_uncertain_both_count_and_new_task_can_use_independent_cpu(unknown):
    gpu = target("gpu", "gpu", used=int(not unknown), uncertain=int(unknown))
    result = evaluate(payload(gpu, target("cpu", "cpu")))
    assert result.target_id == "cpu"
    assert result.reason_code == "CPU_FALLBACK_CONFIRMED_GPU_FULL"
    assert (result.estimated_processing_seconds, result.estimated_queue_seconds,
            result.estimated_cpu_seconds) == (12, 3, 15)


def test_shared_group_consumption_prevents_alias_capacity_and_cpu_bypass():
    data = payload(target("gpu", "gpu", uncertain=1, group="shared"),
                   target("cpu-alias", "cpu", group="shared"))
    result = evaluate(data)
    assert result.action == "wait"
    assert all(item.reason_code == "CAPACITY_FULL" for item in result.candidates)
    assert data["groups"][0]["uncertain"] == 1


def test_group_full_due_to_other_target_still_counts_for_gpu():
    data = payload(target("gpu", "gpu"), target("cpu", "cpu"))
    data["groups"][0]["held"] = 1  # 该组中未列入候选集合的其他目标持有许可。
    assert evaluate(data).target_id == "cpu"


@pytest.mark.parametrize("field,value", [
    ("capacityKnown", False), ("capacityEvidence", None), ("deviceEvidence", None),
    ("compatibilityEvidence", None), ("healthState", "unknown"), ("healthState", "unavailable"),
    ("healthObservedAt", None), ("healthObservedAt", stamp(-31)),
    ("healthObservedAt", stamp(1)), ("capacityObservedAt", stamp(-31)),
    ("capacityObservedAt", stamp(1)), ("slotObservedAt", stamp(-31)),
])
def test_unconfirmed_or_stale_gpu_is_not_full_even_when_counter_is_full(field, value):
    gpu = target("gpu", "gpu", used=1)
    gpu[field] = value
    result = evaluate(payload(gpu, target("cpu", "cpu")))
    assert result.action == "wait" and result.reason_code == "GPU_STATE_UNCONFIRMED"


@pytest.mark.parametrize("field,value", [("capacityKnown", False), ("capacityEvidence", None),
                                         ("observedAt", stamp(-31)), ("observedAt", stamp(1))])
def test_unconfirmed_shared_group_blocks_cpu_fallback(field, value):
    data = payload(target("gpu", "gpu", used=1), target("cpu", "cpu"))
    data["groups"][0][field] = value
    assert evaluate(data).action == "wait"


@pytest.mark.parametrize("used", [0, 1])
def test_unavailable_worker_is_not_a_proof_of_usable_gpu_capacity_exhaustion(used):
    gpu = target("gpu", "gpu", used=used)
    gpu["slotReady"] = False
    assert evaluate(payload(gpu, target("cpu", "cpu"))).action == "wait"


def test_unknown_device_never_masquerades_as_gpu_or_no_gpu_configuration():
    assert evaluate(payload(target("unknown", "unknown"))).action == "wait"
    assert evaluate(payload(target("unknown", "unknown"), target("cpu", "cpu"))).action == "wait"
    assert evaluate(payload(target("unknown", "unknown"), target("gpu", "gpu"))).target_id == "gpu"


@pytest.mark.parametrize("configured", ["configured", "unknown"])
def test_absent_gpu_inventory_does_not_establish_no_gpu(configured):
    assert evaluate(payload(target("cpu", "cpu"), configured=configured)).action == "wait"


def test_explicit_no_gpu_allows_qualified_cpu_but_never_incompatible_model():
    data = payload(target("cpu", "cpu"))
    result = evaluate(data)
    assert result.target_id == "cpu" and result.reason_code == "CPU_NO_GPU_CONFIGURED"
    data["targets"][0]["compatibilityKey"] = "whisper-not-sensevoice"
    assert evaluate(data).action == "wait"


@pytest.mark.parametrize("field,value", [("attempt", 1), ("hasPermit", True),
    ("upstreamExecutionStarted", True), ("status", "running"), ("status", "retry_wait"),
    ("status", "failed"), ("status", "succeeded"), ("status", "cancelled")])
def test_only_unexecuted_queued_task_may_be_selected(field, value):
    data = payload(target("gpu", "gpu"))
    data["task"][field] = value
    assert evaluate(data).action == "wait"


@pytest.mark.parametrize("field", ["routingRevision", "compatibilityKey"])
def test_missing_immutable_route_is_wait_not_an_implicit_old_task_migration(field):
    data = payload(target("gpu", "gpu"))
    del data[field]
    assert evaluate(data).reason_code == "IMMUTABLE_ROUTE_MISSING"


@pytest.mark.parametrize("field", ["queueDeadlineAt", "totalDeadlineAt"])
def test_elapsed_task_deadline_cannot_be_extended_by_selection(field):
    data = payload(target("gpu", "gpu"))
    data["task"][field] = stamp()
    assert evaluate(data).reason_code == "TASK_DEADLINE_EXPIRED"


@pytest.mark.parametrize("field,value", [("cpuSupported", False), ("cpuSupportEvidence", None),
                                         ("cpuProfile", None)])
def test_cpu_needs_explicit_support_and_measured_profile(field, value):
    cpu = target("cpu", "cpu")
    cpu[field] = value
    assert evaluate(payload(cpu)).action == "wait"


@pytest.mark.parametrize("field,value", [("targetRevision", "old-revision"),
    ("measuredAt", stamp(-3601)), ("measuredAt", stamp(1)),
    ("minAudioSeconds", 6), ("maxAudioSeconds", 4)])
def test_cpu_profile_cannot_cross_revision_time_or_measured_audio_range(field, value):
    cpu = target("cpu", "cpu")
    cpu["cpuProfile"][field] = value
    assert evaluate(payload(cpu)).action == "wait"


def test_single_slot_samples_cannot_silently_authorize_higher_shared_concurrency():
    cpu = target("cpu", "cpu", limit=2)
    assert evaluate(payload(cpu)).action == "wait"
    cpu["cpuProfile"]["maxTestedConcurrency"] = 2
    assert evaluate(payload(cpu)).target_id == "cpu"


@pytest.mark.parametrize("budget", ["cpuBudgetSeconds", "attemptBudgetSeconds", "remoteBudgetSeconds"])
def test_cpu_total_estimate_includes_wait_and_must_fit_each_budget(budget):
    data = payload(target("cpu", "cpu"))
    data["task"][budget] = 14.9
    assert evaluate(data).action == "wait"
    data["task"][budget] = 15
    assert evaluate(data).target_id == "cpu"


def test_cpu_has_no_implicit_budget_default():
    data = payload(target("cpu", "cpu"))
    del data["task"]["cpuBudgetSeconds"]
    assert evaluate(data).action == "wait"


def test_total_deadline_is_strict_but_queue_deadline_only_limits_wait():
    data = payload(target("cpu", "cpu"))
    data["task"]["totalDeadlineAt"] = stamp(15)
    assert evaluate(data).action == "wait"
    data["task"]["totalDeadlineAt"] = stamp(16)
    data["task"]["queueDeadlineAt"] = stamp(4)
    assert evaluate(data).target_id == "cpu"  # 总耗时15秒，排队3秒，未要求推理4秒内结束。
    data["task"]["queueDeadlineAt"] = stamp(3)
    assert evaluate(data).action == "wait"


def test_large_finite_input_overflow_returns_wait_and_never_nonfinite_json():
    cpu = target("cpu", "cpu")
    cpu["cpuProfile"]["secondsPerAudioSecond"] = 1e308
    result = evaluate(payload(cpu))
    assert result.action == "wait"
    assert result.candidates[0].reason_code == "CPU_ESTIMATE_NONFINITE"
    assert result.estimated_cpu_seconds is None


def test_isolated_profile_cannot_be_relabelled_as_provided_evidence():
    data = payload(target("cpu", "cpu"))
    data["targets"][0]["evidenceKind"] = data["groups"][0]["evidenceKind"] = "provided"
    assert evaluate(data).evidence_kind == "isolated"


def test_ttl_boundary_is_explicit_and_future_observation_not_accepted():
    gpu = target("gpu", "gpu")
    gpu["healthObservedAt"] = stamp(-30)
    assert evaluate(payload(gpu)).target_id == "gpu"
    gpu["healthObservedAt"] = stamp(-30.001)
    assert evaluate(payload(gpu)).action == "wait"


@pytest.mark.parametrize("key,value", [("executionEnabled", True), ("executionEnabled", 0),
    ("executionEnabled", "false"), ("evaluatedAt", 1), ("evaluatedAt", "2026-10-19T08:00:00"),
    ("extra", "not allowed")])
def test_top_level_strictness_disallows_execution_and_untrusted_shape(key, value):
    data = payload(target("gpu", "gpu"))
    data[key] = value
    with pytest.raises(ValidationError):
        DevicePolicyRequest.model_validate(data)


@pytest.mark.parametrize("field,value", [("audioSeconds", "5"), ("audioSeconds", True),
    ("audioSeconds", float("nan")), ("audioSeconds", float("inf")), ("audioSeconds", 301),
    ("attempt", False), ("attempt", 0.0), ("hasPermit", 0), ("attemptBudgetSeconds", "60")])
def test_numeric_and_boolean_values_are_not_coerced(field, value):
    data = payload(target("gpu", "gpu"))
    data["task"][field] = value
    with pytest.raises(ValidationError):
        DevicePolicyRequest.model_validate(data)


def test_snake_case_duplicate_ids_missing_group_and_contradictory_capacity_are_rejected():
    original = payload(target("gpu", "gpu"))
    variants = []
    item = deepcopy(original)
    item["evaluated_at"] = item.pop("evaluatedAt")
    variants.append(item)
    item = deepcopy(original)
    item["targets"].append(deepcopy(item["targets"][0]))
    variants.append(item)
    item = deepcopy(original)
    item["groups"].append(deepcopy(item["groups"][0]))
    variants.append(item)
    item = deepcopy(original)
    item["targets"][0]["capacityGroupId"] = "missing"
    variants.append(item)
    item = deepcopy(original)
    item["targets"][0]["held"] = 1
    variants.append(item)
    item = deepcopy(original)
    item["gpuConfiguration"] = "none"
    variants.append(item)
    for item in variants:
        with pytest.raises(ValidationError):
            DevicePolicyRequest.model_validate(item)


def test_inventory_has_a_fixed_upper_bound():
    data = payload(*(target(f"gpu-{index}", "gpu") for index in range(65)))
    with pytest.raises(ValidationError):
        DevicePolicyRequest.model_validate(data)


@pytest.mark.parametrize("field,value", [("maxAudioSeconds", 301), ("validForSeconds", float("inf")),
    ("sampleCount", True), ("maxTestedConcurrency", 1.0), ("safetyFactor", "2"),
    ("safetyFactor", 0.99), ("evidenceReference", "   ")])
def test_profile_is_strict_and_does_not_expand_audio_limit(field, value):
    data = payload(target("cpu", "cpu"))
    data["targets"][0]["cpuProfile"][field] = value
    with pytest.raises(ValidationError):
        DevicePolicyRequest.model_validate(data)


def test_validated_input_is_immutable_and_repeated_evaluation_is_deterministic():
    request = DevicePolicyRequest.model_validate(payload(target("gpu", "gpu")))
    before = request.model_dump(mode="json")
    with pytest.raises(ValidationError):
        request.task.attempt = 1
    first = DevicePolicyService.evaluate(request)
    assert first == DevicePolicyService.evaluate(request)
    assert request.model_dump(mode="json") == before
    output = first.model_dump(mode="json")
    assert output["executionEnabled"] is False and "execution_enabled" not in output
