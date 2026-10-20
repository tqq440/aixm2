import pytest

from backend.common.exceptions.app_error import ConfigurationError
from backend.config.settings.app_settings import PROJECT_ROOT, load_settings
from backend.config.settings.model_environment import configure_model_cache


def test_asr_defaults_use_pinned_small_and_project_paths():
    settings = load_settings(None, environ={})
    assert settings.asr_model_dir == PROJECT_ROOT / "runtime/models/whisper-small"
    assert settings.asr_model_revision == "536b0662742c02347bc0e980a01041f333bce120"
    assert (settings.asr_device, settings.asr_compute_type) == ("cpu", "int8")
    assert (settings.asr_max_file_bytes, settings.asr_max_audio_seconds) == (20 * 1024 * 1024, 300)


@pytest.mark.parametrize(("name", "value"), [
    ("INFERENCE_ASR_DEVICE", "cuda"),
    ("INFERENCE_ASR_COMPUTE_TYPE", "float16"),
    ("INFERENCE_ASR_CPU_THREADS", "0"),
    ("INFERENCE_ASR_MAX_FILE_BYTES", "0"),
    ("INFERENCE_ASR_MAX_FILE_BYTES", "20971521"),
    ("INFERENCE_ASR_MAX_AUDIO_SECONDS", "0.000001"),
    ("INFERENCE_ASR_MAX_AUDIO_SECONDS", "301"),
    ("INFERENCE_ASR_MAX_AUDIO_SECONDS", "nan"),
    ("INFERENCE_ASR_MODEL_REVISION", "main"),
])
def test_invalid_asr_configuration_fails_before_running(name, value):
    with pytest.raises(ConfigurationError, match=name):
        load_settings(None, environ={name: value})


def test_xet_cache_is_project_local_without_creating_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_XET_CACHE", "previous")
    cache = configure_model_cache(tmp_path / "models/whisper-small")
    import os

    assert os.environ["HF_XET_CACHE"] == str(cache / "xet")
    assert cache == tmp_path / "models/.huggingface-cache"
    assert not cache.exists()
