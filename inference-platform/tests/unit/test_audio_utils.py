"""真实 PyAV 解码测试；合成波形只验证解码，不作为真实语音转写验收。"""

import wave
from types import SimpleNamespace
from unittest.mock import MagicMock

import av
import numpy as np
import pytest

from backend.utils.audio.audio_utils import (
    AudioDecodeError,
    AudioSampleLimitExceeded,
    decode_audio_file,
)


@pytest.fixture
def wav_factory(tmp_path):
    def create(*, sample_rate=16000, sample_count=16000, channels=1):
        path = tmp_path / f"{sample_rate}-{sample_count}-{channels}.wav"
        samples = np.sin(np.arange(sample_count) * 2 * np.pi * 440 / sample_rate)
        samples = (samples * 16383).astype("<i2")
        if channels == 2:
            samples = np.column_stack((samples, samples))
        with wave.open(str(path), "wb") as target:
            target.setnchannels(channels)
            target.setsampwidth(2)
            target.setframerate(sample_rate)
            target.writeframes(samples.tobytes())
        return path

    return create


@pytest.mark.parametrize("sample_rate,channels", [(16000, 1), (8000, 1), (44100, 2), (48000, 2)])
def test_real_wav_decodes_and_resamples_to_mono_float32(wav_factory, sample_rate, channels):
    path = wav_factory(sample_rate=sample_rate, sample_count=sample_rate, channels=channels)
    decoded = decode_audio_file(path, max_samples=16000)
    assert decoded.samples.dtype == np.float32
    assert decoded.samples.shape == (16000,)
    assert decoded.duration_seconds == 1.0
    assert np.isfinite(decoded.samples).all()
    assert np.abs(decoded.samples).max() > 0.1


def test_exact_sample_limit_allowed_but_one_extra_sample_rejected(wav_factory):
    assert decode_audio_file(wav_factory(sample_count=16000), max_samples=16000).samples.size == 16000
    with pytest.raises(AudioSampleLimitExceeded):
        decode_audio_file(wav_factory(sample_count=16001), max_samples=16000)


def test_resampler_flush_counts_towards_sample_limit(wav_factory):
    with pytest.raises(AudioSampleLimitExceeded):
        decode_audio_file(wav_factory(sample_rate=44100, sample_count=44100), max_samples=15999)


def test_broken_audio_is_rejected(tmp_path):
    path = tmp_path / "broken.wav"
    path.write_bytes(b"not an audio file")
    with pytest.raises(AudioDecodeError):
        decode_audio_file(path, max_samples=16000)


def test_empty_audio_track_is_rejected(wav_factory):
    with pytest.raises(AudioDecodeError):
        decode_audio_file(wav_factory(sample_count=0), max_samples=16000)


def test_truncated_wav_payload_is_not_silently_accepted(wav_factory):
    path = wav_factory()
    path.write_bytes(path.read_bytes()[:-101])
    with pytest.raises(AudioDecodeError):
        decode_audio_file(path, max_samples=16000)


def test_real_video_without_audio_is_rejected(tmp_path):
    path = tmp_path / "video_only.mp4"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=1)
        stream.width = stream.height = 16
        stream.pix_fmt = "yuv420p"
        frame = av.VideoFrame.from_ndarray(np.zeros((16, 16, 3), dtype=np.uint8), format="rgb24")
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    with pytest.raises(AudioDecodeError, match="不包含音轨"):
        decode_audio_file(path, max_samples=16000)


def test_decode_stops_as_soon_as_limit_is_exceeded(monkeypatch, tmp_path):
    frame = av.AudioFrame.from_ndarray(np.zeros((1, 16001), dtype=np.float32), format="flt", layout="mono")
    frame.sample_rate = 16000
    packet = MagicMock()
    packet.is_corrupt = False
    packet.decode.return_value = [frame]
    container = MagicMock()
    container.__enter__.return_value = container
    container.streams.audio = [object()]

    def packets():
        yield packet
        pytest.fail("超限后不应继续读取数据包")

    container.demux.return_value = packets()
    monkeypatch.setattr(av, "open", lambda *args, **kwargs: container)
    with pytest.raises(AudioSampleLimitExceeded):
        decode_audio_file(tmp_path / "unused", max_samples=16000)
    assert container.__exit__.called


def test_corrupt_packet_is_not_skipped(monkeypatch, tmp_path):
    container = MagicMock()
    container.__enter__.return_value = container
    container.streams.audio = [object()]
    container.demux.return_value = [SimpleNamespace(is_corrupt=True)]
    monkeypatch.setattr(av, "open", lambda *args, **kwargs: container)
    with pytest.raises(AudioDecodeError, match="损坏的数据包"):
        decode_audio_file(tmp_path / "unused", max_samples=16000)


@pytest.mark.parametrize("max_samples,sample_rate", [(0, 16000), (-1, 16000), (16000, 0)])
def test_nonpositive_decode_limits_are_rejected(tmp_path, max_samples, sample_rate):
    with pytest.raises(ValueError):
        decode_audio_file(tmp_path / "unused", max_samples=max_samples, sample_rate=sample_rate)
