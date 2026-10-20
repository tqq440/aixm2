"""M12/M14 模型子进程；只执行启动时声明的固定能力，父进程持续续租。"""

from multiprocessing.connection import Connection
import signal

from backend.common.exceptions.app_error import AppError
from backend.common.logging.app_logging import close_logging, configure_logging
from backend.common.process.managed_process import MAX_MESSAGE_BYTES
from backend.common.process.console_isolation import detach_console
from backend.config.vision.model_catalog import MODEL_ID as VISION_MODEL_ID, MODEL_VERSION as VISION_MODEL_VERSION
from backend.data.files.upload_store import LocalUploadStore
from backend.schema.asr.asr_schema import TranscriptionRequest
from backend.schema.vision.detection_schema import DetectionRequest
from backend.schema.execution.execution_schema import FailureInfo
from backend.schema.worker.worker_schema import (
    COMMAND_ADAPTER, FailedEvent, FatalEvent, ReadyEvent, ResultEvent,
    RunCommand, StartCommand, StopCommand,
)
from backend.service.asr.adapter_catalog import configured_asr_plugin
from backend.service.vision.detection_service import DetectionService
from backend.utils.file.file_utils import sha256_file


def inference_child(connection: Connection) -> None:
    """spawn 入口；第一条 start 必须等到父进程完成 Job 绑定后才会送达。"""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    logger = None
    try:
        command = COMMAND_ADAPTER.validate_json(connection.recv_bytes(MAX_MESSAGE_BYTES))
        if not isinstance(command, StartCommand):
            raise ValueError("首条命令必须是 start")
        settings = command.settings
        task_type = command.task_type
        asr_profile = command.asr_profile
        if task_type == "asr":
            try:
                # 原生计算库可另装 CTRL_BREAK handler，Python SIG_IGN 不能隔离广播。
                # 只脱离此子进程的 console；受管 Pipe 与 Job Object 不受影响。
                detach_console()
                # FreeConsole 会重置控制处理器；脱离后重新设定 Python 信号策略。
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                if hasattr(signal, "SIGBREAK"):
                    signal.signal(signal.SIGBREAK, signal.SIG_IGN)
            except OSError as exc:
                raise AppError("ASR_CONSOLE_DETACH_FAILED", "ASR 子进程控制台隔离失败，未加载模型。", 503) from exc
        log_name = ("remote-asr-worker-child.log" if asr_profile == "remote" else
                    "worker-child.log" if task_type == "asr" else "vision-worker-child.log")
        logger = configure_logging(settings.log_dir, settings.log_level, log_name)
        asr_plugin = configured_asr_plugin(settings, profile=asr_profile) if task_type == "asr" else None
        model_spec = asr_plugin.model_spec() if asr_plugin is not None else None
        adapter = asr_plugin.create(settings) if asr_plugin is not None else DetectionService(settings)
        loaded = adapter.prepare_model()
        warmed = adapter.warmup() if task_type == "vision" else 0.0
        connection.send_bytes(ReadyEvent(model_load_seconds=loaded, warmup_seconds=warmed).model_dump_json().encode("utf-8"))
        store = LocalUploadStore(settings.upload_dir, media_type="image" if task_type == "vision" else "audio")
        while True:
            command = COMMAND_ADAPTER.validate_json(connection.recv_bytes(MAX_MESSAGE_BYTES))
            if isinstance(command, StopCommand):
                break
            if not isinstance(command, RunCommand):
                raise ValueError("模型加载后只能接受 run 或 stop")
            input_record = command.input
            try:
                model_id = model_spec.model_id if model_spec is not None else VISION_MODEL_ID
                model_version = model_spec.model_version if model_spec is not None else VISION_MODEL_VERSION
                if input_record.task_type != task_type or input_record.model_id != model_id or input_record.model_version != model_version:
                    raise AppError("MODEL_VERSION_MISMATCH", "任务模型版本与当前 Worker 不匹配。")
                path = store.resolve_path(input_record.input_ref)
                if path.stat().st_size != input_record.input_size_bytes or sha256_file(path) != input_record.input_sha256:
                    raise AppError("INPUT_INTEGRITY_FAILED", "受管输入内容与任务档案不一致。")
                if task_type == "asr":
                    result = adapter.transcribe(TranscriptionRequest(
                        audio_path=path, language=input_record.language, request_id=input_record.task_id,
                    ))
                else:
                    result, _ = adapter.run(DetectionRequest(
                        image_path=path, confidence_threshold=input_record.confidence_threshold,
                        iou_threshold=input_record.iou_threshold, max_detections=input_record.max_detections,
                    ))
                    if (result.image_width, result.image_height) != (input_record.image_width, input_record.image_height):
                        raise AppError("INPUT_METADATA_MISMATCH", "图片解码尺寸与受理时档案不一致。")
                event = ResultEvent(task_id=input_record.task_id, result=result)
            except AppError as exc:
                logger.exception("任务执行失败 task_type=%s task_id=%s", task_type, input_record.task_id)
                event = FailedEvent(task_id=input_record.task_id, failure=FailureInfo(code=exc.code, message=exc.message))
            except (OSError, ValueError):
                logger.exception("任务输入读取失败 task_id=%s", input_record.task_id)
                event = FailedEvent(task_id=input_record.task_id, failure=FailureInfo(code="INPUT_UNAVAILABLE", message="受管输入不可读取，请检查文件与日志。"))
            except Exception:
                logger.exception("任务执行异常 task_id=%s", input_record.task_id)
                event = FailedEvent(task_id=input_record.task_id, failure=FailureInfo(code="TRANSCRIPTION_FAILED" if task_type == "asr" else "DETECTION_FAILED", message="模型执行失败，请检查 Worker 日志。"))
            payload = event.model_dump_json().encode("utf-8")
            if len(payload) > MAX_MESSAGE_BYTES:
                payload = FailedEvent(task_id=input_record.task_id, failure=FailureInfo(code="RESULT_TOO_LARGE", message="模型结果超过进程传输上限。")).model_dump_json().encode("utf-8")
            connection.send_bytes(payload)
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:
        if logger is not None:
            logger.exception("推理子进程初始化或协议错误")
        failure = FailureInfo(code=exc.code, message=exc.message) if isinstance(exc, AppError) else FailureInfo(code="WORKER_CHILD_FAILED", message="推理子进程启动或通信失败，请检查日志。")
        try:
            connection.send_bytes(FatalEvent(failure=failure).model_dump_json().encode("utf-8"))
        except (OSError, EOFError):
            pass
    finally:
        connection.close()
        if logger is not None:
            close_logging(logger)
