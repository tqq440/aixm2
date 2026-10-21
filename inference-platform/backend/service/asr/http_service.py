"""将现有 Whisper 包装为单槽 HTTP 调用；不创建平台任务或下载模型。"""

from contextlib import contextmanager
from tempfile import TemporaryDirectory
from threading import Lock
from typing import BinaryIO
from pathlib import Path
from uuid import uuid4

from backend.common.exceptions.app_error import AppError
from backend.config.asr.model_catalog import MODEL_ID
from backend.config.settings.app_settings import AppSettings
from backend.schema.asr.asr_schema import TranscriptionRequest
from backend.schema.asr.http_schema import AsrHttpTranscription
from backend.service.asr.asr_service import AsrService


class AsrHttpService:
    """由独立 CLI 显式加载；健康查询、应用构造和导入均不加载模型。"""

    def __init__(self, settings: AppSettings, *, adapter=None):
        self.settings = settings
        self.adapter = adapter if adapter is not None else AsrService(settings)
        self.ready = False
        self._slot = Lock()
        self.upload_root = settings.database_path.parent / (settings.database_path.name + ".asr-http-uploads")

    def prepare_model(self):
        with self._slot:
            self.adapter.prepare_model()
            self.ready = True

    def health(self):
        return {"status": "ok" if self.ready else "unavailable", "model_loaded": self.ready,
                "model": MODEL_ID, "busy": self._slot.locked()}

    def models(self):
        if not self.ready:
            raise AppError("ASR_SERVICE_NOT_READY", "Whisper 尚未加载。", 503)
        return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}]}

    @contextmanager
    def request_slot(self):
        if not self._slot.acquire(blocking=False):
            raise AppError("ASR_SERVICE_BUSY", "Whisper 正在处理请求，请等待当前调用完成。", 503)
        try:
            if not self.ready:
                raise AppError("ASR_SERVICE_NOT_READY", "Whisper 尚未加载。", 503)
            yield
        finally:
            self._slot.release()

    def transcribe(self, stream: BinaryIO, command: AsrHttpTranscription):
        """调用者持有 request_slot；临时文件名由服务生成，不采用上传文件名。"""
        command = AsrHttpTranscription.model_validate(command)
        self.upload_root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="request-", dir=self.upload_root) as folder:
            path = Path(folder) / "audio.upload"
            size = 0
            with path.open("xb") as target:
                while True:
                    chunk = stream.read(min(65_536, self.settings.asr_max_file_bytes - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.settings.asr_max_file_bytes:
                        raise AppError("AUDIO_TOO_LARGE", "音频文件超过配置的大小上限，最大 20 MiB。", 413)
                    target.write(chunk)
            if size == 0:
                raise AppError("AUDIO_EMPTY", "请上传非空音频文件。", 422)
            # AsrService 在模型推理前有界解码，并执行配置的最多 300 秒限制。
            result = self.adapter.transcribe(TranscriptionRequest(
                audio_path=path, language=command.language, request_id=uuid4().hex))
            return {"text": result.text,
                    "segments": [{"start": item.start, "end": item.end, "text": item.text}
                                 for item in result.segments],
                    "language": result.language, "duration": result.audio_duration_seconds}
