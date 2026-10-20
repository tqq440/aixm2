"""第二部分的同步本地 ASR 调用；不创建任务、队列或 Worker。"""

import logging
import stat
from collections.abc import Callable
from importlib.metadata import version
from time import perf_counter
from typing import Any

from backend.common.exceptions.app_error import AppError
from backend.config.asr.model_catalog import SUPPORTED_LANGUAGES
from backend.config.settings.app_settings import AppSettings
from backend.schema.asr.asr_schema import (
    AsrEngineMetadata,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionSegment,
)
from backend.service.asr.model_preparation_service import inspect_model
from backend.utils.audio.audio_utils import (
    AudioDecodeError,
    AudioSampleLimitExceeded,
    decode_audio_file,
)

LOGGER = logging.getLogger(__name__)
SAMPLE_RATE = 16000


class AsrService:
    """先检查音频和本地模型，再按 CPU/int8 执行同步转写。"""

    def __init__(
        self,
        settings: AppSettings,
        *,
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self._model_factory = model_factory
        self._model: Any | None = None

    def prepare_model(self) -> float:
        """Worker 在接单前显式加载并校验模型；复用同一实例不会重复加载。"""
        return self._load_model()

    def _load_model(self) -> float:
        if self._model is not None:
            return 0.0
        # 必须包含 tokenizer.json；否则 faster-whisper 本身仍可能远程兜底。
        # 模型缺失/校验错误沿用准备服务的明确 AppError。
        inspect_model(self.settings)
        started = perf_counter()
        try:
            factory = self._model_factory
            if factory is None:
                from faster_whisper import WhisperModel

                factory = WhisperModel
            model = factory(
                str(self.settings.asr_model_dir),
                device=self.settings.asr_device,
                compute_type=self.settings.asr_compute_type,
                cpu_threads=self.settings.asr_cpu_threads,
                num_workers=1,
                local_files_only=True,
            )
            self._model = model
        except Exception as exc:
            LOGGER.exception("本地 ASR 模型加载失败")
            raise AppError(
                "MODEL_LOAD_FAILED",
                "本地模型加载失败，请检查 ASR 依赖、模型文件及可用内存。",
                503,
            ) from exc
        return perf_counter() - started

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        started = perf_counter()
        if request.language is not None and request.language not in SUPPORTED_LANGUAGES:
            raise AppError("LANGUAGE_UNSUPPORTED", "不支持此语言代码；中文请使用 zh，自动识别请使用 auto。")
        try:
            file_info = request.audio_path.stat()
        except FileNotFoundError as exc:
            raise AppError("AUDIO_NOT_FOUND", "音频文件不存在。", 404) from exc
        except OSError as exc:
            raise AppError("AUDIO_DECODE_FAILED", "无法读取音频文件，请检查文件权限。") from exc
        if not stat.S_ISREG(file_info.st_mode):
            raise AppError("AUDIO_NOT_FOUND", "音频路径必须指向普通文件。", 404)
        if file_info.st_size > self.settings.asr_max_file_bytes:
            raise AppError(
                "AUDIO_TOO_LARGE",
                f"音频文件超过大小上限（{self.settings.asr_max_file_bytes} 字节）。",
                413,
            )
        try:
            audio = decode_audio_file(
                request.audio_path,
                sample_rate=SAMPLE_RATE,
                max_samples=int(self.settings.asr_max_audio_seconds * SAMPLE_RATE),
            )
        except AudioSampleLimitExceeded as exc:
            raise AppError(
                "AUDIO_TOO_LONG",
                f"音频时长超过上限（{self.settings.asr_max_audio_seconds} 秒）。",
                413,
            ) from exc
        except AudioDecodeError as exc:
            raise AppError("AUDIO_DECODE_FAILED", "音频无法正常解码或没有有效音轨，请检查文件是否损坏。") from exc
        except ImportError as exc:
            raise AppError("AUDIO_DECODE_FAILED", "音频运行库不可用，请按 requirements/asr.txt 安装依赖。", 503) from exc

        model_load_seconds = self._load_model()
        inference_started = perf_counter()
        try:
            segments_iterator, info = self._model.transcribe(
                audio.samples,
                language=request.language,
                task="transcribe",
                beam_size=5,
                temperature=0.0,
                vad_filter=False,
                condition_on_previous_text=True,
            )
            # faster-whisper 返回惰性生成器；实际计算、异常和计时必须包含完整遍历。
            segments = [
                TranscriptionSegment(start=segment.start, end=segment.end, text=segment.text)
                for segment in segments_iterator
            ]
            transcription_seconds = perf_counter() - inference_started
            engine = getattr(self._model, "model", self._model)
            metadata = AsrEngineMetadata(
                adapter_key="faster-whisper", model_repo=self.settings.asr_model_repo,
                model_revision=self.settings.asr_model_revision,
                device=getattr(engine, "device", self.settings.asr_device),
                compute_type=getattr(engine, "compute_type", self.settings.asr_compute_type),
                cpu_threads=self.settings.asr_cpu_threads, model_load_seconds=model_load_seconds,
                transcription_seconds=transcription_seconds, total_seconds=perf_counter() - started,
                runtime_versions={"faster-whisper": version("faster-whisper"), "ctranslate2": version("ctranslate2")},
            )
            result = TranscriptionResult(
                text="".join(segment.text for segment in segments).strip(),
                segments=segments,
                language_requested=request.language,
                language=info.language,
                language_probability=info.language_probability,
                audio_duration_seconds=audio.duration_seconds,
                engine_metadata=metadata,
                model_repo=metadata.model_repo,
                model_revision=metadata.model_revision,
                device=metadata.device,
                compute_type=metadata.compute_type,
                cpu_threads=metadata.cpu_threads,
                model_load_seconds=metadata.model_load_seconds,
                transcription_seconds=metadata.transcription_seconds,
                total_seconds=metadata.total_seconds,
                faster_whisper_version=metadata.runtime_versions["faster-whisper"],
                ctranslate2_version=metadata.runtime_versions["ctranslate2"],
            )
        except Exception as exc:
            LOGGER.exception("ASR 转写失败")
            raise AppError("TRANSCRIPTION_FAILED", "模型转写失败，请检查日志中的原因。", 500) from exc
        LOGGER.info(
            "ASR 完成 duration_seconds=%.3f device=%s compute_type=%s transcription_seconds=%.3f",
            result.audio_duration_seconds,
            result.device,
            result.compute_type,
            result.transcription_seconds,
        )
        return result
