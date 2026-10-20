import json
from uuid import UUID

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError


def test_health_checks_database_and_returns_camel_case_request_id(app):
    response = app.test_client().get("/api/health")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data"] == {"status": "ok", "database": "ok", "service": "inference-platform"}
    assert UUID(payload["requestId"])
    assert response.headers["X-Request-ID"] == payload["requestId"]
    assert response.headers["Cache-Control"] == "no-store"
    assert "request_id" not in payload
    assert inspect(app.extensions["database"].engine).get_table_names() == []


def test_valid_request_id_is_returned_and_recorded_in_json_log(app):
    request_id = "lesson-01.health_test"
    response = app.test_client().get("/api/health", headers={"X-Request-ID": request_id})
    assert response.get_json()["requestId"] == request_id
    log_file = app.config["APP_SETTINGS"].log_dir / "api.log"
    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert any(entry["requestId"] == request_id and "GET /api/health 200" in entry["message"] for entry in entries)


@pytest.mark.parametrize("request_id", ["", "bad id", "-bad", "含中文", "a" * 65])
def test_invalid_request_id_returns_safe_422_with_new_correlation_id(app, request_id):
    response = app.test_client().get("/api/health", headers={"X-Request-ID": request_id})
    assert response.status_code == 422
    payload = response.get_json()
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert payload["error"]["details"]
    assert UUID(payload["requestId"])
    assert response.headers["X-Request-ID"] == payload["requestId"]
    assert "input" not in payload["error"]["details"][0]


def test_health_rejects_unrecognized_query_parameters(app):
    response = app.test_client().get("/api/health?secret=private-value")
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"
    assert "private-value" not in response.get_data(as_text=True)


@pytest.mark.parametrize(("method", "path", "status"), [("GET", "/missing", 404), ("POST", "/api/health", 405)])
def test_http_errors_use_same_json_contract(app, method, path, status):
    response = app.test_client().open(path, method=method)
    assert response.status_code == status
    assert response.is_json
    payload = response.get_json()
    assert set(payload) == {"error", "requestId"}
    assert set(payload["error"]) == {"code", "message", "details"}
    assert payload["error"]["code"] == f"HTTP_{status}"
    assert response.headers["X-Request-ID"] == payload["requestId"]
    if status == 405:
        assert "GET" in response.headers["Allow"]


def test_database_failure_becomes_503_and_does_not_leak_database_details(app, monkeypatch):
    def unavailable():
        raise OperationalError("SELECT secret", {}, RuntimeError("private-database-path"))

    monkeypatch.setattr(app.extensions["database"], "check_connection", unavailable)
    response = app.test_client().get("/api/health", headers={"X-Request-ID": "db-unavailable"})
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert "private-database-path" not in response.get_data(as_text=True)
    entries = [json.loads(line) for line in (app.config["APP_SETTINGS"].log_dir / "api.log").read_text(encoding="utf-8").splitlines()]
    assert any(entry["requestId"] == "db-unavailable" and "private-database-path" in entry.get("exception", "") for entry in entries)


def test_unhandled_exception_is_safe_for_client_and_correlated_in_log(app):
    @app.get("/test-error")
    def unexpected_error():
        raise RuntimeError("private-exception-detail")

    response = app.test_client().get("/test-error", headers={"X-Request-ID": "failure-500"})
    assert response.status_code == 500
    assert response.get_json()["error"]["code"] == "INTERNAL_ERROR"
    assert "private-exception-detail" not in response.get_data(as_text=True)
    entries = [json.loads(line) for line in (app.config["APP_SETTINGS"].log_dir / "api.log").read_text(encoding="utf-8").splitlines()]
    assert any(entry["requestId"] == "failure-500" and "private-exception-detail" in entry.get("exception", "") for entry in entries)


def test_application_instances_do_not_share_database_or_logging(app_factory):
    first = app_factory(port=5001)
    second = app_factory(port=5002)
    assert first.config["APP_SETTINGS"].port == 5001
    assert second.config["APP_SETTINGS"].port == 5002
    assert first.extensions["database"] is not second.extensions["database"]
    assert first.extensions["app_logger"] is not second.extensions["app_logger"]
    first.test_client().get("/api/health", headers={"X-Request-ID": "only-first"})
    second.test_client().get("/api/health", headers={"X-Request-ID": "only-second"})
    first_log = (first.config["APP_SETTINGS"].log_dir / "api.log").read_text(encoding="utf-8")
    second_log = (second.config["APP_SETTINGS"].log_dir / "api.log").read_text(encoding="utf-8")
    assert "only-first" in first_log and "only-second" not in first_log
    assert "only-second" in second_log and "only-first" not in second_log
