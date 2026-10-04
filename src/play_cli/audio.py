from __future__ import annotations

import io
import struct
import wave
from dataclasses import dataclass


@dataclass(frozen=True)
class ValidatedPcmWav:
    sample_rate_hz: int
    frames: int
    frame_data: bytes


class PcmWavValidationError(Exception):
    pass


def validate_pcm16_mono_wav(
    data: bytes,
    *,
    expected_sample_rate_hz: int | None = None,
    expected_frames: int | None = None,
) -> ValidatedPcmWav:
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            sample_width = source.getsampwidth()
            compression = source.getcomptype()
            sample_rate = source.getframerate()
            channels = source.getnchannels()
            frames = source.getnframes()
            frame_data = source.readframes(frames)
    except (EOFError, OSError, struct.error, wave.Error) as error:
        raise PcmWavValidationError("WAV data is unreadable") from error

    if sample_width != 2 or compression != "NONE" or channels != 1:
        raise PcmWavValidationError("WAV must be mono PCM16")
    if expected_sample_rate_hz is not None and sample_rate != expected_sample_rate_hz:
        raise PcmWavValidationError("WAV sample rate does not match")
    if expected_frames is not None and frames != expected_frames:
        raise PcmWavValidationError("WAV frame count does not match")
    if len(frame_data) != frames * channels * sample_width:
        raise PcmWavValidationError("WAV frame data is truncated")
    return ValidatedPcmWav(
        sample_rate_hz=sample_rate,
        frames=frames,
        frame_data=frame_data,
    )
