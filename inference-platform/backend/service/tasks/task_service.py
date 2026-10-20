"""M02/M03 的本机任务创建与查询；独立的 M08/Worker 服务负责投递与执行。"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from typing import BinaryIO
from uuid import uuid4

from filelock import Timeout
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from backend.common.exceptions.app_error import AppError
from backend.config.vision.model_catalog import QUEUE_KEY as VISION_QUEUE_KEY
from backend.config.settings.app_settings import AppSettings
from backend.data.files.upload_store import EmptyUploadError, LocalUploadStore, UnsafeUploadPathError, UploadTooLargeError
from backend.data.tasks.task_models import DispatchEvent, TaskRecord
from backend.data.tasks.task_repository import TaskRepository
from backend.schema.tasks.task_schema import (
    AsrTaskForm, CancelReceipt, SubmissionHeaders, TaskInput, TaskListQuery, TaskPage,
    ImageTaskInput, TaskParameters, TaskReceipt, TaskResultView, TaskView, UploadMetadata,
    VisionTaskForm, VisionTaskParameters,
)
from backend.schema.asr.asr_schema import TranscriptionResult
from backend.schema.vision.detection_schema import DetectionResult
from backend.service.tasks.model_service import ModelService
from backend.service.execution.execution_service import ExecutionService
from backend.utils.audio.audio_utils import AudioDecodeError, AudioSampleLimitExceeded, decode_audio_file
from backend.service.vision.image_service import prepare_image
from backend.service.asr.adapter_catalog import configured_asr_plugin
from backend.service.agent.asr_gate import AsrGate
from backend.service.asr.remote_service import RemoteAsrService, REMOTE_QUEUE
from backend.service.asr.scheduling_service import AsrSchedulingService
from backend.utils.file.file_utils import sha256_file
from backend.utils.image.annotation_utils import annotated_png

LOCAL_CALLER_ID = "local-user"
TASK_MESSAGES = {
    "created": "任务已保存，等待投递器发送通知。",
    "queued": "通知已入队，等待就绪的 Worker 领取。",
    "running": "任务已领取，尚未保存结果。",
    "succeeded": "转写完成，结果已保存。",
    "retry_wait": "本次执行暂时失败，已保存记录，等待有限重试。",
    "cancel_requested": "已受理取消，等待 Worker 停止执行或租约失效。",
    "cancelled": "任务已取消，不再接受执行结果；进程是否可接单请查看运行状态。",
    "failed": "任务已最终失败，请查看错误说明；不会无限自动重试。",
}


class TaskService:
    def __init__(self, settings: AppSettings, repository: TaskRepository,
                 store: LocalUploadStore, models: ModelService, logger: logging.Logger):
        self.settings = settings
        self.repository = repository
        self.store = store
        self.image_store = LocalUploadStore(settings.upload_dir, media_type="image")
        self.models = models
        self.logger = logger
        self.execution = ExecutionService(repository.database, settings)
        self.asr_gate = AsrGate(repository.database, settings)
        self.remote_asr = RemoteAsrService(repository.database, settings)

    @staticmethod
    def _view(task: TaskRecord, version_info=None) -> TaskView:
        remote = RemoteAsrService.is_remote(task)
        active = task.status in {"created", "queued", "retry_wait", "running", "cancel_requested"}
        cancellable = active and (not remote or task.status in {"created", "queued", "retry_wait"})
        outcome = None
        if remote:
            outcome = ("not_dispatched" if task.attempt == 0 else "pending" if active else
                       "unknown" if task.error_json and task.error_json.get("code") in {
                           "REMOTE_ASR_RESULT_UNKNOWN", "REMOTE_ASR_UNKNOWN"} else "confirmed")
        if task.task_type == "vision":
            parameters = VisionTaskParameters.model_validate(task.parameters)
            input_view = ImageTaskInput(file_name=task.input_filename, size_bytes=task.input_size_bytes,
                                        sha256=task.input_sha256, width=task.image_width, height=task.image_height)
        else:
            parameters = TaskParameters.model_validate(task.parameters)
            input_view = TaskInput(file_name=task.input_filename, size_bytes=task.input_size_bytes,
                                   sha256=task.input_sha256, duration_seconds=task.audio_duration_seconds)
        return TaskView(
            task_id=task.task_id, caller_id=task.caller_id, task_type=task.task_type,
            model_id=task.model_id, model_version=task.model_version,
            version_info=version_info,
            parameters=parameters, status=task.status,
            attempt=task.attempt,
            max_attempts=task.max_attempts if task.max_attempts is not None else 2,
            execution_plan_version=task.execution_plan_version,
            input=input_view,
            created_at=task.created_at, updated_at=task.updated_at,
            started_at=task.started_at, finished_at=task.finished_at,
            queue_deadline_at=task.queue_deadline_at, total_deadline_at=task.total_deadline_at,
            attempt_deadline_at=task.attempt_deadline_at, next_retry_at=task.next_retry_at,
            error=task.error_json, result_available=task.status == "succeeded" and task.result_json is not None,
            message="图片检测完成，结果已保存。" if task.task_type == "vision" and task.status == "succeeded" else TASK_MESSAGES[task.status],
            cancel_allowed=cancellable,
            cancel_unavailable_reason="外部服务没有取消接口；任务领取后须等待结果。" if remote and active and not cancellable else None,
            remote_outcome=outcome,
        )

    def _version_infos(self, tasks):
        identities = [
            (task.model_id, task.model_version) for task in tasks if RemoteAsrService.is_remote(task)
        ]
        return self.models.registry.version_infos(identities) if identities else {}

    def _task_view(self, task):
        info = self._version_infos([task]).get((task.model_id, task.model_version))
        return self._view(task, info).model_copy(update={"routing": self._routing_views([task], detailed=True).get(task.task_id)})

    def _routing_views(self, tasks, *, detailed=False):
        ids = [task.task_id for task in tasks if RemoteAsrService.is_remote(task)]
        if not ids:
            return {}
        with self.repository.database.session_factory() as session:
            session.execute(text("BEGIN"))
            routes = self.remote_asr.routing.repository.get_task_routes(session, ids)
            status = AsrSchedulingService(self.repository.database, self.settings)
            tasks_by_id = {task.task_id: task for task in tasks}
            return {key: status.task_view(session, tasks_by_id[key], row, detailed=detailed) for key, row in routes.items()}

    @staticmethod
    def _storage_error() -> AppError:
        return AppError("TASK_STORAGE_UNAVAILABLE", "任务数据库暂不可用，请检查日志并确认已执行 alembic upgrade head。", 503)

    def get_task(self, task_id: str) -> TaskView:
        try:
            task = self.repository.get_task(LOCAL_CALLER_ID, task_id)
        except SQLAlchemyError as exc:
            raise self._storage_error() from exc
        if task is None:
            raise AppError("TASK_NOT_FOUND", "任务不存在。", 404)
        return self._task_view(task)

    def list_tasks(self, query: TaskListQuery) -> TaskPage:
        try:
            tasks, total = self.repository.list_tasks(LOCAL_CALLER_ID, page=query.page, page_size=query.page_size,
                                                      task_type=None if query.task_type == "all" else query.task_type)
        except SQLAlchemyError as exc:
            raise self._storage_error() from exc
        infos = self._version_infos(tasks)
        routing = self._routing_views(tasks)
        return TaskPage(items=[self._view(task, infos.get((task.model_id, task.model_version))).model_copy(
            update={"routing": routing.get(task.task_id)}) for task in tasks], total=total,
                        page=query.page, page_size=query.page_size)

    def cancel(self, task_id: str) -> CancelReceipt:
        try:
            decision = self.execution.request_cancel(task_id, LOCAL_CALLER_ID)
        except SQLAlchemyError as exc:
            raise self._storage_error() from exc
        # 重新查询允许返回已推进的权威状态，例如 Worker 已完成取消收尾。
        return CancelReceipt(task=self.get_task(task_id), decision=decision)

    def get_result(self, task_id: str) -> TaskResultView:
        try:
            task = self.repository.get_task(LOCAL_CALLER_ID, task_id)
        except SQLAlchemyError as exc:
            raise self._storage_error() from exc
        if task is None:
            raise AppError("TASK_NOT_FOUND", "任务不存在。", 404)
        if task.status == "failed":
            raise AppError("TASK_FAILED", "此任务执行失败，请查询任务详情中的错误说明。", 409)
        if task.status == "cancelled":
            raise AppError("TASK_CANCELLED", "此任务已取消，没有可下载的结果。", 409)
        if task.status != "succeeded" or task.result_json is None:
            raise AppError("RESULT_NOT_READY", "任务结果尚未保存，请稍后查询任务状态。", 409)
        return TaskResultView(task_id=task.task_id, model_id=task.model_id, model_version=task.model_version,
                              version_info=self._version_infos([task]).get((task.model_id, task.model_version)),
                              output=(DetectionResult if task.task_type == "vision" else TranscriptionResult).model_validate(task.result_json))

    def get_image(self, task_id: str, *, annotated: bool = False) -> bytes:
        """按调用者与任务引用交付 PNG；框图只使用已提交的权威结果，不重新推理。"""
        try:
            task = self.repository.get_task(LOCAL_CALLER_ID, task_id)
        except SQLAlchemyError as exc:
            raise self._storage_error() from exc
        if task is None:
            raise AppError("TASK_NOT_FOUND", "任务不存在。", 404)
        if task.task_type != "vision":
            raise AppError("TASK_TYPE_MISMATCH", "该任务没有图片输入。", 422)
        result = self.get_result(task_id).output if annotated else None
        try:
            path = self.image_store.resolve_path(task.input_ref)
            if path.stat().st_size != task.input_size_bytes or sha256_file(path) != task.input_sha256:
                raise AppError("INPUT_INTEGRITY_FAILED", "受管图片内容与任务档案不一致。", 409)
            # 已受理档案的有效大小不因后来调低部署准入值而失去下载资格。
            prepared = prepare_image(path, max_file_bytes=task.input_size_bytes,
                                     max_pixels=task.image_width * task.image_height)
            if (prepared.width, prepared.height) != (task.image_width, task.image_height):
                raise AppError("INPUT_INTEGRITY_FAILED", "受管图片尺寸与任务档案不一致。", 409)
            annotations = [] if result is None else [
                (item.class_name, item.confidence, (item.box.x_min, item.box.y_min, item.box.x_max, item.box.y_max))
                for item in result.detections
            ]
            return annotated_png(prepared.rgb, annotations)
        except (OSError, UnsafeUploadPathError) as exc:
            raise AppError("INPUT_UNAVAILABLE", "受管图片不可读取，请检查文件存储和日志。", 503) from exc

    def _replay(self, record, fingerprint: str) -> TaskReceipt:
        old_fingerprint, task = record
        if old_fingerprint != fingerprint:
            raise AppError("IDEMPOTENCY_CONFLICT", "此幂等键已用于不同内容；新任务请使用新的 Idempotency-Key。", 409)
        return TaskReceipt(task=self._task_view(task), replayed=True)

    def submit(self, form: AsrTaskForm, headers: SubmissionHeaders,
               metadata: UploadMetadata, stream: BinaryIO) -> TaskReceipt:
        return self._submit(form, headers, metadata, stream, task_type="asr")

    def submit_vision(self, form: VisionTaskForm, headers: SubmissionHeaders,
                      metadata: UploadMetadata, stream: BinaryIO) -> TaskReceipt:
        return self._submit(form, headers, metadata, stream, task_type="vision")

    def _submit(self, form, headers, metadata, stream, *, task_type: str) -> TaskReceipt:
        store = self.image_store if task_type == "vision" else self.store
        kind = "IMAGE" if task_type == "vision" else "AUDIO"
        try:
            # 两种输入与孤立对象维护使用同一把锁，保护正在受理的文件。
            with store.lock():
                return self._submit_locked(form, headers, metadata, stream, task_type=task_type)
        except UploadTooLargeError as exc:
            raise AppError(f"{kind}_TOO_LARGE", "上传文件超过配置的大小上限。", 413) from exc
        except EmptyUploadError as exc:
            raise AppError(f"{kind}_EMPTY", "上传文件为空。", 400) from exc
        except Timeout as exc:
            raise AppError("UPLOAD_BUSY", "文件存储正在处理其他请求，请稍后使用原幂等键重试。", 503) from exc
        except (OSError, UnsafeUploadPathError) as exc:
            raise AppError("UPLOAD_STORAGE_UNAVAILABLE", "文件保存失败，请检查受管目录权限和磁盘空间。", 503) from exc
        except SQLAlchemyError as exc:
            raise self._storage_error() from exc

    def _submit_locked(self, form, headers, metadata, stream, *, task_type="asr") -> TaskReceipt:
        vision = task_type == "vision"
        store = self.image_store if vision else self.store
        queue_key = VISION_QUEUE_KEY if vision else configured_asr_plugin(self.settings).queue_key
        max_pixels = self.settings.vision_max_pixels
        # 先确认数据库可读，避免未迁移时接收持久化文件。
        existing = self.repository.find_idempotent(LOCAL_CALLER_ID, headers.idempotency_key)
        language = None if vision else form.language
        remote = False
        if existing is not None:
            # 重放依原任务的明确版本裁决，不受当前默认版本或启用状态影响。
            old_task = existing[1]
            remote = RemoteAsrService.is_remote(old_task)
            if remote and "language" not in form.model_fields_set:
                language = None
            model_version = form.model_version or old_task.model_version
            max_file_bytes = max(self.settings.vision_max_file_bytes if vision else self.settings.asr_max_file_bytes,
                                 old_task.input_size_bytes)
            max_audio_seconds = self.settings.asr_max_audio_seconds  # 重放无需再次解码。
        else:
            model = (self.models.resolve(form.model_id, form.model_version, task_type="vision") if vision else
                     self.models.resolve(form.model_id, form.model_version))
            remote = not vision and model.adapter_key == "remote-v2-sensevoice"
            if remote:
                queue_key = REMOTE_QUEUE
                if "language" not in form.model_fields_set:
                    language = None
            if not vision:
                self.models.validate_language(language, model)
                if remote:
                    self.remote_asr.ensure_available()
            model_version = model.model_version
            max_file_bytes = model.input_constraints.max_file_bytes
            if vision:
                max_pixels = model.input_constraints.max_pixels
            else:
                max_audio_seconds = model.input_constraints.max_audio_seconds
        upload = store.save(stream, max_bytes=max_file_bytes)
        keep_object = False
        try:
            parameters = (VisionTaskParameters.model_validate(form.model_dump(exclude={"model_id", "model_version"}))
                          if vision else TaskParameters(language=language))
            canonical_fields = {
                "schema_version": 1, "task_type": task_type, "model_id": form.model_id,
                "model_version": model_version, "parameters": parameters.model_dump(),
                "input_sha256": upload.sha256, "input_size_bytes": upload.size_bytes,
            }
            if vision:
                canonical_fields["input_media_type"] = "image"
            canonical = json.dumps(canonical_fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if existing is not None:
                return self._replay(existing, fingerprint)

            duration, image_width, image_height = None, None, None
            if vision:
                image = prepare_image(upload.path, max_file_bytes=max_file_bytes, max_pixels=max_pixels)
                image_width, image_height = image.width, image.height
                del image
            else:
                try:
                    audio = decode_audio_file(upload.path, max_samples=int(max_audio_seconds * 16000))
                    duration = audio.duration_seconds
                    del audio
                except AudioSampleLimitExceeded as exc:
                    raise AppError("AUDIO_TOO_LONG", "音频时长超过配置的上限。", 413) from exc
                except AudioDecodeError as exc:
                    raise AppError("AUDIO_DECODE_FAILED", "音频无法正常解码或没有有效音轨。", 400) from exc
                except ImportError as exc:
                    raise AppError("AUDIO_DECODE_FAILED", "音频运行库不可用，请安装 requirements/asr.txt。", 503) from exc

            now = datetime.now(timezone.utc)
            task = TaskRecord(
                task_id=uuid4().hex, caller_id=LOCAL_CALLER_ID, task_type=task_type,
                model_id=form.model_id, model_version=model_version, parameters=parameters.model_dump(),
                input_ref=upload.input_ref, input_filename=metadata.file_name,
                input_sha256=upload.sha256, input_size_bytes=upload.size_bytes,
                audio_duration_seconds=duration, status="created", attempt=0,
                input_media_type="image" if vision else "audio", image_width=image_width, image_height=image_height,
                execution_plan_version=1, dispatch_version=1, queue_key=queue_key,
                created_at=now, updated_at=now,
                max_attempts=1 if remote else self.settings.max_attempts,
            )
            event = DispatchEvent(
                event_id=uuid4().hex, task_id=task.task_id, event_type="task.created",
                execution_plan_version=1, dispatch_version=1, queue_key=queue_key,
                status="pending", available_at=now, created_at=now,
            )
            def prepare_acceptance(accepted_at: datetime, inflight: int) -> None:
                if inflight >= self.settings.max_inflight_tasks:
                    raise AppError("TASK_CAPACITY_EXCEEDED", "当前在途任务已达上限，请等待任务结束后再提交。", 429)
                task.created_at = task.updated_at = accepted_at
                task.queue_deadline_at = accepted_at + timedelta(seconds=self.settings.queue_timeout_seconds)
                task.total_deadline_at = accepted_at + timedelta(seconds=self.settings.total_timeout_seconds)
                event.created_at = event.available_at = accepted_at

            try:
                def validate_admission(session):
                    if vision:
                        self.models.validate_admission(session, form.model_id, model_version, task_type="vision")
                    else:
                        self.models.validate_admission(session, form.model_id, model_version)
                        if remote:
                            self.remote_asr.validate_submission(session, task)
                        else:
                            self.asr_gate.validate_submission(session, form.model_id, model_version)

                self.repository.create_with_event(task=task, event=event,
                    idempotency_key=headers.idempotency_key, request_fingerprint=fingerprint,
                    before_create=prepare_acceptance,
                    admission_check=validate_admission,
                    after_create=(lambda session: self.remote_asr.record_submission(session, task)) if remote else None,
                )
            except SQLAlchemyError:
                # 约束竞争或提交结果未知时重新读权威记录，不能直接删可能已被引用的文件。
                try:
                    recovered = self.repository.find_idempotent(LOCAL_CALLER_ID, headers.idempotency_key)
                    own_task = self.repository.get_task(LOCAL_CALLER_ID, task.task_id)
                    keep_object = own_task is not None
                except SQLAlchemyError:
                    keep_object = True
                    self.logger.exception("提交结果无法复查；保留输入供孤立对象维护核对 input_ref=%s", upload.input_ref)
                    raise
                if recovered is not None:
                    return self._replay(recovered, fingerprint)
                raise
            keep_object = True
            self.logger.info("任务已保存 task_id=%s status=created", task.task_id)
            return TaskReceipt(task=self._task_view(task), replayed=False)
        finally:
            if not keep_object:
                try:
                    store.delete(upload.input_ref)
                except (OSError, ValueError):
                    self.logger.exception("未受理输入即时清理失败，等待维护核对 input_ref=%s", upload.input_ref)
