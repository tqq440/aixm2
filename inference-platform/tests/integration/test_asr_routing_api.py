"""Flask真实提交/幂等/查询投影与路由登记；无网络和真实模型调用。"""

from io import BytesIO
import json
import wave

from sqlalchemy import select

from backend.data.asr.routing_models import AsrTaskRoute
from backend.data.asr.target_models import AsrComputeTarget
from backend.common.cli import execution
from backend.modules.asr import targets_cli
from backend.service.asr.routing_service import AsrRoutingService
from backend.modules.api.app import create_app, close_app
from backend.service.asr.remote_catalog import remote_asr_model_spec
from backend.service.dispatch.dispatch_service import DispatchService
from backend.service.models.model_registry_service import ModelRegistryService
from tests.integration.test_asr_routing_execution import routing_case, routing_factory, no_network
from tests.integration.test_asr_target_admission import target_case


def test_api_submission_freezes_route_replay_and_safe_batched_projection(routing_case):
    case = routing_case
    app = create_app(case.root_settings)
    try:
        registry = ModelRegistryService(case.db, case.root_settings)
        spec = remote_asr_model_spec(case.root_settings)
        registry.register(spec)
        registry.enable(spec.model_id, spec.model_version)
        registry.set_default(spec.model_id, spec.model_version)
        audio = BytesIO()
        with wave.open(audio, "wb") as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
            stream.writeframes(b"\0\0" * 1600)
        client = app.test_client()
        def submit():
            return client.post("/api/tasks/asr", headers={"Idempotency-Key": "route-api"},
                data={"modelId": spec.model_id, "file": (BytesIO(audio.getvalue()), "isolated.wav")})
        first = submit()
        assert first.status_code == 201, first.get_json()
        task = first.get_json()["data"]["task"]
        assert task["routing"]["routingRevisionId"] == case.route_id
        assert task["routing"]["selectionState"] == "waiting"
        task_id = task["taskId"]
        assert submit().status_code == 200
        with case.db.session_factory() as session:
            routes = session.scalars(select(AsrTaskRoute)).all()
            assert len(routes) == 1 and routes[0].selected_catalog_revision_id is None
        assert DispatchService(case.db, case.root_settings, app.extensions["app_logger"]).tick() == 1
        for target in case.records.values():
            if target.kind == "gpu":
                delivery = case.broker.receive(worker_id=target.session_id, queue_key=target.executor.queue_key,
                    visibility_seconds=30)
                assert target.executor.acquire(delivery).decision == "acquired"
        detail = client.get(f"/api/tasks/{task_id}").get_json()["data"]
        listing = client.get("/api/tasks").get_json()["data"]["items"]
        frozen_fields = ("routingRevisionId", "selectionState", "catalogRevisionId", "targetId",
                         "deviceKind", "reasonCode", "evidenceKind", "selectedAt")
        assert {key: detail["routing"].get(key) for key in frozen_fields} == {
            key: listing[0]["routing"].get(key) for key in frozen_fields}
        assert detail["routing"]["observedAt"]
        assert listing[0]["routing"].get("observedAt") is None
        assert detail["routing"]["reasonCode"] == "GPU_PREFERRED"
        assert detail["routing"]["evidenceKind"] == "isolated"
        text = json.dumps(detail)
        assert all(name not in text for name in ("baseUrl", "caFile", "deviceEvidence", "compatibilityEvidence"))
    finally:
        close_app(app)


def test_controlled_cli_replay_preserves_revision_and_does_not_enable_targets(routing_factory, tmp_path, monkeypatch, capsys):
    case = routing_factory(enabled=False)
    with case.db.session_factory() as session:
        snapshot = AsrRoutingService(case.db, case.root_settings).repository.get_revision(session, case.route_id).snapshot
    path = tmp_path / "route.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(execution, "load_settings", lambda: case.root_settings)
    assert targets_cli.main(["register-route", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"routingRevisionId": case.route_id, "created": False, "executionEnabled": False}
    with case.db.session_factory() as session:
        assert all(not target.enabled for target in session.scalars(select(AsrComputeTarget)))
