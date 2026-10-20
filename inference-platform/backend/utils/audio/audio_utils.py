"""有界解码并重采样音频；本模块不决定业务文件大小或时长上限。"""

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING
import wave

if TYPE_CHECKING:
    import numpy as np


class AudioDecodeError(ValueError):
    """没有有效音轨，或容器、音频帧不能正常解码。"""


class AudioSampleLimitExceeded(ValueError):
    """解码得到的样本数超过调用者提供的上限。"""


@dataclass(frozen=True)
class DecodedAudio:
    samples: "np.ndarray"
    sample_rate: int

    @property
    def duration_seconds(self) -> float:
        return self.samples.size / self.sample_rate


def encode_pcm16_wav(audio: DecodedAudio) -> bytes:
    """将已解码的单声道浮点样本转为标准 WAV，不依赖原文件扩展名。"""
    import numpy as np

    if (audio.sample_rate <= 0 or audio.samples.ndim != 1 or audio.samples.size == 0
            or not np.isfinite(audio.samples).all()):
        raise ValueError("音频采样格式无效")
    pcm = np.rint(np.clip(audio.samples, -1, 1) * 32767).astype("<i2")
    buffer = BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(audio.sample_rate)
        output.writeframes(pcm.tobytes())
    return buffer.getvalue()


def decode_audio_file(
    path: Path,
    *,
    max_samples: int,
    sample_rate: int = 16000,
) -> DecodedAudio:
    """将第一条音轨流式转为单声道 float32，按实际采样数限制内存。

    不信任容器声明的 duration，不跳过被标记损坏的包、帧或解码异常。
    PyAV 随包提供 FFmpeg 解码库，不调用系统 ffmpeg 命令。
    """
    if max_samples <= 0 or sample_rate <= 0:
        raise ValueError("样本数上限和采样率必须大于零")

    # 仅实际解码时加载原生扩展，使 API 基础入口不依赖音频 DLL 初始化。
    import av
    import numpy as np

    chunks = []
    sample_count = 0
    resampler = av.AudioResampler(format="flt", layout="mono", rate=sample_rate)

    def append_frames(frames):
        nonlocal sample_count
        for frame in frames:
            if sample_count + frame.samples > max_samples:
                raise AudioSampleLimitExceeded("解码采样数超过上限")
            array = frame.to_ndarray().reshape(-1)
            if not np.isfinite(array).all():
                raise AudioDecodeError("音频包含无效采样值")
            sample_count += array.size
            # 复制以免将解码器帧缓冲区保留到整个文件解码完成。
            chunks.append(array.copy())

    try:
        with av.open(str(path), mode="r") as container:
            if not container.streams.audio:
                raise AudioDecodeError("文件不包含音轨")
            stream = container.streams.audio[0]
            for packet in container.demux(stream):
                if packet.is_corrupt:
                    raise AudioDecodeError("音频包含损坏的数据包")
                for frame in packet.decode():
                    if frame.is_corrupt:
                        raise AudioDecodeError("音频包含损坏的音频帧")
                    # 根据解码后的实际样本顺序拼接，不依赖容器的时间戳连续性。
                    frame.pts = None
                    append_frames(resampler.resample(frame))
            append_frames(resampler.resample(None))
    except (AudioDecodeError, AudioSampleLimitExceeded):
        raise
    except (av.FFmpegError, OSError, ValueError) as exc:
        raise AudioDecodeError("音频解码失败") from exc

    if sample_count == 0:
        raise AudioDecodeError("音轨没有可解码的音频样本")
    samples = np.concatenate(chunks).astype(np.float32, copy=False)
    return DecodedAudio(samples=samples, sample_rate=sample_rate)
