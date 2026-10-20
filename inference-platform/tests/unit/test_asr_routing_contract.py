"""路由隔离边界、严格输入与资源余量语义；全部无网络。"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.common.exceptions.app_error import AppError
from backend.config.asr.routing_config import require_routing_isolation
from backend.config.settings.app_settings import AppSettings, load_settings
from backend.schema.asr.routing_schema import RoutingRegistration
from backend.service.asr.device_policy_service import DevicePolicyService


def policy():
    return json.loads(Path("tests/fixtures/device-policy/gpu-full-cpu.json").read_text("utf-8"))


def test_routing_defaults_off_and_explicit_environment_is_validated():
    assert load_settings(None, environ={}).remote_asr_routing_revision is None
    assert not load_settings(None, environ={}).remote_asr_routing_isolation
    parsed = load_settings(None, environ={"INFERENCE_REMOTE_ASR_ROUTING_REVISION": "a" * 64,
        "INFERENCE_REMOTE_ASR_ROUTING_ISOLATION": "true"})
    assert parsed.remote_asr_routing_isolation and parsed.remote_asr_routing_revision == "a" * 64
    with pytest.raises(ValidationError):
        AppSettings(remote_asr_routing_revision="not-a-digest")


@pytest.mark.parametrize("origin", ["https://127.0.0.1:8765", "https://localhost", "https://[::1]", "https://fixture.invalid"])
def test_only_explicit_isolated_paths_and_targets_are_allowed(tmp_path, origin):
    settings = AppSettings(database_path=tmp_path / "db.sqlite3", upload_dir=tmp_path / "uploads",
        log_dir=tmp_path / "logs", remote_asr_routing_isolation=True)
    require_routing_isolation(settings, [origin])
    with pytest.raises(AppError, match="本阶段"):
        require_routing_isolation(settings.model_copy(update={"remote_asr_routing_isolation": False}), [origin])
    with pytest.raises(AppError, match="真实目标"):
        require_routing_isolation(settings, ["https://10.8.21.33:8003"])
    with pytest.raises(AppError, match="隔离"):
        require_routing_isolation(AppSettings(remote_asr_routing_isolation=True), [origin])


@pytest.mark.parametrize("available,confirmed,action", [(None, True, "wait"), (0, False, "wait"), (0, True, "select")])
def test_unknown_or_conservative_exhaustion_does_not_fabricate_gpu_full(available, confirmed, action):
    value = policy()
    value["targets"][0].update(held=0, resourceRequired=True, resourceAvailable=available,
                               resourceExhaustionConfirmed=confirmed)
    value["groups"][0]["held"] = 0
    result = DevicePolicyService.evaluate(value)
    assert result.action == action
    if action == "wait":
        assert result.reason_code == "GPU_STATE_UNCONFIRMED"
    else:
        assert result.device_kind == "cpu"


def test_strict_cpu_profile_expiry_guard_prevents_boundary_dispatch():
    value = policy()
    value["targets"][1]["cpuProfileUsable"] = False
    result = DevicePolicyService.evaluate(value)
    assert result.action == "wait"
    assert next(item for item in result.candidates if item.device_kind == "cpu").reason_code == "CPU_PROFILE_EXPIRED_OR_FUTURE"


@pytest.mark.parametrize("changes", [{"cpuBudgetSeconds": -1}, {"cpuBudgetSeconds": True},
    {"cpuBudgetSeconds": float("inf")}, {"modelVersion": "bad version"}, {"unknown": True},
    {"candidates": []}, {"candidates": [{"catalogRevisionId": "a" * 64}] * 2}])
def test_routing_registration_rejects_ambiguous_or_unbounded_input(changes):
    value = dict(modelId="fixture", modelVersion="v1", compatibilityKey="same-model",
        evidenceKind="isolated", evidence="isolated fixture", candidates=[{"catalogRevisionId": "a" * 64}])
    with pytest.raises(ValidationError):
        RoutingRegistration.model_validate({**value, **changes})
