"""第三部分真实 SQLite / PCM 音频接口测试；不调用或下载推理模型。"""

import hashlib
import io
import json
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from werkzeug.datastructures import MultiDict

from backend.config.settings.app_settings import PROJECT_ROOT


@pytest.fixture
def audio_bytes():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 2000)
    return stream.getvalue()


@pytest.fixture
def task_app(app_factory, tmp_path):
    app = app_factory(upload_dir=tmp_path / "uploads")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = app.config["APP_SETTINGS"]
    command.upgrade(config, "head")
    return app


@pytest.fixture
def submit(task_app, audio_bytes):
    def create(key="task-test-1", *, audio=None, name="中文样例.wav", fields=None):
        data = {"file": (io.BytesIO(audio_bytes if audio is None else audio), name)}
        data.update(fields or {})
        headers = {} if key is None else {"Idempotency-Key": key}
        return task_app.test_client().post("/api/tasks/asr", data=data, headers=headers)

    return create


@pytest.fixture
def stored_counts(task_app):
    def counts():
        with task_app.extensions["database"].engine.connect() as connection:
            return {
                name: connection.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar_one()
                for name in ("tasks", "idempotency_records", "dispatch_events")
            }

    return counts


@pytest.fixture
def saved_audio_files(task_app):
    def files():
        return sorted(task_app.config["APP_SETTINGS"].upload_dir.rglob("*.audio"))

    return files


def test_models_report_fixed_supported_model_without_loading_weights(task_app):
    response = task_app.test_client().get("/api/models")
    assert response.status_code == 200
    model = response.get_json()["data"]["items"][0]
    assert model["modelId"] == "whisper-small"
    assert model["modelVersion"] == "536b0662742c02347bc0e980a01041f333bce120"
    assert model["taskType"] == "asr"
    assert len(response.get_json()["data"]["items"]) == 1


def test_creation_returns_created_task_and_commits_all_three_records(task_app, submit, stored_counts, saved_audio_files, audio_bytes):
    response = submit()
    assert response.status_code == 201, response.get_json()
    envelope = response.get_json()
    assert UUID(envelope["requestId"])
    assert envelope["data"]["replayed"] is False
    task = envelope["data"]["task"]
    assert UUID(task["taskId"]).hex == task["taskId"]
    assert task["callerId"] == "local-user"
    assert task["taskType"] == "asr"
    assert task["modelId"] == "whisper-small"
    assert task["modelVersion"] == "536b0662742c02347bc0e980a01041f333bce120"
    assert task["parameters"] == {"language": "zh"}
    assert task["status"] == "created"
    assert task["executionPlanVersion"] == 1
    assert task["input"] == {
        "fileName": "中文样例.wav",
        "sizeBytes": len(audio_bytes),
        "sha256": hashlib.sha256(audio_bytes).hexdigest(),
        "durationSeconds": 0.125,
    }
    assert task["createdAt"] == task["updatedAt"]
    assert task["message"] == "任务已保存，等待投递器发送通知。"
    assert "inputRef" not in json.dumps(task)
    assert "filePath" not in json.dumps(task)
    assert str(task_app.config["APP_SETTINGS"].upload_dir) not in response.get_data(as_text=True)
    assert stored_counts() == {"tasks": 1, "idempotency_records": 1, "dispatch_events": 1}
    saved = saved_audio_files()
    assert len(saved) == 1
    assert saved[0].read_bytes() == audio_bytes
    detail = task_app.test_client().get(f"/api/tasks/{task['taskId']}")
    assert detail.status_code == 200
    assert detail.get_json()["data"] == task


def test_same_key_and_normalized_content_returns_original_even_when_name_changes(submit, stored_counts, saved_audio_files):
    original = submit().get_json()["data"]["task"]
    replay = submit(name="换个名字.wav", fields={"modelId": "whisper-small", "language": "zh"})
    assert replay.status_code == 200
    assert replay.get_json()["data"] == {"task": original, "replayed": True}
    assert stored_counts() == {"tasks": 1, "idempotency_records": 1, "dispatch_events": 1}
    assert len(saved_audio_files()) == 1


@pytest.mark.parametrize("change", ["audio", "language"])
def test_same_key_with_changed_content_conflicts_and_removes_extra_file(submit, stored_counts, saved_audio_files, audio_bytes, change):
    assert submit().status_code == 201
    changed = {"audio": audio_bytes[:-2] + b"\x01\x00"} if change == "audio" else {"fields": {"language": "auto"}}
    response = submit(**changed)
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert stored_counts() == {"tasks": 1, "idempotency_records": 1, "dispatch_events": 1}
    assert len(saved_audio_files()) == 1


def test_different_keys_can_create_separate_tasks_with_same_audio(submit, stored_counts, saved_audio_files):
    first = submit("first").get_json()["data"]["task"]["taskId"]
    second = submit("second", fields={"language": "auto"})
    assert second.status_code == 201
    assert second.get_json()["data"]["task"]["taskId"] != first
    assert second.get_json()["data"]["task"]["parameters"]["language"] is None
    assert stored_counts() == {"tasks": 2, "idempotency_records": 2, "dispatch_events": 2}
    assert len(saved_audio_files()) == 2


def test_list_is_paginated_and_returns_total_without_duplicate_items(task_app, submit):
    created = {submit(f"page-{index}").get_json()["data"]["task"]["taskId"] for index in range(3)}
    client = task_app.test_client()
    page_one = client.get("/api/tasks?page=1&pageSize=2").get_json()["data"]
    page_two = client.get("/api/tasks?page=2&pageSize=2").get_json()["data"]
    assert (page_one["total"], page_one["page"], page_one["pageSize"]) == (3, 1, 2)
    assert (page_two["total"], page_two["page"], page_two["pageSize"]) == (3, 2, 2)
    assert len(page_one["items"]) == 2
    assert len(page_two["items"]) == 1
    all_ids = [item["taskId"] for item in page_one["items"] + page_two["items"]]
    assert set(all_ids) == created and len(all_ids) == len(set(all_ids))
    assert client.get("/api/tasks?page=3&pageSize=2").get_json()["data"]["items"] == []


@pytest.mark.parametrize("path", [
    "/api/models?unknown=1", "/api/tasks?unknown=1", "/api/tasks?page=0", "/api/tasks?pageSize=101",
    "/api/tasks?pageSize=0", "/api/tasks?page=abc", "/api/tasks?page=1&page=2",
    "/api/tasks/invalid", "/api/tasks/" + "a" * 32 + "?unknown=1",
])
def test_query_and_path_input_is_validated(task_app, path):
    response = task_app.test_client().get(path)
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"


def test_missing_task_is_clear_404(task_app):
    response = task_app.test_client().get("/api/tasks/" + "0" * 32)
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "TASK_NOT_FOUND"


@pytest.mark.parametrize("key", [None, "", "bad key", "-bad", "a" * 129])
def test_idempotency_key_is_required_and_validated(submit, stored_counts, saved_audio_files, key):
    response = submit(key)
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "VALIDATION_ERROR"
    assert stored_counts() == {"tasks": 0, "idempotency_records": 0, "dispatch_events": 0}
    assert saved_audio_files() == []


@pytest.mark.parametrize("fields", [
    {"callerId": "someone-else"}, {"audioPath": "C:/private.wav"}, {"modelId": "another-model"},
    {"language": "unsupported-language"}, {"language": ""}, {"executionPlanVersion": "2"},
])
def test_upload_form_cannot_override_identity_paths_or_fixed_contract(submit, stored_counts, saved_audio_files, fields):
    response = submit(fields=fields)
    assert response.status_code == 422
    assert stored_counts() == {"tasks": 0, "idempotency_records": 0, "dispatch_events": 0}
    assert saved_audio_files() == []


@pytest.mark.parametrize("shape", ["no-file", "two-files", "extra-file", "duplicate-language", "unexpected-query"])
def test_upload_requires_one_file_and_unambiguous_parameters(task_app, audio_bytes, stored_counts, shape):
    data = MultiDict()
    if shape != "no-file":
        data.add("file", (io.BytesIO(audio_bytes), "one.wav"))
    if shape == "two-files":
        data.add("file", (io.BytesIO(audio_bytes), "two.wav"))
    if shape == "extra-file":
        data.add("another", (io.BytesIO(audio_bytes), "two.wav"))
    if shape == "duplicate-language":
        data.add("language", "zh")
        data.add("language", "en")
    path = "/api/tasks/asr?extra=1" if shape == "unexpected-query" else "/api/tasks/asr"
    response = task_app.test_client().post(path, data=data, content_type="multipart/form-data", headers={"Idempotency-Key": "shape"})
    assert response.status_code == 422
    assert stored_counts() == {"tasks": 0, "idempotency_records": 0, "dispatch_events": 0}


def test_corrupt_audio_is_not_accepted_and_is_removed(submit, stored_counts, saved_audio_files):
    response = submit(audio=b"This is not a WAV file.")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "AUDIO_DECODE_FAILED"
    assert stored_counts() == {"tasks": 0, "idempotency_records": 0, "dispatch_events": 0}
    assert saved_audio_files() == []


def test_file_save_failure_never_commits_or_claims_acceptance(task_app, submit, stored_counts, saved_audio_files, monkeypatch):
    def unavailable(stream, *, max_bytes):
        raise OSError("private-upload-directory")

    monkeypatch.setattr(task_app.extensions["task_service"].store, "save", unavailable)
    response = submit()
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "UPLOAD_STORAGE_UNAVAILABLE"
    assert "private-upload-directory" not in response.get_data(as_text=True)
    assert stored_counts() == {"tasks": 0, "idempotency_records": 0, "dispatch_events": 0}
    assert saved_audio_files() == []


@pytest.mark.parametrize(("overrides", "code"), [
    ({"asr_max_file_bytes": 100}, "AUDIO_TOO_LARGE"),
    ({"asr_max_audio_seconds": 0.05}, "AUDIO_TOO_LONG"),
])
def test_upload_limits_reject_and_remove_partial_or_complete_file(app_factory, tmp_path, audio_bytes, overrides, code):
    app = app_factory(upload_dir=tmp_path / "limited-uploads", **overrides)
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = app.config["APP_SETTINGS"]
    command.upgrade(config, "head")
    response = app.test_client().post("/api/tasks/asr", data={"file": (io.BytesIO(audio_bytes), "limit.wav")}, headers={"Idempotency-Key": "limited"})
    assert response.status_code == 413
    assert response.get_json()["error"]["code"] == code
    with app.extensions["database"].engine.connect() as connection:
        for table in ("tasks", "idempotency_records", "dispatch_events"):
            assert connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0
    assert list(app.config["APP_SETTINGS"].upload_dir.rglob("*.audio")) == []
    assert list(app.config["APP_SETTINGS"].upload_dir.rglob("*.part")) == []


def test_write_failure_rolls_back_task_idempotency_and_event_and_compensates_file(task_app, submit, stored_counts, saved_audio_files):
    engine = task_app.extensions["database"].engine

    def fail_event_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO DISPATCH_EVENTS"):
            raise OperationalError("private-sql", {}, RuntimeError("private-db-failure"))

    event.listen(engine, "before_cursor_execute", fail_event_insert)
    try:
        response = submit("retry-after-failure")
    finally:
        event.remove(engine, "before_cursor_execute", fail_event_insert)
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "TASK_STORAGE_UNAVAILABLE"
    assert "private-db-failure" not in response.get_data(as_text=True)
    assert stored_counts() == {"tasks": 0, "idempotency_records": 0, "dispatch_events": 0}
    assert saved_audio_files() == []
    assert submit("retry-after-failure").status_code == 201


def test_unmigrated_database_returns_storage_error_and_never_accepts_upload(app_factory, tmp_path, audio_bytes):
    app = app_factory(upload_dir=tmp_path / "unmigrated-uploads")
    client = app.test_client()
    for path in ("/api/tasks", "/api/tasks/" + "0" * 32):
        response = client.get(path)
        assert response.status_code == 503
        assert response.get_json()["error"]["code"] == "TASK_STORAGE_UNAVAILABLE"
    response = client.post("/api/tasks/asr", data={"file": (io.BytesIO(audio_bytes), "test.wav")}, headers={"Idempotency-Key": "unmigrated"})
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "TASK_STORAGE_UNAVAILABLE"
    assert list(app.config["APP_SETTINGS"].upload_dir.rglob("*.audio")) == []


def test_admission_deadlines_and_attempt_budget_are_fixed_on_replay(task_app, submit):
    service = task_app.extensions["task_service"]
    service.settings = service.settings.model_copy(update={
        "queue_timeout_seconds": 17, "total_timeout_seconds": 42, "max_attempts": 3,
    })
    first = submit().get_json()["data"]["task"]
    accepted = datetime.fromisoformat(first["createdAt"])
    assert (datetime.fromisoformat(first["queueDeadlineAt"]) - accepted).total_seconds() == 17
    assert (datetime.fromisoformat(first["totalDeadlineAt"]) - accepted).total_seconds() == 42
    assert first["maxAttempts"] == 3 and first["attempt"] == 0
    assert first["attemptDeadlineAt"] is first["nextRetryAt"] is None
    service.settings = service.settings.model_copy(update={"queue_timeout_seconds": 999, "max_attempts": 1})
    assert submit().get_json()["data"]["task"] == first


def test_capacity_limit_is_atomic_preserves_idempotency_and_cleans_rejected_file(task_app, submit, stored_counts, saved_audio_files):
    service = task_app.extensions["task_service"]
    service.settings = service.settings.model_copy(update={"max_inflight_tasks": 1})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, ("capacity-a", "capacity-b")))
    assert sorted(response.status_code for response in responses) == [201, 429]
    winner = next(response for response in responses if response.status_code == 201).get_json()["data"]["task"]
    rejected = next(response for response in responses if response.status_code == 429)
    assert rejected.get_json()["error"]["code"] == "TASK_CAPACITY_EXCEEDED"
    assert stored_counts() == {"tasks": 1, "idempotency_records": 1, "dispatch_events": 1}
    assert len(saved_audio_files()) == 1
    winning_key = "capacity-a" if responses[0].status_code == 201 else "capacity-b"
    assert submit(winning_key).get_json()["data"]["task"] == winner
    cancelled = task_app.test_client().post(f"/api/tasks/{winner['taskId']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.get_json()["data"]["task"]["status"] == "cancelled"
    assert submit("after-capacity-released").status_code == 201


@pytest.mark.parametrize("inflight_status", ["created", "queued", "running", "retry_wait", "cancel_requested"])
def test_all_nonterminal_states_count_towards_capacity(task_app, submit, inflight_status):
    service = task_app.extensions["task_service"]
    service.settings = service.settings.model_copy(update={"max_inflight_tasks": 1})
    task_id = submit().get_json()["data"]["task"]["taskId"]
    with task_app.extensions["database"].engine.begin() as connection:
        connection.execute(text("UPDATE tasks SET status=:status WHERE task_id=:task_id"),
                           {"status": inflight_status, "task_id": task_id})
    assert submit("second").status_code == 429
