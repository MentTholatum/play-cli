#!/usr/bin/env python3
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
import struct
import subprocess
import sys
import tempfile
import wave
import zipfile
from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse

import play_cli.sound_pack_storage as storage

MIDI_MIN = 21
MIDI_MAX = 108
QUALIFICATION_SCHEMA_VERSION = "1.0"
PLAYCLI_SCHEMA_VERSION = "1.5"
APPROVED_PLAYCLI_GIT_COMMIT = "2860da46c1e58dfbd27f5276fc042711c4433161"
APPROVED_PLAYCLI_SOURCE_MANIFEST_SHA256 = (
    "2315a3d602b7d8a24a6a63661a1c68b684300be5107ac39cdacf4370ff56f715"
)
WHEEL_AUTHORITY = (
    Path(__file__).parents[1] / "standards/play-cli-0.7.0-qualification-wheel.lock.json"
)
APPROVED_WHEEL_AUTHORITY_SHA256 = "6c820ee3ba3fef711782c6bac2f6ccc5df80a40b70cd41c4c2e52abc6b604ef1"
PITCH_TEST_NOTES = (21, 33, 45, 57, 60, 69, 81, 93)
REPRESENTATIVE_SINGLE_NOTES = PITCH_TEST_NOTES + (108,)
SCALE_NOTES = (60, 62, 64, 65, 67, 69, 71, 72)
ARPEGGIO_NOTES = (60, 64, 67, 72)
STEPS = (
    ("C", 0),
    ("C", 1),
    ("D", 0),
    ("D", 1),
    ("E", 0),
    ("F", 0),
    ("F", 1),
    ("G", 0),
    ("G", 1),
    ("A", 0),
    ("A", 1),
    ("B", 0),
)


class QualificationFailure(Exception):
    pass


@dataclass(frozen=True)
class ScoreItem:
    midi: int
    duration_divisions: int
    rest_divisions: int


@dataclass(frozen=True)
class BundleEvidence:
    response: dict[str, object]
    events: tuple[dict[str, object], ...]
    sample_rate: int
    samples: tuple[int, ...]
    artifact_sha256: dict[str, str]
    manifest_sha256: str
    performance_bytes: int


@dataclass(frozen=True)
class DistributionEvidence:
    path_name: str
    sha256: str
    source_manifest_sha256: str
    runtime_python_sha256: str
    runtime: RuntimeBinding


@dataclass(frozen=True)
class FileBinding:
    path: Path
    identity: tuple[int, int, int, int]
    sha256: str


@dataclass(frozen=True)
class RuntimeBinding:
    playcli: FileBinding
    runtime_python: FileBinding
    runtime_python_command: Path
    runtime_python_command_identity: tuple[int, int, int, int]
    installed_files: tuple[FileBinding, ...]
    site_packages: Path
    installed_inventory: tuple[tuple[str, str], ...]


COVERAGE_ITEMS = tuple(ScoreItem(midi, 4, 2) for midi in range(MIDI_MIN, MIDI_MAX + 1))
AUDITION_ITEMS = (
    tuple(ScoreItem(midi, 16, 8) for midi in REPRESENTATIVE_SINGLE_NOTES)
    + tuple(ScoreItem(midi, 2, 1) for midi in SCALE_NOTES)
    + tuple(ScoreItem(midi, 2, 1) for midi in ARPEGGIO_NOTES)
)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise QualificationFailure(f"{label} is not a JSON object.")
    return value


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise QualificationFailure(f"{label} is not a JSON array.")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise QualificationFailure(f"{label} is not a non-empty string.")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise QualificationFailure(f"{label} is not an integer.")
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_object(data: bytes | str, label: str) -> dict[str, object]:
    try:
        parsed: object = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise QualificationFailure(f"{label} is not valid JSON.") from error
    return _object(parsed, label)


def _record_rows(data: str, label: str) -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    try:
        for row in csv.reader(data.splitlines()):
            if len(row) != 3 or row[0] in rows:
                raise QualificationFailure(f"{label} has an invalid row.")
            rows[row[0]] = (row[1], row[2])
    except csv.Error as error:
        raise QualificationFailure(f"{label} is invalid CSV.") from error
    return rows


def _record_hash(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"sha256={encoded}"


def _bind_file(path: Path, label: str) -> tuple[FileBinding, bytes]:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        before = os.fstat(descriptor)
        data = storage.read_open_file(descriptor)
        after = os.fstat(descriptor)
    except OSError as error:
        raise QualificationFailure(f"Cannot safely inspect {label}.") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity or not stat.S_ISREG(after.st_mode) or after.st_nlink != 1:
        raise QualificationFailure(f"{label} changed while it was inspected.")
    return FileBinding(path, before_identity, _sha256(data)), data


def _verify_file_binding(binding: FileBinding) -> None:
    current, _ = _bind_file(binding.path, "bound PLAY CLI runtime file")
    if current.identity != binding.identity or current.sha256 != binding.sha256:
        raise QualificationFailure("PLAY CLI runtime identity changed during qualification.")


def _load_wheel_authority() -> tuple[dict[str, tuple[int, str]], str, str]:
    try:
        authority_bytes = WHEEL_AUTHORITY.read_bytes()
    except OSError as error:
        raise QualificationFailure("Cannot read the PLAY CLI wheel authority.") from error
    if _sha256(authority_bytes) != APPROVED_WHEEL_AUTHORITY_SHA256:
        raise QualificationFailure("PLAY CLI wheel authority integrity failed.")
    authority = _json_object(authority_bytes, "PLAY CLI wheel authority")
    if set(authority) != {
        "schema_version",
        "project",
        "version",
        "git_commit",
        "wheel_sha256",
        "files",
    } or (
        authority.get("schema_version") != "1.0"
        or authority.get("project") != "play-cli"
        or authority.get("version") != "0.7.0"
        or authority.get("git_commit") != APPROVED_PLAYCLI_GIT_COMMIT
    ):
        raise QualificationFailure("PLAY CLI wheel authority contract is invalid.")
    approved_files: dict[str, tuple[int, str]] = {}
    for item in _array(authority.get("files"), "PLAY CLI wheel authority files"):
        entry = _object(item, "PLAY CLI wheel authority file")
        if set(entry) != {"path", "byte_size", "sha256"}:
            raise QualificationFailure("PLAY CLI wheel authority file is invalid.")
        name = _string(entry.get("path"), "PLAY CLI wheel authority path")
        size = _integer(entry.get("byte_size"), "PLAY CLI wheel authority byte size")
        sha256 = _string(entry.get("sha256"), "PLAY CLI wheel authority hash")
        if name in approved_files or size < 0:
            raise QualificationFailure("PLAY CLI wheel authority inventory is invalid.")
        approved_files[name] = (size, sha256)
    return (
        approved_files,
        _string(authority.get("wheel_sha256"), "approved PLAY CLI wheel hash"),
        _string(authority.get("git_commit"), "approved PLAY CLI git commit"),
    )


def _verified_wheel(
    distribution: Path, claimed_git_commit: str
) -> tuple[Path, bytes, dict[str, bytes], dict[str, tuple[str, str]], str]:
    approved_files, approved_wheel_sha256, approved_commit = _load_wheel_authority()
    if claimed_git_commit != approved_commit:
        raise QualificationFailure(
            "PLAY CLI git commit is not the approved qualification baseline."
        )
    try:
        wheel_path = distribution.resolve(strict=True)
        wheel_bytes = wheel_path.read_bytes()
    except OSError as error:
        raise QualificationFailure("PLAY CLI distribution is not a readable wheel.") from error
    if _sha256(wheel_bytes) != approved_wheel_sha256:
        raise QualificationFailure("PLAY CLI wheel does not match the approved git baseline.")
    try:
        with zipfile.ZipFile(BytesIO(wheel_bytes)) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            if len(names) != len(set(names)) or any(
                Path(name).is_absolute() or ".." in Path(name).parts for name in names
            ):
                raise QualificationFailure("PLAY CLI wheel inventory is unsafe.")
            wheel_files = {name: archive.read(name) for name in names}
    except (zipfile.BadZipFile, KeyError) as error:
        raise QualificationFailure("PLAY CLI distribution is not a readable wheel.") from error
    actual_files = {name: (len(data), _sha256(data)) for name, data in wheel_files.items()}
    if actual_files != approved_files:
        raise QualificationFailure("PLAY CLI wheel inventory is not approved.")
    record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
    if len(record_names) != 1:
        raise QualificationFailure("PLAY CLI wheel RECORD is missing or ambiguous.")
    record_name = record_names[0]
    wheel_record = _record_rows(wheel_files[record_name].decode("utf-8"), "PLAY CLI wheel RECORD")
    if set(wheel_record) != set(wheel_files):
        raise QualificationFailure("PLAY CLI wheel RECORD inventory is not exact.")
    for name, data in wheel_files.items():
        expected = ("", "") if name == record_name else (_record_hash(data), str(len(data)))
        if wheel_record.get(name) != expected:
            raise QualificationFailure("PLAY CLI wheel RECORD hash does not match.")
    package_files = {
        name: data
        for name, data in wheel_files.items()
        if name.startswith("play_cli/") and name.endswith(".py")
    }
    source_entries = [f"{name}\0{_sha256(data)}\n" for name, data in sorted(package_files.items())]
    source_manifest_sha256 = _sha256("".join(source_entries).encode())
    if source_manifest_sha256 != APPROVED_PLAYCLI_SOURCE_MANIFEST_SHA256:
        raise QualificationFailure("PLAY CLI wheel source baseline is inconsistent.")
    return wheel_path, wheel_bytes, wheel_files, wheel_record, source_manifest_sha256


def _inspect_runtime(
    playcli: Path,
    wheel_path: Path,
    wheel_bytes: bytes,
    wheel_files: dict[str, bytes],
    wheel_record: dict[str, tuple[str, str]],
) -> RuntimeBinding:
    absolute_playcli = storage.absolute_path(playcli)
    runtime_python_command = absolute_playcli.parent / "python"
    try:
        python_link = os.lstat(runtime_python_command)
        runtime_python_path = runtime_python_command.resolve(strict=True)
    except OSError as error:
        raise QualificationFailure("Cannot inspect the playcli runtime Python.") from error
    python_command_identity = (
        python_link.st_dev,
        python_link.st_ino,
        python_link.st_size,
        python_link.st_mtime_ns,
    )
    playcli_binding, script_bytes = _bind_file(absolute_playcli, "playcli entry point")
    runtime_python, _ = _bind_file(runtime_python_path, "playcli runtime Python")
    try:
        script_lines = script_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise QualificationFailure("playcli entry point is not UTF-8.") from error
    expected_script = [
        f"#!{absolute_playcli.parent / 'python'}",
        "# -*- coding: utf-8 -*-",
        "import re",
        "import sys",
        "from play_cli.cli import main",
        "if __name__ == '__main__':",
        "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])",
        "    sys.exit(main())",
    ]
    if script_lines != expected_script:
        raise QualificationFailure("playcli entry point content is not canonical.")
    site_roots = list((absolute_playcli.parents[1] / "lib").glob("python*/site-packages"))
    if len(site_roots) != 1:
        raise QualificationFailure("Installed PLAY CLI site-packages is ambiguous.")
    site_packages = site_roots[0]
    forbidden_startup = [
        path
        for pattern in ("sitecustomize.*", "usercustomize.*", "*.pth")
        for path in site_packages.glob(pattern)
    ]
    if forbidden_startup:
        raise QualificationFailure("Installed PLAY CLI contains unapproved startup code.")
    dist_infos = list(site_packages.glob("play_cli-*.dist-info"))
    if len(dist_infos) != 1 or dist_infos[0].name != "play_cli-0.7.0.dist-info":
        raise QualificationFailure("Installed PLAY CLI metadata is ambiguous.")
    dist_info = dist_infos[0]
    installed_record_binding, installed_record_bytes = _bind_file(
        dist_info / "RECORD", "installed PLAY CLI RECORD"
    )
    try:
        installed_record = _record_rows(
            installed_record_bytes.decode("utf-8"), "installed PLAY CLI RECORD"
        )
    except UnicodeDecodeError as error:
        raise QualificationFailure("Installed PLAY CLI RECORD is not UTF-8.") from error
    record_name = next(name for name in wheel_files if name.endswith(".dist-info/RECORD"))
    dist_info_prefix = record_name.removesuffix("RECORD")
    script_record_name = "../../../bin/playcli"
    site_packages_fd: int | None = None
    try:
        site_packages_fd = storage.open_directory(site_packages)
        installed_disk_files = {
            item.path: item.data for item in storage.scan_regular_files(site_packages_fd)
        }
    except OSError as error:
        raise QualificationFailure("Cannot safely scan the installed PLAY CLI runtime.") from error
    finally:
        if site_packages_fd is not None:
            os.close(site_packages_fd)
    if set(installed_disk_files) != set(installed_record) - {script_record_name}:
        raise QualificationFailure("Installed PLAY CLI filesystem inventory is not exact.")
    permitted_generated = {
        script_record_name,
        f"{dist_info_prefix}INSTALLER",
        f"{dist_info_prefix}REQUESTED",
        f"{dist_info_prefix}direct_url.json",
    }
    permitted_pyc = {
        name
        for name in installed_record
        if name.startswith("play_cli/__pycache__/")
        and name.endswith(".pyc")
        and installed_record[name] == ("", "")
    }
    if set(installed_record) != set(wheel_record) | permitted_generated | permitted_pyc:
        raise QualificationFailure("Installed PLAY CLI RECORD inventory is not exact.")
    bindings: list[FileBinding] = [installed_record_binding]
    generated_data: dict[str, bytes] = {}
    for name, wheel_data in wheel_files.items():
        if name == record_name:
            if installed_record.get(name) != ("", ""):
                raise QualificationFailure("Installed PLAY CLI RECORD self-entry is invalid.")
            continue
        installed_binding, installed_data = _bind_file(
            site_packages / name, "installed PLAY CLI wheel file"
        )
        if installed_data != wheel_data or installed_record.get(name) != wheel_record[name]:
            raise QualificationFailure("Installed PLAY CLI files do not match the wheel.")
        bindings.append(installed_binding)
    for name in permitted_generated | permitted_pyc:
        path = absolute_playcli if name == script_record_name else site_packages / name
        binding, data = _bind_file(path, "installed PLAY CLI generated file")
        row = installed_record[name]
        if row != ("", "") and row != (_record_hash(data), str(len(data))):
            raise QualificationFailure("Installed PLAY CLI RECORD hash does not match.")
        bindings.append(binding)
        generated_data[name] = data
    direct_url = _json_object(
        generated_data[f"{dist_info_prefix}direct_url.json"], "playcli direct_url.json"
    )
    archive_info = _object(direct_url.get("archive_info"), "playcli direct_url archive_info")
    hashes = _object(archive_info.get("hashes"), "playcli direct_url hashes")
    parsed_url = urlparse(_string(direct_url.get("url"), "playcli direct_url"))
    if (
        parsed_url.scheme != "file"
        or Path(unquote(parsed_url.path)).resolve() != wheel_path
        or hashes.get("sha256") != _sha256(wheel_bytes)
    ):
        raise QualificationFailure("Installed PLAY CLI direct_url does not bind the wheel.")
    return RuntimeBinding(
        playcli_binding,
        runtime_python,
        runtime_python_command,
        python_command_identity,
        tuple(bindings),
        site_packages,
        tuple((name, _sha256(data)) for name, data in sorted(installed_disk_files.items())),
    )


def _verify_runtime(runtime: RuntimeBinding) -> None:
    if (
        any(runtime.site_packages.glob("sitecustomize.*"))
        or any(runtime.site_packages.glob("usercustomize.*"))
        or any(runtime.site_packages.glob("*.pth"))
    ):
        raise QualificationFailure("Installed PLAY CLI contains unapproved startup code.")
    _verify_file_binding(runtime.playcli)
    try:
        python_link = os.lstat(runtime.runtime_python_command)
        python_target = runtime.runtime_python_command.resolve(strict=True)
    except OSError as error:
        raise QualificationFailure(
            "PLAY CLI runtime identity changed during qualification."
        ) from error
    if (
        python_link.st_dev,
        python_link.st_ino,
        python_link.st_size,
        python_link.st_mtime_ns,
    ) != runtime.runtime_python_command_identity or python_target != runtime.runtime_python.path:
        raise QualificationFailure("PLAY CLI runtime identity changed during qualification.")
    _verify_file_binding(runtime.runtime_python)
    for binding in runtime.installed_files:
        _verify_file_binding(binding)
    site_packages_fd: int | None = None
    try:
        site_packages_fd = storage.open_directory(runtime.site_packages)
        current_inventory = tuple(
            (item.path, _sha256(item.data)) for item in storage.scan_regular_files(site_packages_fd)
        )
    except OSError as error:
        raise QualificationFailure(
            "PLAY CLI runtime inventory changed during qualification."
        ) from error
    finally:
        if site_packages_fd is not None:
            os.close(site_packages_fd)
    if current_inventory != runtime.installed_inventory:
        raise QualificationFailure("PLAY CLI runtime inventory changed during qualification.")


def _verify_distribution(
    playcli: Path,
    distribution: Path,
    claimed_git_commit: str,
    private_runtime_root: Path,
) -> DistributionEvidence:
    wheel_path, wheel_bytes, wheel_files, wheel_record, source_manifest_sha256 = _verified_wheel(
        distribution, claimed_git_commit
    )
    supplied_runtime = _inspect_runtime(playcli, wheel_path, wheel_bytes, wheel_files, wheel_record)
    _verify_runtime(supplied_runtime)
    private_wheel = private_runtime_root.parent / wheel_path.name
    descriptor: int | None = None
    try:
        descriptor = os.open(
            private_wheel,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o400,
        )
        view = memoryview(wheel_bytes)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
    except OSError as error:
        raise QualificationFailure("Cannot snapshot the approved PLAY CLI wheel.") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    private_wheel_binding, private_wheel_bytes = _bind_file(
        private_wheel, "approved PLAY CLI wheel snapshot"
    )
    if private_wheel_bytes != wheel_bytes:
        raise QualificationFailure("Approved PLAY CLI wheel snapshot is inconsistent.")
    created = subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(private_runtime_root)],
        capture_output=True,
        check=False,
        text=True,
    )
    if created.returncode != 0:
        raise QualificationFailure("Cannot create the isolated PLAY CLI runtime.")
    installed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "--python",
            str(private_runtime_root / "bin/python"),
            "install",
            "--no-index",
            "--no-deps",
            str(private_wheel),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if installed.returncode != 0:
        raise QualificationFailure("Cannot install the approved wheel in the isolated runtime.")
    _verify_file_binding(private_wheel_binding)
    runtime = _inspect_runtime(
        private_runtime_root / "bin/playcli",
        private_wheel,
        wheel_bytes,
        wheel_files,
        wheel_record,
    )
    _verify_runtime(runtime)
    return DistributionEvidence(
        path_name=wheel_path.name,
        sha256=_sha256(wheel_bytes),
        source_manifest_sha256=source_manifest_sha256,
        runtime_python_sha256=runtime.runtime_python.sha256,
        runtime=runtime,
    )


def _run_playcli(runtime: RuntimeBinding, operation: str, *arguments: str) -> dict[str, object]:
    _verify_runtime(runtime)
    environment = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        environment.pop(name, None)
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "SETUPTOOLS_USE_DISTUTILS": "stdlib",
        }
    )
    result = subprocess.run(
        [
            str(runtime.runtime_python_command),
            "-I",
            str(runtime.playcli.path),
            *arguments,
        ],
        capture_output=True,
        check=False,
        text=True,
        env=environment,
    )
    _verify_runtime(runtime)
    try:
        response = _json_object(result.stdout, f"playcli {operation} response")
    except QualificationFailure as error:
        raise QualificationFailure(f"playcli {operation} returned malformed JSON.") from error
    if result.returncode != 0:
        raise QualificationFailure(
            f"playcli {operation} failed with exit code {result.returncode}: {result.stderr}"
        )
    if (
        response.get("schema_version") != PLAYCLI_SCHEMA_VERSION
        or response.get("operation") != operation
        or response.get("status") != "ok"
    ):
        raise QualificationFailure(f"playcli {operation} returned an invalid success envelope.")
    return response


def _pitch_xml(midi: int) -> str:
    step, alter = STEPS[midi % 12]
    alter_xml = f"<alter>{alter}</alter>" if alter else ""
    octave = midi // 12 - 1
    return f"<pitch><step>{step}</step>{alter_xml}<octave>{octave}</octave></pitch>"


def _note_type(duration_divisions: int) -> str:
    return {16: "whole", 4: "quarter", 2: "eighth"}.get(duration_divisions, "sixteenth")


def _score_xml(items: tuple[ScoreItem, ...], *, tempo: int) -> str:
    events = []
    for item in items:
        events.append(
            f"<note>{_pitch_xml(item.midi)}<duration>{item.duration_divisions}</duration>"
            f"<type>{_note_type(item.duration_divisions)}</type></note>"
        )
        events.append(f"<note><rest/><duration>{item.rest_divisions}</duration></note>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<score-partwise version="4.0"><part-list><score-part id="P1">'
        '<part-name>Piano</part-name></score-part></part-list><part id="P1">'
        '<measure number="1"><attributes><divisions>4</divisions><key><fifths>0</fifths>'
        "<mode>major</mode></key><time><beats>4</beats><beat-type>4</beat-type></time>"
        f'<clef><sign>G</sign><line>2</line></clef></attributes><direction placement="above">'
        "<direction-type><metronome><beat-unit>quarter</beat-unit>"
        f"<per-minute>{tempo}</per-minute></metronome></direction-type>"
        f'<sound tempo="{tempo}"/></direction>{"".join(events)}</measure></part>'
        "</score-partwise>\n"
    )


def _read_pcm16(data: bytes, label: str) -> tuple[int, tuple[int, ...]]:
    try:
        with wave.open(BytesIO(data), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getcomptype() != "NONE"
            ):
                raise QualificationFailure("Qualification WAV is not mono PCM16.")
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            frame_data = source.readframes(frame_count)
    except (EOFError, OSError, wave.Error) as error:
        raise QualificationFailure(f"Qualification WAV is unreadable: {label}") from error
    if len(frame_data) != frame_count * 2:
        raise QualificationFailure(f"Qualification WAV is truncated: {label}")
    return sample_rate, struct.unpack(f"<{frame_count}h", frame_data)


def _rational(value: object, label: str) -> Fraction:
    contract = _object(value, label)
    numerator = _integer(contract.get("numerator"), f"{label}.numerator")
    denominator = _integer(contract.get("denominator"), f"{label}.denominator")
    if denominator <= 0:
        raise QualificationFailure(f"{label}.denominator must be positive.")
    return Fraction(numerator, denominator)


def _event_frames(event: dict[str, object], tempo: int, sample_rate: int) -> tuple[int, int]:
    onset = _rational(event.get("onset_quarters_exact"), "event onset")
    duration = _rational(event.get("duration_quarters_exact"), "event duration")
    start = round(float(onset * 60 * sample_rate / tempo))
    length = round(float(duration * 60 * sample_rate / tempo))
    return start, start + length


def _block_average(values: tuple[int, ...], block_size: int) -> list[float]:
    return [
        sum(values[index : index + block_size]) / block_size
        for index in range(0, len(values) - block_size + 1, block_size)
    ]


def _estimate_pitch(values: tuple[int, ...], sample_rate: int, expected_frequency: float) -> float:
    start = min(len(values) // 8, sample_rate // 20)
    window = values[start : min(len(values), start + sample_rate * 3 // 5)]
    block_size = max(1, int(sample_rate / (expected_frequency * 96)))
    reduced = _block_average(window, block_size)
    reduced_rate = sample_rate / block_size
    low_frequency = expected_frequency * 2 ** (-100 / 1200)
    high_frequency = expected_frequency * 2 ** (100 / 1200)
    minimum_lag = max(2, math.floor(reduced_rate / high_frequency))
    maximum_lag = min(len(reduced) // 2, math.ceil(reduced_rate / low_frequency))
    differences: dict[int, float] = {}
    for lag in range(minimum_lag, maximum_lag + 1):
        count = len(reduced) - lag
        differences[lag] = (
            sum((reduced[index] - reduced[index + lag]) ** 2 for index in range(count)) / count
        )
    best_lag = min(differences, key=differences.__getitem__)
    refined_lag = float(best_lag)
    if best_lag - 1 in differences and best_lag + 1 in differences:
        left = differences[best_lag - 1]
        center = differences[best_lag]
        right = differences[best_lag + 1]
        denominator = left - 2 * center + right
        if denominator:
            refined_lag += 0.5 * (left - right) / denominator
    return reduced_rate / refined_lag


def _render(
    runtime: RuntimeBinding,
    score: Path,
    store: Path,
    pack_id: str,
    version: str,
    destination: Path,
) -> dict[str, object]:
    return _run_playcli(
        runtime,
        "render",
        "render",
        str(score),
        "--sound-pack",
        pack_id,
        "--sound-pack-version",
        version,
        "--sound-pack-store",
        str(store),
        "--output",
        str(destination),
    )


def _verify_bundle(response: dict[str, object], root: Path, score_bytes: bytes) -> BundleEvidence:
    result_bundle = _object(response.get("result_bundle"), "render result_bundle")
    resolved_root = root.resolve()
    if (
        result_bundle.get("schema_version") != "1.1"
        or result_bundle.get("status") != "complete"
        or result_bundle.get("path") != str(resolved_root)
        or result_bundle.get("manifest") != str(resolved_root / "result.json")
    ):
        raise QualificationFailure("Render Result Bundle envelope is invalid.")
    root_fd: int | None = None
    try:
        root_fd = storage.open_directory(root)
        stored = storage.scan_regular_files(root_fd)
    except OSError as error:
        raise QualificationFailure("Cannot safely read the Render Result Bundle.") from error
    finally:
        if root_fd is not None:
            os.close(root_fd)
    expected_names = {
        "score.musicxml",
        "validation.json",
        "schedule.json",
        "performance.wav",
        "result.json",
    }
    files = {item.path: item.data for item in stored}
    if set(files) != expected_names:
        raise QualificationFailure("Render Result Bundle inventory is not exact.")
    manifest = _json_object(files["result.json"], "Result Bundle manifest")
    response_manifest = {key: value for key, value in response.items() if key != "playback"}
    if manifest != response_manifest:
        raise QualificationFailure("Result Bundle manifest does not match the CLI response.")
    artifacts = _object(result_bundle.get("artifacts"), "render artifacts")
    artifact_names = {
        "score": "score.musicxml",
        "validation": "validation.json",
        "schedule": "schedule.json",
        "performance": "performance.wav",
    }
    hashes: dict[str, str] = {}
    for key, name in artifact_names.items():
        artifact = _object(artifacts.get(key), f"render artifact {key}")
        if artifact.get("path") != str(resolved_root / name):
            raise QualificationFailure(f"Render artifact path is invalid: {name}")
        declared = _string(artifact.get("sha256"), f"render artifact {key} hash")
        actual = _sha256(files[name])
        if declared != actual:
            raise QualificationFailure(f"Render artifact hash does not match: {name}")
        hashes[name] = actual
    if files["score.musicxml"] != score_bytes:
        raise QualificationFailure("Result Bundle score does not match the authored MusicXML.")
    _json_object(files["validation.json"], "validation artifact")
    schedule = _json_object(files["schedule.json"], "schedule artifact")
    raw_events = _array(schedule.get("events"), "schedule events")
    events = tuple(_object(event, "schedule event") for event in raw_events)
    sample_rate, samples = _read_pcm16(files["performance.wav"], "performance.wav")
    wav = _object(response.get("wav"), "render wav summary")
    if (
        wav.get("format") != "PCM_S16LE"
        or _integer(wav.get("channels"), "wav channels") != 1
        or _integer(wav.get("frames"), "wav frames") != len(samples)
    ):
        raise QualificationFailure("Render WAV summary does not match complete WAV data.")
    return BundleEvidence(
        response,
        events,
        sample_rate,
        samples,
        hashes,
        _sha256(files["result.json"]),
        len(files["performance.wav"]),
    )


def _validate_schedule(
    events: tuple[dict[str, object], ...], items: tuple[ScoreItem, ...]
) -> Fraction:
    if len(events) != len(items) * 2:
        raise QualificationFailure("Schedule event count does not match the authored score.")
    onset = Fraction(0)
    for item_index, item in enumerate(items):
        note = events[item_index * 2]
        rest = events[item_index * 2 + 1]
        note_duration = Fraction(item.duration_divisions, 4)
        rest_duration = Fraction(item.rest_divisions, 4)
        if (
            note.get("kind") != "note"
            or _integer(note.get("order"), "note order") != item_index * 2
            or _rational(note.get("onset_quarters_exact"), "note onset") != onset
            or _rational(note.get("duration_quarters_exact"), "note duration") != note_duration
            or _integer(_object(note.get("pitch"), "note pitch").get("midi"), "note midi")
            != item.midi
            or _integer(note.get("velocity"), "note velocity") != 64
        ):
            raise QualificationFailure("Schedule note does not match the authored score.")
        onset += note_duration
        if (
            rest.get("kind") != "rest"
            or _integer(rest.get("order"), "rest order") != item_index * 2 + 1
            or _rational(rest.get("onset_quarters_exact"), "rest onset") != onset
            or _rational(rest.get("duration_quarters_exact"), "rest duration") != rest_duration
            or rest.get("pitch") is not None
            or _integer(rest.get("velocity"), "rest velocity") != 0
        ):
            raise QualificationFailure("Schedule rest does not match the authored score.")
        onset += rest_duration
    return onset


def _expected_frames(total_quarters: Fraction, tempo: int, sample_rate: int) -> int:
    return round(float(total_quarters * 60 * sample_rate / tempo))


def _require_silent_ending(
    events: tuple[dict[str, object], ...], samples: tuple[int, ...], tempo: int, rate: int
) -> None:
    start, end = _event_frames(events[-1], tempo, rate)
    if end != len(samples) or start >= end or any(samples[start:end]):
        raise QualificationFailure("Rendered Performance does not end in complete silence.")


def _pack_build_evidence(
    source: Path, pack_id: str, version: str, pack_hash: str
) -> dict[str, object]:
    root_fd: int | None = None
    try:
        root_fd = storage.open_directory(source)
        stored = storage.scan_regular_files(root_fd)
    except OSError as error:
        raise QualificationFailure("Cannot safely read the qualified Sound Pack.") from error
    finally:
        if root_fd is not None:
            os.close(root_fd)
    files = {item.path: item.data for item in stored if item.path != ".playcli-receipt.json"}
    manifest_bytes = files.get("manifest.json")
    receipt_bytes = files.get("SOURCE-RECEIPT.json")
    if manifest_bytes is None or receipt_bytes is None:
        raise QualificationFailure("Qualified Sound Pack is missing build evidence.")
    manifest = _json_object(manifest_bytes, "Sound Pack manifest")
    receipt = _json_object(receipt_bytes, "Sound Pack source receipt")
    if manifest.get("id") != pack_id or manifest.get("version") != version:
        raise QualificationFailure("Sound Pack manifest identity changed.")
    build = _object(manifest.get("build"), "Sound Pack build record")
    builder_sha256 = _string(build.get("builder_sha256"), "Sound Pack builder hash")
    generator = _object(manifest.get("generator"), "Sound Pack generator")
    if generator.get("version") != "1.1" or generator.get("sha256") != builder_sha256:
        raise QualificationFailure("Sound Pack builder provenance is inconsistent.")
    receipt_builder = _object(receipt.get("builder"), "Sound Pack source receipt builder")
    if receipt_builder != {"version": "1.1", "sha256": builder_sha256}:
        raise QualificationFailure("Sound Pack source receipt builder is inconsistent.")
    conversion = _object(build.get("conversion"), "Sound Pack conversion record")
    ffmpeg = _object(conversion.get("ffmpeg"), "FFmpeg build evidence")
    ffprobe = _object(conversion.get("ffprobe"), "ffprobe build evidence")
    for name, tool in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        _string(tool.get("path"), f"{name} path")
        _string(tool.get("sha256"), f"{name} sha256")
        _string(tool.get("version_output"), f"{name} version output")
    conversion_evidence: dict[str, object] = {
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "platform": _string(conversion.get("platform"), "conversion platform"),
        "python": _string(conversion.get("python"), "conversion Python"),
        "input": _string(conversion.get("input"), "conversion input"),
        "output": _string(conversion.get("output"), "conversion output"),
        "downmix": _string(conversion.get("downmix"), "conversion downmix"),
        "dither": _string(conversion.get("dither"), "conversion dither"),
        "fade_in_ms": _integer(conversion.get("fade_in_ms"), "conversion fade"),
        "velocity": _integer(conversion.get("velocity"), "conversion velocity"),
        "velocity_layer": _string(conversion.get("velocity_layer"), "conversion velocity layer"),
        "omitted_layers": _array(conversion.get("omitted_layers"), "conversion omitted layers"),
    }
    source_receipt_sha256 = _sha256(receipt_bytes)
    if build.get("source_receipt_sha256") != source_receipt_sha256:
        raise QualificationFailure("Sound Pack source receipt hash does not match.")
    samples = _object(manifest.get("samples"), "Sound Pack samples")
    if set(samples) != {str(midi) for midi in range(MIDI_MIN, MIDI_MAX + 1)}:
        raise QualificationFailure("Sound Pack manifest is not an exact 88-key mapping.")
    canonical_entries: list[str] = []
    sample_bytes = 0
    for midi in range(MIDI_MIN, MIDI_MAX + 1):
        sample = _object(samples[str(midi)], f"Sound Pack sample {midi}")
        path = _string(sample.get("path"), f"Sound Pack sample {midi} path")
        sha256 = _string(sample.get("sha256"), f"Sound Pack sample {midi} hash")
        byte_size = _integer(sample.get("byte_size"), f"Sound Pack sample {midi} byte size")
        frames = _integer(sample.get("frames"), f"Sound Pack sample {midi} frames")
        canonical_entries.append(f"{midi}\0{path}\0{sha256}\0{byte_size}\0{frames}\n")
        sample_bytes += byte_size
    return {
        "source_revision": _string(build.get("source_revision"), "source revision"),
        "source_tree": _string(build.get("source_tree"), "source tree"),
        "source_lock_sha256": _string(build.get("source_lock_sha256"), "source lock hash"),
        "source_receipt_sha256": source_receipt_sha256,
        "builder_sha256": builder_sha256,
        "manifest_sha256": _sha256(manifest_bytes),
        "pack_hash": pack_hash,
        "files": len(files),
        "samples": len(samples),
        "sample_bytes": sample_bytes,
        "total_bytes": sum(len(data) for data in files.values()),
        "sample_inventory_sha256": _sha256("".join(canonical_entries).encode()),
        "conversion_environment": conversion_evidence,
    }


def qualify(
    source: Path,
    playcli: Path,
    output: Path,
    playcli_git_commit: str,
    distribution: Path,
) -> dict[str, object]:
    distribution_evidence = _verify_distribution(
        playcli,
        distribution,
        playcli_git_commit,
        output / "runtime",
    )
    runtime = distribution_evidence.runtime
    store = output / "pack-store"
    installed = _run_playcli(
        runtime,
        "sound-pack.install",
        "sound-pack",
        "install",
        str(source),
        "--store",
        str(store),
    )
    pack = _object(installed.get("sound_pack"), "installed sound_pack")
    pack_id = _string(pack.get("id"), "sound_pack id")
    version = _string(pack.get("version"), "sound_pack version")
    pack_hash = _string(pack.get("pack_hash"), "sound_pack hash")
    listed = _run_playcli(runtime, "sound-pack.list", "sound-pack", "list", "--store", str(store))
    listed_packs = _array(listed.get("sound_packs"), "listed sound_packs")
    if not any(
        _object(item, "listed sound_pack").get("id") == pack_id
        and _object(item, "listed sound_pack").get("version") == version
        and _object(item, "listed sound_pack").get("status") == "verified"
        for item in listed_packs
    ):
        raise QualificationFailure("Installed Sound Pack was not returned as verified by list.")
    verified = _run_playcli(
        runtime,
        "sound-pack.verify",
        "sound-pack",
        "verify",
        pack_id,
        "--version",
        version,
        "--store",
        str(store),
    )
    verified_pack = _object(verified.get("sound_pack"), "verified sound_pack")
    if (
        verified_pack.get("id") != pack_id
        or verified_pack.get("version") != version
        or verified_pack.get("pack_hash") != pack_hash
    ):
        raise QualificationFailure("Verified Sound Pack identity changed.")
    build_evidence = _pack_build_evidence(store / pack_id / version, pack_id, version, pack_hash)

    coverage_score = output / "coverage.musicxml"
    coverage_bytes = _score_xml(COVERAGE_ITEMS, tempo=240).encode()
    coverage_score.write_bytes(coverage_bytes)
    coverage_result = output / "coverage-result"
    coverage_response = _render(runtime, coverage_score, store, pack_id, version, coverage_result)
    coverage = _verify_bundle(coverage_response, coverage_result, coverage_bytes)
    coverage_total = _validate_schedule(coverage.events, COVERAGE_ITEMS)
    _require_silent_ending(coverage.events, coverage.samples, 240, coverage.sample_rate)
    responsive: list[int] = []
    release_failures: list[int] = []
    click_failures: list[int] = []
    note_events = [event for event in coverage.events if event.get("kind") == "note"]
    for event in note_events:
        midi = _integer(_object(event.get("pitch"), "note pitch").get("midi"), "note midi")
        start, end = _event_frames(event, 240, coverage.sample_rate)
        note = coverage.samples[start:end]
        if note and max(abs(value) for value in note) >= 64:
            responsive.append(midi)
        if not note or abs(note[-1]) > 32:
            release_failures.append(midi)
        before = coverage.samples[start - 1] if start else 0
        if not note or abs(note[0] - before) > 2_048:
            click_failures.append(midi)
    expected_coverage_frames = _expected_frames(coverage_total, 240, coverage.sample_rate)
    if len(coverage.samples) != expected_coverage_frames:
        raise QualificationFailure("Coverage render timing does not match the authored score.")

    audition_score = output / "audition.musicxml"
    audition_bytes = _score_xml(AUDITION_ITEMS, tempo=80).encode()
    audition_score.write_bytes(audition_bytes)
    audition_result = output / "audition-result"
    audition_response = _render(runtime, audition_score, store, pack_id, version, audition_result)
    audition = _verify_bundle(audition_response, audition_result, audition_bytes)
    audition_total = _validate_schedule(audition.events, AUDITION_ITEMS)
    _require_silent_ending(audition.events, audition.samples, 80, audition.sample_rate)
    audition_events = [event for event in audition.events if event.get("kind") == "note"]
    pitch_checks: list[dict[str, object]] = []
    cents_values: list[float] = []
    for event in audition_events[: len(PITCH_TEST_NOTES)]:
        midi = _integer(_object(event.get("pitch"), "note pitch").get("midi"), "note midi")
        start, end = _event_frames(event, 80, audition.sample_rate)
        expected = 440.0 * 2 ** ((midi - 69) / 12)
        measured = _estimate_pitch(audition.samples[start:end], audition.sample_rate, expected)
        cents = 1200 * math.log2(measured / expected)
        cents_values.append(cents)
        pitch_checks.append(
            {
                "midi": midi,
                "measured_hz": round(measured, 3),
                "cents": round(cents, 3),
            }
        )
    max_cents = max(abs(cents) for cents in cents_values)
    clipped = sum(
        abs(value) >= 32_767
        for samples in (coverage.samples, audition.samples)
        for value in samples
    )
    expected_audition_frames = _expected_frames(audition_total, 80, audition.sample_rate)
    if len(audition.samples) != expected_audition_frames:
        raise QualificationFailure("Audition render timing does not match the authored score.")
    failures: dict[str, object] = {
        "missing_notes": sorted(set(range(21, 109)) - set(responsive)),
        "release_failures": release_failures,
        "boundary_click_failures": click_failures,
    }
    if any(failures.values()) or max_cents > 35 or clipped:
        raise QualificationFailure(
            "Automated audio qualification failed: "
            f"failures={failures}, max_absolute_cents={max_cents:.3f}, "
            f"clipped_samples={clipped}"
        )
    coverage_provenance = _object(coverage.response.get("provenance"), "coverage provenance")
    cli = _object(coverage_provenance.get("cli"), "coverage cli provenance")
    renderer = _object(coverage_provenance.get("renderer"), "coverage renderer provenance")
    pack_provenance = _object(coverage_provenance.get("sound_pack"), "coverage pack provenance")
    if (
        pack_provenance.get("id") != pack_id
        or pack_provenance.get("version") != version
        or pack_provenance.get("pack_hash") != pack_hash
    ):
        raise QualificationFailure("Render provenance does not bind the installed Sound Pack.")
    _verify_runtime(runtime)
    builder = Path(__file__).with_name("build_salamander_standard_piano_pack.py")
    builder_sha256 = _sha256(builder.read_bytes())
    if build_evidence.get("builder_sha256") != builder_sha256:
        raise QualificationFailure("Qualified Sound Pack was built by a different recipe.")
    qualification_record: dict[str, object] = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "status": "automated_checks_passed",
        "tested_playcli": {
            "git_commit": APPROVED_PLAYCLI_GIT_COMMIT,
            "executable_sha256": runtime.playcli.sha256,
            "cli": cli,
            "response_schema_version": coverage.response.get("schema_version"),
            "renderer": renderer,
        },
        "tested_distribution": {
            "path_name": distribution_evidence.path_name,
            "sha256": distribution_evidence.sha256,
            "source_manifest_sha256": distribution_evidence.source_manifest_sha256,
            "runtime_python_sha256": distribution_evidence.runtime_python_sha256,
        },
        "qualification_tools": {
            "builder_sha256": builder_sha256,
            "qualifier_sha256": _sha256(Path(__file__).read_bytes()),
            "python": sys.version,
            "platform": platform.platform(),
        },
        "sound_pack": {"id": pack_id, "version": version, "pack_hash": pack_hash},
        "derived_pack": build_evidence,
        "public_operations": {
            "sound-pack.install": "ok",
            "sound-pack.list": "ok",
            "sound-pack.verify": "ok",
            "render.coverage": "ok",
            "render.audition": "ok",
        },
        "result_bundles": {
            "coverage": {
                "manifest_sha256": coverage.manifest_sha256,
                "artifact_sha256": coverage.artifact_sha256,
            },
            "audition": {
                "manifest_sha256": audition.manifest_sha256,
                "artifact_sha256": audition.artifact_sha256,
            },
        },
        "automated_checks": {
            "responsive_notes": len(responsive),
            "expected_notes": 88,
            "timing_expected_frames": expected_coverage_frames,
            "timing_actual_frames": len(coverage.samples),
            "pitch_tested_midi": list(PITCH_TEST_NOTES),
            "pitch_limit_cents": 35,
            "max_absolute_cents": round(max_cents, 3),
            "minimum_response_peak": 64,
            "maximum_boundary_delta": 2_048,
            "maximum_release_sample": 32,
            "clipped_samples": clipped,
            "boundary_click_failures": failures["boundary_click_failures"],
            "release_failures": failures["release_failures"],
            "silent_ending": True,
        },
        "listening_acceptance": {
            "status": "pending_human_listening",
            "audition_wav_sha256": audition.artifact_sha256["performance.wav"],
            "audition_wav_bytes": audition.performance_bytes,
            "audition_wav_frames": len(audition.samples),
            "audition_wav_duration_seconds": round(len(audition.samples) / audition.sample_rate, 6),
        },
        "distribution_approval": "blocked_pending_human_listening",
        "audio_committed_to_source_repository": False,
    }
    return {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "operation": "qualify-standard-piano",
        "status": "automated_checks_passed",
        "qualification_output": str(output.resolve()),
        "sound_pack": {"id": pack_id, "version": version, "pack_hash": pack_hash},
        "public_operations": {
            "sound-pack.install": "ok",
            "sound-pack.list": "ok",
            "sound-pack.verify": "ok",
            "render.coverage": "ok",
            "render.audition": "ok",
        },
        "coverage": {
            "midi_min": MIDI_MIN,
            "midi_max": MIDI_MAX,
            "expected_notes": 88,
            "responsive_notes": len(responsive),
            "missing_notes": failures["missing_notes"],
        },
        "pitch": {
            "tested_midi": list(PITCH_TEST_NOTES),
            "max_absolute_cents": round(max_cents, 3),
            "measurements": pitch_checks,
        },
        "timing": {
            "expected_frames": expected_coverage_frames,
            "actual_frames": len(coverage.samples),
        },
        "audio_integrity": {
            "clipped_samples": clipped,
            "release_failures": failures["release_failures"],
            "boundary_click_failures": failures["boundary_click_failures"],
        },
        "listening_acceptance": {
            "status": "pending_human_listening",
            "content": {
                "representative_single_notes_midi": list(REPRESENTATIVE_SINGLE_NOTES),
                "monophonic_scale_midi": [60, 62, 64, 65, 67, 69, 71, 72],
                "monophonic_arpeggio_midi": [60, 64, 67, 72],
                "contains_chords": False,
                "representative_note_duration_seconds": 3.0,
                "representative_silence_seconds": 1.5,
                "scale_note_duration_seconds": 0.375,
                "scale_silence_seconds": 0.1875,
            },
            "required_checks": [
                "recognizable_natural_acoustic_piano",
                "reliable_note_response",
                "clean_releases",
                "no_audible_clicks_or_clipping",
            ],
            "wav": str((audition_result / "performance.wav").resolve()),
        },
        "qualification_record": qualification_record,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--playcli", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--record", type=Path)
    parser.add_argument("--playcli-git-commit", default="unrecorded")
    parser.add_argument("--distribution", type=Path)
    arguments = parser.parse_args()
    output = storage.absolute_path(arguments.output)
    attempt: Path | None = None
    try:
        output.mkdir(parents=True, exist_ok=True)
        attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=output))
        distribution = arguments.distribution or arguments.playcli
        report = qualify(
            arguments.source,
            arguments.playcli,
            attempt,
            arguments.playcli_git_commit,
            distribution,
        )
        if arguments.record is not None:
            record_bytes = (
                json.dumps(
                    report["qualification_record"],
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            arguments.record.write_text(record_bytes)
    except Exception as error:
        if attempt is not None:
            try:
                (attempt / "qualification-failure.json").write_text(
                    json.dumps(
                        {
                            "schema_version": QUALIFICATION_SCHEMA_VERSION,
                            "operation": "qualify-standard-piano",
                            "status": "failed",
                            "error": {
                                "id": "qualification_failed",
                                "message": "Standard Piano qualification failed.",
                            },
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            except OSError:
                pass
        print(
            json.dumps(
                {
                    "schema_version": QUALIFICATION_SCHEMA_VERSION,
                    "operation": "qualify-standard-piano",
                    "status": "failed",
                    "error": {
                        "id": "qualification_failed",
                        "message": "Standard Piano qualification failed.",
                    },
                },
                separators=(",", ":"),
            )
        )
        print(f"Standard Piano qualification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
