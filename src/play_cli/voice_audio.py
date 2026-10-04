from __future__ import annotations

import struct
import sys
from array import array

MAX_VOICES = 16
MAX_OUTPUT_BYTES = 512 * 1024 * 1024


def encode_voice(pcm: bytes, sample_rate: int) -> bytes:
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return encode_voice_samples(samples, sample_rate)


def encode_voice_samples(samples: array[int], sample_rate: int) -> bytes:
    values = array("d", (value / 32768 for value in samples))
    if sys.byteorder != "little":
        values.byteswap()
    payload = values.tobytes()
    return voice_wav(payload, sample_rate)


def voice_wav(payload: bytes, sample_rate: int) -> bytes:
    frames = len(payload) // 8
    return (
        struct.pack(
            "<4sI4s4sIHHIIHH",
            b"RIFF",
            48 + len(payload),
            b"WAVE",
            b"fmt ",
            16,
            3,
            1,
            sample_rate,
            sample_rate * 8,
            8,
            64,
        )
        + struct.pack("<4sII4sI", b"fact", 4, frames, b"data", len(payload))
        + payload
    )


def validate_voice(data: bytes, sample_rate: int, frames: int) -> None:
    if len(data) != 56 + frames * 8:
        raise OSError("Voice WAV frame data is truncated or oversized")
    expected = struct.pack(
        "<4sI4s4sIHHIIHH4sII4sI",
        b"RIFF",
        len(data) - 8,
        b"WAVE",
        b"fmt ",
        16,
        3,
        1,
        sample_rate,
        sample_rate * 8,
        8,
        64,
        b"fact",
        4,
        frames,
        b"data",
        frames * 8,
    )
    if data[:56] != expected:
        raise OSError("Voice WAV format does not match the declared float64 timeline")
