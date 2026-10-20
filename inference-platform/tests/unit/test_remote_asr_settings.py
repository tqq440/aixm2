"""外部配置只验证契约，不连接实际网络。"""

import pytest
from pydantic import ValidationError

from backend.common.exceptions.app_error import ConfigurationError
from backend.config.settings.app_settings import AppSettings, load_settings
from backend.service.asr.adapter_catalog import (
    asr_execution_supported, config_fingerprint, configured_asr_plugin, remote_asr_configured,
    remote_asr_model_spec, resolve_asr_plugin,
)
from backend.service.asr.remote_catalog import remote_asr_model_version
from backend.utils.json.json_utils import canonical_json_sha256


def configured(**changes):
    return AppSettings(**{"remote_asr_base_url": "https://isolated.test:8003",
                          "remote_asr_model_id": "fixture-sensevoice",
                          "remote_asr_model_version": "fixture-deployment-1", **changes})


def test_default_is_not_configured_and_local_identity_is_unchanged():
    settings = load_settings(None, environ={})
    assert not remote_asr_configured(settings)
    assert settings.remote_asr_connect_timeout_seconds == 5
    assert settings.remote_asr_total_timeout_seconds == 600
    assert settings.remote_asr_insecure_tls_origin is None
    assert configured_asr_plugin(settings).adapter_key == "faster-whisper"


@pytest.mark.parametrize("changes", [
    {"remote_asr_model_id": None}, {"remote_asr_base_url": None},
    {"remote_asr_base_url": "http://isolated.test:8003"},
    {"remote_asr_base_url": "https://user:secret@isolated.test:8003"},
    {"remote_asr_base_url": "https://isolated.test:8003?secret=yes"},
    {"remote_asr_base_url": "https://isolated.test:8003#fragment"},
    {"remote_asr_base_url": "https://isolated.test:8003/api/v1/asr"},
    {"remote_asr_base_url": "https://isolated.test:0"},
    {"remote_asr_base_url": "https://isolated.test:99999"},
    {"remote_asr_base_url": "https://isolated.test:\\evil"},
    {"remote_asr_base_url": "https://isolated.test:8003\n"},
    {"remote_asr_connect_timeout_seconds": 0}, {"remote_asr_total_timeout_seconds": float("nan")},
])
def test_invalid_or_partial_configuration_is_rejected(changes):
    with pytest.raises(ValidationError):
        configured(**changes)


def test_settings_errors_do_not_expose_endpoint():
    with pytest.raises(ConfigurationError) as caught:
        load_settings(None, environ={"INFERENCE_REMOTE_ASR_BASE_URL": "https://user:secret@private.test"})
    assert "secret" not in str(caught.value) and "private.test" not in str(caught.value)


def test_ca_is_local_file_and_does_not_disable_tls(tmp_path):
    with pytest.raises(ValidationError):
        configured(remote_asr_ca_file=tmp_path / "missing.pem")
    ca = tmp_path / "ca.pem"
    ca.write_text("test-file-content-not-a-valid-certificate")
    settings = configured(remote_asr_ca_file=ca)
    assert settings.remote_asr_ca_file == ca
    assert "remote_asr_verify" not in AppSettings.model_fields


def test_remote_catalog_requires_full_matching_spec_and_config():
    settings = configured()
    plugin = configured_asr_plugin(settings, profile="remote")
    spec = plugin.model_spec()
    assert spec.model_repo is None
    assert spec.model_version == "fixture-deployment-1" and spec.model_version != "14.2"
    assert spec.version_info is None and "version_info" not in spec.model_dump()
    assert "versionInfo" not in spec.model_dump(by_alias=True)
    assert spec.adapter_key == "remote-v2-sensevoice"
    assert plugin.queue_key == "asr.remote-v2-sensevoice"
    assert spec.parameter_schema["languageMode"] == "server"
    assert not asr_execution_supported(spec)
    assert asr_execution_supported(spec, settings)
    assert resolve_asr_plugin(spec, settings).adapter_key == plugin.adapter_key
    assert not asr_execution_supported(spec.model_copy(update={"model_version": "other"}), settings)
    assert not asr_execution_supported(spec.model_copy(update={"input_constraints": {"maxFileBytes": 1}}), settings)
    spec.parameter_schema["languages"].append("zh")
    assert not asr_execution_supported(spec, settings)
    assert remote_asr_model_spec(settings).parameter_schema["languages"] == ["auto"]


def test_binding_fingerprint_is_private_stable_and_changes_with_target():
    settings = configured()
    assert len(config_fingerprint(settings)) == 64
    assert config_fingerprint(settings) == config_fingerprint(configured(remote_asr_base_url="https://isolated.test:8003/"))
    assert config_fingerprint(settings) != config_fingerprint(configured(remote_asr_base_url="https://other.test:8003"))
    assert config_fingerprint(settings) != config_fingerprint(configured(remote_asr_model_version="other"))


@pytest.mark.parametrize("origin", [
    "", " ", "*", "https://*", "https://*.test:8003", "http://isolated.test:8003",
    "https://isolated.test:8003/", "https://isolated.test:8003/api",
    "https://user:secret@isolated.test:8003", "https://isolated.test:8003?",
    "https://isolated.test:8003#", "https://isolated.test:8003?x=1",
    "https://isolated.test:8003\\other", "https://isolated.test:8003\n",
    "https://isolated.test:0", "https://isolated.test:99999", "https://isolated.test:",
    "https://isolated%2etest:8003", "https://other.test:8003", "https://isolated.test:8004",
])
def test_insecure_tls_requires_an_exact_origin_without_path_or_wildcard(origin):
    with pytest.raises(ValidationError):
        configured(remote_asr_insecure_tls_origin=origin)


@pytest.mark.parametrize("base,origin,expected", [
    ("https://ISOLATED.test:8003/", "https://isolated.TEST:8003", "https://isolated.test:8003"),
    ("https://isolated.test/", "https://isolated.test:443", "https://isolated.test"),
    ("https://[::1]:8003/", "https://[::1]:8003", "https://[::1]:8003"),
])
def test_insecure_tls_origin_is_normalized_before_matching(base, origin, expected):
    settings = configured(remote_asr_base_url=base, remote_asr_insecure_tls_origin=origin)
    assert settings.remote_asr_insecure_tls_origin == expected
    assert expected not in repr(settings)


@pytest.mark.parametrize("base,origin", [
    ("https://faß.test:8003", "https://faß.test:8003"),
    ("https://faß.test:8003", "https://fass.test:8003"),
    ("https://fass.test:8003", "https://faß.test:8003"),
])
def test_tls_exception_rejects_unicode_hosts_and_idna_aliases(base, origin):
    with pytest.raises(ValidationError):
        configured(remote_asr_base_url=base, remote_asr_insecure_tls_origin=origin)


def test_tls_exception_accepts_explicit_ascii_idna_without_restricting_strict_mode():
    origin = "https://xn--fa-hia.test:8003"
    assert configured(remote_asr_base_url=origin,
                      remote_asr_insecure_tls_origin=origin).remote_asr_insecure_tls_origin == origin
    assert configured(remote_asr_base_url="https://faß.test:8003").remote_asr_insecure_tls_origin is None


def test_insecure_tls_requires_full_configuration_and_cannot_use_ca(tmp_path):
    origin = "https://isolated.test:8003"
    with pytest.raises(ValidationError):
        AppSettings(remote_asr_insecure_tls_origin=origin)
    for missing in ("remote_asr_base_url", "remote_asr_model_id"):
        with pytest.raises(ValidationError):
            configured(**{missing: None}, remote_asr_insecure_tls_origin=origin)
    ca = tmp_path / "ca.pem"
    ca.write_text("fixture-not-a-valid-certificate")
    with pytest.raises(ValidationError):
        configured(remote_asr_insecure_tls_origin=origin, remote_asr_ca_file=ca)


def test_insecure_tls_environment_is_explicit_private_and_rejects_changed_target():
    values = {"INFERENCE_REMOTE_ASR_BASE_URL": "https://isolated.test:8003",
              "INFERENCE_REMOTE_ASR_MODEL_ID": "fixture-sensevoice",
              "INFERENCE_REMOTE_ASR_MODEL_VERSION": "fixture-deployment-1",
              "INFERENCE_REMOTE_ASR_INSECURE_TLS_ORIGIN": "https://isolated.test:8003"}
    assert load_settings(None, environ=values).remote_asr_insecure_tls_origin == values["INFERENCE_REMOTE_ASR_BASE_URL"]
    values["INFERENCE_REMOTE_ASR_BASE_URL"] = "https://changed-private.test:8003"
    with pytest.raises(ConfigurationError) as caught:
        load_settings(None, environ=values)
    assert "changed-private" not in str(caught.value) and "isolated.test" not in str(caught.value)


def test_tls_policy_fingerprint_preserves_strict_history_and_binds_the_exception():
    strict = configured()
    legacy_binding = {"profile": "remote-v2-sensevoice", "baseUrl": "https://isolated.test:8003",
                      "modelId": "fixture-sensevoice", "modelVersion": "fixture-deployment-1", "caFile": None}
    assert config_fingerprint(strict) == canonical_json_sha256(legacy_binding)
    insecure = configured(remote_asr_insecure_tls_origin="https://isolated.test:8003")
    assert config_fingerprint(insecure) != config_fingerprint(strict)
    changed = configured(remote_asr_base_url="https://other.test:8003",
                         remote_asr_insecure_tls_origin="https://other.test:8003")
    assert config_fingerprint(changed) != config_fingerprint(insecure)
    assert config_fingerprint(configured()) == config_fingerprint(strict)


@pytest.mark.parametrize("version", [None, "", "   "])
def test_unknown_upstream_version_uses_an_explicit_local_integration_revision(version):
    settings = configured(remote_asr_model_version=version)
    assert settings.remote_asr_model_version is None and remote_asr_configured(settings)
    spec = remote_asr_model_spec(settings)
    assert spec.model_version.startswith("integration-") and len(spec.model_version) == 60
    assert spec.model_version == remote_asr_model_version(settings)
    assert spec.version_info.version_basis == "integration_revision"
    assert spec.version_info.upstream_version is None
    assert spec.model_dump(by_alias=True)["versionInfo"] == {
        "versionBasis": "integration_revision", "upstreamVersion": None,
    }
    assert asr_execution_supported(spec, settings)
    assert not asr_execution_supported(spec.model_copy(update={"version_info": None}), settings)


def test_unknown_version_environment_needs_only_address_and_model_id():
    values = {"INFERENCE_REMOTE_ASR_BASE_URL": "https://isolated.test:8003",
              "INFERENCE_REMOTE_ASR_MODEL_ID": "fixture-sensevoice"}
    without = load_settings(None, environ=values)
    values["INFERENCE_REMOTE_ASR_MODEL_VERSION"] = ""
    blank = load_settings(None, environ=values)
    assert remote_asr_model_version(without) == remote_asr_model_version(blank)
    with pytest.raises(ValidationError):
        AppSettings(remote_asr_model_version="provided-upstream-release")


def test_local_revision_is_stable_and_changes_only_with_connection_identity(tmp_path):
    settings = configured(remote_asr_model_version=None)
    version = remote_asr_model_version(settings)
    assert remote_asr_model_version(configured(remote_asr_model_version=None)) == version
    assert remote_asr_model_version(configured(remote_asr_model_version=None,
        remote_asr_base_url="https://isolated.test:8003/")) == version
    assert remote_asr_model_version(configured(remote_asr_model_version=None,
        remote_asr_total_timeout_seconds=300)) == version
    ca = tmp_path / "trusted-ca.pem"
    ca.write_text("isolated-certificate-placeholder")
    for changes in ({"remote_asr_base_url": "https://different.test:8003"},
                    {"remote_asr_model_id": "another-platform-model"},
                    {"remote_asr_ca_file": ca},
                    {"remote_asr_insecure_tls_origin": settings.remote_asr_base_url}):
        altered = configured(remote_asr_model_version=None, **changes)
        assert remote_asr_model_version(altered) != version
        assert config_fingerprint(altered) != config_fingerprint(settings)


def test_unknown_identity_cannot_be_relabelled_as_known_or_backfill_an_old_spec():
    unknown = configured(remote_asr_model_version=None)
    old_spec = remote_asr_model_spec(unknown)
    pretending_known = configured(remote_asr_model_version=old_spec.model_version)
    assert config_fingerprint(unknown) != config_fingerprint(pretending_known)
    assert not asr_execution_supported(old_spec, pretending_known)
    known = configured(remote_asr_model_version="provided-release-2")
    assert remote_asr_model_version(known) == "provided-release-2"
    assert remote_asr_model_spec(known).version_info is None
    assert old_spec.version_info.upstream_version is None
    assert old_spec.model_version == remote_asr_model_version(unknown)
