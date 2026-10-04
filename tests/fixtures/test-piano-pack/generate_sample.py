from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 16_000
TONE_FRAMES = 7_200
FADE_FRAMES = 320
TAIL_FRAMES = 800
ROOT = Path(__file__).parent


def main() -> None:
    frames = bytearray()
    for frame in range(TONE_FRAMES):
        time = frame / SAMPLE_RATE
        attack = min(1.0, time / 0.005)
        decay = math.exp(-5.5 * time)
        waveform = (
            math.sin(2 * math.pi * 261.625565 * time)
            + 0.45 * math.sin(2 * math.pi * 523.25113 * time)
            + 0.2 * math.sin(2 * math.pi * 784.876695 * time)
        ) / 1.65
        sample = round(0.72 * 32_767 * attack * decay * waveform)
        if frame >= TONE_FRAMES - FADE_FRAMES:
            remaining = TONE_FRAMES - 1 - frame
            magnitude = (abs(sample) * remaining + (FADE_FRAMES - 1) // 2) // (FADE_FRAMES - 1)
            sample = magnitude if sample >= 0 else -magnitude
        frames.extend(struct.pack("<h", sample))
    frames.extend(b"\0\0" * TAIL_FRAMES)

    sample_path = ROOT / "samples/c4.wav"
    sample_path.parent.mkdir(exist_ok=True)
    with wave.open(str(sample_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(frames)


if __name__ == "__main__":
    main()
