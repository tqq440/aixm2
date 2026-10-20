"""代码控制的 ASR 插件目录；登记一份 ModelSpec 不会安装或启用新执行器。"""

from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from backend.common.exceptions.app_error import AppError
from backend.config.asr.model_catalog import MODEL_ID, QUEUE_KEY, SUPPORTED_LANGUAGES
from backend.config.settings.app_settings import AppSettings
from backend.schema.asr.asr_schema import TranscriptionRequest, TranscriptionResult
from backend.schema.models.model_schema import ModelRegistration
from backend.service.asr.model_preparation_service import MODEL_REPO, MODEL_REVISION
from backend.service.asr.remote_catalog import (
    REMOTE_ADAPTER_KEY, REMOTE_QUEUE_KEY, config_fingerprint, is_remote_asr_spec,
    remote_asr_configured, remote_asr_model_spec, remote_result_matches,
)
from backend.utils.json.json_utils import json_values_equal


class AsrAdapter(Protocol):
    """仅调用契约；取消、租约和进程退出确认继续由既有 Worker 管理。"""

    def prepare_model(self) -> float: ...

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult: ...


def _whisper_spec() -> ModelRegistration:
    # 每次返回独立规范，调用者不能修改目录内的嵌套字典或列表。
    return ModelRegistration(
        model_id=MODEL_ID, model_version=MODEL_REVISION, model_repo=MODEL_REPO,
        task_type="asr", execution_mode="offline", adapter_key="faster-whisper",
        parameter_schema={"defaultLanguage": "zh", "autoDetectionValue": "auto", "languages": sorted(SUPPORTED_LANGUAGES)},
        input_constraints={"maxFileBytes": 20 * 1024 * 1024, "maxAudioSeconds": 300.0},
        output_capabilities=["text", "segments", "language"],
        resource_requirements={"device": "cpu", "computeType": "int8", "maxConcurrency": 1},
    )


def _create_whisper(settings: AppSettings) -> AsrAdapter:
    # 延迟导入且只构造对象；实际文件核验和模型加载仍在 prepare_model 中进行。
    from backend.service.asr.asr_service import AsrService

    _validate_whisper_settings(settings)
    return AsrService(settings)


def _validate_whisper_settings(settings: AppSettings) -> None:
    if (settings.asr_model_repo != MODEL_REPO or settings.asr_model_revision != MODEL_REVISION
            or settings.asr_device != "cpu" or settings.asr_compute_type != "int8"):
        raise AppError("MODEL_EXECUTION_UNSUPPORTED", "当前 ASR 插件尚不支持该模型或运行配置。", 422)


def _whisper_result_matches(settings: AppSettings, result: TranscriptionResult) -> bool:
    metadata = result.engine_metadata
    if metadata is not None and metadata.adapter_key != "faster-whisper":
        return False
    repo = metadata.model_repo if metadata is not None and metadata.model_repo is not None else result.model_repo
    revision = (metadata.model_revision if metadata is not None and metadata.model_revision is not None
                else result.model_revision)
    return (repo == MODEL_REPO == settings.asr_model_repo
            and revision == MODEL_REVISION == settings.asr_model_revision
            and result.language in SUPPORTED_LANGUAGES
            and (result.language_requested is None or result.language == result.language_requested))


@dataclass(frozen=True)
class AsrPlugin:
    """一个已由代码实现并评审的插件；没有动态模块名、shell 命令或网络下载入口。"""

    adapter_key: str
    queue_key: str
    model_spec: Callable[[], ModelRegistration]
    factory: Callable[[AppSettings], AsrAdapter]
    result_matches: Callable[[AppSettings, TranscriptionResult], bool]

    def create(self, settings: AppSettings) -> AsrAdapter:
        return self.factory(settings)


_PLUGINS = MappingProxyType({
    "faster-whisper": AsrPlugin("faster-whisper", QUEUE_KEY, _whisper_spec, _create_whisper, _whisper_result_matches),
})


def configured_remote_asr_plugin(settings: AppSettings) -> AsrPlugin:
    remote_asr_model_spec(settings)
    from backend.service.asr.remote_v2_adapter import RemoteV2AsrAdapter

    return AsrPlugin(REMOTE_ADAPTER_KEY, REMOTE_QUEUE_KEY, lambda: remote_asr_model_spec(settings),
                     RemoteV2AsrAdapter, remote_result_matches)


def resolve_asr_plugin(spec: ModelRegistration, settings: AppSettings | None = None) -> AsrPlugin:
    """完整规范必须匹配代码目录；不因 adapter_key 相同就承认任意模型或资源声明。"""
    spec = ModelRegistration.model_validate(spec.model_dump())
    plugin = _PLUGINS.get(spec.adapter_key)
    if is_remote_asr_spec(spec) and settings is not None and remote_asr_configured(settings):
        plugin = configured_remote_asr_plugin(settings)
    if plugin is None or not json_values_equal(spec.model_dump(), plugin.model_spec().model_dump()):
        raise AppError("MODEL_EXECUTION_UNSUPPORTED", "当前 ASR 插件尚不支持该模型版本或规范。", 422)
    return plugin


def asr_execution_supported(spec: ModelRegistration, settings: AppSettings | None = None) -> bool:
    try:
        resolve_asr_plugin(spec, settings)
    except AppError:
        return False
    return True


def configured_asr_plugin(settings: AppSettings, profile: str = "local") -> AsrPlugin:
    """只允许代码内明确实现的本机或外部通道，不开放动态代码与命令。"""
    if profile == "remote":
        return configured_remote_asr_plugin(settings)
    if profile != "local":
        raise AppError("MODEL_EXECUTION_UNSUPPORTED", "不支持此 ASR 执行通道。", 422)
    plugin = _PLUGINS["faster-whisper"]
    _validate_whisper_settings(settings)
    return plugin
