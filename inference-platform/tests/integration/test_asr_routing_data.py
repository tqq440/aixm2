"""0014 与任务路由仓储的隔离验证，不访问日常库或外部服务。"""

from datetime import timedelta
from uuid import uuid4

from alembic import command
import pytest
from sqlalchemy import event, inspect, select, text
from sqlalchemy.exc import IntegrityError

from backend.data.asr.routing_models import AsrRoutingRevision, AsrTaskRoute
from backend.data.asr.routing_repository import AsrRoutingRepository
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.execution.attempt_models import ExecutionAttempt
from backend.data.execution.execution_repository import ExecutionRepository
from backend.data.tasks.task_models import TaskRecord
from tests.integration.test_asr_catalog_data import (
    NOW, TARGETS, add_catalog, add_observation, add_target, seed_old_business_rows,
    setup_database, snapshot,
)


REVISION = "20261020_0014"
PREVIOUS = "20261020_0013"
NEW_TABLES = {"asr_routing_revisions", "asr_task_routes"}
REPOSITORY = AsrRoutingRepository()


@pytest.fixture
def routing_database(tmp_path):
    database, config = setup_database(tmp_path, REVISION)
    try:
        yield database, config
    finally:
        database.dispose()


def add_revision(session, **changes):
    values = dict(routing_revision_id="routing-a", model_id="fixture-model",
        model_version="fixture-version", evidence_kind="isolated",
        snapshot={"catalogRevisionIds": ["catalog-a"], "fixtureOnly": True}, created_at=NOW)
    values.update(changes)
    return REPOSITORY.add_revision(session, **values)


def add_task(session):
    task = TaskRecord(task_id=uuid4().hex, caller_id="fixture-user", task_type="asr",
        model_id="fixture-model", model_version="fixture-version", parameters={"language": None},
        input_ref=uuid4().hex, input_filename="fixture.wav", input_sha256="e" * 64,
        input_size_bytes=10, input_media_type="audio", audio_duration_seconds=1.5,
        queue_key="asr.remote-v2-sensevoice", status="queued", attempt=0, max_attempts=1,
        created_at=NOW, updated_at=NOW, queue_deadline_at=NOW + timedelta(minutes=10),
        total_deadline_at=NOW + timedelta(minutes=30))
    session.add(task)
    session.flush()
    return task


def add_routed_task(session):
    add_target(session)
    add_catalog(session)
    add_revision(session)
    task = add_task(session)
    route = REPOSITORY.add_task_route(session, task_id=task.task_id,
        routing_revision_id="routing-a", created_at=NOW)
    return task, route


def test_0014_preserves_all_old_tables_and_empty_upgrade_downgrade_roundtrip(tmp_path):
    database, config = setup_database(tmp_path, PREVIOUS)
    try:
        with database.transaction() as session:
            target, connection = add_target(session)
            TARGETS.set_fields(target, enabled=True)
            seed_old_business_rows(session, connection)
            add_catalog(session)
            add_observation(session)
        old_tables = sorted(set(inspect(database.engine).get_table_names()) - {"alembic_version"})
        before = snapshot(database, old_tables)
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        command.check(config)
        assert snapshot(database, old_tables) == before
        assert set(inspect(database.engine).get_table_names()) - set(old_tables) == NEW_TABLES | {"alembic_version"}
        with database.engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
            assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            for name in NEW_TABLES:
                assert connection.scalar(text(f'SELECT count(*) FROM "{name}"')) == 0
        command.downgrade(config, PREVIOUS)
        assert snapshot(database, old_tables) == before
        assert not NEW_TABLES & set(inspect(database.engine).get_table_names())
        command.upgrade(config, "head")
        command.check(config)
        assert snapshot(database, old_tables) == before
    finally:
        database.dispose()


@pytest.mark.parametrize("selected", [None, False, True])
def test_0014_refuses_lossy_downgrade_even_with_only_policy(routing_database, selected):
    database, config = routing_database
    with database.transaction() as session:
        if selected is None:
            add_revision(session)
        else:
            task, _ = add_routed_task(session)
            if selected:
                REPOSITORY.select_target(session, task.task_id, "catalog-a", {"reasonCode": "GPU_PREFERRED"}, NOW)
    tables = sorted(inspect(database.engine).get_table_names())
    before = snapshot(database, tables)
    with pytest.raises(RuntimeError, match="拒绝有损降级"):
        command.downgrade(config, PREVIOUS)
    assert snapshot(database, tables) == before


@pytest.mark.parametrize("changes", [
    {"routing_revision_id": ""}, {"routing_revision_id": "x" * 65},
    {"model_id": ""}, {"model_version": "x" * 65},
    {"evidence_kind": "verified"}, {"snapshot": None},
])
def test_routing_revision_constraints(routing_database, changes):
    database, _ = routing_database
    with pytest.raises(IntegrityError), database.transaction() as session:
        add_revision(session, **changes)
    with database.session_factory() as session:
        assert session.scalars(select(AsrRoutingRevision)).all() == []


def test_revision_reinsert_cannot_replace_policy_and_routes_require_real_foreign_keys(routing_database):
    database, _ = routing_database
    with database.transaction() as session:
        original = add_revision(session)
        task = add_task(session)
    with pytest.raises(IntegrityError), database.transaction() as session:
        add_revision(session, snapshot={"changed": True})
    for fields in ({"task_id": "f" * 32, "routing_revision_id": "routing-a"},
                   {"task_id": task.task_id, "routing_revision_id": "missing"}):
        with pytest.raises(IntegrityError), database.transaction() as session:
            REPOSITORY.add_task_route(session, **fields, created_at=NOW)
    with database.session_factory() as session:
        assert REPOSITORY.get_revision(session, "routing-a").to_dict() == original.to_dict()
        assert REPOSITORY.get_revision(session, "missing") is None
        assert REPOSITORY.get_task_route(session, task.task_id) is None


@pytest.mark.parametrize("changes", [
    {"selected_at": NOW},
    {"selected_catalog_revision_id": "catalog-a"},
    {"selected_catalog_revision_id": "catalog-a", "selected_at": NOW},
    {"selected_catalog_revision_id": "missing", "selected_at": NOW, "decision_snapshot": {}},
])
def test_route_rejects_partial_selection_or_missing_catalog(routing_database, changes):
    database, _ = routing_database
    with database.transaction() as session:
        add_target(session)
        add_catalog(session)
        add_revision(session)
        task = add_task(session)
    with pytest.raises(IntegrityError), database.transaction() as session:
        REPOSITORY.add_task_route(session, task_id=task.task_id, routing_revision_id="routing-a",
                                 created_at=NOW, **changes)


def test_selection_is_once_only_idempotent_and_persists_across_sessions(routing_database):
    database, _ = routing_database
    decision = {"reasonCode": "GPU_PREFERRED", "selected": True}
    with database.transaction(immediate=True) as session:
        task, route = add_routed_task(session)
        add_catalog(session, "catalog-b")
        assert route.selected_catalog_revision_id is None and route.selected_at is None
        chosen = REPOSITORY.select_target(session, task.task_id, "catalog-a", decision, NOW)
        original = chosen.to_dict()
    with database.transaction(immediate=True) as session:
        repeated = REPOSITORY.select_target(session, task.task_id, "catalog-a", dict(decision), NOW + timedelta(hours=1))
        assert repeated.to_dict() == original
    for catalog_id, new_decision in (("catalog-b", decision), ("catalog-a", {"changed": True}),
                                     ("catalog-a", {"reasonCode": "GPU_PREFERRED", "selected": 1})):
        with pytest.raises(ValueError, match="cannot be replaced"), database.transaction(immediate=True) as session:
            REPOSITORY.select_target(session, task.task_id, catalog_id, new_decision, NOW)
    with database.session_factory() as session:
        assert REPOSITORY.get_task_route(session, task.task_id).to_dict() == original
        with pytest.raises(ValueError, match="does not exist"):
            REPOSITORY.select_target(session, "f" * 32, "catalog-a", decision, NOW)
    with pytest.raises(IntegrityError), database.transaction() as session:
        REPOSITORY.add_task_route(session, task_id=task.task_id, routing_revision_id="routing-a", created_at=NOW)


def test_catalog_fetch_is_batched_and_empty_input_issues_no_query(routing_database):
    database, _ = routing_database
    with database.transaction() as session:
        add_target(session)
        add_catalog(session)
        add_catalog(session, "catalog-b")
    queries = []

    def count_query(_connection, _cursor, statement, _parameters, _context, _executemany):
        queries.append(statement)

    event.listen(database.engine, "before_cursor_execute", count_query)
    try:
        with database.session_factory() as session:
            assert REPOSITORY.get_many_catalogs(session, []) == {} and queries == []
            result = REPOSITORY.get_many_catalogs(session, ["catalog-a", "catalog-b", "catalog-a", "missing"])
            assert set(result) == {"catalog-a", "catalog-b"}
            assert result["catalog-a"].to_dict()["target_id"] == "target-a"
            assert len(queries) == 1
    finally:
        event.remove(database.engine, "before_cursor_execute", count_query)


def test_task_route_fetch_uses_one_query_for_page_and_none_for_empty_page(routing_database):
    database, _ = routing_database
    with database.transaction() as session:
        task, _ = add_routed_task(session)
        second = add_task(session)
        legacy = add_task(session)
        REPOSITORY.add_task_route(session, task_id=second.task_id, routing_revision_id="routing-a", created_at=NOW)
    queries = []

    def count_query(_connection, _cursor, statement, _parameters, _context, _executemany):
        queries.append(statement)

    event.listen(database.engine, "before_cursor_execute", count_query)
    try:
        with database.session_factory() as session:
            assert REPOSITORY.get_task_routes(session, []) == {} and queries == []
            routes = REPOSITORY.get_task_routes(session, [task.task_id, second.task_id, legacy.task_id, "f" * 32])
            assert set(routes) == {task.task_id, second.task_id}
            assert all(row.routing_revision_id == "routing-a" for row in routes.values())
            assert len(queries) == 1
    finally:
        event.remove(database.engine, "before_cursor_execute", count_query)


def test_original_execution_transaction_rolls_back_route_attempt_and_permit_together(routing_database):
    database, _ = routing_database
    worker_id = uuid4().hex
    with database.transaction() as session:
        task, route = add_routed_task(session)
        TARGETS.add_worker(session, session_id=worker_id, revision_id="revision-a", worker_key="fixture-slot",
            active_slot_key="fixture-slot", slot_index=0, pid=123, state="ready", model_ready=True,
            started_at=NOW, heartbeat_at=NOW)
        original_task, original_route = task.to_dict(), route.to_dict()
    with pytest.raises(RuntimeError, match="rollback fixture"):
        with ExecutionRepository(database).lock_task(task.task_id) as tx:
            tx.start(worker_id=worker_id, lease_token=uuid4().hex,
                expires_at=tx.now + timedelta(seconds=30), attempt_deadline_at=tx.now + timedelta(seconds=60))
            REPOSITORY.select_target(tx.session, task.task_id, "catalog-a", {"reasonCode": "GPU_PREFERRED"}, tx.now)
            TARGETS.add_permit(tx.session, task_id=task.task_id, revision_id="revision-a",
                capacity_group_id="shared-fixture", worker_session_id=worker_id,
                lease_token=tx.task.lease_token, state="held", created_at=tx.now)
            tx.session.flush()
            raise RuntimeError("rollback fixture")
    with database.session_factory() as session:
        assert session.get(TaskRecord, task.task_id).to_dict() == original_task
        assert REPOSITORY.get_task_route(session, task.task_id).to_dict() == original_route
        assert session.scalars(select(ExecutionAttempt)).all() == []
        assert TARGETS.get_permit(session, task.task_id) is None


@pytest.mark.parametrize("task_state,call_state,routed,expected", [
    ("created", "queued", False, True), ("queued", "queued", False, True),
    ("retry_wait", "queued", False, True), ("running", "held", False, False),
    ("failed", "uncertain", False, False), ("succeeded", "released", False, False),
    ("cancelled", "queued", False, False), ("queued", "released", False, False),
    ("queued", "queued", True, False),
])
def test_legacy_waiting_detection_is_readonly_and_excludes_routed_or_finished_tasks(
        routing_database, task_state, call_state, routed, expected):
    database, _ = routing_database
    with database.transaction() as session:
        assert not REPOSITORY.has_legacy_waiting(session)
        task = add_task(session)
        task.status = task_state
        session.add(RemoteAsrCall(task_id=task.task_id, profile_id="remote-v2-sensevoice",
            config_fingerprint="a" * 64, request_id=task.task_id, state=call_state, created_at=NOW))
        if routed:
            add_revision(session)
            REPOSITORY.add_task_route(session, task_id=task.task_id, routing_revision_id="routing-a", created_at=NOW)
    tables = ["tasks", "remote_asr_calls", "asr_routing_revisions", "asr_task_routes"]
    before = snapshot(database, tables)
    with database.session_factory() as session:
        assert REPOSITORY.has_legacy_waiting(session) is expected
    assert snapshot(database, tables) == before


@pytest.mark.parametrize("task_state,call_state,has_route,expected", [
    ("created", "queued", True, True), ("queued", "queued", True, True),
    ("retry_wait", "queued", True, True), ("running", "held", True, False),
    ("failed", "uncertain", True, False), ("cancelled", "queued", True, False),
    ("queued", "queued", False, False),
])
def test_other_waiting_route_requires_drain_without_rebinding_tasks(
        routing_database, task_state, call_state, has_route, expected):
    database, _ = routing_database
    with database.transaction() as session:
        assert not REPOSITORY.has_other_route_waiting(session, "routing-b")
        add_revision(session)
        add_revision(session, routing_revision_id="routing-b", snapshot={"fixtureOnly": "new default"})
        task = add_task(session)
        task.status = task_state
        session.add(RemoteAsrCall(task_id=task.task_id, profile_id="remote-v2-sensevoice",
            config_fingerprint="a" * 64, request_id=task.task_id, state=call_state, created_at=NOW))
        if has_route:
            REPOSITORY.add_task_route(session, task_id=task.task_id, routing_revision_id="routing-a", created_at=NOW)
    tables = ["tasks", "remote_asr_calls", "asr_routing_revisions", "asr_task_routes"]
    before = snapshot(database, tables)
    with database.session_factory() as session:
        assert REPOSITORY.has_other_route_waiting(session, "routing-b") is expected
        assert not REPOSITORY.has_other_route_waiting(session, "routing-a")
    assert snapshot(database, tables) == before
