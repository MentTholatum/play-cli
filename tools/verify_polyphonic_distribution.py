"""Verify an installed polyphonic candidate; listening remains a human decision."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import stat
import struct
import subprocess
import sys
import time
import venv
import wave
import zipfile
from pathlib import Path
from typing import Any

from verify_platform_distribution import _inspect_wheel, _runtime_metadata

ROOT = Path(__file__).resolve().parents[1]


def read_result(path: Path) -> dict[str, str]:
    manifest = json.loads((path / "result.json").read_text())
    voices = manifest["result_bundle"]["voices"]
    names = {"score.musicxml", "schedule.json", "validation.json", "performance.wav", "result.json"}
    names.update(v["relative_path"] for v in voices)
    assert {p.name for p in path.iterdir()} == names
    assert stat.S_IMODE(path.stat().st_mode) == 0o555
    assert all(
        stat.S_ISREG(p.lstat().st_mode)
        and stat.S_IMODE(p.stat().st_mode) == 0o444
        and p.stat().st_nlink == 1
        for p in path.iterdir()
    )
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.iterdir()}
    for artifact in manifest["result_bundle"]["artifacts"].values():
        assert hashes[Path(artifact["path"]).name] == artifact["sha256"]
    for voice in voices:
        data = (path / voice["relative_path"]).read_bytes()
        assert hashes[voice["relative_path"]] == voice["sha256"]
        assert struct.unpack_from("<HH", data, 20) == (3, 1)
        assert struct.unpack_from("<I", data, 24)[0] == manifest["wav"]["sample_rate_hz"]
        assert len(data) == 56 + voice["frames"] * 8
        assert voice["frames"] == manifest["wav"]["frames"]
    with wave.open(str(path / "performance.wav")) as wav:
        assert wav.getnchannels() == 1 and wav.getsampwidth() == 2
        assert wav.getnframes() == manifest["wav"]["frames"]
        assert len(wav.readframes(wav.getnframes())) == wav.getnframes() * 2
    return hashes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--wheel", type=Path)
    group.add_argument("--playcli", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--score", type=Path)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--benchmark", choices=("mono", "dense"))
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    wheel_record = None
    wheel_metadata = None
    runtime_metadata = None
    installed_inventory: dict[str, str] = {}
    if args.wheel:
        version, entry_points, wheel_metadata = _inspect_wheel(args.wheel)
        assert version == "0.10.0"
        assert entry_points == {"playcli": "play_cli.cli:main"}
        runtime = output / "runtime"
        venv.EnvBuilder(with_pip=True).create(runtime)
        before_executables = {p.name for p in (runtime / "bin").iterdir()}
        subprocess.run(
            [
                str(runtime / "bin/python"),
                "-m",
                "pip",
                "install",
                "--no-deps",
                str(args.wheel.resolve()),
            ],
            check=True,
            capture_output=True,
            env=environment,
        )
        cli = runtime / "bin/playcli"
        assert {p.name for p in (runtime / "bin").iterdir()} - before_executables == {"playcli"}
        assert os.access(cli, os.X_OK)
        runtime_metadata = _runtime_metadata(runtime / "bin/python")
        assert runtime_metadata["version"] == "0.10.0"
        assert runtime_metadata["entry_points"] == [
            {"group": "console_scripts", "name": "playcli", "value": "play_cli.cli:main"}
        ]
        site = next(runtime.glob("lib/python*/site-packages"))
        with zipfile.ZipFile(args.wheel) as archive:
            for name in archive.namelist():
                if name.endswith("/") or name.endswith(".dist-info/RECORD"):
                    continue
                installed = site / name
                assert installed.read_bytes() == archive.read(name)
                installed_inventory[str(installed)] = hashlib.sha256(
                    installed.read_bytes()
                ).hexdigest()
        wheel_record = {
            "filename": args.wheel.name,
            "sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
        }
    else:
        cli = args.playcli.resolve()

    operations: list[dict[str, Any]] = []

    def run(*arguments: str) -> dict[str, Any]:
        started = time.perf_counter()
        command = [str(cli), "--schema-version", "1.6", *arguments]
        measure_memory = arguments[0] == "render" and sys.platform == "darwin"
        if measure_memory:
            command = ["/usr/bin/time", "-l", *command]
        result = subprocess.run(
            command,
            cwd=output,
            env=environment,
            capture_output=True,
            text=True,
        )
        elapsed = time.perf_counter() - started
        assert result.returncode == 0, result.stderr
        assert len(result.stdout.splitlines()) == 1
        value: dict[str, Any] = json.loads(result.stdout)
        assert value["schema_version"] == "1.6" and value["status"] == "ok"
        memory = re.search(r"(\d+)\s+maximum resident set size", result.stderr)
        operations.append(
            {
                "operation": value["operation"],
                "elapsed_seconds": elapsed,
                "peak_rss_bytes": int(memory[1]) if memory else None,
            }
        )
        return value

    dependencies = run("doctor")["dependencies"]
    assert dependencies["python"]["status"] == "available"
    assert dependencies["atomic_noreplace"]["status"] == "available"
    assert dependencies["platform"]["status"] == "supported"
    store = args.store.resolve() if args.store else output / "packs"
    pack = "play-cli-standard-piano-salamander" if args.store else "play-cli-performance-test-piano"
    if not args.store:
        run(
            "sound-pack",
            "install",
            str(ROOT / "tests/fixtures/performance-test-piano-pack"),
            "--store",
            str(store),
        )
    run("sound-pack", "list", "--store", str(store))
    run("sound-pack", "verify", pack, "--version", "1.0.0", "--store", str(store))
    score = (
        args.score.resolve()
        if args.score
        else ROOT / "tests/fixtures/polyphonic-score/interpretation.musicxml"
    )
    if args.benchmark:
        voices = 1 if args.benchmark == "mono" else 8
        measures = 300 if args.benchmark == "mono" else 10
        bars = []
        for number in range(measures):
            music = "<attributes><divisions>1</divisions></attributes>" if number == 0 else ""
            if number == 0:
                music += (
                    "<direction><direction-type><metronome><beat-unit>quarter</beat-unit>"
                    "<per-minute>120</per-minute></metronome></direction-type></direction>"
                )
            for voice in range(voices):
                if voice:
                    music += "<backup><duration>4</duration></backup>"
                music += (
                    f"<note><pitch><step>C</step><octave>4</octave></pitch>"
                    f"<duration>1</duration><voice>{voice + 1}</voice></note>"
                ) * 4
            bars.append(f'<measure number="{number + 1}">{music}</measure>')
        score = output / "benchmark.musicxml"
        score.write_text(
            '<score-partwise><part-list><score-part id="P1"><part-name>Piano</part-name>'
            '</score-part></part-list><part id="P1">' + "".join(bars) + "</part></score-partwise>"
        )
    validation = run("validate", str(score))
    if not args.score and not args.benchmark:
        expected = []
        for measure in range(4):
            for beat, midi in enumerate((60, 78, 60, 78)):
                expected.append(("melody", measure * 4 + beat, 1, midi, measure // 2 + 1))
            expected.append(("bass", measure * 4, 4, 56, measure // 2 + 1))
        actual = [
            (
                e["voice"],
                e["onset_quarters"],
                e["duration_quarters"],
                e["pitch"]["midi"],
                e["pages"][0],
            )
            for e in validation["events"]
        ]
        assert sorted(actual) == sorted(expected)
    options = (
        "--sound-pack",
        pack,
        "--sound-pack-version",
        "1.0.0",
        "--sound-pack-store",
        str(store),
    )
    result = run("render", str(score), *options, "--output", str(output / "first"))
    hashes = read_result(output / "first")
    assert (
        json.loads((output / "first/schedule.json").read_text())["events"] == validation["events"]
    )
    revision = None
    if not args.benchmark:
        revision = run(
            "render",
            str(score),
            *options,
            "--output",
            str(output / "second"),
            "--previous-result",
            str(output / "first"),
            "--revision-reason",
            "Synthetic integration replay; not user feedback.",
        )
        read_result(output / "second")
        assert read_result(output / "first") == hashes
        assert revision["performance_request"]["current_delivered_revision"] == 2
    record = {
        "schema_version": "1.0",
        "status": "ok",
        "wheel": wheel_record,
        "cli": result["cli"],
        "platform": platform.platform(),
        "python": sys.version,
        "source_sha256": hashlib.sha256(score.read_bytes()).hexdigest(),
        "operations": operations,
        "artifact_hashes": hashes,
        "wav": result["wav"],
        "voices": result["result_bundle"]["voices"],
        "event_count": len(validation["events"]),
        "revision_verified": revision is not None,
        "human_listening": "not_performed",
        "wheel_metadata": wheel_metadata,
        "runtime_metadata": runtime_metadata,
        "runtime_dependencies": dependencies,
        "installed_inventory_sha256": hashlib.sha256(
            json.dumps(installed_inventory, sort_keys=True).encode()
        ).hexdigest(),
    }
    assert all(
        hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
        for path, digest in installed_inventory.items()
    )
    if args.wheel:
        skill_path = output / "play-cli-skill-0.10.0.zip"
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
        record["skill"] = {
            "path": str(skill_path),
            "sha256": hashlib.sha256(skill_path.read_bytes()).hexdigest(),
        }
    (output / "record.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
