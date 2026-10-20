"""本机-2A配置及资源输入边界；全部设备/容量值为隔离样例。"""

from datetime import datetime, timezone

from pydantic import ValidationError
import pytest

from backend.schema.asr.catalog_schema import (
    CatalogQuery, CatalogRegistration, ResourceObservationInput,
)


def registration(**overrides):
    value = {
        "targetId": "fixture-gpu",
        "connection": {"baseUrl": "https://catalog-fixture.invalid:8003", "modelId": "fixture-sensevoice"},
        "deviceKind": "gpu", "nodeId": "fixture-node", "deviceId": "fixture-device-0",
        "deviceEvidence": "isolated device mapping, not hardware verification",
        "compatibilityKey": "fixture-same-model", "compatibilityEvidence": "isolated compatibility only",
        "evidenceKind": "isolated",
    }
    value.update(overrides)
    return value


def observation(**overrides):
    value = {
        "catalogRevisionId": "a" * 64, "scope": "target", "source": "fixture-source",
        "evidenceKind": "isolated", "evidence": "isolated sample, not server capacity",
        "observedAt": "2026-10-20T01:00:00+00:00", "ttlSeconds": 30,
    }
    value.update(overrides)
    return value


def test_configured_gpu_is_evidence_input_and_does_not_enable_execution():
    value = CatalogRegistration.model_validate(registration())
    assert value.target_id == "fixture-gpu" and value.device_kind == "gpu"
    assert value.capacity_group_id == "remote-asr-shared"
    assert value.connection.model_version is None
    assert "enabled" not in value.model_dump() and "routing_enabled" not in value.model_dump()


def test_unknown_device_has_no_fabricated_identity_or_compatibility():
    value = CatalogRegistration.model_validate({
        "targetId": "fixture-unknown",
        "connection": {"baseUrl": "https://unknown-fixture.invalid", "modelId": "fixture-sensevoice"},
    })
    assert value.device_kind == "unknown" and value.node_id is None and value.device_id is None
    assert value.device_evidence is None and value.compatibility_key is None
    assert value.compatibility_evidence is None


@pytest.mark.parametrize("field", ["nodeId", "deviceId", "deviceEvidence"])
@pytest.mark.parametrize("kind", ["cpu", "gpu"])
def test_device_declaration_requires_complete_binding_evidence(field, kind):
    payload = registration(deviceKind=kind)
    payload.pop(field)
    with pytest.raises(ValidationError):
        CatalogRegistration.model_validate(payload)


@pytest.mark.parametrize("fields", [
    {"nodeId": "claimed-node"}, {"deviceId": "claimed-device"}, {"deviceEvidence": "claimed device"},
])
def test_unknown_device_cannot_carry_a_hidden_binding(fields):
    payload = {"targetId": "fixture-unknown", "connection": registration()["connection"], **fields}
    with pytest.raises(ValidationError):
        CatalogRegistration.model_validate(payload)


@pytest.mark.parametrize("missing", ["compatibilityKey", "compatibilityEvidence"])
def test_model_compatibility_key_and_evidence_must_be_a_pair(missing):
    payload = registration()
    payload.pop(missing)
    with pytest.raises(ValidationError):
        CatalogRegistration.model_validate(payload)


@pytest.mark.parametrize("field,value", [
    ("targetId", "../arbitrary"), ("targetId", "x" * 65), ("targetId", ""),
    ("capacityGroupId", "../group"), ("deviceKind", "cuda:0"),
    ("evidenceKind", "verified"), ("deviceEvidence", "   "),
    ("compatibilityEvidence", "\n\t"), ("enabled", True),
    ("routingEnabled", True), ("command", "do-not-run"),
])
def test_registration_rejects_invalid_identity_uncontrolled_scope_and_blank_evidence(field, value):
    with pytest.raises(ValidationError):
        CatalogRegistration.model_validate(registration(**{field: value}))


@pytest.mark.parametrize("changed", [
    {"apiKey": "not-a-real-secret"}, {"command": "do-not-run"},
])
def test_connection_rejects_uncontrolled_fields(changed):
    payload = registration()
    payload["connection"].update(changed)
    with pytest.raises(ValidationError):
        CatalogRegistration.model_validate(payload)


def test_resource_unknown_is_none_instead_of_zero_or_ready():
    value = ResourceObservationInput.model_validate(observation())
    assert value.health_state == "unknown" and value.available_concurrency is None
    assert value.gpu_utilization_percent is None and value.gpu_memory_free_mi_b is None
    assert value.cpu_available_cores is None


@pytest.mark.parametrize("stamp", ["2026-10-20T01:00:00", datetime(2026, 10, 20), 1_792_456_000, "1792456000", None])
def test_observation_requires_aware_iso_time_and_does_not_guess_unix_units(stamp):
    with pytest.raises(ValidationError):
        ResourceObservationInput.model_validate(observation(observedAt=stamp))


@pytest.mark.parametrize("stamp,ttl", [
    ("0001-01-01T00:00:00+01:00", 30),
    ("9999-12-31T23:59:59-01:00", 30),
    ("9999-12-31T23:59:59+00:00", 1),
    ("9999-12-31T23:59:00+00:00", 60),
])
def test_observation_rejects_utc_conversion_or_expiry_overflow(stamp, ttl):
    with pytest.raises(ValidationError):
        ResourceObservationInput.model_validate(observation(observedAt=stamp, ttlSeconds=ttl))


@pytest.mark.parametrize("stamp,ttl", [
    ("0001-01-01T01:00:00+01:00", 30),
    ("9999-12-31T23:59:58+00:00", 1),
])
def test_observation_accepts_representable_utc_and_expiry_boundaries(stamp, ttl):
    result = ResourceObservationInput.model_validate(observation(observedAt=stamp, ttlSeconds=ttl))
    assert result.ttl_seconds == ttl


def test_observation_accepts_explicit_timezone_and_zero_capacity_without_claiming_free_gpu():
    value = ResourceObservationInput.model_validate(observation(
        observedAt=datetime(2026, 10, 20, 1, tzinfo=timezone.utc), availableConcurrency=0,
        gpuUtilizationPercent=100, gpuMemoryFreeMiB=0, cpuAvailableCores=0, healthState="unknown"))
    assert value.available_concurrency == 0 and value.health_state == "unknown"
    assert value.observed_at.utcoffset().total_seconds() == 0


@pytest.mark.parametrize("field,value", [
    ("catalogRevisionId", "A" * 64), ("catalogRevisionId", "a" * 63),
    ("source", "../request"), ("source", "x" * 65), ("scope", "server"),
    ("evidenceKind", "verified"), ("evidence", "  "), ("ttlSeconds", 0), ("ttlSeconds", 3601),
    ("ttlSeconds", True), ("ttlSeconds", 1.5), ("availableConcurrency", True),
    ("availableConcurrency", -1), ("availableConcurrency", 1025), ("availableConcurrency", 1.5),
    ("gpuUtilizationPercent", -0.1), ("gpuUtilizationPercent", 100.1),
    ("gpuUtilizationPercent", float("nan")), ("gpuMemoryFreeMiB", -1),
    ("gpuMemoryFreeMiB", True), ("gpuMemoryFreeMiB", 1.5),
    ("cpuAvailableCores", -1), ("cpuAvailableCores", float("inf")),
    ("healthState", "running"), ("usableForScheduling", True), ("routingEnabled", True),
])
def test_observation_rejects_invalid_numbers_scope_and_execution_claims(field, value):
    with pytest.raises(ValidationError):
        ResourceObservationInput.model_validate(observation(**{field: value}))


@pytest.mark.parametrize("payload", [
    {"offset": -1}, {"limit": 0}, {"limit": 101}, {"targetId": "arbitrary"}, {"url": "https://example.invalid"},
])
def test_catalog_query_is_bounded_and_not_an_arbitrary_proxy(payload):
    with pytest.raises(ValidationError):
        CatalogQuery.model_validate(payload)


def test_catalog_query_has_bounded_default_page():
    value = CatalogQuery()
    assert value.offset == 0 and value.limit == 20
