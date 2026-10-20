"""Remote-V2 同步文件调用；无模型启停、重试或取消成功推断。"""

import asyncio
import ssl
import stat
from time import perf_counter

import httpx2 as httpx
from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings, remote_asr_origin
from backend.schema.asr.asr_schema import AsrEngineMetadata, TranscriptionRequest, TranscriptionResult, TranscriptionSegment
from backend.schema.asr.remote_v2_schema import (
    RemoteAsrHealth, RemoteHealthResponse, RemoteRejection, RemoteTranscriptResponse,
)
from backend.service.asr.remote_catalog import REMOTE_ADAPTER_KEY, remote_asr_configured
from backend.utils.audio.audio_utils import decode_audio_file, encode_pcm16_wav

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_HEALTH_BYTES = 65_536


def _ssl_context(settings: AppSettings) -> ssl.SSLContext:
    """默认验证证书；显式例外只对完整配置绑定的唯一 HTTPS 来源生效。"""
    origin = settings.remote_asr_insecure_tls_origin
    if origin is None:
        return ssl.create_default_context(cafile=str(settings.remote_asr_ca_file) if settings.remote_asr_ca_file else None)
    if (not remote_asr_configured(settings) or settings.remote_asr_ca_file is not None
            or origin != remote_asr_origin(settings.remote_asr_base_url)):
        raise ValueError("TLS 例外与当前外部 ASR 配置不匹配")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def _request_bytes(settings: AppSettings, method: str, path: str, *, transport,
                         total_timeout: float, audio: bytes | None = None,
                         request_id: str | None = None) -> tuple[int, bytes]:
    # 在发送前核对证书策略；例外不跟随重定向，也不作用于其他来源。
    context = _ssl_context(settings)
    timeout = httpx.Timeout(total_timeout, connect=min(settings.remote_asr_connect_timeout_seconds, total_timeout))
    async with httpx.AsyncClient(
        verify=context, transport=transport, timeout=timeout, trust_env=False, follow_redirects=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    ) as client:
        kwargs = {}
        if audio is not None:
            kwargs = {"files": {"audio": ("audio.wav", audio, "audio/wav")},
                      "data": {"outputs": "transcript", "engine": "sensevoice", "request_id": request_id}}
        async with client.stream(method, settings.remote_asr_base_url.rstrip("/") + path,
                                 headers={"Accept": "application/json", "Accept-Encoding": "identity"}, **kwargs) as response:
            if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                raise ValueError("不接受压缩响应，避免绕过接收大小上限")
            maximum = MAX_RESPONSE_BYTES if method == "POST" else MAX_HEALTH_BYTES
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > maximum:
                    raise ValueError("响应超过接收上限")
                data.extend(chunk)
            return response.status_code, bytes(data)


async def _bounded_request(settings: AppSettings, method: str, path: str, *, transport,
                           total_timeout: float, audio: bytes | None = None,
                           request_id: str | None = None) -> tuple[int, bytes]:
    return await asyncio.wait_for(
        _request_bytes(settings, method, path, transport=transport, total_timeout=total_timeout,
                       audio=audio, request_id=request_id), timeout=total_timeout,
    )


def probe_remote_asr(settings: AppSettings, *, transport=None) -> RemoteAsrHealth:
    """有界只读健康查询；失败仅返回可公开原因，未配置时不产生网络请求。"""
    if not remote_asr_configured(settings):
        return RemoteAsrHealth(configured=False, ready=False, error_code="REMOTE_ASR_NOT_CONFIGURED",
                               error_message="外部 ASR 尚未配置实际连接和平台模型标识。")
    try:
        code, body = asyncio.run(_bounded_request(
            settings, "GET", "/api/v1/asr/health", transport=transport,
            total_timeout=min(settings.remote_asr_connect_timeout_seconds, settings.remote_asr_total_timeout_seconds),
        ))
        payload = RemoteHealthResponse.model_validate_json(body)
        if code != 200 or payload.data.status != "ok" or payload.data.model_loaded is not True:
            raise ValueError("模型尚未就绪")
        return RemoteAsrHealth(configured=True, ready=True, api_version=payload.data.version)
    except Exception:
        return RemoteAsrHealth(configured=True, ready=False, error_code="REMOTE_ASR_UNAVAILABLE",
                               error_message="外部 ASR 未通过就绪检查，请核对服务、网络与可信证书。")


class RemoteV2AsrAdapter:
    """测试只能显式注入 transport；日常运行没有模拟地址或假结果回退。"""

    def __init__(self, settings: AppSettings, *, transport=None) -> None:
        self.settings = settings
        self._transport = transport
        self._api_version: str | None = None

    def prepare_model(self) -> float:
        started = perf_counter()
        health = probe_remote_asr(self.settings, transport=self._transport)
        if not health.ready:
            raise AppError(health.error_code, health.error_message, 503)
        self._api_version = health.api_version
        return perf_counter() - started

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        # 此区间失败可以证明尚未发送 POST；读取/转换失败不保留不明远端占位。
        try:
            request = TranscriptionRequest.model_validate(request.model_dump())
            if not remote_asr_configured(self.settings) or not request.request_id or request.language is not None:
                raise ValueError("外部调用要求完整配置、关联编号以及服务端决定语言")
            file_info = request.audio_path.stat()
            if not stat.S_ISREG(file_info.st_mode) or file_info.st_size > self.settings.asr_max_file_bytes:
                raise ValueError("音频文件类型或大小无效")
            audio = decode_audio_file(request.audio_path, sample_rate=16000,
                                      max_samples=int(self.settings.asr_max_audio_seconds * 16000))
            encoded = encode_pcm16_wav(audio)
            # 提前验证证书文件；网络阶段再次创建上下文，配置失效仍从保守路径收尾。
            _ssl_context(self.settings)
        except Exception:
            raise AppError("REMOTE_ASR_NOT_SENT", "外部转写请求尚未发送；请检查音频及连接配置。", 422) from None

        try:
            code, body = asyncio.run(_bounded_request(
                self.settings, "POST", "/api/v1/asr/transcripts", transport=self._transport,
                total_timeout=self.settings.remote_asr_total_timeout_seconds,
                audio=encoded, request_id=request.request_id,
            ))
            if code in (400, 413, 503):
                rejection = RemoteRejection.model_validate_json(body)
                if code != 503 or rejection.detail.strip().lower() == "service not ready":
                    raise AppError("REMOTE_ASR_REJECTED", "外部服务明确拒绝本次转写，请检查服务状态和输入。", 502)
            if code != 200:
                raise ValueError("上游状态未证明结果")
            payload = RemoteTranscriptResponse.model_validate_json(body)
            if payload.details.request_id is not None and payload.details.request_id != request.request_id:
                raise ValueError("响应关联编号不匹配")
            segments = [TranscriptionSegment(start=item.start, end=item.end, text=item.text)
                        for item in payload.details.segments]
            if any(segment.end > audio.duration_seconds + 1 / 16000 for segment in segments):
                raise ValueError("分段超出音频时长")
            if any(second.start < first.start for first, second in zip(segments, segments[1:])):
                raise ValueError("分段时间顺序无效")
            text = "".join(segment.text for segment in segments).strip()
            if len(text) > 131_072:
                raise ValueError("转写文本超过接收上限")
            return TranscriptionResult(
                text=text, segments=segments, audio_duration_seconds=audio.duration_seconds,
                engine_metadata=AsrEngineMetadata(adapter_key=REMOTE_ADAPTER_KEY,
                    runtime_versions={"remote-v2-api": self._api_version} if self._api_version else {}),
            )
        except AppError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            # 建连/TLS 失败时 HTTP 请求尚未发送；不能将读取/写入失败归入此类。
            raise AppError("REMOTE_ASR_NOT_SENT", "尚未向外部服务发送转写请求，连接或证书检查失败。", 503) from None
        except Exception:
            # 包括超时、断线、非契约错误和坏结果；连接关闭不证明远端推理结束。
            raise AppError("REMOTE_ASR_UNKNOWN", "外部转写结果未能确认；不会自动重发，请核对远端请求。", 502) from None
