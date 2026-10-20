"""GPU优先及有条件CPU承接的纯函数预演，不读取配置、数据库或网络。"""

from fractions import Fraction
from math import isfinite

from backend.schema.asr.device_policy_schema import DevicePolicyDecision, DevicePolicyRequest


def _fresh(observed, ttl, now):
    return observed is not None and 0 <= (now - observed).total_seconds() <= ttl


def _provenance(request):
    kinds = [item.evidence_kind for item in request.groups]
    kinds.extend(item.evidence_kind for item in request.targets)
    kinds.extend(item.cpu_profile.evidence_kind for item in request.targets if item.cpu_profile is not None)
    return "isolated" if "isolated" in kinds else "provided" if kinds else "none"


def _common_reason(target, group, request):
    """明确的满额和缺失/过期事实分开，后者不能充当CPU回退理由。"""
    now = request.evaluated_at
    if target.compatibility_key != request.compatibility_key:
        return "MODEL_INCOMPATIBLE"
    if not target.compatibility_evidence:
        return "COMPATIBILITY_UNCONFIRMED"
    if target.device_kind == "unknown":
        return "DEVICE_UNKNOWN"
    if not target.device_evidence:
        return "DEVICE_UNCONFIRMED"
    if not _fresh(target.health_observed_at, target.health_ttl_seconds, now):
        return "HEALTH_OBSERVATION_UNCONFIRMED"
    if target.health_state != "ready":
        return "HEALTH_NOT_READY"
    if (not target.capacity_known or not group.capacity_known
            or not target.capacity_evidence or not group.capacity_evidence):
        return "CAPACITY_UNCONFIRMED"
    if (not _fresh(target.capacity_observed_at, target.capacity_ttl_seconds, now)
            or not _fresh(group.observed_at, group.ttl_seconds, now)):
        return "CAPACITY_OBSERVATION_UNCONFIRMED"
    if not _fresh(target.slot_observed_at, target.slot_ttl_seconds, now):
        return "SLOT_OBSERVATION_UNCONFIRMED"
    # slotReady表示绑定Worker执行能力已确认，并非“没有在途任务”。
    if not target.slot_ready:
        return "SLOT_NOT_READY"
    if ((target.resource_required and target.resource_available is None)
            or (group.resource_required and group.resource_available is None)):
        return "CAPACITY_UNCONFIRMED"
    exhausted = [item for item in (target, group) if item.resource_required and item.resource_available == 0]
    if exhausted:
        return "CAPACITY_FULL" if all(item.resource_exhaustion_confirmed for item in exhausted) else "CAPACITY_UNCONFIRMED"
    if target.held + target.uncertain >= target.limit or group.held + group.uncertain >= group.limit:
        return "CAPACITY_FULL"
    return "ELIGIBLE"


def _cpu_estimate(target, group, request):
    task, profile, now = request.task, target.cpu_profile, request.evaluated_at
    if not target.cpu_supported or not target.cpu_support_evidence:
        return "CPU_SUPPORT_UNCONFIRMED", None
    if profile is None:
        return "CPU_PROFILE_MISSING", None
    if not target.cpu_profile_usable:
        return "CPU_PROFILE_EXPIRED_OR_FUTURE", None
    if profile.target_revision != target.target_revision:
        return "CPU_PROFILE_REVISION_MISMATCH", None
    if not _fresh(profile.measured_at, profile.valid_for_seconds, now):
        return "CPU_PROFILE_EXPIRED_OR_FUTURE", None
    if not profile.min_audio_seconds <= task.audio_seconds <= profile.max_audio_seconds:
        return "CPU_AUDIO_OUTSIDE_MEASURED_RANGE", None
    # 不能把单槽样本直接推广到扩容后的目标或共享组。
    if max(target.limit, group.limit) > profile.max_tested_concurrency:
        return "CPU_CONCURRENCY_OUTSIDE_MEASURED_RANGE", None
    if task.cpu_budget_seconds is None:
        return "CPU_BUDGET_MISSING", None
    processing = (profile.fixed_overhead_seconds + task.audio_seconds * profile.seconds_per_audio_second) * profile.safety_factor
    waiting = profile.queue_wait_upper_bound_seconds
    total = processing + waiting
    if not isfinite(processing) or not isfinite(total) or processing <= 0:
        return "CPU_ESTIMATE_NONFINITE", None
    # queue_deadline只约束等待；不要求推理在排队截止时间之前结束。
    if waiting >= (task.queue_deadline_at - now).total_seconds():
        return "CPU_QUEUE_DEADLINE_INSUFFICIENT", None
    budgets = (task.cpu_budget_seconds, task.attempt_budget_seconds, task.remote_budget_seconds)
    if (any(total > budget for budget in budgets)
            or total >= (task.total_deadline_at - now).total_seconds()):
        return "CPU_EXECUTION_BUDGET_INSUFFICIENT", None
    return "ELIGIBLE", (total, processing, waiting)


class DevicePolicyService:
    @staticmethod
    def evaluate(request: DevicePolicyRequest) -> DevicePolicyDecision:
        request = DevicePolicyRequest.model_validate(request)
        evidence = _provenance(request)
        assessments = {}

        def result(reason, selected=None, estimate=None):
            return DevicePolicyDecision.model_validate({
                "action": "select" if selected is not None else "wait",
                "reasonCode": reason, "evaluatedAt": request.evaluated_at,
                "routingRevision": request.routing_revision,
                "targetId": selected.target_id if selected else None,
                "targetRevision": selected.target_revision if selected else None,
                "deviceKind": selected.device_kind if selected else None,
                "evidenceKind": evidence,
                "estimatedCpuSeconds": estimate[0] if estimate else None,
                "estimatedProcessingSeconds": estimate[1] if estimate else None,
                "estimatedQueueSeconds": estimate[2] if estimate else None,
                "candidates": [assessments[key] for key in sorted(assessments)],
            })

        task = request.task
        if (task.status != "queued" or task.attempt != 0 or task.has_permit
                or task.upstream_execution_started):
            return result("TASK_ALREADY_STARTED_OR_NOT_QUEUED")
        if request.routing_revision is None or request.compatibility_key is None:
            return result("IMMUTABLE_ROUTE_MISSING")
        if request.evaluated_at >= min(task.queue_deadline_at, task.total_deadline_at):
            return result("TASK_DEADLINE_EXPIRED")

        groups = {item.capacity_group_id: item for item in request.groups}
        reasons = {}
        for target in request.targets:
            reason = _common_reason(target, groups[target.capacity_group_id], request)
            reasons[target.target_id] = reason
            assessments[target.target_id] = {"targetId": target.target_id,
                "deviceKind": target.device_kind, "eligible": False, "reasonCode": reason}

        def ranked(target):
            return Fraction(target.held + target.uncertain, target.limit), target.target_id

        gpus = [target for target in request.targets if target.device_kind == "gpu"
                and target.compatibility_key == request.compatibility_key]
        cpus = [target for target in request.targets if target.device_kind == "cpu"
                and target.compatibility_key == request.compatibility_key]
        available_gpus = sorted((target for target in gpus if reasons[target.target_id] == "ELIGIBLE"), key=ranked)
        if available_gpus:
            for target in available_gpus:
                assessments[target.target_id].update(eligible=True, reasonCode="GPU_ELIGIBLE")
            for target in cpus:
                if reasons[target.target_id] == "ELIGIBLE":
                    assessments[target.target_id]["reasonCode"] = "CPU_WAIT_GPU_AVAILABLE"
            return result("GPU_PREFERRED", available_gpus[0])

        unknown_devices = any(target.device_kind == "unknown"
            and target.compatibility_key == request.compatibility_key for target in request.targets)
        if unknown_devices or request.gpu_configuration == "unknown":
            fallback = "GPU_STATE_UNCONFIRMED"
        elif request.gpu_configuration == "configured":
            fallback = ("CPU_FALLBACK_CONFIRMED_GPU_FULL" if gpus
                and all(reasons[target.target_id] == "CAPACITY_FULL" for target in gpus)
                else "GPU_STATE_UNCONFIRMED")
        else:
            fallback = "CPU_NO_GPU_CONFIGURED"
        if fallback == "GPU_STATE_UNCONFIRMED":
            for target in cpus:
                if reasons[target.target_id] == "ELIGIBLE":
                    assessments[target.target_id]["reasonCode"] = "CPU_WAIT_GPU_UNCONFIRMED"
            return result(fallback)

        usable_cpus = []
        for target in cpus:
            if reasons[target.target_id] != "ELIGIBLE":
                continue
            reason, estimate = _cpu_estimate(target, groups[target.capacity_group_id], request)
            assessments[target.target_id].update(eligible=reason == "ELIGIBLE", reasonCode=(
                "CPU_ELIGIBLE" if reason == "ELIGIBLE" else reason))
            if estimate is not None:
                usable_cpus.append((target, estimate))
        if not usable_cpus:
            return result("NO_ELIGIBLE_CPU_TARGET")
        selected, estimate = min(usable_cpus, key=lambda item: ranked(item[0]))
        return result(fallback, selected, estimate)
