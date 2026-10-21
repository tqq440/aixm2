"""多资源组准入后的纯选择；ASR CPU 性能复用原设备策略。"""

from fractions import Fraction

from pydantic import ValidationError

from backend.schema.asr.device_policy_schema import DevicePolicyRequest
from backend.service.asr.device_policy_service import DevicePolicyService
from backend.utils.json.json_utils import canonical_json_sha256
from backend.utils.time.time_utils import as_utc


def target_identity(target):
    return canonical_json_sha256({"origin": target.base_url, "path": target.invoke_path})


def select_target(config, candidates, *, task, invocation, now, profiles, attempt_budget):
    """candidates 为事务内重新检查的目标/可用性/占用，不读网络或修改输入。"""
    ranked = lambda candidate: (Fraction(candidate["occupied"], candidate["resources"]["target:" + candidate["key"]]), candidate["target"].target_id)
    if config.policy == "fixed":
        enabled = [candidate for candidate in candidates if candidate["target"].enabled]
        if len(enabled) != 1:
            return None, "FIXED_TARGET_AMBIGUOUS"
        return (enabled[0], "FIXED_TARGET") if enabled[0]["reason"] == "AVAILABLE" else (None, enabled[0]["reason"])
    gpus = [candidate for candidate in candidates if candidate["target"].device_kind == "gpu"]
    ready = [candidate for candidate in gpus if candidate["reason"] == "AVAILABLE"]
    if ready:
        return min(ready, key=ranked), "GPU_PREFERRED"
    if any(candidate["target"].device_kind == "unknown" for candidate in candidates):
        return None, "GPU_STATE_UNCONFIRMED"
    if gpus and any(candidate["reason"] != "CAPACITY_FULL" for candidate in gpus):
        return None, "GPU_STATE_UNCONFIRMED"
    if config.kind != "asr" or task is None:
        return None, "CPU_AUTOMATIC_FALLBACK_UNAVAILABLE"
    for candidate in sorted((item for item in candidates if item["target"].device_kind == "cpu"), key=ranked):
        if candidate["reason"] != "AVAILABLE":
            continue
        target, measured = candidate["target"], profiles.get(candidate["target"].target_id)
        if measured is None:
            continue
        key = target_identity(target)
        # 容量由公共层已对每个真实资源组检查；这里仅复用原 CPU 支持、样本范围和时间预算判定。
        group_limit = max([target.concurrency_limit, *candidate["group_limits"]])
        try:
            request = DevicePolicyRequest.model_validate({
                "evaluatedAt": as_utc(now), "routingRevision": invocation.service_revision,
                "compatibilityKey": config.model_id, "gpuConfiguration": "none",
                "task": {"status": "queued", "attempt": 0,
                    "hasPermit": False, "upstreamExecutionStarted": False, "audioSeconds": float(task.audio_duration_seconds),
                    "queueDeadlineAt": as_utc(invocation.queue_deadline_at), "totalDeadlineAt": as_utc(invocation.deadline_at),
                    "attemptBudgetSeconds": float(attempt_budget), "remoteBudgetSeconds": float(target.total_timeout_seconds),
                    "cpuBudgetSeconds": float(measured.cpu_budget_seconds)},
                "groups": [{"capacityGroupId": "combined", "limit": group_limit, "held": 0, "uncertain": 0,
                    "capacityKnown": True, "observedAt": as_utc(now), "ttlSeconds": 1.0,
                    "capacityEvidence": "approved common reservation budget", "evidenceKind": "provided"}],
                "targets": [{"targetId": target.target_id, "targetRevision": key, "compatibilityKey": config.model_id,
                    "deviceKind": "cpu", "deviceEvidence": target.device_evidence,
                    "compatibilityEvidence": target.compatibility_evidence, "evidenceKind": "provided",
                    "capacityGroupId": "combined", "limit": target.concurrency_limit, "held": 0, "uncertain": 0,
                    "capacityKnown": True, "capacityObservedAt": as_utc(now), "capacityTtlSeconds": 1.0,
                    "capacityEvidence": target.capacity_evidence, "healthState": "ready", "healthObservedAt": as_utc(now),
                    "healthTtlSeconds": 1.0, "slotReady": True, "slotObservedAt": as_utc(now), "slotTtlSeconds": 1.0,
                    "cpuSupported": True, "cpuSupportEvidence": measured.profile["evidenceReference"], "cpuProfile": measured.profile}],
            })
            decision = DevicePolicyService.evaluate(request)
        except (ValidationError, ValueError, TypeError, KeyError):
            continue
        if decision.action == "select":
            return candidate, "CPU_FALLBACK"
    return None, "CPU_PERFORMANCE_UNCONFIRMED"
