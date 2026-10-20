"""调度诊断的公开输出边界；不读取配置、数据库或外部服务。"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.schema.asr.routing_schema import TaskRoutingView
from backend.schema.asr.scheduling_schema import SchedulingStatus


def status_value(**changes):
    return dict(observedAt="2026-10-20T02:00:00Z", mode="legacy", routingConfigured=False,
        executionScope="legacy", reasonCode="LEGACY_FIXED_TARGET", candidateCount=0,
        message="沿用固定目标；设备尚未核实。", **changes)


def routing_value(**changes):
    value = dict(routingRevisionId="a" * 64, selectionState="waiting",
        observedAt="2026-10-20T02:00:00Z", reasonCategory="waiting", message="等待资源资料。")
    value.update(changes)
    return value


def test_scheduling_status_uses_explicit_public_camel_case_contract():
    value = SchedulingStatus.model_validate(status_value()).model_dump(mode="json", by_alias=True)
    assert set(value) == {"observedAt", "mode", "routingConfigured", "executionScope",
        "reasonCode", "candidateCount", "message"}
    assert value["mode"] == value["executionScope"] == "legacy"
    assert "available" not in value


@pytest.mark.parametrize("field,value", [
    ("mode", "gpu"), ("executionScope", "real"), ("candidateCount", -1),
    ("candidateCount", 9), ("baseUrl", "https://private.invalid"),
    ("available", True), ("observedAt", "not-a-time"),
])
def test_scheduling_contract_rejects_unknown_or_unbounded_output(field, value):
    payload = status_value()
    payload[field] = value
    with pytest.raises(ValidationError):
        SchedulingStatus.model_validate(payload)


@pytest.mark.parametrize("mode,scope,configured", [
    ("legacy", "legacy", False), ("isolated", "isolated", True), ("blocked", "disabled", True),
])
def test_scheduling_modes_round_trip(mode, scope, configured):
    payload = status_value()
    payload.update(mode=mode, executionScope=scope, routingConfigured=configured)
    parsed = SchedulingStatus.model_validate(payload)
    assert parsed.mode == mode and parsed.execution_scope == scope


def test_task_diagnostic_default_candidates_do_not_share_mutable_list():
    first = TaskRoutingView.model_validate(routing_value())
    second = TaskRoutingView.model_validate(routing_value())
    assert first.candidates == second.candidates == []
    assert first.candidates is not second.candidates


def test_selected_reason_and_current_unknown_status_are_separate():
    value = TaskRoutingView.model_validate(routing_value(selectionState="selected",
        catalogRevisionId="b" * 64, targetId="fixture-gpu", deviceKind="gpu",
        reasonCode="GPU_PREFERRED", evidenceKind="isolated",
        selectedAt=datetime(2026, 10, 20, 2, tzinfo=timezone.utc),
        reasonCategory="outcome_unknown", statusCode="REMOTE_ASR_RESULT_UNKNOWN"))
    output = value.model_dump(mode="json", by_alias=True)
    assert output["reasonCode"] == "GPU_PREFERRED"
    assert output["statusCode"] == "REMOTE_ASR_RESULT_UNKNOWN"
    assert output["reasonCategory"] == "outcome_unknown"


@pytest.mark.parametrize("kind", ["unknown", "gpu", "cpu"])
def test_candidate_output_is_bounded_to_safe_explanation_fields(kind):
    value = TaskRoutingView.model_validate(routing_value(candidates=[dict(targetId="fixture-target",
        deviceKind=kind, eligible=False, reasonCode="CAPACITY_UNCONFIRMED", message="容量资料待确认。")]))
    candidate = value.model_dump(mode="json", by_alias=True)["candidates"][0]
    assert set(candidate) == {"targetId", "deviceKind", "eligible", "reasonCode", "message"}


@pytest.mark.parametrize("extra", ["baseUrl", "caFile", "deviceEvidence", "compatibilityEvidence", "source"])
def test_candidate_cannot_accidentally_serialize_private_raw_evidence(extra):
    candidate = dict(targetId="fixture-target", deviceKind="gpu", eligible=False,
        reasonCode="CAPACITY_UNCONFIRMED", message="等待资料。")
    candidate[extra] = "private diagnostic material"
    with pytest.raises(ValidationError):
        TaskRoutingView.model_validate(routing_value(candidates=[candidate]))


@pytest.mark.parametrize("field,value", [("reasonCategory", "running"), ("selectionState", "suggested")])
def test_readonly_diagnostics_do_not_invent_another_selection_state_machine(field, value):
    with pytest.raises(ValidationError):
        TaskRoutingView.model_validate(routing_value(**{field: value}))
