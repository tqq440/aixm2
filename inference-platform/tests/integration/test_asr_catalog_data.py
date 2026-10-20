"""0013 与目录仓储的隔离 SQLite 验证；不访问日常数据库或外部服务。"""

from datetime import datetime, timedelta
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import event, inspect, select, text
from sqlalchemy.exc import IntegrityError

from backend.config.settings.app_settings import AppSettings, PROJECT_ROOT
from backend.data.asr.catalog_models import AsrCatalogRevision, AsrResourceObservation
from backend.data.asr.catalog_repository import AsrCatalogRepository
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.asr.target_repository import AsrTargetRepository
from backend.data.database.database import Database
from backend.data.tasks.task_models import DispatchEvent, IdempotencyRecord, TaskRecord


NOW = datetime(2026, 10, 20, 1, 2, 3, 456000)
REVISION = "20261020_0013"
PREVIOUS = "20261019_0012"
NEW_TABLES = {"asr_catalog_revisions", "asr_resource_observations"}
REPOSITORY = AsrCatalogRepository()
TARGETS = AsrTargetRepository()


def setup_database(tmp_path, revision=REVISION):
    settings = AppSettings(database_path=tmp_path / "catalog.sqlite3", log_dir=tmp_path / "logs")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    command.upgrade(config, revision)
    return Database(settings), config


@pytest.fixture
def catalog_database(tmp_path):
    database, config = setup_database(tmp_path)
    try:
        yield database, config
    finally:
        database.dispose()


def add_target(session, target_id="target-a", revision_id="revision-a", *, created_at=NOW):
    if TARGETS.get_group(session, "shared-fixture") is None:
        TARGETS.add_group(session, group_id="shared-fixture", capacity_limit=1, created_at=NOW)
        session.flush()
    target = TARGETS.get_target(session, target_id)
    if target is None:
        target = TARGETS.add_target(session, target_id=target_id, capacity_group_id="shared-fixture",
                                   enabled=False, target_limit=1, created_at=NOW)
        session.flush()
    revision = None
    if revision_id is not None:
        revision = TARGETS.add_revision(session, revision_id=revision_id, target_id=target_id,
            config_fingerprint=(uuid4().hex * 2), endpoint_identity="b" * 64,
            config_snapshot={"fixtureOnly": True}, device_kind="unknown", created_at=created_at)
        session.flush()
    return target, revision


def add_catalog(session, catalog_id="catalog-a", target_id="target-a", revision_id="revision-a", **changes):
    values = dict(catalog_revision_id=catalog_id, target_id=target_id, revision_id=revision_id,
                  metadata_snapshot={"fixtureOnly": True, "deviceKind": "unknown"}, created_at=NOW)
    values.update(changes)
    return REPOSITORY.add_catalog(session, **values)


def add_observation(session, observation_id="observation-a", **changes):
    values = dict(observation_id=observation_id, catalog_revision_id="catalog-a", scope="target",
                  source="fixture-only", evidence_kind="isolated", observed_at=NOW, ttl_seconds=60,
                  payload={"healthy": None, "availableConcurrency": None}, recorded_at=NOW)
    values.update(changes)
    return REPOSITORY.add_observation(session, **values)


def snapshot(database, tables):
    with database.engine.connect() as connection:
        return {name: {
            "schema": connection.execute(text(
                "SELECT type,name,sql FROM sqlite_master WHERE tbl_name=:name ORDER BY type,name"
            ), {"name": name}).all(),
            "rows": connection.execute(text(f'SELECT * FROM "{name}" ORDER BY rowid')).all(),
        } for name in tables}


def seed_old_business_rows(session, revision):
    """让升级保护覆盖真实列契约的非空任务、结果、幂等、旧调用与许可。"""
    task_id = uuid4().hex
    for kind, identifier in (("asr", task_id), ("vision", uuid4().hex)):
        session.add(TaskRecord(
            task_id=identifier, caller_id="fixture-user", task_type=kind, model_id="fixture-model",
            model_version="fixture-version", parameters={"fixtureOnly": True}, input_ref=uuid4().hex,
            input_filename="fixture.wav" if kind == "asr" else "fixture.png", input_sha256="e" * 64,
            input_size_bytes=10, input_media_type="audio" if kind == "asr" else "image",
            audio_duration_seconds=1.5 if kind == "asr" else None,
            image_width=10 if kind == "vision" else None, image_height=10 if kind == "vision" else None,
            queue_key="fixture-queue", status="failed" if kind == "asr" else "succeeded",
            attempt=1, result_json=None if kind == "asr" else {"detections": [], "fixtureOnly": True},
            created_at=NOW, updated_at=NOW, queue_deadline_at=NOW + timedelta(minutes=10),
            total_deadline_at=NOW + timedelta(minutes=30),
        ))
        session.flush()
        session.add(IdempotencyRecord(id=uuid4().hex, caller_id="fixture-user", idempotency_key=kind,
                                     request_fingerprint="f" * 64, task_id=identifier, created_at=NOW))
        session.add(DispatchEvent(event_id=uuid4().hex, task_id=identifier, queue_key="fixture-queue",
                                  status="published", available_at=NOW, created_at=NOW, published_at=NOW))
    worker = TARGETS.add_worker(session, session_id=uuid4().hex, revision_id=revision.revision_id,
        worker_key="fixture-worker", slot_index=0, active_slot_key=None, pid=123, state="stopped",
        heartbeat_at=NOW, started_at=NOW, ended_at=NOW)
    session.flush()
    token = uuid4().hex
    session.add(RemoteAsrCall(task_id=task_id, profile_id="fixture-profile",
        config_fingerprint=revision.config_fingerprint, request_id=task_id, state="uncertain",
        worker_id=worker.session_id, lease_token=token, created_at=NOW, reserved_at=NOW))
    TARGETS.add_permit(session, task_id=task_id, revision_id=revision.revision_id,
        capacity_group_id="shared-fixture", worker_session_id=worker.session_id,
        lease_token=token, state="uncertain", created_at=NOW)


def test_0013_is_additive_preserves_every_old_table_and_seeds_no_resource_fact(tmp_path):
    database, config = setup_database(tmp_path, PREVIOUS)
    try:
        with database.transaction() as session:
            target, revision = add_target(session)
            TARGETS.set_fields(target, enabled=True)
            seed_old_business_rows(session, revision)
        old_tables = sorted(set(inspect(database.engine).get_table_names()) - {"alembic_version"})
        before = snapshot(database, old_tables)
        command.upgrade(config, REVISION)
        command.upgrade(config, REVISION)
        assert snapshot(database, old_tables) == before
        assert set(inspect(database.engine).get_table_names()) - set(old_tables) == NEW_TABLES | {"alembic_version"}
        with database.engine.connect() as connection:
            for name in NEW_TABLES:
                assert connection.scalar(text(f'SELECT count(*) FROM "{name}"')) == 0
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
            assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        command.downgrade(config, PREVIOUS)
        assert snapshot(database, old_tables) == before
        assert not NEW_TABLES & set(inspect(database.engine).get_table_names())
        command.upgrade(config, REVISION)
        command.upgrade(config, "head")
        command.check(config)
        assert snapshot(database, old_tables) == before
    finally:
        database.dispose()


def test_0013_refuses_lossy_downgrade_and_keeps_observations(catalog_database):
    database, config = catalog_database
    with database.transaction() as session:
        add_target(session)
        add_catalog(session)
        add_observation(session)
    names = sorted(inspect(database.engine).get_table_names())
    before = snapshot(database, names)
    with pytest.raises(RuntimeError, match="拒绝有损降级"):
        command.downgrade(config, PREVIOUS)
    assert snapshot(database, names) == before


def test_catalog_constraints_foreign_keys_unique_ids_and_base_to_dict(catalog_database):
    database, config = catalog_database
    with database.transaction() as session:
        add_target(session)
        original = add_catalog(session)
        assert original.sequence > 0
        assert original.to_dict()["metadata_snapshot"] == {"fixtureOnly": True, "deviceKind": "unknown"}
    invalid = [
        {"catalog_revision_id": "catalog-a"}, {"catalog_revision_id": ""},
        {"catalog_revision_id": "x" * 65}, {"target_id": "missing"}, {"revision_id": "missing"},
    ]
    for changes in invalid:
        with pytest.raises(IntegrityError), database.transaction() as session:
            add_catalog(session, catalog_id="fresh", **changes)
    with database.session_factory() as session:
        assert REPOSITORY.get_catalog(session, "missing") is None
        assert REPOSITORY.latest_catalog(session, "missing") is None
        assert REPOSITORY.get_catalog(session, "catalog-a").to_dict() == original.to_dict()
    command.upgrade(config, "head")
    command.check(config)


@pytest.mark.parametrize("changes", [
    {"observation_id": ""}, {"observation_id": "x" * 65}, {"catalog_revision_id": "missing"},
    {"scope": "gpu"}, {"evidence_kind": "real_gpu"}, {"source": ""}, {"source": " "},
    {"source": "x" * 65}, {"ttl_seconds": 0}, {"ttl_seconds": 3601}, {"observed_at": None},
])
def test_observation_constraints_reject_invalid_resource_metadata(catalog_database, changes):
    database, _ = catalog_database
    with database.transaction() as session:
        add_target(session)
        add_catalog(session)
    with pytest.raises(IntegrityError), database.transaction() as session:
        add_observation(session, **changes)
    with database.session_factory() as session:
        assert REPOSITORY.latest_observations(session, ["catalog-a"]) == {}


def test_observations_append_and_choose_sample_time_before_insert_order(catalog_database):
    database, _ = catalog_database
    with database.transaction() as session:
        add_target(session)
        first_catalog = add_catalog(session)
        first = add_observation(session)
        first_saved = first.to_dict()
        later = add_observation(session, "later", observed_at=NOW + timedelta(seconds=10))
        # 采样更旧、录入更晚，不得取代更新的样本。
        add_observation(session, "late-arrival", observed_at=NOW - timedelta(seconds=10),
                        recorded_at=NOW + timedelta(seconds=20))
        tie = add_observation(session, "same-sample-time", observed_at=later.observed_at)
        group = add_observation(session, "group", scope="capacity_group")
        newer_catalog = add_catalog(session, "catalog-b", metadata_snapshot={"fixtureOnly": True, "label": "new"})
        other = add_observation(session, "other-catalog", catalog_revision_id="catalog-b")
        assert newer_catalog.sequence > first_catalog.sequence
        assert REPOSITORY.latest_catalog(session, "target-a").catalog_revision_id == "catalog-b"
        assert REPOSITORY.get_catalog(session, "catalog-a").metadata_snapshot == first_catalog.metadata_snapshot
    with database.session_factory() as session:
        found = REPOSITORY.latest_observations(session, ["catalog-a", "catalog-b"])
        assert {key: row.observation_id for key, row in found.items()} == {
            ("catalog-a", "target"): tie.observation_id,
            ("catalog-a", "capacity_group"): group.observation_id,
            ("catalog-b", "target"): other.observation_id,
        }
        assert REPOSITORY.get_observation(session, "observation-a").to_dict() == first_saved
        assert REPOSITORY.get_observation(session, "missing") is None
        assert len(session.scalars(select(AsrResourceObservation)).all()) == 6
        assert REPOSITORY.latest_observations(session, []) == {}
    with pytest.raises(IntegrityError), database.transaction() as session:
        add_observation(session)


def test_target_page_keeps_legacy_and_catalog_bound_revision_without_n_plus_one(catalog_database):
    database, _ = catalog_database
    with database.transaction() as session:
        add_target(session, "target-a", "a-pinned")
        add_catalog(session, "a-old", "target-a", "a-pinned")
        add_catalog(session, "a-current", "target-a", "a-pinned", metadata_snapshot={"label": "current"})
        add_target(session, "target-a", "a-newer-connection", created_at=NOW + timedelta(days=1))
        add_target(session, "target-b", "b-old", created_at=NOW - timedelta(days=1))
        add_target(session, "target-b", "b-new-first")
        add_target(session, "target-b", "b-new-last")
        add_target(session, "target-c", None)
        add_observation(session, catalog_revision_id="a-current")
    queries = []

    def count_query(_connection, _cursor, statement, _parameters, _context, _executemany):
        queries.append(statement)

    event.listen(database.engine, "before_cursor_execute", count_query)
    try:
        with database.session_factory() as session:
            total, rows = REPOSITORY.page_targets(session, 0, 10)
            observations = REPOSITORY.latest_observations(session, [row[2].catalog_revision_id for row in rows if row[2]])
            assert total == 3 and len(rows) == 3
            assert [(target.target_id, catalog.catalog_revision_id if catalog else None,
                     revision.revision_id if revision else None) for target, _, catalog, revision in rows] == [
                ("target-a", "a-current", "a-pinned"), ("target-b", None, "b-new-last"), ("target-c", None, None),
            ]
            assert all(group.capacity_limit == target.target_limit == 1 and not target.enabled
                       for target, group, _, _ in rows)
            assert set(observations) == {("a-current", "target")}
            assert len(queries) == 3  # COUNT + 一页联接 + 按页批量观测，逐行访问不会再查询。
    finally:
        event.remove(database.engine, "before_cursor_execute", count_query)
    with database.session_factory() as session:
        total, middle = REPOSITORY.page_targets(session, 1, 1)
        assert total == 3 and middle[0][0].target_id == "target-b"
        total, past_end = REPOSITORY.page_targets(session, 3, 1)
        assert total == 3 and past_end == []
    with database.engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
