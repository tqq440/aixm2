"""验证提交结果不明确、并发重复请求和孤立文件维护的边界。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import os
import wave

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import PROJECT_ROOT
from backend.data.files.upload_store import UnsafeUploadPathError
from backend.schema.files.cleanup_schema import CleanupOptions
from backend.service.files.upload_cleanup_service import clean_uploads


@pytest.fixture
def upload_case(app_factory, tmp_path):
    app = app_factory(upload_dir=tmp_path / "uploads")
    settings = app.config["APP_SETTINGS"]
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 1600)
    content = stream.getvalue()

    def submit(key="recovery"):
        with app.test_client() as client:
            return client.post("/api/tasks/asr", headers={"Idempotency-Key": key},
                               data={"file": (io.BytesIO(content), "sample.wav")})

    return app, submit, content


def test_simultaneous_same_key_requests_create_one_task_and_one_object(upload_case):
    app, submit, _ = upload_case
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: submit(), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert len({response.get_json()["data"]["task"]["taskId"] for response in responses}) == 1
    assert len(list(app.config["APP_SETTINGS"].upload_dir.rglob("*.audio"))) == 1


def test_valid_audio_across_multipart_chunks_is_not_rejected(upload_case):
    app, _, _ = upload_case
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        # CR 字节会使 multipart 解析器保留部分边界；覆盖 64 KiB 跨块情况。
        audio.writeframes(b"\r\0" * 80000)
    with app.test_client() as client:
        response = client.post("/api/tasks/asr", headers={"Idempotency-Key": "multipart-chunks"},
                               data={"file": (io.BytesIO(buffer.getvalue()), "five-seconds.wav")})
    assert response.status_code == 201, response.get_json()
    assert response.get_json()["data"]["task"]["input"]["durationSeconds"] == 5


def test_commit_completed_but_reported_error_recovers_authoritative_task(upload_case, monkeypatch):
    app, submit, _ = upload_case
    repository = app.extensions["task_service"].repository
    original = repository.create_with_event

    def uncertain_commit(**kwargs):
        original(**kwargs)
        raise OperationalError("commit outcome unknown", {}, RuntimeError("connection lost"))

    monkeypatch.setattr(repository, "create_with_event", uncertain_commit)
    response = submit()
    assert response.status_code == 200
    assert response.get_json()["data"]["replayed"] is True
    assert len(list(app.config["APP_SETTINGS"].upload_dir.rglob("*.audio"))) == 1


def test_unverifiable_commit_preserves_file_until_maintenance(upload_case, monkeypatch):
    app, submit, _ = upload_case
    service = app.extensions["task_service"]
    original_find = service.repository.find_idempotent
    calls = 0

    def failing_find(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_find(*args)
        raise OperationalError("lookup", {}, RuntimeError("offline"))

    def failed_write(**kwargs):
        raise OperationalError("commit", {}, RuntimeError("offline"))

    monkeypatch.setattr(service.repository, "find_idempotent", failing_find)
    monkeypatch.setattr(service.repository, "create_with_event", failed_write)
    response = submit()
    assert response.status_code == 503
    files = list(service.settings.upload_dir.rglob("*.audio"))
    assert len(files) == 1
    report = clean_uploads(service.settings, CleanupOptions(apply=True), app.extensions["app_logger"])
    assert report.deleted == 0  # 未满 24 小时，不立即回收未知结果。
    old = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    os.utime(files[0], (old, old))
    report = clean_uploads(service.settings, CleanupOptions(apply=True), app.extensions["app_logger"])
    assert report.deleted == 1
    assert not files[0].exists()


def test_maintenance_dry_run_and_apply_keep_referenced_and_recent_files(upload_case):
    app, submit, content = upload_case
    assert submit().status_code == 201
    service = app.extensions["task_service"]
    referenced = next(service.settings.upload_dir.rglob("*.audio"))
    with service.store.lock():
        orphan = service.store.save(io.BytesIO(content), max_bytes=10000)
        recent = service.store.save(io.BytesIO(content), max_bytes=10000)
    old = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    for path in (referenced, orphan.path):
        os.utime(path, (old, old))
    temporary = service.settings.upload_dir / "staging" / ("a" * 32 + ".part")
    temporary.write_bytes(b"partial upload")
    os.utime(temporary, (old, old))
    logger = app.extensions["app_logger"]
    report = clean_uploads(service.settings, CleanupOptions(), logger)
    assert set(report.candidates) == {orphan.path.name, temporary.name}
    assert report.deleted == 0 and orphan.path.exists() and temporary.exists()
    report = clean_uploads(service.settings, CleanupOptions(apply=True), logger)
    assert report.deleted == 2
    assert referenced.exists() and recent.path.exists()
    assert not orphan.path.exists() and not temporary.exists()


def test_maintenance_database_failure_does_not_delete_files(upload_case):
    app, _, content = upload_case
    service = app.extensions["task_service"]
    orphan = service.store.save(io.BytesIO(content), max_bytes=10000)
    old = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    os.utime(orphan.path, (old, old))
    with app.extensions["database"].engine.begin() as connection:
        connection.execute(text("DROP TABLE tasks"))
    with pytest.raises(AppError) as caught:
        clean_uploads(service.settings, CleanupOptions(apply=True), app.extensions["app_logger"])
    assert caught.value.code == "TASK_STORAGE_UNAVAILABLE"
    assert orphan.path.exists()


def test_storage_path_refusal_has_explicit_safe_error(upload_case, monkeypatch):
    app, submit, _ = upload_case

    def fail_path(*args, **kwargs):
        raise UnsafeUploadPathError("private path contains junction")

    monkeypatch.setattr(app.extensions["task_service"].store, "save", fail_path)
    response = submit()
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "UPLOAD_STORAGE_UNAVAILABLE"
    assert "private path" not in response.get_data(as_text=True)
