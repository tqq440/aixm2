"""纯快照决策的独立边界回归，不连接数据库、设备或网络。"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.schema.asr.device_policy_schema import DevicePolicyRequest
from backend.service.asr.device_policy_service import DevicePolicyService


NOW = datetime(2026, 10, 19, 6, 30, tzinfo=timezone.utc)


def _time(seconds=0):
    return (NOW + timedelta(seconds=seconds)).isoformat()


def _group(name, **changes):
    value = {
        "capacityGroupId": name, "limit": 1, "held": 0, "uncertain": 0,
        "capacityKnown": True, "observedAt": _time(-1), "ttlSeconds": 30,
        "capacityEvidence": "isolated fixture capacity", "evidenceKind": "isolated",
    }
    value.update(changes)
    return value


def _target(name, kind="gpu", **changes):
    value = {
        "targetId": name, "targetRevision": f"revision-{name}",
        "compatibilityKey": "sensevoice-fixture-compatible", "deviceKind": kind,
        "deviceEvidence": "isolated fixture binding",
        "compatibilityEvidence": "isolated fixture compatible model and input",
        "evidenceKind": "isolated", "capacityGroupId": f"group-{name}",
        "limit": 1, "held": 0, "uncertain": 0, "capacityKnown": True,
        "capacityObservedAt": _time(-1), "capacityTtlSeconds": 30,
        "capacityEvidence": "isolated fixture capacity",
        "healthState": "ready", "healthObservedAt": _time(-1), "healthTtlSeconds": 30,
        "slotReady": True, "slotObservedAt": _time(-1), "slotTtlSeconds": 30,
    }
    if kind == "cpu":
        value.update({
            "cpuSupported": True, "cpuSupportEvidence": "isolated fixture CPU support",
            "cpuProfile": {
                "targetRevision": f"revision-{name}", "evidenceKind": "isolated",
                "evidenceReference": "isolated fixture measurements, not a real benchmark",
                "measuredAt": _time(-10), "validForSeconds": 300,
                "sampleCount": 3, "minAudioSeconds": 1, "maxAudioSeconds": 10,
                "maxTestedConcurrency": 1, "fixedOverheadSeconds": 1,
                "secondsPerAudioSecond": 0.5, "safetyFactor": 2,
                "queueWaitUpperBoundSeconds": 2,
            },
        })
    value.update(changes)
    return value


@pytest.fixture
def snapshot():
    return {
        "evaluatedAt": _time(), "executionEnabled": False,
        "routingRevision": "isolated-routing-v1",
        "compatibilityKey": "sensevoice-fixture-compatible",
        "gpuConfiguration": "configured",
        "task": {
            "status": "queued", "attempt": 0, "hasPermit": False,
            "upstreamExecutionStarted": False, "audioSeconds": 5,
            "queueDeadlineAt": _time(20), "totalDeadlineAt": _time(120),
            "attemptBudgetSeconds": 30, "remoteBudgetSeconds": 20,
            "cpuBudgetSeconds": 15,
        },
        "groups": [_group("group-gpu-a"), _group("group-cpu-a")],
        "targets": [_target("gpu-a"), _target("cpu-a", "cpu")],
    }


def _decide(snapshot):
    return DevicePolicyService().evaluate(DevicePolicyRequest.model_validate(snapshot))


def _full_gpu(snapshot, *, uncertain=False):
    key = "uncertain" if uncertain else "held"
    snapshot["targets"][0][key] = 1
    snapshot["groups"][0][key] = 1


def _change(snapshot, path, value):
    item = snapshot
    for key in path[:-1]:
        item = item[key]
    item[path[-1]] = value


def test_preview_prefers_gpu_is_deterministic_and_does_not_mutate_input(snapshot):
    before = deepcopy(snapshot)
    first = _decide(snapshot)
    snapshot["targets"].reverse()
    snapshot["groups"].reverse()
    second = _decide(snapshot)
    assert first.action == second.action == "select"
    assert first.target_id == second.target_id == "gpu-a"
    assert first.target_revision == "revision-gpu-a"
    assert first.device_kind == "gpu"
    assert first.mode == "preview" and first.execution_enabled is False
    assert first.evidence_kind == "isolated"
    assert snapshot == {**before, "targets": list(reversed(before["targets"])),
                        "groups": list(reversed(before["groups"]))}


def test_gpu_occupancy_ratio_then_identifier_break_ties(snapshot):
    snapshot["targets"] = [
        _target("gpu-z", limit=2, held=1),
        _target("gpu-b", limit=4, held=1),
        _target("gpu-a", limit=4, held=1),
    ]
    snapshot["groups"] = [
        _group("group-gpu-z", limit=2, held=1),
        _group("group-gpu-b", limit=4, held=1),
        _group("group-gpu-a", limit=4, held=1),
    ]
    assert _decide(snapshot).target_id == "gpu-a"
    snapshot["targets"].reverse()
    assert _decide(snapshot).target_id == "gpu-a"


@pytest.mark.parametrize("uncertain", [False, True])
def test_confirmed_gpu_capacity_exhaustion_allows_cpu(snapshot, uncertain):
    _full_gpu(snapshot, uncertain=uncertain)
    decision = _decide(snapshot)
    assert decision.action == "select"
    assert decision.target_id == "cpu-a" and decision.device_kind == "cpu"
    assert decision.estimated_cpu_seconds > 0
    assert decision.estimated_queue_seconds == 2
    assert decision.estimated_processing_seconds > 0


@pytest.mark.parametrize("changes", [
    {"deviceKind": "unknown"}, {"deviceEvidence": None},
    {"compatibilityEvidence": None}, {"capacityKnown": False},
    {"capacityObservedAt": None}, {"capacityObservedAt": _time(-31)},
    {"capacityObservedAt": _time(1)}, {"capacityEvidence": None},
    {"healthState": "unknown"}, {"healthState": "unavailable"},
    {"healthObservedAt": None}, {"healthObservedAt": _time(-31)},
    {"healthObservedAt": _time(1)}, {"slotReady": False},
    {"slotObservedAt": None}, {"slotObservedAt": _time(-31)},
    {"slotObservedAt": _time(1)},
])
def test_incomplete_gpu_evidence_never_means_full_cpu_fallback(snapshot, changes):
    _full_gpu(snapshot)
    snapshot["targets"][0].update(changes)
    decision = _decide(snapshot)
    assert decision.action == "wait"
    assert decision.target_id is None


@pytest.mark.parametrize("changes", [
    {"capacityKnown": False}, {"observedAt": None},
    {"observedAt": _time(-31)}, {"observedAt": _time(1)},
    {"capacityEvidence": None},
])
def test_unknown_or_stale_gpu_shared_group_does_not_enable_cpu(snapshot, changes):
    _full_gpu(snapshot)
    snapshot["groups"][0].update(changes)
    assert _decide(snapshot).action == "wait"


@pytest.mark.parametrize("uncertain", [False, True])
def test_gpu_aliases_do_not_bypass_saturated_shared_group(snapshot, uncertain):
    snapshot["targets"] = [
        _target("alias-a", capacityGroupId="shared"),
        _target("alias-b", capacityGroupId="shared"),
    ]
    snapshot["groups"] = [_group("shared", **{"uncertain" if uncertain else "held": 1})]
    decision = _decide(snapshot)
    assert decision.action == "wait" and decision.target_id is None


@pytest.mark.parametrize("gpu_configuration", ["configured", "unknown"])
def test_missing_gpu_inventory_is_not_explicit_no_gpu(snapshot, gpu_configuration):
    snapshot["targets"] = snapshot["targets"][1:]
    snapshot["groups"] = snapshot["groups"][1:]
    snapshot["gpuConfiguration"] = gpu_configuration
    assert _decide(snapshot).action == "wait"


def test_explicit_no_gpu_can_use_compatible_measured_cpu(snapshot):
    snapshot["targets"] = snapshot["targets"][1:]
    snapshot["groups"] = snapshot["groups"][1:]
    snapshot["gpuConfiguration"] = "none"
    assert _decide(snapshot).target_id == "cpu-a"


@pytest.mark.parametrize("changes", [
    {"status": "created"}, {"status": "running"}, {"status": "retry_wait"},
    {"status": "cancel_requested"}, {"status": "cancelled"},
    {"status": "failed"}, {"status": "succeeded"},
    {"attempt": 1}, {"hasPermit": True}, {"upstreamExecutionStarted": True},
    {"queueDeadlineAt": _time()}, {"totalDeadlineAt": _time()},
])
def test_only_unstarted_unexpired_queued_task_can_be_selected(snapshot, changes):
    snapshot["task"].update(changes)
    decision = _decide(snapshot)
    assert decision.action == "wait" and decision.target_id is None


@pytest.mark.parametrize("changes", [
    {"targetRevision": "different-cpu-revision"},
    {"measuredAt": _time(-301)}, {"measuredAt": _time(1)},
    {"minAudioSeconds": 6}, {"maxAudioSeconds": 4},
    {"queueWaitUpperBoundSeconds": 100},
    {"secondsPerAudioSecond": 100},
])
def test_cpu_requires_current_matching_in_range_affordable_measurements(snapshot, changes):
    _full_gpu(snapshot)
    snapshot["targets"][1]["cpuProfile"].update(changes)
    assert _decide(snapshot).action == "wait"


@pytest.mark.parametrize("changes", [
    {"cpuSupported": False}, {"cpuSupportEvidence": None},
    {"cpuProfile": None}, {"deviceEvidence": None},
    {"compatibilityEvidence": None}, {"compatibilityKey": "whisper-different-model"},
    {"capacityKnown": False}, {"capacityObservedAt": _time(-31)},
    {"healthState": "unavailable"}, {"slotReady": False},
])
def test_cpu_support_and_compatibility_are_not_inferred_from_device_name(snapshot, changes):
    _full_gpu(snapshot)
    snapshot["targets"][1].update(changes)
    assert _decide(snapshot).action == "wait"


def test_cpu_profile_cannot_be_extrapolated_to_higher_concurrency(snapshot):
    _full_gpu(snapshot)
    snapshot["targets"][1].update(limit=2, held=1)
    snapshot["groups"][1].update(limit=2, held=1)
    assert _decide(snapshot).action == "wait"


@pytest.mark.parametrize("changes", [
    {"cpuBudgetSeconds": None}, {"cpuBudgetSeconds": 1},
    {"attemptBudgetSeconds": 1}, {"remoteBudgetSeconds": 1},
    {"queueDeadlineAt": _time(1)}, {"totalDeadlineAt": _time(1)},
])
def test_cpu_queue_and_execution_must_fit_each_original_budget(snapshot, changes):
    _full_gpu(snapshot)
    snapshot["task"].update(changes)
    assert _decide(snapshot).action == "wait"


@pytest.mark.parametrize("key", [None, "different-compatibility"])
def test_missing_or_different_compatibility_is_not_an_unknown_version_match(snapshot, key):
    snapshot["compatibilityKey"] = key
    assert _decide(snapshot).action == "wait"


@pytest.mark.parametrize("path,value", [
    (("task", "attempt"), True), (("task", "attempt"), 1.0),
    (("task", "hasPermit"), 0), (("task", "audioSeconds"), True),
    (("task", "audioSeconds"), "5"), (("task", "audioSeconds"), float("nan")),
    (("task", "audioSeconds"), float("inf")),
    (("targets", 0, "limit"), True), (("targets", 0, "limit"), 9),
    (("targets", 0, "held"), -1), (("targets", 0, "held"), 1_000_001),
    (("targets", 0, "slotReady"), 1),
    (("targets", 0, "capacityTtlSeconds"), 0),
    (("targets", 0, "deviceEvidence"), "   "),
    (("targets", 1, "cpuProfile", "sampleCount"), 0),
    (("targets", 1, "cpuProfile", "sampleCount"), True),
    (("targets", 1, "cpuProfile", "safetyFactor"), float("inf")),
    (("targets", 1, "cpuProfile", "queueWaitUpperBoundSeconds"), -1),
    (("evaluatedAt",), "2026-10-19T06:30:00"),
    (("evaluatedAt",), 12345), (("evaluatedAt",), True),
    (("task", "queueDeadlineAt"), datetime(2026, 10, 19)),
    (("executionEnabled",), True), (("routingRevision",), "unsafe revision !"),
])
def test_schema_rejects_ambiguous_nonfinite_or_out_of_scope_inputs(snapshot, path, value):
    _change(snapshot, path, value)
    with pytest.raises(ValidationError):
        DevicePolicyRequest.model_validate(snapshot)


@pytest.mark.parametrize("change", ["duplicate-target", "duplicate-group", "missing-group",
                                           "group-under-count", "contradictory-no-gpu", "extra-command"])
def test_schema_rejects_inventory_contradictions_and_execution_commands(snapshot, change):
    if change == "duplicate-target":
        snapshot["targets"].append(deepcopy(snapshot["targets"][0]))
    elif change == "duplicate-group":
        snapshot["groups"].append(deepcopy(snapshot["groups"][0]))
    elif change == "missing-group":
        snapshot["groups"].pop(0)
    elif change == "group-under-count":
        snapshot["targets"][0]["held"] = 1
    elif change == "contradictory-no-gpu":
        snapshot["gpuConfiguration"] = "none"
    else:
        snapshot["command"] = "start-service"
    with pytest.raises(ValidationError):
        DevicePolicyRequest.model_validate(snapshot)


def test_finite_cpu_samples_cannot_overflow_into_an_infinite_decision(snapshot):
    _full_gpu(snapshot)
    snapshot["targets"][1]["cpuProfile"].update(
        fixedOverheadSeconds=1e308, secondsPerAudioSecond=1e308, safetyFactor=1e308,
    )
    decision = _decide(snapshot)
    assert decision.action == "wait" and decision.target_id is None
    assert "Infinity" not in decision.model_dump_json()
    assert "NaN" not in decision.model_dump_json()


@pytest.mark.parametrize("field,seconds", [("queueDeadlineAt", 2), ("totalDeadlineAt", 9)])
def test_cpu_cannot_finish_or_leave_queue_exactly_at_deadline(snapshot, field, seconds):
    _full_gpu(snapshot)
    snapshot["task"][field] = _time(seconds)
    assert _decide(snapshot).action == "wait"


def test_cpu_estimate_may_equal_budget_but_not_deadline(snapshot):
    _full_gpu(snapshot)
    snapshot["task"].update(cpuBudgetSeconds=9, attemptBudgetSeconds=9,
                            remoteBudgetSeconds=9, queueDeadlineAt=_time(3),
                            totalDeadlineAt=_time(10))
    decision = _decide(snapshot)
    assert decision.target_id == "cpu-a"
    assert decision.estimated_cpu_seconds == 9
    assert decision.estimated_processing_seconds == 7
    assert decision.estimated_queue_seconds == 2


def test_cpu_processing_can_extend_past_queue_deadline_after_start(snapshot):
    _full_gpu(snapshot)
    snapshot["task"]["queueDeadlineAt"] = _time(3)
    assert _decide(snapshot).target_id == "cpu-a"


def test_uncertain_gpu_alongside_full_gpu_still_blocks_cpu(snapshot):
    _full_gpu(snapshot)
    snapshot["targets"].append(_target("gpu-unknown", healthState="unknown"))
    snapshot["groups"].append(_group("group-gpu-unknown"))
    assert _decide(snapshot).action == "wait"


def test_full_cpu_shared_group_does_not_gain_capacity_from_new_target_alias(snapshot):
    _full_gpu(snapshot)
    snapshot["groups"][1]["uncertain"] = 1
    alias = _target("cpu-alias", "cpu", capacityGroupId="group-cpu-a")
    snapshot["targets"].append(alias)
    assert _decide(snapshot).action == "wait"


def test_route_revision_is_required_and_returned_without_reinterpretation(snapshot):
    snapshot["routingRevision"] = None
    assert _decide(snapshot).action == "wait"
    snapshot["routingRevision"] = "immutable-route-v9"
    decision = _decide(snapshot)
    assert decision.routing_revision == "immutable-route-v9"
    assert decision.target_revision == snapshot["targets"][0]["targetRevision"]


def test_equivalent_explicit_timezones_produce_same_selection(snapshot):
    reference = _decide(snapshot)
    snapshot["evaluatedAt"] = NOW.astimezone(timezone(timedelta(hours=8))).isoformat()
    actual = _decide(snapshot)
    assert actual.model_dump() == reference.model_dump()


@pytest.mark.parametrize("name,kind", [
    ("gpu-first", "gpu"), ("gpu-full-cpu", "cpu"),
    ("gpu-unknown-wait", None), ("cpu-outside-measurement-wait", None),
])
def test_published_offline_examples_are_preview_only(name, kind):
    path = Path(__file__).resolve().parents[1] / "fixtures" / "device-policy" / f"{name}.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    before = deepcopy(snapshot)
    decision = _decide(snapshot)
    assert decision.action == ("select" if kind else "wait")
    assert decision.device_kind == kind
    assert decision.mode == "preview" and decision.execution_enabled is False
    assert decision.evidence_kind == "isolated"
    assert snapshot == before
