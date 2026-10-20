import pytest
from pydantic import ValidationError

from backend.common.exceptions.app_error import ConfigurationError
from backend.config.settings.app_settings import AppSettings, load_settings


def test_execution_defaults_and_environment_are_validated():
    settings = load_settings(env_file=None, environ={"INFERENCE_LEASE_SECONDS": "45", "INFERENCE_NOTIFICATION_VISIBILITY_SECONDS": "45"})
    assert settings.lease_seconds == 45
    assert settings.renew_interval_seconds == 10
    assert settings.runtime_stale_seconds == 15


@pytest.mark.parametrize("overrides", [
    {"lease_seconds": 20}, {"notification_visibility_seconds": 20},
    {"runtime_heartbeat_seconds": 8}, {"poll_interval_seconds": 3},
    {"lease_seconds": float("inf")}, {"worker_stop_timeout_seconds": 0},
])
def test_unsafe_or_unbounded_intervals_are_rejected(overrides):
    with pytest.raises(ValidationError):
        AppSettings(**overrides)


def test_cross_field_configuration_error_is_sanitized_without_index_error():
    with pytest.raises(ConfigurationError, match="运行间隔组合"):
        load_settings(env_file=None, environ={"INFERENCE_RENEW_INTERVAL_SECONDS": "25"})


def test_recovery_budgets_are_configurable_and_total_can_limit_an_attempt():
    settings = load_settings(env_file=None, environ={
        "INFERENCE_QUEUE_TIMEOUT_SECONDS": "7", "INFERENCE_ATTEMPT_TIMEOUT_SECONDS": "9",
        "INFERENCE_TOTAL_TIMEOUT_SECONDS": "3", "INFERENCE_MAX_ATTEMPTS": "1",
        "INFERENCE_RETRY_WAIT_SECONDS": "0.5", "INFERENCE_MAX_INFLIGHT_TASKS": "2",
    })
    assert settings.total_timeout_seconds == 3 < settings.attempt_timeout_seconds
    assert settings.queue_timeout_seconds == 7 and settings.retry_wait_seconds == 0.5
    assert settings.max_attempts == 1 and settings.max_inflight_tasks == 2


@pytest.mark.parametrize("overrides", [
    {"max_attempts": 0}, {"max_inflight_tasks": 21}, {"queue_timeout_seconds": 0},
    {"attempt_timeout_seconds": float("inf")}, {"total_timeout_seconds": float("nan")},
    {"retry_wait_seconds": -1},
])
def test_recovery_budgets_reject_invalid_or_unbounded_values(overrides):
    with pytest.raises(ValidationError):
        AppSettings(**overrides)
