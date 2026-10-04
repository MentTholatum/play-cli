from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"


def read_bundle(
    bundle: Path,
    score: Path,
    validation: dict[str, Any],
    frames: int,
    rest_frames: tuple[tuple[int, int], ...],
) -> dict[str, str]:
    manifest = json.loads((bundle / "result.json").read_text())
    assert {item.name for item in bundle.iterdir()} == {
        "score.musicxml",
        "validation.json",
        "schedule.json",
        "performance.wav",
        "result.json",
    } | {voice["relative_path"] for voice in manifest["result_bundle"].get("voices", [])}
    hashes = {item.name: hashlib.sha256(item.read_bytes()).hexdigest() for item in bundle.iterdir()}
    manifest = json.loads((bundle / "result.json").read_text())
    for voice in manifest["result_bundle"].get("voices", []):
        assert hashes[voice["relative_path"]] == voice["sha256"]
        assert voice["frames"] == frames
    for artifact in manifest["result_bundle"]["artifacts"].values():
        path = Path(artifact["path"])
        assert path == bundle / path.name
        assert hashes[path.name] == artifact["sha256"]
    assert (bundle / "score.musicxml").read_bytes() == score.read_bytes()
    assert manifest["warnings"] == validation["warnings"]
    assert (
        json.loads((bundle / "validation.json").read_text())["warnings"] == validation["warnings"]
    )
    assert json.loads((bundle / "schedule.json").read_text())["events"] == validation["events"]
    with wave.open(str(bundle / "performance.wav")) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 48000)
        assert wav.getnframes() == frames == manifest["wav"]["frames"]
        pcm = wav.readframes(wav.getnframes())
        assert len(pcm) == frames * 2
        for start, end in rest_frames:
            assert pcm[start * 2 : end * 2] == bytes((end - start) * 2)
    return hashes
