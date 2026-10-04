"""Bind local MVP scenario and audio checks to an installed candidate wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import struct
import subprocess
import sys
import time
import wave
import zipfile
from pathlib import Path

from benchmark_render_performance import _fixture
from workflow_evidence import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    executable = runtime / "bin/playcli"
    site = next(runtime.glob("lib/python*/site-packages"))
    with zipfile.ZipFile(args.wheel) as archive:
        expected = {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if name.startswith("play_cli/")
            or name.endswith(("/METADATA", "/WHEEL", "/entry_points.txt"))
        }

    def verify_runtime() -> None:
        assert {str(path.relative_to(site)) for path in (site / "play_cli").glob("*.py")} == {
            name for name in expected if name.startswith("play_cli/")
        }
        for name, digest in expected.items():
            assert hashlib.sha256((site / name).read_bytes()).hexdigest() == digest

    verify_runtime()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    runs = []
    base = ["--playcli", str(executable), "--store", str(args.store.resolve())]
    for name, script, extra in [
        ("numbered", "verify_numbered_score_flow.py", ["--notation", "numbered"]),
        ("staff", "verify_numbered_score_flow.py", ["--notation", "staff"]),
        (
            "correction",
            "verify_listening_correction.py",
            ["--feedback", str(ROOT / "tests/fixtures/listening-correction/feedback.json")],
        ),
        ("no-feedback", "verify_listening_correction.py", []),
    ]:
        command = [
            sys.executable,
            str(ROOT / "tools" / script),
            *base,
            "--output",
            str(output / name),
            *extra,
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        evidence = output / name / "evidence.json"
        runs.append(
            {
                "name": name,
                "argv": command,
                "response": json.loads(result.stdout),
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            }
        )
        verify_runtime()
    audio = []
    for bundle in (
        output / "numbered/bundle",
        output / "staff/bundle",
        output / "correction/revision-1",
        output / "correction/revision-2",
    ):
        manifest = json.loads((bundle / "result.json").read_text())
        schedule = json.loads((bundle / "schedule.json").read_text())["events"]
        with wave.open(str(bundle / "performance.wav")) as wav:
            rate = wav.getframerate()
            samples = struct.unpack(f"<{wav.getnframes()}h", wav.readframes(wav.getnframes()))
        peak = max(abs(value) for value in samples)
        bpm = manifest["score_context"]["tempo"]["bpm"]
        boundaries = {round(event["onset_quarters"] * 60 / bpm * rate) for event in schedule}
        maximum_boundary = max(
            abs(samples[index] - samples[index - 1])
            for index in boundaries
            if 0 < index < len(samples)
        )
        assert 64 <= peak < 32767
        assert abs(samples[0]) <= 2048 and abs(samples[-1]) <= 2
        assert maximum_boundary <= 2048
        audio.append(
            {
                "wav": str(bundle / "performance.wav"),
                "peak": peak,
                "maximum_onset_boundary_delta": maximum_boundary,
                "final_sample": samples[-1],
                "clipped_samples": 0,
            }
        )
    score, expected_events = _fixture(600)
    score_path = output / "ten-minute.musicxml"
    score_path.write_bytes(score)
    timings = []
    benchmark_hashes = []
    for index in range(6):
        bundle = output / f"benchmark-{index}"
        started = time.perf_counter()
        rendered = subprocess.run(
            [
                str(executable),
                "render",
                str(score_path),
                "--sound-pack",
                "play-cli-standard-piano-salamander",
                "--sound-pack-version",
                "1.0.0",
                "--sound-pack-store",
                str(args.store.resolve()),
                "--output",
                str(bundle),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        timings.append(time.perf_counter() - started)
        manifest = json.loads(rendered.stdout)
        assert json.loads((bundle / "result.json").read_text())["wav"] == manifest["wav"]
        assert json.loads((bundle / "schedule.json").read_text())["events"] == expected_events
        assert (bundle / "score.musicxml").read_bytes() == score
        assert json.loads((bundle / "validation.json").read_text())["valid"] is True
        for artifact in manifest["result_bundle"]["artifacts"].values():
            assert (
                hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest()
                == artifact["sha256"]
            )
        with wave.open(str(bundle / "performance.wav")) as wav:
            assert wav.getnframes() == 28800000
            assert wav.getframerate() == 48000
            assert len(wav.readframes(wav.getnframes())) == 57600000
        benchmark_hashes.append(manifest["result_bundle"]["artifacts"]["performance"]["sha256"])
        verify_runtime()
    assert len(set(benchmark_hashes)) == 1
    assert timings[0] <= 5.5 and statistics.median(timings[1:]) <= 5 and max(timings[1:]) <= 5.5
    skill_path = output / "play-cli-skill-0.9.0.zip"
    skill_files = [*sorted((ROOT / "skills/play-cli").glob("*.md")), ROOT / "LICENSE"]
    with zipfile.ZipFile(skill_path, "x") as archive:
        for path in skill_files:
            info = zipfile.ZipInfo(f"play-cli/{path.name}", date_time=(2026, 9, 12, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    with zipfile.ZipFile(skill_path) as archive:
        assert set(archive.namelist()) == {f"play-cli/{path.name}" for path in skill_files}
        for path in skill_files:
            assert archive.read(f"play-cli/{path.name}") == path.read_bytes()
    record = {
        "schema_version": SCHEMA_VERSION,
        "status": "automated_checks_passed",
        "human_listening": "pending",
        "wheel": {
            "path": str(args.wheel.resolve()),
            "sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
        },
        "installed_wheel_inventory": expected,
        "skill": {
            "path": str(skill_path),
            "sha256": hashlib.sha256(skill_path.read_bytes()).hexdigest(),
        },
        "runtime": str(runtime),
        "scenario_runs": runs,
        "audio_metrics": audio,
        "audio_scope": "Boundary/sample checks are not human absence-of-clicks certification.",
        "benchmark": {
            "seconds": timings,
            "repeat_median": statistics.median(timings[1:]),
            "wav_sha256": benchmark_hashes[0],
            "duration_seconds": 600,
        },
        "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (output / "evidence.json").write_text(json.dumps(record, indent=2) + "\n")
    print(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "status": record["status"],
                "evidence": str(output / "evidence.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
