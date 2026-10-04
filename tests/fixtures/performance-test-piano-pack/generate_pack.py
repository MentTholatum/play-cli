from __future__ import annotations

import hashlib
import json
import math
import shutil
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 16_000
FADE_FRAMES = 320
SAMPLE_FRAMES = {56: 40_000, 60: 8_000, 78: 14_400}
ROOT = Path(__file__).parent
LICENSE_SOURCE = ROOT.parent / "test-piano-pack" / "LICENSE"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_sample(midi: int, frame_count: int) -> Path:
    frequency = 440.0 * 2 ** ((midi - 69) / 12)
    frames = bytearray()
    for frame in range(frame_count):
        time = frame / SAMPLE_RATE
        attack = min(1.0, time / 0.005)
        decay = math.exp(-0.6 * time)
        waveform = (
            math.sin(2 * math.pi * frequency * time)
            + 0.35 * math.sin(4 * math.pi * frequency * time)
            + 0.15 * math.sin(6 * math.pi * frequency * time)
        ) / 1.5
        release = min(1.0, (frame_count - 1 - frame) / (FADE_FRAMES - 1))
        sample = round(0.45 * 32_767 * attack * decay * release * waveform)
        frames.extend(struct.pack("<h", sample))

    path = ROOT / "samples" / f"midi-{midi}.wav"
    path.parent.mkdir(exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(frames)
    return path


def main() -> None:
    shutil.copyfile(LICENSE_SOURCE, ROOT / "LICENSE")
    samples: dict[str, object] = {}
    for midi, frame_count in SAMPLE_FRAMES.items():
        path = _write_sample(midi, frame_count)
        samples[str(midi)] = {
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": _sha256(path),
            "byte_size": path.stat().st_size,
            "format": "PCM_S16LE",
            "sample_rate_hz": SAMPLE_RATE,
            "channels": 1,
            "frames": frame_count,
        }

    manifest = {
        "schema_version": "1.2",
        "id": "play-cli-performance-test-piano",
        "version": "1.0.0",
        "license": {
            "id": "CC0-1.0",
            "file": "LICENSE",
            "sha256": _sha256(ROOT / "LICENSE"),
            "complete_text": True,
        },
        "source": {
            "uri": "urn:play-cli:performance-test-piano-generator",
            "revision": {"type": "version", "value": "1.0"},
        },
        "attribution": "PLAY CLI contributors",
        "modifications": ["Deterministic synthesized fixture; not production piano audio."],
        "generator": {
            "version": "1.0",
            "command": (
                ".venv/bin/python tests/fixtures/performance-test-piano-pack/generate_pack.py"
            ),
        },
        "samples": samples,
    }
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
