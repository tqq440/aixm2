"""ASR 插件边界与结果兼容；无网络、模型加载或虚构的第二个执行器。"""

import pytest
from pydantic import ValidationError

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.schema.asr.asr_schema import AsrEngineMetadata, TranscriptionResult
from backend.service.asr.adapter_catalog import asr_execution_supported, configured_asr_plugin, resolve_asr_plugin


def test_existing_whisper_is_resolved_by_accurate_spec_without_loading_model(monkeypatch):
    settings = AppSettings()
    plugin = configured_asr_plugin(settings)
    constructed = []

    def factory(value):
        constructed.append(value)
        return object()

    monkeypatch.setattr("backend.service.asr.asr_service.AsrService", factory)
    spec = plugin.model_spec()
    assert spec.adapter_key == "faster-whisper"
    assert spec.model_version == settings.asr_model_revision
    assert plugin.queue_key == "asr.whisper-small"
    assert asr_execution_supported(spec)
    assert resolve_asr_plugin(spec) is plugin
    assert constructed == []
    assert plugin.create(settings) is not None
    assert constructed == [settings]


@pytest.mark.parametrize("field,value", [
    ("adapter_key", "unimplemented-asr"), ("model_id", "other-asr"), ("model_version", "other-version"),
    ("model_repo", "other/repo"), ("task_type", "vision"),
    ("resource_requirements", {"device": "cuda", "maxConcurrency": 10}),
    ("input_constraints", {"maxFileBytes": 999, "maxAudioSeconds": 300}),
    ("parameter_schema", {"languages": ["zh"]}), ("output_capabilities", ["text"]),
])
def test_registered_names_do_not_create_unsupported_execution_capabilities(field, value):
    spec = configured_asr_plugin(AppSettings()).model_spec().model_copy(update={field: value})
    assert not asr_execution_supported(spec)
    with pytest.raises(AppError) as caught:
        resolve_asr_plugin(spec)
    assert caught.value.code == "MODEL_EXECUTION_UNSUPPORTED"


def test_callers_cannot_mutate_catalog_through_returned_spec():
    plugin = configured_asr_plugin(AppSettings())
    spec = plugin.model_spec()
    spec.resource_requirements["device"] = "cuda"
    spec.parameter_schema["languages"].clear()
    assert not asr_execution_supported(spec)
    assert plugin.model_spec().resource_requirements["device"] == "cpu"
    assert len(plugin.model_spec().parameter_schema["languages"]) == 99


@pytest.mark.parametrize("field,value", [
    ("asr_model_repo", "unknown/repo"), ("asr_model_revision", "0" * 40),
    ("asr_device", "cuda"), ("asr_compute_type", "float16"),
])
def test_plugin_refuses_unvalidated_runtime_configuration(field, value):
    settings = AppSettings().model_copy(update={field: value})
    plugin = configured_asr_plugin(AppSettings())
    with pytest.raises(AppError):
        configured_asr_plugin(settings)
    with pytest.raises(AppError):
        plugin.create(settings)


def test_common_transcription_does_not_require_or_invent_whisper_facts():
    result = TranscriptionResult(text="通用结果契约，不代表真实第二模型", segments=[], audio_duration_seconds=1)
    assert result.language is None and result.language_probability is None
    assert result.engine_metadata is None
    assert result.device is None and result.faster_whisper_version is None and result.ctranslate2_version is None
    assert "engineMetadata" not in result.model_dump(by_alias=True)
    # 允许通用结果形状不代表现有 Whisper Worker 接受该结果。
    assert not configured_asr_plugin(AppSettings()).result_matches(AppSettings(), result)


def test_engine_specific_metadata_is_optional_and_not_converted_to_whisper_versions():
    result = TranscriptionResult(text="协议测试", segments=[], audio_duration_seconds=1,
        engine_metadata={"adapterKey": "future-asr", "runtimeVersions": {"future-library": "1.0"}})
    assert result.model_repo is None and result.cpu_threads is None
    assert result.faster_whisper_version is None and result.ctranslate2_version is None
    assert result.engine_metadata.runtime_versions == {"future-library": "1.0"}
    assert not configured_asr_plugin(AppSettings()).result_matches(AppSettings(), result)


@pytest.mark.parametrize("field,legacy,current", [
    ("model_repo", "repo/old", "repo/new"), ("model_revision", "old", "new"),
    ("device", "cpu", "cuda"), ("compute_type", "int8", "float16"), ("cpu_threads", 4, 8),
    ("model_load_seconds", 1, 2), ("transcription_seconds", 2, 3), ("total_seconds", 3, 4),
])
def test_conflicting_legacy_and_engine_fields_rejected(field, legacy, current):
    with pytest.raises(ValidationError):
        TranscriptionResult(text="", segments=[], audio_duration_seconds=1,
            **{field: legacy}, engine_metadata={"adapter_key": "faster-whisper", field: current})


@pytest.mark.parametrize("legacy,key", [("faster_whisper_version", "faster-whisper"), ("ctranslate2_version", "ctranslate2")])
def test_conflicting_runtime_versions_rejected(legacy, key):
    with pytest.raises(ValidationError):
        TranscriptionResult(text="", segments=[], audio_duration_seconds=1,
            **{legacy: "1.0"}, engine_metadata={"adapterKey": "faster-whisper", "runtimeVersions": {key: "2.0"}})


@pytest.mark.parametrize("metadata", [
    {"adapterKey": "faster-whisper", "endpoint": "http://private"},
    {"adapterKey": "faster-whisper", "apiKey": "private-key"},
    {"adapterKey": "faster-whisper", "totalSeconds": float("nan")},
    {"adapterKey": "faster-whisper", "cpuThreads": 0},
    {"adapterKey": "faster-whisper", "runtimeVersions": {"library": "http://private?key=secret"}},
])
def test_metadata_rejects_unexpected_sensitive_fields_and_invalid_measurements(metadata):
    with pytest.raises(ValidationError):
        AsrEngineMetadata.model_validate(metadata)


def test_whisper_profile_requires_accurate_identity_even_when_metadata_is_generic():
    settings = AppSettings()
    plugin = configured_asr_plugin(settings)
    base = {"text": "测试", "segments": [], "audio_duration_seconds": 1, "language": "zh",
            "engine_metadata": {"adapterKey": "faster-whisper", "modelRepo": settings.asr_model_repo,
                                "modelRevision": settings.asr_model_revision}}
    assert plugin.result_matches(settings, TranscriptionResult(**base))
    for changes in ({"adapterKey": "other"}, {"modelRepo": "other"}, {"modelRevision": "other"}):
        data = {**base, "engine_metadata": {**base["engine_metadata"], **changes}}
        assert not plugin.result_matches(settings, TranscriptionResult(**data))
