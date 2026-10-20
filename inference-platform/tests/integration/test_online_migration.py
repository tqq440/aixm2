"""仅在隔离 SQLite 检查在线元数据迁移和公共输入边界。"""

from datetime import datetime, timedelta, timezone

from alembic import command
from alembic.config import Config
from pydantic import ValidationError
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.database.base import Base
from backend.data.database.database import Database
from backend.data.llm.online_models import OnlineRequestRecord
from backend.schema.llm.online_schema import (
    LlmStatus, OnlineParameters, OnlineRequestInput, OnlineRequestView,
)


@pytest.fixture
def online_database(tmp_path):
    settings = AppSettings(database_path=tmp_path / "online.sqlite3", log_dir=tmp_path / "logs")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    database = Database(settings)
    try:
        yield database, config
    finally:
        database.dispose()


def online_record(**overrides):
    now = datetime(2026, 10, 13, 2, 3, 4, 123456)
    values = dict(
        online_request_id="a" * 32, caller_id="local-user", model_id="test-llm",
        model_version="fixed-test", owner_id="b" * 32, request_fingerprint="c" * 64,
        accepted_at=now, updated_at=now, deadline_at=now + timedelta(seconds=60),
        lease_expires_at=now + timedelta(seconds=30),
    )
    values.update(overrides)
    return OnlineRequestRecord(**values)


def old_snapshot(database, tables):
    with database.engine.connect() as connection:
        return {
            table: {
                "sql": connection.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:table"), {"table": table}).scalar_one(),
                "rows": [dict(row) for row in connection.execute(text(f'SELECT * FROM "{table}" ORDER BY rowid')).mappings()],
            }
            for table in tables
        }


def test_0007_preserves_old_tables_and_models_through_upgrade_recheck_and_downgrade(tmp_path):
    settings = AppSettings(database_path=tmp_path / "upgrade.sqlite3", log_dir=tmp_path / "logs")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, "20261013_0006")
    database = Database(settings)
    try:
        old_tables = sorted(set(inspect(database.engine).get_table_names()) - {"alembic_version"})
        with database.engine.begin() as connection:
            for task_type, task_id in (("asr", "d" * 32), ("vision", "e" * 32)):
                connection.execute(text("""
                    INSERT INTO tasks (task_id, caller_id, task_type, model_id, model_version,
                        parameters, input_ref, input_filename, input_sha256, input_size_bytes,
                        input_media_type, audio_duration_seconds, image_width, image_height,
                        queue_key, status, attempt, created_at, updated_at, result_json)
                    VALUES (:id, 'local-user', :kind, :model, 'original-version', :parameters,
                        :id, :filename, :sha, 123, :media, :duration, :width, :height,
                        :queue, 'succeeded', 1, '2026-10-13 01:02:03.123456',
                        '2026-10-13 01:03:04.654321', :result)
                """), {
                    "id": task_id, "kind": task_type,
                    "model": "whisper-small" if task_type == "asr" else "yolo-general",
                    "parameters": '{"language":"zh"}' if task_type == "asr" else '{"confidence_threshold":0.25}',
                    "filename": "保留音频.wav" if task_type == "asr" else "保留图片.png",
                    "sha": "f" * 64, "media": "audio" if task_type == "asr" else "image",
                    "duration": 1.25 if task_type == "asr" else None,
                    "width": None if task_type == "asr" else 810,
                    "height": None if task_type == "asr" else 1080,
                    "queue": f"{task_type}.original",
                    "result": '{"text":"原始转写"}' if task_type == "asr" else '{"detections":[]}',
                })
                connection.execute(text("""
                    INSERT INTO idempotency_records (id, caller_id, idempotency_key, request_fingerprint, task_id)
                    VALUES (:id, 'local-user', :kind, :fingerprint, :id)
                """), {"id": task_id, "kind": task_type, "fingerprint": "1" * 64})
                connection.execute(text("""
                    INSERT INTO dispatch_events (event_id, task_id, queue_key, status)
                    VALUES (:id, :id, :queue, 'published')
                """), {"id": task_id, "queue": f"{task_type}.original"})
        before = old_snapshot(database, old_tables)
        command.upgrade(config, "20261013_0007")
        command.upgrade(config, "20261013_0007")
        assert old_snapshot(database, old_tables) == before
        with database.engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM online_requests")).scalar_one() == 0
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20261013_0007"
            assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        with database.transaction() as session:
            session.add(online_record())
        command.downgrade(config, "20261013_0006")
        assert "online_requests" not in inspect(database.engine).get_table_names()
        assert old_snapshot(database, old_tables) == before
        command.upgrade(config, "20261013_0007")
        assert old_snapshot(database, old_tables) == before
        # 0007 边界独立验收；当前 ORM 的无漂移检查在最新迁移上执行。
        command.upgrade(config, "head")
        command.check(config)
        current = old_snapshot(database, old_tables)
        assert {name: value for name, value in current.items() if name != "runtime_components"} == {
            name: value for name, value in before.items() if name != "runtime_components"}
        assert current["runtime_components"]["rows"] == before["runtime_components"]["rows"]
        assert "remote_asr_worker" in current["runtime_components"]["sql"]
    finally:
        database.dispose()


def test_online_record_defaults_columns_constraints_and_indexes_match(online_database):
    database, config = online_database
    with database.transaction() as session:
        record = online_record()
        session.add(record)
    stored = record.to_dict()
    assert stored["state"] == "accepted" and stored["permit_state"] == "waiting"
    assert stored["output_characters"] == 0
    assert stored["cancel_requested"] is False and stored["cancellation_confirmed"] is False
    assert stored["error_json"] is stored["first_token_at"] is stored["ended_at"] is None
    assert set(stored) == {
        "online_request_id", "caller_id", "model_id", "model_version", "instance_id", "owner_id",
        "request_fingerprint", "state", "permit_state", "accepted_at", "updated_at", "deadline_at",
        "lease_expires_at", "first_token_at", "ended_at", "output_characters", "finish_reason",
        "error_json", "cancel_requested", "cancellation_confirmed",
    }
    inspector = inspect(database.engine)
    table = Base.metadata.tables["online_requests"]
    assert inspector.get_foreign_keys(table.name) == []
    assert {item["name"] for item in inspector.get_check_constraints(table.name)} == {
        item.name for item in table.constraints if item.__class__.__name__ == "CheckConstraint"
    }
    assert {tuple(item["column_names"]) for item in inspector.get_indexes(table.name)} == {
        ("caller_id", "accepted_at", "online_request_id"),
        ("instance_id", "permit_state"), ("state", "lease_expires_at"),
    }
    command.check(config)


@pytest.mark.parametrize("invalid", [
    {"state": "running"}, {"permit_state": "available"}, {"output_characters": -1},
    {"online_request_id": "short"}, {"deadline_at": None}, {"lease_expires_at": None},
])
def test_online_database_rejects_invalid_lifecycle_metadata(online_database, invalid):
    database, _ = online_database
    with pytest.raises(IntegrityError), database.transaction() as session:
        session.add(online_record(**invalid))
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM online_requests")).scalar_one() == 0


@pytest.mark.parametrize("invalid", [
    {"temperature": True}, {"temperature": "0.7"}, {"temperature": -0.01},
    {"temperature": 2.01}, {"temperature": float("nan")}, {"temperature": float("inf")},
    {"maxTokens": True}, {"maxTokens": "512"}, {"maxTokens": 1.0},
    {"maxTokens": 0}, {"maxTokens": 2049}, {"topP": 0.9},
])
def test_online_parameters_reject_coercion_nonfinite_bounds_and_unknown_fields(invalid):
    with pytest.raises(ValidationError):
        OnlineParameters.model_validate(invalid)


def request_input(**overrides):
    values = {
        "onlineRequestId": "a" * 32, "modelId": "test-llm", "modelVersion": "v1.0+test",
        "messages": [{"role": "user", "content": "你好"}],
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize("invalid", [
    {"onlineRequestId": "A" * 32}, {"modelId": "bad/model"}, {"modelVersion": "bad version"},
    {"messages": []}, {"messages": [{"role": "user", "content": "x"}] * 33},
    {"messages": [{"role": "user", "content": ""}]},
    {"messages": [{"role": "tool", "content": "x"}]},
    {"messages": [{"role": "assistant", "content": "no user"}]},
    {"messages": [{"role": "user", "content": "x" * 16_001}]},
    {"messages": [{"role": "system", "content": "x" * 8000}, {"role": "user", "content": "y" * 8001}]},
    {"endpoint": "https://example.invalid"},
])
def test_online_request_limits_identity_messages_and_unknown_fields(invalid):
    with pytest.raises(ValidationError):
        OnlineRequestInput.model_validate(request_input(**invalid))


def test_online_request_accepts_exact_total_character_limit_and_default_parameters():
    request = OnlineRequestInput.model_validate(request_input(messages=[
        {"role": "system", "content": "中" * 8000}, {"role": "user", "content": "文" * 8000},
    ]))
    assert request.parameters.model_dump(by_alias=True) == {"temperature": 0.7, "maxTokens": 512}
    assert sum(len(item.content) for item in request.messages) == 16_000


def test_online_public_view_uses_utc_error_alias_and_excludes_private_metadata():
    values = online_record().to_dict()
    for key in ("owner_id", "request_fingerprint", "updated_at", "lease_expires_at"):
        values.pop(key)
    values.update(state="failed", permit_state="released", output_characters=3,
                  error_json={"code": "UPSTREAM_FAILED", "message": "上游连接失败"},
                  cancel_requested=False, cancellation_confirmed=False)
    values["first_token_at"] = datetime(2026, 10, 13, 10, tzinfo=timezone(timedelta(hours=8)))
    result = OnlineRequestView.model_validate(values)
    payload = result.model_dump(mode="json", by_alias=True)
    assert payload["error"] == {"code": "UPSTREAM_FAILED", "message": "上游连接失败"}
    assert payload["acceptedAt"].endswith("Z")
    assert payload["deadlineAt"].endswith("Z")
    assert payload["firstTokenAt"] == "2026-10-13T02:00:00Z"
    assert payload["endedAt"] is None
    assert not {"ownerId", "requestFingerprint", "updatedAt", "leaseExpiresAt", "errorJson"} & payload.keys()
    with pytest.raises(ValidationError):
        OnlineRequestView.model_validate({**values, "owner_id": "b" * 32})


def test_llm_status_cannot_claim_real_service_verification():
    values = dict(configured=False, availability="unconfigured", message="尚未配置",
                  max_concurrent_requests=1, active_requests=0, uncertain_permits=0)
    assert LlmStatus(**values).model_dump(by_alias=True)["serviceVerified"] is False
    with pytest.raises(ValidationError):
        LlmStatus(**values, service_verified=True)
