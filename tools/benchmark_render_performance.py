from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import math
import os
import platform
import stat
import subprocess
import sys
import time
import wave
import zipfile
from collections.abc import Callable
from io import BytesIO, StringIO
from pathlib import Path
from statistics import median
from typing import Any

from play_cli import sound_pack_storage as storage
from play_cli.result_bundle import (
    REQUIRED_ARTIFACTS,
    PublishedResultBundle,
    ResultBundleFailure,
    open_published_result_bundle,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "1.0"
OPERATION = "benchmark-render-performance"
FORMAL_DURATION_SECONDS = 600
FORMAL_REPEAT_RUNS = 5
FIRST_THRESHOLD_SECONDS = 5.5
MEDIAN_THRESHOLD_SECONDS = 5.0
MAX_THRESHOLD_SECONDS = 5.5
SUBPROCESS_TIMEOUT_SECONDS = 120
STANDARD_PACK = {
    "id": "play-cli-standard-piano-salamander",
    "version": "1.0.0",
    "pack_hash": "f82e6d9aff03d7162f1d35254f1e7524c7f26a01a86435997b23eb23a0d7ba3e",
}
QUALIFICATION_RECORD = (
    ROOT / "standards/salamander-3382bf9496bba2486f5ab0de55a264d1dfc38404.qualification.json"
)
QUALIFICATION_SHA256 = "59dd8d18e49dceb1196e46bdb88106b0a4b6cc19964b84caabc729c5987411d0"
LISTENING_RECORD = (
    ROOT / "standards/salamander-3382bf9496bba2486f5ab0de55a264d1dfc38404.listening-acceptance.json"
)
LISTENING_SHA256 = "08e938dc5b411e6e335aa29e1a1c07880c1a5621c37c861192ad90f78cba9c98"
PLATFORM_RECORD = ROOT / "standards/play-cli-0.8.1-macos-arm64.packaging.json"
PLATFORM_RECORD_SHA256 = "326df3fed6d328f4e571a5fadbe44936f0a46a63660deb046217cbae68da6304"
ACCEPTED_WHEEL_SHA256 = "b9458ddefc7b72e0c145015ba09bd62e6c6e613cf59139f605ab296d7e77f44a"
SCALE = (
    ("C", 4, 60),
    ("D", 4, 62),
    ("E", 4, 64),
    ("F", 4, 65),
    ("G", 4, 67),
    ("A", 4, 69),
    ("B", 4, 71),
    ("C", 5, 72),
)


class BenchmarkFailure(Exception):
    def __init__(self, error_id: str, message: str) -> None:
        super().__init__(message)
        self.error_id = error_id
        self.message = message


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object(data: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except json.JSONDecodeError as error:
        raise BenchmarkFailure("benchmark_contract_invalid", f"{label} is not JSON.") from error
    if not isinstance(value, dict):
        raise BenchmarkFailure("benchmark_contract_invalid", f"{label} is not an object.")
    return value


def _wheel_inventory(wheel: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            if len(names) != len(set(names)):
                raise BenchmarkFailure("benchmark_runtime_invalid", "Wheel inventory is ambiguous.")
            inventory = {
                name: _sha256(archive.read(name))
                for name in names
                if name.startswith("play_cli/") or name.startswith("play_cli-0.8.1.dist-info/")
            }
    except (OSError, zipfile.BadZipFile) as error:
        raise BenchmarkFailure(
            "benchmark_runtime_invalid", "Wheel inventory is invalid."
        ) from error
    if not inventory or "play_cli-0.8.1.dist-info/RECORD" not in inventory:
        raise BenchmarkFailure("benchmark_runtime_invalid", "Wheel inventory is incomplete.")
    return inventory


def _runtime_identity(
    playcli: Path, wheel_inventory: dict[str, str] | None = None
) -> dict[str, Any]:
    runtime = playcli.parent.parent.resolve(strict=True)
    expected_entrypoint = (
        f"#!{runtime / 'bin/python'}\n"
        "# -*- coding: utf-8 -*-\n"
        "import re\n"
        "import sys\n"
        "from play_cli.cli import main\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        "    sys.exit(main())\n"
    ).encode()
    if playcli.read_bytes() != expected_entrypoint:
        raise BenchmarkFailure("benchmark_runtime_invalid", "Installed entry point differs.")
    records = tuple(runtime.glob("lib/python*/site-packages/play_cli-0.8.1.dist-info/RECORD"))
    if len(records) != 1:
        raise BenchmarkFailure("benchmark_runtime_invalid", "Installed RECORD is ambiguous.")
    record = records[0]
    site_packages = record.parent.parent
    allowed_pth = (
        b"import os; var = 'SETUPTOOLS_USE_DISTUTILS'; "
        b"enabled = os.environ.get(var, 'local') == 'local'; "
        b"enabled and __import__('_distutils_hack').add_shim(); \n"
    )
    if any(
        path.name != "distutils-precedence.pth" or path.read_bytes() != allowed_pth
        for path in site_packages.glob("*.pth")
    ) or any(
        tuple(site_packages.rglob(pattern)) for pattern in ("sitecustomize*", "usercustomize*")
    ):
        raise BenchmarkFailure("benchmark_runtime_invalid", "Runtime startup code is not allowed.")
    if wheel_inventory is not None:
        expected_package = {name for name in wheel_inventory if name.startswith("play_cli/")}
        actual_package = {
            path.relative_to(site_packages).as_posix()
            for path in (site_packages / "play_cli").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        expected_metadata = {
            name for name in wheel_inventory if name.startswith("play_cli-0.8.1.dist-info/")
        }
        allowed_installer_metadata = {
            "play_cli-0.8.1.dist-info/INSTALLER",
            "play_cli-0.8.1.dist-info/REQUESTED",
            "play_cli-0.8.1.dist-info/direct_url.json",
        }
        actual_metadata = {
            path.relative_to(site_packages).as_posix()
            for path in record.parent.rglob("*")
            if path.is_file()
        }
        if actual_package != expected_package or actual_metadata != (
            expected_metadata | allowed_installer_metadata
        ):
            raise BenchmarkFailure("benchmark_runtime_invalid", "Installed inventory differs.")
        for name, expected_hash in wheel_inventory.items():
            if name.endswith(".dist-info/RECORD"):
                continue
            installed_path = site_packages / name
            if not installed_path.is_file() or _file_sha(installed_path) != expected_hash:
                raise BenchmarkFailure("benchmark_runtime_invalid", "Installed wheel file differs.")
    try:
        rows = list(csv.reader(StringIO(record.read_text())))
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise BenchmarkFailure(
            "benchmark_runtime_invalid", "Installed RECORD is invalid."
        ) from error
    inventory: list[list[object]] = []
    for row in rows:
        if len(row) != 3 or not row[0]:
            raise BenchmarkFailure("benchmark_runtime_invalid", "Installed RECORD is invalid.")
        candidate = (record.parent.parent / row[0]).resolve(strict=True)
        try:
            relative = candidate.relative_to(runtime)
        except ValueError as error:
            raise BenchmarkFailure(
                "benchmark_runtime_invalid", "Installed file escaped runtime."
            ) from error
        metadata = candidate.stat()
        if not candidate.is_file() or metadata.st_nlink != 1:
            raise BenchmarkFailure("benchmark_runtime_invalid", "Installed file is unsafe.")
        actual_data = candidate.read_bytes()
        if row[1]:
            try:
                algorithm, declared = row[1].split("=", 1)
            except ValueError as error:
                raise BenchmarkFailure(
                    "benchmark_runtime_invalid", "Installed RECORD hash is invalid."
                ) from error
            actual = base64.urlsafe_b64encode(hashlib.sha256(actual_data).digest()).rstrip(b"=")
            if algorithm != "sha256" or actual.decode() != declared:
                raise BenchmarkFailure(
                    "benchmark_runtime_invalid", "Installed RECORD hash differs."
                )
        if row[2] and row[2] != str(len(actual_data)):
            raise BenchmarkFailure("benchmark_runtime_invalid", "Installed RECORD size differs.")
        inventory.append([relative.as_posix(), _file_sha(candidate), metadata.st_mode & 0o777])
    inventory.sort(key=lambda item: str(item[0]))
    complete_inventory: list[list[object]] = []
    roots = [playcli.parent, record.parent.parent]
    for root in roots:
        for candidate in sorted(root.rglob("*")):
            relative_name = candidate.relative_to(runtime).as_posix()
            metadata = candidate.lstat()
            if candidate.is_symlink():
                complete_inventory.append([relative_name, "symlink", os.readlink(candidate)])
            elif candidate.is_dir():
                complete_inventory.append([relative_name, "directory", metadata.st_mode & 0o777])
            elif candidate.is_file() and metadata.st_nlink == 1:
                complete_inventory.append(
                    [relative_name, "file", _file_sha(candidate), metadata.st_mode & 0o777]
                )
            else:
                raise BenchmarkFailure(
                    "benchmark_runtime_invalid", "Installed runtime inventory is unsafe."
                )
    return {
        "record_sha256": _file_sha(record),
        "installed_inventory_sha256": _sha256(
            json.dumps(inventory, separators=(",", ":")).encode()
        ),
        "installed_files": len(inventory),
        "complete_runtime_inventory_sha256": _sha256(
            json.dumps(complete_inventory, separators=(",", ":")).encode()
        ),
        "complete_runtime_entries": len(complete_inventory),
    }


def _confirm_runtime(
    playcli: Path,
    expected: dict[str, Any] | None,
    wheel_inventory: dict[str, str] | None,
) -> None:
    if expected is not None and _runtime_identity(playcli, wheel_inventory) != expected:
        raise BenchmarkFailure("benchmark_runtime_invalid", "Installed runtime changed.")


def _run(
    playcli: Path,
    arguments: list[str],
    cwd: Path,
    runtime_identity: dict[str, Any] | None = None,
    wheel_inventory: dict[str, str] | None = None,
) -> tuple[dict[str, Any], float]:
    _confirm_runtime(playcli, runtime_identity, wheel_inventory)
    started = time.perf_counter()
    try:
        result = subprocess.run(
            [str(playcli), *arguments],
            capture_output=True,
            check=False,
            text=True,
            cwd=cwd,
            env={"PATH": str(playcli.parent), "PYTHONNOUSERSITE": "1"},
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise BenchmarkFailure(
            "benchmark_subprocess_timeout", "PLAY CLI operation timed out."
        ) from error
    elapsed = time.perf_counter() - started
    _confirm_runtime(playcli, runtime_identity, wheel_inventory)
    response = _object(result.stdout, "PLAY CLI response")
    if result.returncode != 0 or response.get("status") != "ok":
        raise BenchmarkFailure("benchmark_render_failed", "PLAY CLI operation failed.")
    return response, elapsed


def _fixture(duration_seconds: int) -> tuple[bytes, list[dict[str, Any]]]:
    count = duration_seconds * 2
    events: list[dict[str, Any]] = []
    measures: list[str] = []
    for start in range(0, count, 4):
        notes: list[str] = []
        for order in range(start, min(start + 4, count)):
            step, octave, midi = SCALE[order % len(SCALE)]
            notes.append(
                "      <note><pitch>"
                f"<step>{step}</step><octave>{octave}</octave>"
                "</pitch><duration>1</duration><type>quarter</type></note>\n"
            )
            events.append(
                {
                    "order": order,
                    "kind": "note",
                    "onset_quarters": order,
                    "onset_quarters_exact": {"numerator": order, "denominator": 1},
                    "duration_quarters": 1,
                    "duration_quarters_exact": {"numerator": 1, "denominator": 1},
                    "pitch": {"step": step, "alter": 0, "octave": octave, "midi": midi},
                    "velocity": 64,
                }
            )
        context = ""
        if start == 0:
            context = (
                "      <attributes><divisions>1</divisions>"
                "<key><fifths>0</fifths><mode>major</mode></key>"
                "<time><beats>4</beats><beat-type>4</beat-type></time></attributes>\n"
                "      <direction><direction-type><metronome>"
                "<beat-unit>quarter</beat-unit><per-minute>120</per-minute>"
                '</metronome></direction-type><sound tempo="120"/></direction>\n'
            )
        measures.append(
            f'    <measure number="{start // 4 + 1}">\n{context}{"".join(notes)}    </measure>\n'
        )
    score = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<score-partwise version="4.0">\n'
        '  <part-list><score-part id="P1"><part-name>Piano</part-name>'
        '</score-part></part-list>\n  <part id="P1">\n'
        f"{''.join(measures)}  </part>\n</score-partwise>\n"
    ).encode()
    return score, events


def _schedule_data(events: list[dict[str, Any]], modern: bool = False) -> bytes:
    if modern:
        events = [
            {
                **event,
                "voice": "1",
                "staff": 1,
                "staves": [1],
                "pages": [1],
                "event_id": f"event-{event['order'] + 1:06d}",
                "sources": [f"measure:{event['order'] // 4 + 1}"],
            }
            for event in events
        ]
    return (
        json.dumps(
            {"schema_version": "1.2" if modern else "1.1", "events": events},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _verify_pack(
    playcli: Path,
    store: Path,
    pack_id: str,
    version: str,
    cwd: Path,
    runtime_identity: dict[str, Any] | None,
    wheel_inventory: dict[str, str] | None,
) -> dict[str, Any]:
    response, _ = _run(
        playcli,
        ["sound-pack", "verify", pack_id, "--version", version, "--store", str(store)],
        cwd,
        runtime_identity,
        wheel_inventory,
    )
    pack = response.get("sound_pack")
    if not isinstance(pack, dict):
        raise BenchmarkFailure("benchmark_contract_invalid", "Sound Pack identity is missing.")
    return pack


def _authority(
    qualification_path: Path | None, listening_path: Path | None, pack: dict[str, Any]
) -> dict[str, Any] | None:
    if qualification_path is None and listening_path is None:
        if any(pack.get(key) != value for key, value in STANDARD_PACK.items()):
            return None
        qualification_path = QUALIFICATION_RECORD
        listening_path = LISTENING_RECORD
    if qualification_path is None or listening_path is None:
        raise BenchmarkFailure(
            "benchmark_arguments_invalid", "Both Sound Pack authority records are required."
        )
    qualification = qualification_path.resolve(strict=True)
    listening = listening_path.resolve(strict=True)
    if qualification != QUALIFICATION_RECORD or listening != LISTENING_RECORD:
        raise BenchmarkFailure(
            "benchmark_sound_pack_invalid", "Sound Pack authority is not repository-approved."
        )
    qualification_data = qualification.read_bytes()
    listening_data = listening.read_bytes()
    if (
        _sha256(qualification_data) != QUALIFICATION_SHA256
        or _sha256(listening_data) != LISTENING_SHA256
        or any(pack.get(key) != value for key, value in STANDARD_PACK.items())
    ):
        raise BenchmarkFailure("benchmark_sound_pack_invalid", "Sound Pack authority differs.")
    qualified = _object(qualification_data.decode(), "qualification record")
    qualified_pack = qualified.get("sound_pack")
    if qualified.get("status") != "automated_checks_passed" or not isinstance(qualified_pack, dict):
        raise BenchmarkFailure("benchmark_sound_pack_invalid", "Qualification is invalid.")
    if any(qualified_pack.get(key) != pack.get(key) for key in ("id", "version", "pack_hash")):
        raise BenchmarkFailure("benchmark_sound_pack_invalid", "Sound Pack identity differs.")
    accepted = _object(listening_data.decode(), "listening record")
    bound = accepted.get("qualification_record")
    if (
        accepted.get("status") != "accepted"
        or not isinstance(bound, dict)
        or bound.get("sha256") != _sha256(qualification_data)
    ):
        raise BenchmarkFailure("benchmark_sound_pack_invalid", "Listening record is invalid.")
    return {
        "qualification_record_sha256": _sha256(qualification_data),
        "listening_record_sha256": _sha256(listening_data),
        "verified": True,
    }


def _verify_wav(data_source: bytes, duration: int, expected: dict[str, Any]) -> dict[str, Any]:
    try:
        with wave.open(BytesIO(data_source), "rb") as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            rate = source.getframerate()
            frames = source.getnframes()
            compression = source.getcomptype()
            data = source.readframes(frames)
    except (EOFError, OSError, wave.Error) as error:
        raise BenchmarkFailure("benchmark_bundle_invalid", "WAV is unreadable.") from error
    complete = len(data) == frames * channels * width
    valid = (
        channels == 1
        and width == 2
        and compression == "NONE"
        and frames == duration * rate
        and complete
        and any(data)
        and data[-2:] == b"\0\0"
        and expected.get("format") == "PCM_S16LE"
        and expected.get("sample_rate_hz") == rate
        and expected.get("frames") == frames
        and expected.get("duration_seconds") == duration
    )
    if not valid:
        raise BenchmarkFailure("benchmark_bundle_invalid", "WAV evidence is incomplete.")
    return {
        "format": "PCM_S16LE",
        "sample_rate_hz": rate,
        "channels": channels,
        "sample_width_bytes": width,
        "frames": frames,
        "frame_bytes": len(data),
        "duration_seconds": duration,
        "complete_frame_bytes": True,
        "non_silent": True,
        "tail_complete": True,
    }


def _verify_bundle(
    bundle: Path,
    response: dict[str, Any],
    score: bytes,
    events: list[dict[str, Any]],
    duration: int,
    pack: dict[str, Any],
    retained_snapshots: list[PublishedResultBundle],
) -> dict[str, Any]:
    snapshot = open_published_result_bundle(bundle)
    try:
        artifacts = snapshot.artifacts
        manifest = snapshot.manifest
        inventory = sorted(artifacts)
        hashes = {name: _sha256(data) for name, data in artifacts.items()}
        validation = _object(artifacts["validation.json"].decode(), "validation artifact")
        persisted = dict(response)
        persisted.pop("playback", None)
        sound_pack = response.get("sound_pack")
        modern = response.get("schema_version") == "1.6"
        expected_inventory = set(REQUIRED_ARTIFACTS) | ({"voice-0001.wav"} if modern else set())
        valid = (
            inventory == sorted(expected_inventory)
            and artifacts["score.musicxml"] == score
            and artifacts["schedule.json"] == _schedule_data(events, modern)
            and validation.get("valid") is True
            and validation.get("warnings") == []
            and manifest == persisted
            and response.get("warnings") == []
            and response.get("applied_score_defaults") == []
            and response.get("renderer")
            == {"id": "pcm-sampler", "version": "3.0" if modern else "2.0"}
            and isinstance(sound_pack, dict)
            and all(sound_pack.get(key) == pack.get(key) for key in ("id", "version", "pack_hash"))
        )
        if not valid:
            raise BenchmarkFailure("benchmark_bundle_invalid", "Result Bundle contract changed.")
        wav_response = response.get("wav")
        if not isinstance(wav_response, dict):
            raise BenchmarkFailure("benchmark_bundle_invalid", "WAV response is missing.")
        evidence = {
            "path": str(bundle),
            "exact_inventory": inventory,
            "artifact_sha256": hashes,
            "hashes_verified": True,
            "schedule_verified": True,
            "provenance_verified": True,
            "wav": _verify_wav(artifacts["performance.wav"], duration, wav_response),
        }
        snapshot.confirm()
        retained_snapshots.append(snapshot)
        return evidence
    except BaseException:
        snapshot.close()
        raise


def _file_sha(path: Path) -> str:
    return _sha256(path.resolve(strict=True).read_bytes())


def _sysctl(name: str) -> str | None:
    if sys.platform != "darwin":
        return None
    result = subprocess.run(
        ["/usr/sbin/sysctl", "-n", name], capture_output=True, check=False, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _environment(
    playcli: Path,
    response: dict[str, Any],
    distribution: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "hardware": {
            "model": _sysctl("hw.model") or platform.machine(),
            "processor": _sysctl("machdep.cpu.brand_string") or platform.processor(),
            "logical_cpu_count": os.cpu_count(),
            "memory_bytes": int(_sysctl("hw.memsize") or 0) or None,
        },
        "os": {
            "name": platform.system(),
            "release": platform.release(),
            "version": platform.mac_ver()[0] if sys.platform == "darwin" else platform.version(),
            "machine": platform.machine(),
        },
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable_sha256": _file_sha(Path(sys.executable)),
        },
        "playcli": {
            "path": str(playcli),
            "sha256": _file_sha(playcli),
            "cli": response.get("cli"),
        },
        "renderer": response.get("renderer"),
        "benchmark_tool_sha256": _file_sha(Path(__file__)),
        "distribution": distribution,
    }


def _prepare_runtime(
    arguments: argparse.Namespace, output: Path
) -> tuple[
    Path,
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, str] | None,
]:
    if arguments.distribution is None:
        assert arguments.playcli is not None
        return arguments.playcli.resolve(strict=True), None, None, None
    wheel = arguments.distribution.resolve(strict=True)
    wheel_sha256 = _file_sha(wheel)
    wheel_inventory = _wheel_inventory(wheel)
    committed_record_data = PLATFORM_RECORD.read_bytes()
    committed_record = _object(committed_record_data.decode(), "committed platform acceptance")
    committed_wheel = committed_record.get("wheel")
    if (
        _sha256(committed_record_data) != PLATFORM_RECORD_SHA256
        or committed_record.get("status") != "ok"
        or not isinstance(committed_wheel, dict)
        or committed_wheel.get("filename") != wheel.name
        or committed_wheel.get("sha256") != wheel_sha256
        or wheel_sha256 != ACCEPTED_WHEEL_SHA256
    ):
        raise BenchmarkFailure(
            "benchmark_runtime_invalid", "Committed platform acceptance differs."
        )
    acceptance_output = output / "platform-acceptance"
    acceptance_record = output / "platform-acceptance.json"
    command = [
        sys.executable,
        str(ROOT / "tools/verify_platform_distribution.py"),
        str(wheel),
        "--output",
        str(acceptance_output),
        "--record",
        str(acceptance_record),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            cwd=output,
            env={"PATH": os.defpath, "PYTHONNOUSERSITE": "1"},
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise BenchmarkFailure(
            "benchmark_runtime_invalid", "Distribution verification timed out."
        ) from error
    report = _object(completed.stdout, "platform acceptance")
    wheel_contract = report.get("wheel")
    if (
        completed.returncode != 0
        or report.get("status") != "ok"
        or not isinstance(wheel_contract, dict)
        or wheel_contract.get("filename") != wheel.name
        or wheel_contract.get("sha256") != wheel_sha256
    ):
        raise BenchmarkFailure("benchmark_runtime_invalid", "Distribution verification failed.")
    playcli = acceptance_output / "runtime/bin/playcli"
    resolved_playcli = playcli.resolve(strict=True)
    installed_records = tuple(
        acceptance_output.glob("runtime/lib/python*/site-packages/play_cli-0.8.1.dist-info/RECORD")
    )
    if len(installed_records) != 1:
        raise BenchmarkFailure("benchmark_runtime_invalid", "Installed RECORD is ambiguous.")
    identity = _runtime_identity(resolved_playcli, wheel_inventory)
    return (
        resolved_playcli,
        {
            "wheel": wheel_contract,
            "committed_platform_acceptance_record_sha256": PLATFORM_RECORD_SHA256,
            "attempt_platform_acceptance_record_path": str(acceptance_record.resolve()),
            "attempt_platform_acceptance_record_sha256": _file_sha(acceptance_record),
            "installed_runtime": identity,
            "verified": True,
        },
        identity,
        wheel_inventory,
    )


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _hash_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _supported_python_version(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split(".")
    return (
        len(parts) >= 2
        and all(part.isdigit() for part in parts)
        and tuple(int(part) for part in parts[:2]) >= (3, 11)
    )


def _exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise BenchmarkFailure("benchmark_contract_invalid", f"{label} is invalid.")
    return value


def validate_record(record: dict[str, Any]) -> None:
    expected_keys = {
        "schema_version",
        "operation",
        "status",
        "measurement_scope",
        "fixture",
        "reference_environment",
        "sound_pack",
        "sound_pack_authority",
        "thresholds",
        "runs",
        "summary",
    }
    if record.get("status") == "error":
        expected_keys.add("error")
    if (
        set(record) != expected_keys
        or record.get("schema_version") != SCHEMA_VERSION
        or record.get("operation") != OPERATION
        or record.get("status") not in {"ok", "error"}
    ):
        raise BenchmarkFailure("benchmark_contract_invalid", "Benchmark envelope is invalid.")
    if record.get("measurement_scope") != {
        "includes": "playcli render subprocess through complete Result Bundle publication",
        "source_score_recognition_included": False,
        "sound_pack_install_included": False,
    }:
        raise BenchmarkFailure("benchmark_contract_invalid", "Measurement scope is invalid.")
    fixture = _exact(
        record.get("fixture"),
        {
            "kind",
            "tempo_bpm",
            "duration_seconds",
            "events",
            "musicxml_sha256",
            "expected_schedule_sha256",
        },
        "Benchmark fixture",
    )
    duration = fixture.get("duration_seconds")
    event_count = fixture.get("events")
    if (
        fixture.get("kind") != "generated_monophonic_scale"
        or isinstance(duration, bool)
        or not isinstance(duration, int)
        or duration < 1
        or isinstance(event_count, bool)
        or not isinstance(event_count, int)
        or event_count < 1
        or event_count != duration * 2
        or fixture.get("tempo_bpm") != 120
    ):
        raise BenchmarkFailure("benchmark_contract_invalid", "Benchmark fixture is invalid.")
    for name in ("musicxml_sha256", "expected_schedule_sha256"):
        value = fixture.get(name)
        if not _hash_value(value):
            raise BenchmarkFailure("benchmark_contract_invalid", "Fixture hash is invalid.")
    thresholds = _exact(
        record.get("thresholds"),
        {"first_run_seconds", "repeat_median_seconds", "repeat_max_seconds"},
        "Benchmark thresholds",
    )
    if not all(_number(value) and float(value) > 0 for value in thresholds.values()):
        raise BenchmarkFailure("benchmark_contract_invalid", "Benchmark thresholds are invalid.")
    runs = record.get("runs")
    if not isinstance(runs, list) or len(runs) < 2:
        raise BenchmarkFailure("benchmark_contract_invalid", "Benchmark runs are invalid.")
    raw: list[float] = []
    reference = record.get("reference_environment")
    reference_cli = reference.get("playcli") if isinstance(reference, dict) else None
    modern = isinstance(reference_cli, dict) and reference_cli.get("cli") == {
        "name": "play-cli",
        "version": "0.10.0",
    }
    expected_inventory = set(REQUIRED_ARTIFACTS) | ({"voice-0001.wav"} if modern else set())
    stable_payloads: dict[str, str] | None = None
    for index, value in enumerate(runs):
        run = _exact(
            value,
            {
                "sequence",
                "cache_state",
                "status",
                "elapsed_seconds",
                "elapsed_seconds_raw",
                "cli_duration_ms",
                "result_bundle",
            },
            "Benchmark run",
        )
        if (
            type(run.get("sequence")) is not int
            or run.get("sequence") != index + 1
            or run.get("cache_state") != ("first_after_verification" if index == 0 else "repeat")
            or run.get("status") != "ok"
            or not _number(run.get("elapsed_seconds_raw"))
            or float(run["elapsed_seconds_raw"]) <= 0
            or not _number(run.get("elapsed_seconds"))
            or float(run["elapsed_seconds"]) <= 0
            or not _number(run.get("cli_duration_ms"))
            or float(run["cli_duration_ms"]) <= 0
            or run.get("elapsed_seconds") != round(float(run["elapsed_seconds_raw"]), 6)
        ):
            raise BenchmarkFailure("benchmark_contract_invalid", "Benchmark run is invalid.")
        evidence = _exact(
            run.get("result_bundle"),
            {
                "path",
                "exact_inventory",
                "artifact_sha256",
                "hashes_verified",
                "schedule_verified",
                "provenance_verified",
                "wav",
            },
            "Result Bundle evidence",
        )
        hashes = evidence.get("artifact_sha256")
        if (
            not isinstance(evidence.get("path"), str)
            or not evidence.get("path")
            or evidence.get("exact_inventory") != sorted(expected_inventory)
            or not isinstance(hashes, dict)
            or set(hashes) != expected_inventory
            or not all(_hash_value(value) for value in hashes.values())
            or evidence.get("hashes_verified") is not True
            or evidence.get("schedule_verified") is not True
            or evidence.get("provenance_verified") is not True
        ):
            raise BenchmarkFailure("benchmark_contract_invalid", "Bundle evidence is invalid.")
        current_payloads = {name: hashes[name] for name in expected_inventory - {"result.json"}}
        if (
            current_payloads["score.musicxml"] != fixture.get("musicxml_sha256")
            or current_payloads["schedule.json"] != fixture.get("expected_schedule_sha256")
            or (stable_payloads is not None and current_payloads != stable_payloads)
        ):
            raise BenchmarkFailure("benchmark_contract_invalid", "Bundle payloads differ.")
        stable_payloads = current_payloads
        wav = _exact(
            evidence.get("wav"),
            {
                "format",
                "sample_rate_hz",
                "channels",
                "sample_width_bytes",
                "frames",
                "frame_bytes",
                "duration_seconds",
                "complete_frame_bytes",
                "non_silent",
                "tail_complete",
            },
            "WAV evidence",
        )
        sample_rate = wav.get("sample_rate_hz")
        frames = wav.get("frames")
        frame_bytes = wav.get("frame_bytes")
        if (
            wav.get("format") != "PCM_S16LE"
            or type(wav.get("channels")) is not int
            or wav.get("channels") != 1
            or type(wav.get("sample_width_bytes")) is not int
            or wav.get("sample_width_bytes") != 2
            or wav.get("duration_seconds") != fixture.get("duration_seconds")
            or type(sample_rate) is not int
            or sample_rate < 1
            or type(frames) is not int
            or frames < 1
            or type(frame_bytes) is not int
            or frame_bytes < 1
            or frames != sample_rate * duration
            or frame_bytes != frames * 2
            or any(
                wav.get(key) is not True
                for key in ("complete_frame_bytes", "non_silent", "tail_complete")
            )
        ):
            raise BenchmarkFailure("benchmark_contract_invalid", "WAV evidence is invalid.")
        raw.append(float(run["elapsed_seconds_raw"]))
    summary = _exact(
        record.get("summary"),
        {
            "first_run_seconds",
            "repeat_median_seconds",
            "repeat_max_seconds",
            "formal_ten_minute_acceptance",
            "target_met",
        },
        "Benchmark summary",
    )
    repeat_median = float(median(raw[1:]))
    repeat_max = max(raw[1:])
    target_met = (
        raw[0] <= float(thresholds["first_run_seconds"])
        and repeat_median <= float(thresholds["repeat_median_seconds"])
        and repeat_max <= float(thresholds["repeat_max_seconds"])
    )
    environment = _exact(
        record.get("reference_environment"),
        {
            "hardware",
            "os",
            "python",
            "playcli",
            "renderer",
            "benchmark_tool_sha256",
            "distribution",
        },
        "Reference environment",
    )
    distribution = environment.get("distribution")
    authority = record.get("sound_pack_authority")
    hardware = _exact(
        environment.get("hardware"),
        {"model", "processor", "logical_cpu_count", "memory_bytes"},
        "Hardware evidence",
    )
    operating_system = _exact(
        environment.get("os"), {"name", "release", "version", "machine"}, "OS evidence"
    )
    python = _exact(
        environment.get("python"),
        {"version", "implementation", "executable_sha256"},
        "Python evidence",
    )
    memory = hardware.get("memory_bytes")
    if (
        not isinstance(hardware.get("model"), str)
        or not hardware.get("model")
        or not isinstance(hardware.get("processor"), str)
        or not hardware.get("processor")
        or type(hardware.get("logical_cpu_count")) is not int
        or hardware.get("logical_cpu_count", 0) < 1
        or (memory is not None and (type(memory) is not int or memory < 1))
        or not all(
            isinstance(operating_system.get(key), str) and operating_system.get(key)
            for key in operating_system
        )
        or not _supported_python_version(python.get("version"))
        or not isinstance(python.get("implementation"), str)
        or not python.get("implementation")
        or not _hash_value(python.get("executable_sha256"))
        or not _hash_value(environment.get("benchmark_tool_sha256"))
        or environment.get("benchmark_tool_sha256")
        not in {
            _file_sha(Path(__file__)),
            "16813f6fecd703686c30f39112e1efbc898ada0fee4498c7f97e8e57dcd54b65",
        }
    ):
        raise BenchmarkFailure("benchmark_contract_invalid", "Environment evidence is invalid.")
    pack = _exact(
        record.get("sound_pack"),
        {"id", "version", "pack_hash", "path", "status"},
        "Sound Pack",
    )
    if (
        not all(isinstance(pack.get(key), str) and pack.get(key) for key in pack)
        or pack.get("status") != "verified"
        or not _hash_value(pack.get("pack_hash"))
    ):
        raise BenchmarkFailure("benchmark_contract_invalid", "Sound Pack is invalid.")
    if authority is not None:
        authority_contract = _exact(
            authority,
            {"qualification_record_sha256", "listening_record_sha256", "verified"},
            "Sound Pack authority",
        )
        if authority_contract != {
            "qualification_record_sha256": QUALIFICATION_SHA256,
            "listening_record_sha256": LISTENING_SHA256,
            "verified": True,
        } or any(pack.get(key) != value for key, value in STANDARD_PACK.items()):
            raise BenchmarkFailure("benchmark_contract_invalid", "Sound Pack authority is invalid.")
    playcli = _exact(environment.get("playcli"), {"path", "sha256", "cli"}, "PLAY CLI")
    cli = _exact(playcli.get("cli"), {"name", "version"}, "CLI identity")
    if (
        cli
        not in [
            {"name": "play-cli", "version": version} for version in ("0.8.1", "0.9.0", "0.10.0")
        ]
        or not isinstance(playcli.get("path"), str)
        or not playcli.get("path")
        or not Path(playcli["path"]).is_absolute()
        or not _hash_value(playcli.get("sha256"))
        or environment.get("renderer")
        != {"id": "pcm-sampler", "version": "3.0" if modern else "2.0"}
    ):
        raise BenchmarkFailure("benchmark_contract_invalid", "PLAY CLI identity is invalid.")
    if distribution is not None:
        if cli["version"] != "0.8.1":
            raise BenchmarkFailure("benchmark_contract_invalid", "Formal wheel authority is 0.8.1.")
        distribution_contract = _exact(
            distribution,
            {
                "wheel",
                "committed_platform_acceptance_record_sha256",
                "attempt_platform_acceptance_record_path",
                "attempt_platform_acceptance_record_sha256",
                "installed_runtime",
                "verified",
            },
            "Distribution evidence",
        )
        wheel = _exact(distribution_contract.get("wheel"), {"filename", "sha256"}, "Wheel")
        installed_runtime = _exact(
            distribution_contract.get("installed_runtime"),
            {
                "record_sha256",
                "installed_inventory_sha256",
                "installed_files",
                "complete_runtime_inventory_sha256",
                "complete_runtime_entries",
            },
            "Installed runtime",
        )
        if (
            wheel.get("filename") != "play_cli-0.8.1-py3-none-any.whl"
            or wheel.get("sha256") != ACCEPTED_WHEEL_SHA256
            or distribution_contract.get("verified") is not True
            or distribution_contract.get("committed_platform_acceptance_record_sha256")
            != PLATFORM_RECORD_SHA256
            or not isinstance(
                distribution_contract.get("attempt_platform_acceptance_record_path"), str
            )
            or not distribution_contract.get("attempt_platform_acceptance_record_path")
            or not Path(
                distribution_contract["attempt_platform_acceptance_record_path"]
            ).is_absolute()
            or not _hash_value(
                distribution_contract.get("attempt_platform_acceptance_record_sha256")
            )
            or type(installed_runtime.get("installed_files")) is not int
            or installed_runtime.get("installed_files", 0) < 1
            or type(installed_runtime.get("complete_runtime_entries")) is not int
            or installed_runtime.get("complete_runtime_entries", 0) < 1
            or any(
                not _hash_value(installed_runtime.get(key))
                for key in (
                    "record_sha256",
                    "installed_inventory_sha256",
                    "complete_runtime_inventory_sha256",
                )
            )
        ):
            raise BenchmarkFailure(
                "benchmark_contract_invalid", "Distribution evidence is invalid."
            )
    formal_shape = (
        fixture.get("duration_seconds") == FORMAL_DURATION_SECONDS
        and len(runs) == FORMAL_REPEAT_RUNS + 1
        and operating_system.get("name") == "Darwin"
        and all(
            run["result_bundle"]["wav"]["sample_rate_hz"] == 48_000
            and run["result_bundle"]["wav"]["frames"] == 28_800_000
            and run["result_bundle"]["wav"]["frame_bytes"] == 57_600_000
            for run in runs
        )
        and thresholds
        == {
            "first_run_seconds": FIRST_THRESHOLD_SECONDS,
            "repeat_median_seconds": MEDIAN_THRESHOLD_SECONDS,
            "repeat_max_seconds": MAX_THRESHOLD_SECONDS,
        }
        and isinstance(authority, dict)
        and authority.get("verified") is True
        and isinstance(distribution, dict)
        and distribution.get("verified") is True
    )
    if (
        not _number(summary.get("first_run_seconds"))
        or float(summary["first_run_seconds"]) <= 0
        or not _number(summary.get("repeat_median_seconds"))
        or float(summary["repeat_median_seconds"]) <= 0
        or not _number(summary.get("repeat_max_seconds"))
        or float(summary["repeat_max_seconds"]) <= 0
        or summary.get("first_run_seconds") != round(raw[0], 6)
        or summary.get("repeat_median_seconds") != round(repeat_median, 6)
        or summary.get("repeat_max_seconds") != round(repeat_max, 6)
        or summary.get("target_met") is not target_met
        or summary.get("formal_ten_minute_acceptance") is not (formal_shape and target_met)
        or record.get("status") != ("ok" if target_met else "error")
    ):
        raise BenchmarkFailure("benchmark_contract_invalid", "Benchmark summary is invalid.")
    if not target_met:
        error = _exact(record.get("error"), {"id", "message"}, "Benchmark error")
        if error.get("id") != "render_performance_target_missed":
            raise BenchmarkFailure("benchmark_contract_invalid", "Benchmark error is invalid.")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    runtime = parser.add_mutually_exclusive_group(required=True)
    runtime.add_argument("--playcli", type=Path)
    runtime.add_argument("--distribution", type=Path)
    parser.add_argument("--sound-pack-store", type=Path, required=True)
    parser.add_argument("--sound-pack-id", required=True)
    parser.add_argument("--sound-pack-version", required=True)
    parser.add_argument("--qualification-record", type=Path)
    parser.add_argument("--listening-acceptance-record", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=_positive, default=FORMAL_DURATION_SECONDS)
    parser.add_argument("--repeat-runs", type=_positive, default=FORMAL_REPEAT_RUNS)
    parser.add_argument("--median-threshold-seconds", type=float, default=MEDIAN_THRESHOLD_SECONDS)
    parser.add_argument("--max-threshold-seconds", type=float, default=MAX_THRESHOLD_SECONDS)
    return parser.parse_args()


def _benchmark(
    arguments: argparse.Namespace,
    retained_snapshots: list[PublishedResultBundle],
) -> dict[str, Any]:
    if not (
        arguments.duration_seconds <= FORMAL_DURATION_SECONDS
        and arguments.repeat_runs <= FORMAL_REPEAT_RUNS
        and 0 < arguments.median_threshold_seconds <= MEDIAN_THRESHOLD_SECONDS
        and 0 < arguments.max_threshold_seconds <= MAX_THRESHOLD_SECONDS
    ):
        raise BenchmarkFailure("benchmark_arguments_invalid", "Benchmark limits are invalid.")
    store = arguments.sound_pack_store.resolve(strict=True)
    output = Path(os.path.realpath(arguments.output))
    if output.exists():
        raise BenchmarkFailure("benchmark_output_exists", "Benchmark output exists.")
    output.mkdir(parents=True)
    playcli, distribution, runtime_identity, wheel_inventory = _prepare_runtime(arguments, output)
    score_data, events = _fixture(arguments.duration_seconds)
    score = output / "benchmark.musicxml"
    score.write_bytes(score_data)
    pack = _verify_pack(
        playcli,
        store,
        arguments.sound_pack_id,
        arguments.sound_pack_version,
        output,
        runtime_identity,
        wheel_inventory,
    )
    authority = _authority(
        arguments.qualification_record, arguments.listening_acceptance_record, pack
    )
    runs: list[dict[str, Any]] = []
    first_response: dict[str, Any] | None = None
    for index in range(arguments.repeat_runs + 1):
        bundle = output / f"run-{index:02d}"
        response, elapsed = _run(
            playcli,
            [
                "render",
                str(score),
                "--sound-pack",
                arguments.sound_pack_id,
                "--sound-pack-version",
                arguments.sound_pack_version,
                "--sound-pack-store",
                str(store),
                "--output",
                str(bundle),
            ],
            output,
            runtime_identity,
            wheel_inventory,
        )
        first_response = first_response or response
        timing = response.get("operation_timing")
        if not isinstance(timing, dict) or type(timing.get("duration_ms")) not in {int, float}:
            raise BenchmarkFailure("benchmark_contract_invalid", "Render timing is missing.")
        runs.append(
            {
                "sequence": index + 1,
                "cache_state": "first_after_verification" if index == 0 else "repeat",
                "status": "ok",
                "elapsed_seconds": round(elapsed, 6),
                "elapsed_seconds_raw": elapsed,
                "cli_duration_ms": timing["duration_ms"],
                "result_bundle": _verify_bundle(
                    bundle,
                    response,
                    score_data,
                    events,
                    arguments.duration_seconds,
                    pack,
                    retained_snapshots,
                ),
            }
        )
    assert first_response is not None
    first = runs[0]["elapsed_seconds_raw"]
    repeats = [run["elapsed_seconds_raw"] for run in runs[1:]]
    repeat_median = float(median(repeats))
    repeat_max = max(repeats)
    target_met = (
        first <= FIRST_THRESHOLD_SECONDS
        and repeat_median <= arguments.median_threshold_seconds
        and repeat_max <= arguments.max_threshold_seconds
    )
    formal = (
        arguments.duration_seconds == FORMAL_DURATION_SECONDS
        and arguments.repeat_runs == FORMAL_REPEAT_RUNS
        and arguments.median_threshold_seconds == MEDIAN_THRESHOLD_SECONDS
        and arguments.max_threshold_seconds == MAX_THRESHOLD_SECONDS
        and authority is not None
        and distribution is not None
        and sys.platform == "darwin"
    )
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "operation": OPERATION,
        "status": "ok" if target_met else "error",
        "measurement_scope": {
            "includes": "playcli render subprocess through complete Result Bundle publication",
            "source_score_recognition_included": False,
            "sound_pack_install_included": False,
        },
        "fixture": {
            "kind": "generated_monophonic_scale",
            "tempo_bpm": 120,
            "duration_seconds": arguments.duration_seconds,
            "events": len(events),
            "musicxml_sha256": _sha256(score_data),
            "expected_schedule_sha256": _sha256(
                _schedule_data(events, first_response.get("schema_version") == "1.6")
            ),
        },
        "reference_environment": _environment(playcli, first_response, distribution),
        "sound_pack": pack,
        "sound_pack_authority": authority,
        "thresholds": {
            "first_run_seconds": FIRST_THRESHOLD_SECONDS,
            "repeat_median_seconds": arguments.median_threshold_seconds,
            "repeat_max_seconds": arguments.max_threshold_seconds,
        },
        "runs": runs,
        "summary": {
            "first_run_seconds": round(first, 6),
            "repeat_median_seconds": round(repeat_median, 6),
            "repeat_max_seconds": round(repeat_max, 6),
            "formal_ten_minute_acceptance": formal and target_met,
            "target_met": target_met,
        },
    }
    if not target_met:
        record["error"] = {
            "id": "render_performance_target_missed",
            "message": "Render performance target was not met.",
        }
    validate_record(record)
    return record


def _write(
    path: Path,
    record: dict[str, Any],
    commit_guard: Callable[[], None] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_fd = storage.open_directory(path.parent)
    parent_identity = (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino)
    pending = f".{path.name}.pending-{os.getpid()}"
    created: tuple[int, int] | None = None
    descriptor: int | None = None
    try:
        data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        descriptor = os.open(
            pending,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        written = 0
        while written < len(data):
            count = os.write(descriptor, data[written:])
            if count == 0:
                raise OSError("Benchmark record write made no progress.")
            written += count
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o444)
        created_stat = os.fstat(descriptor)
        created = (created_stat.st_dev, created_stat.st_ino)

        def guard() -> None:
            assert descriptor is not None and created is not None
            entry = storage.stat_at(parent_fd, pending)
            current = os.fstat(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            retained_data = storage.read_open_file(descriptor)
            if (
                entry is None
                or not stat.S_ISREG(entry.st_mode)
                or (entry.st_dev, entry.st_ino) != created
                or entry.st_nlink != 1
                or entry.st_mode & 0o777 != 0o444
                or not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != created
                or current.st_nlink != 1
                or current.st_mode & 0o777 != 0o444
                or retained_data != data
            ):
                raise OSError("Benchmark record pending binding changed.")
            if commit_guard is not None:
                commit_guard()

        storage.rename_noreplace(
            parent_fd,
            pending,
            parent_fd,
            path.name,
            commit_guard=guard,
        )
        published = storage.stat_at(parent_fd, path.name)
        rebound_parent = storage.open_directory(path.parent)
        try:
            rebound_identity = (
                os.fstat(rebound_parent).st_dev,
                os.fstat(rebound_parent).st_ino,
            )
        finally:
            os.close(rebound_parent)
        if (
            published is None
            or not stat.S_ISREG(published.st_mode)
            or (published.st_dev, published.st_ino) != created
            or published.st_nlink != 1
            or published.st_mode & 0o777 != 0o444
            or storage.read_file_at(parent_fd, path.name) != data
            or rebound_identity != parent_identity
        ):
            raise OSError("Benchmark record publication changed.")
        if commit_guard is not None:
            commit_guard()
    finally:
        current = storage.stat_at(parent_fd, pending)
        if (
            created is not None
            and current is not None
            and (current.st_dev, current.st_ino) == created
        ):
            os.unlink(pending, dir_fd=parent_fd)
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def main() -> int:
    arguments = _arguments()
    if arguments.record.exists():
        record = {
            "schema_version": SCHEMA_VERSION,
            "operation": OPERATION,
            "status": "error",
            "error": {"id": "benchmark_record_failed", "message": "Benchmark record exists."},
        }
        print("Benchmark record exists.", file=sys.stderr)
        print(json.dumps(record, sort_keys=True, separators=(",", ":")))
        return 1
    retained_snapshots: list[PublishedResultBundle] = []
    benchmark_complete = False
    try:
        record = _benchmark(arguments, retained_snapshots)
        benchmark_complete = True
    except BenchmarkFailure as error:
        record = {
            "schema_version": SCHEMA_VERSION,
            "operation": OPERATION,
            "status": "error",
            "error": {"id": error.error_id, "message": error.message},
        }
        print(error.message, file=sys.stderr)
    except (OSError, ValueError, UnicodeDecodeError, ResultBundleFailure) as error:
        record = {
            "schema_version": SCHEMA_VERSION,
            "operation": OPERATION,
            "status": "error",
            "error": {"id": "benchmark_failed", "message": "Benchmark failed."},
        }
        print(f"Benchmark failed: {error}", file=sys.stderr)
    if not benchmark_complete:
        for snapshot in retained_snapshots:
            snapshot.close()
        retained_snapshots.clear()

    def confirm_snapshots() -> None:
        for snapshot in retained_snapshots:
            snapshot.confirm()

    try:
        _write(
            arguments.record,
            record,
            commit_guard=confirm_snapshots,
        )
    except (OSError, ResultBundleFailure) as error:
        record = {
            "schema_version": SCHEMA_VERSION,
            "operation": OPERATION,
            "status": "error",
            "error": {"id": "benchmark_record_failed", "message": "Benchmark record failed."},
        }
        print(f"Benchmark record failed: {error}", file=sys.stderr)
    finally:
        for snapshot in retained_snapshots:
            snapshot.close()
    print(json.dumps(record, sort_keys=True, separators=(",", ":")))
    return 0 if record["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
