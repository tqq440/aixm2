"""固定 Remote-V2 调用配置与模型身份，不读取网络或自动登记模型。"""

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.schema.asr.asr_schema import TranscriptionResult
from backend.schema.models.model_schema import ModelRegistration
from backend.utils.json.json_utils import canonical_json_sha256

REMOTE_ADAPTER_KEY = "remote-v2-sensevoice"
REMOTE_QUEUE_KEY = "asr.remote-v2-sensevoice"
REMOTE_PROFILE_ID = "remote-v2-sensevoice"


def _protocol_binding(settings: AppSettings) -> dict:
    """仅追加新协议配置，默认 Remote-V2 的历史字节身份保持不变。"""
    optional = {
        "protocol": (settings.remote_asr_protocol, "remote-v2"),
        "transcribePath": (settings.remote_asr_transcribe_path, "/api/v1/asr/transcripts"),
        "healthPath": (settings.remote_asr_health_path, "/api/v1/asr/health"),
        "upstreamModel": (settings.remote_asr_upstream_model, None),
        "apiKeyEnv": (settings.remote_asr_api_key_env, None),
    }
    return {name: value for name, (value, default) in optional.items() if value != default}


def remote_asr_configured(settings: AppSettings) -> bool:
    return bool(settings.remote_asr_base_url and settings.remote_asr_model_id)


def remote_asr_model_version(settings: AppSettings) -> str:
    """已知时使用上游版本；未知时只标识本地接入配置，不能证明远端模型固定。"""
    if not remote_asr_configured(settings):
        raise AppError("REMOTE_ASR_NOT_CONFIGURED", "外部 ASR 尚未配置实际连接和平台模型标识。", 503)
    if settings.remote_asr_model_version is not None:
        return settings.remote_asr_model_version
    binding = {
        "revisionScheme": "remote-v2-sync-integration-v1", "adapterKey": REMOTE_ADAPTER_KEY,
        "baseUrl": settings.remote_asr_base_url.rstrip("/"), "modelId": settings.remote_asr_model_id,
        "caFile": str(settings.remote_asr_ca_file) if settings.remote_asr_ca_file else None,
        "insecureTlsOrigin": settings.remote_asr_insecure_tls_origin,
    }
    binding.update(_protocol_binding(settings))
    return "integration-" + canonical_json_sha256(binding)[:48]


def remote_asr_model_spec(settings: AppSettings) -> ModelRegistration:
    if not remote_asr_configured(settings):
        raise AppError("REMOTE_ASR_NOT_CONFIGURED", "外部 ASR 尚未配置实际连接和平台模型标识。", 503)
    version_info = ({"version_basis": "integration_revision", "upstream_version": None}
                    if settings.remote_asr_model_version is None else None)
    return ModelRegistration(
        model_id=settings.remote_asr_model_id, model_version=remote_asr_model_version(settings),
        version_info=version_info,
        model_repo=None, task_type="asr", execution_mode="offline", adapter_key=REMOTE_ADAPTER_KEY,
        parameter_schema={"defaultLanguage": "auto", "languages": ["auto"], "languageMode": "server"},
        input_constraints={"maxFileBytes": 20 * 1024 * 1024, "maxAudioSeconds": 300.0},
        output_capabilities=["text", "segments"],
        # 历史模型规范保持不可变；此处为登记时单调用槽的基础要求。
        # 2B多个槽的运行限额独立存于计算目标/共享容量组，不改写旧模型版本。
        resource_requirements={"managementMode": "external", "maxConcurrency": 1},
    )


def is_remote_asr_spec(spec: ModelRegistration) -> bool:
    return spec.task_type == "asr" and spec.adapter_key == REMOTE_ADAPTER_KEY


def config_fingerprint(settings: AppSettings) -> str:
    """绑定固定通道的目标配置；不向结果或 HTTP 输出连接原文。"""
    remote_asr_model_spec(settings)
    binding = {
        "profile": REMOTE_PROFILE_ID,
        "baseUrl": settings.remote_asr_base_url.rstrip("/"),
        "modelId": settings.remote_asr_model_id,
        "modelVersion": remote_asr_model_version(settings),
        "caFile": str(settings.remote_asr_ca_file) if settings.remote_asr_ca_file else None,
    }
    if settings.remote_asr_insecure_tls_origin is not None:
        # 严格模式保留原绑定结构；策略改变不得复用已排队任务的旧指纹。
        binding["insecureTlsOrigin"] = settings.remote_asr_insecure_tls_origin
    if settings.remote_asr_model_version is None:
        binding["versionBasis"] = "integration_revision"
    binding.update(_protocol_binding(settings))
    return canonical_json_sha256(binding)


def remote_result_matches(settings: AppSettings, result: TranscriptionResult) -> bool:
    metadata = result.engine_metadata
    allowed_versions = {"remote-v2-api"} if settings.remote_asr_protocol == "remote-v2" else set()
    return bool(remote_asr_configured(settings) and metadata is not None
                and metadata.adapter_key == REMOTE_ADAPTER_KEY
                and all(getattr(metadata, name) is None for name in (
                    "model_repo", "model_revision", "device", "compute_type", "cpu_threads",
                    "model_load_seconds", "transcription_seconds", "total_seconds"))
                and set(metadata.runtime_versions).issubset(allowed_versions)
                and result.language is None and result.language_requested is None
                and result.language_probability is None
                and all(getattr(result, name) is None for name in (
                    "model_repo", "model_revision", "device", "compute_type", "cpu_threads",
                    "model_load_seconds", "transcription_seconds", "total_seconds",
                    "faster_whisper_version", "ctranslate2_version")))
