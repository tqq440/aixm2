"""明确标记的合成0014资料，只写调用方新建的演练库，不读日常配置。"""

from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from backend.data.asr.catalog_models import AsrCatalogRevision, AsrResourceObservation
from backend.data.asr.remote_models import RemoteAsrCall
from backend.data.asr.routing_models import AsrRoutingRevision, AsrTaskRoute
from backend.data.asr.target_models import AsrCallPermit, AsrCapacityGroup, AsrComputeTarget, AsrTargetRevision, AsrTargetWorkerSession
from backend.data.broker.broker_models import NotificationRecord
from backend.data.execution.attempt_models import ExecutionAttempt
from backend.data.llm.online_models import OnlineRequestRecord
from backend.data.models.model_models import ModelRecord, ModelVersionRecord
from backend.data.tasks.task_models import DispatchEvent, IdempotencyRecord, TaskRecord
from backend.utils.files.source_archive import sha256


FIXTURE_TIME = datetime(2026, 10, 20, 0, 0, 0)
FIXTURE_STATES = ("succeeded", "succeeded", "queued", "queued", "running", "failed", "failed", "cancelled")


def fixture_uploads():
    """非真实媒体，不送解码器或推理服务；只验证历史文件原字节保留。"""
    return {f"{100 + n:032x}.{'image' if n == 1 else 'audio'}":
            f"SYNTHETIC MIGRATION INPUT {n}\n".encode() for n in range(8)}


def seed_legacy_fixture(engine):
    now = FIXTURE_TIME
    with Session(engine) as session, session.begin():
        connection = session.connection()
        if connection.exec_driver_sql("SELECT version_num FROM alembic_version").fetchall() != [("20261020_0014",)]:
            raise ValueError("MIGRATION_FIXTURE_REQUIRES_EMPTY_0014")
        # 0005固定种入Whisper目录，不是业务历史；不能因此删除基线模型。
        baseline = "536b0662742c02347bc0e980a01041f333bce120"
        if (connection.exec_driver_sql("SELECT model_id,default_version FROM models").fetchall() != [("whisper-small", baseline)] or
                connection.exec_driver_sql("SELECT model_id,model_version FROM model_versions").fetchall() != [("whisper-small", baseline)]):
            raise ValueError("MIGRATION_FIXTURE_REQUIRES_EMPTY_0014")
        tables = [row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('alembic_version','models','model_versions')")]
        if any(connection.exec_driver_sql('SELECT 1 FROM "' + name.replace('"', '""') + '" LIMIT 1').first() for name in tables):
            raise ValueError("MIGRATION_FIXTURE_REQUIRES_EMPTY_0014")
        for model in ("synthetic-asr", "synthetic-vision", "synthetic-llm"):
            session.add(ModelRecord(model_id=model, default_version="fixture-v1", created_at=now, updated_at=now))
        session.flush()
        for model in ("synthetic-asr", "synthetic-vision", "synthetic-llm"):
            session.add(ModelVersionRecord(model_id=model, model_version="fixture-v1", enabled=False,
                spec_json={"syntheticOnly": True, "notExecutable": True}, created_at=now, updated_at=now))
        uploads = list(fixture_uploads().items())
        for n, state in enumerate(FIXTURE_STATES):
            identifier = f"{n + 1:032x}"
            filename, content = uploads[n]
            vision = n == 1
            active = n in (4, 5)
            session.add(TaskRecord(task_id=identifier, caller_id="synthetic-legacy-owner",
                task_type="vision" if vision else "asr", model_id="synthetic-vision" if vision else "synthetic-asr",
                model_version="fixture-v1", parameters={"syntheticOnly": True}, input_ref=filename[:32],
                input_filename="合成图片.png" if vision else "合成音频.wav", input_sha256=sha256(content),
                input_size_bytes=len(content), input_media_type="image" if vision else "audio",
                audio_duration_seconds=None if vision else 1.0, image_width=1 if vision else None,
                image_height=1 if vision else None, queue_key="synthetic:vision" if vision else "synthetic:asr",
                status=state, attempt=1 if state != "queued" else 0, max_attempts=1,
                queue_deadline_at=now + timedelta(seconds=60), total_deadline_at=now + timedelta(seconds=300),
                lease_token=f"{500 + n:032x}" if active else None,
                lease_worker_id="synthetic-worker" if active else None,
                lease_expires_at=now + timedelta(seconds=30) if active else None,
                started_at=now if state != "queued" else None,
                finished_at=now + timedelta(seconds=5) if state in ("succeeded", "failed", "cancelled") else None,
                result_json={"syntheticOnly": True, "text": "迁移保留测试结果"} if n == 0 else
                            {"syntheticOnly": True, "detections": []} if vision else None,
                result_fingerprint=sha256(f"result-{n}".encode()) if state == "succeeded" else None,
                error_json={"code": "RESULT_UNKNOWN" if n == 5 else "SYNTHETIC_FAILURE"} if state == "failed" else None,
                created_at=now, updated_at=now))
            session.flush()
            session.add(IdempotencyRecord(id=f"{200 + n:032x}", caller_id="synthetic-legacy-owner",
                idempotency_key=f"fixture-{n}", request_fingerprint=sha256(f"request-{n}".encode()),
                task_id=identifier, created_at=now))
            event = DispatchEvent(event_id=f"{300 + n:032x}", task_id=identifier,
                queue_key="synthetic:vision" if vision else "synthetic:asr",
                status="pending" if state == "queued" else "published", available_at=now, created_at=now)
            session.add(event)
            session.flush()
            session.add(NotificationRecord(notification_id=f"{400 + n:032x}", event_id=event.event_id,
                task_id=identifier, queue_key=event.queue_key, dispatch_version=1, execution_plan_version=1,
                status="ready" if state == "queued" else "inflight" if active else "acked", visible_at=now, created_at=now))
            if state != "queued":
                session.add(ExecutionAttempt(lease_token=f"{500 + n:032x}", task_id=identifier, attempt=1,
                    worker_id="synthetic-worker", dispatch_version=1, execution_plan_version=1,
                    started_at=now, outcome="running" if active else state,
                    finished_at=None if active else now + timedelta(seconds=5)))
            if not vision:
                session.add(RemoteAsrCall(task_id=identifier, profile_id="synthetic-profile",
                    config_fingerprint="b" * 64, request_id=identifier,
                    state="held" if n == 4 else "uncertain" if n == 5 else "queued" if state == "queued" else "released",
                    worker_id="synthetic-worker" if active else None,
                    lease_token=f"{500 + n:032x}" if active else None, created_at=now))
        session.add(AsrCapacityGroup(group_id="synthetic-shared", capacity_limit=2, created_at=now))
        session.flush()
        session.add(AsrComputeTarget(target_id="synthetic-target", capacity_group_id="synthetic-shared", enabled=False, target_limit=2, created_at=now))
        session.flush()
        session.add(AsrTargetRevision(revision_id="a" * 64, target_id="synthetic-target", config_fingerprint="b" * 64,
            endpoint_identity="c" * 64, config_snapshot={"syntheticOnly": True, "url": "http://invalid.invalid"}, device_kind="unknown", created_at=now))
        session.flush()
        session.add(AsrTargetWorkerSession(session_id="d" * 32, revision_id="a" * 64, worker_key="synthetic-worker",
            slot_index=0, active_slot_key="synthetic-target:0", pid=999999, state="unknown", model_ready=False,
            heartbeat_at=now, started_at=now))
        session.flush()
        for n, state in ((0, "released"), (4, "held"), (5, "uncertain")):
            session.add(AsrCallPermit(task_id=f"{n + 1:032x}", revision_id="a" * 64,
                capacity_group_id="synthetic-shared", worker_session_id="d" * 32,
                lease_token=f"{500 + n:032x}", state=state, created_at=now))
        session.add(AsrCatalogRevision(catalog_revision_id="synthetic-catalog", target_id="synthetic-target",
            revision_id="a" * 64, metadata_snapshot={"syntheticOnly": True}, created_at=now))
        session.flush()
        session.add(AsrResourceObservation(observation_id="synthetic-observation", catalog_revision_id="synthetic-catalog",
            scope="target", source="synthetic-fixture", evidence_kind="isolated", observed_at=now, ttl_seconds=5,
            payload={"device": "unknown"}, recorded_at=now))
        session.add(AsrRoutingRevision(routing_revision_id="synthetic-route", model_id="synthetic-asr",
            model_version="fixture-v1", evidence_kind="isolated", snapshot={"syntheticOnly": True}, created_at=now))
        session.flush()
        session.add(AsrTaskRoute(task_id=f"{5:032x}", routing_revision_id="synthetic-route",
            selected_catalog_revision_id="synthetic-catalog", selected_at=now, created_at=now,
            decision_snapshot={"device": "unknown", "syntheticOnly": True}))
        for n, state, permit in ((1, "completed", "released"), (2, "failed", "uncertain")):
            session.add(OnlineRequestRecord(online_request_id=f"{700 + n:032x}", caller_id="synthetic-legacy-owner",
                model_id="synthetic-llm", model_version="fixture-v1", instance_id="synthetic-llm-instance",
                owner_id="e" * 32, request_fingerprint=sha256(f"online-{n}".encode()), state=state, permit_state=permit,
                accepted_at=now, updated_at=now, deadline_at=now + timedelta(seconds=300),
                lease_expires_at=now + timedelta(seconds=30), ended_at=now + timedelta(seconds=5), output_characters=12))
