#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import play_cli.sound_pack_storage as storage

ROOT = Path(__file__).parents[1]
SOURCE_LOCK = ROOT / "standards/salamander-3382bf9496bba2486f5ab0de55a264d1dfc38404.lock.json"
FIXTURE_SOURCE_LOCK = ROOT / "standards/standard-piano-builder-fixture.lock.json"
PACK_ID = "play-cli-standard-piano-salamander"
PACK_VERSION = "1.0.0"
LICENSE_ID = "CC-BY-3.0"
SAMPLE_RATE = 48_000
BUILDER_SCHEMA_VERSION = "1.0"
BUILDER_OPERATION = "build-standard-piano"
REQUIRED_REVISION = "3382bf9496bba2486f5ab0de55a264d1dfc38404"
APPROVED_SOURCE_LOCK_SHA256 = "c115197af805e4fd1b6e9f6a568042ae901243f22cd2a8dfa1fb6441a9db5922"
APPROVED_FIXTURE_LOCK_SHA256 = "52e51423bf88751a43b58d645ef59319ea28a01532aef317218b1d616d9c7b14"
LOCK_KEYS = {
    "schema_version",
    "acquired_at",
    "repository",
    "revision",
    "tree",
    "acquisition_method",
    "tree_api_uri",
    "blob_api_uri_template",
    "archive_used",
    "selected_bytes",
    "files",
}
LOCK_FILE_KEYS = {"path", "byte_size", "git_blob_oid", "sha256"}
REGION_PATTERN = re.compile(
    r"region_label=(?P<label>[0-9]{2}).*lokey=(?P<low>[0-9]+) "
    r"hikey=(?P<high>[0-9]+) pitch_keycenter=(?P<center>[0-9]+) "
    r"sample=(?P<sample>[^$ ]+)\$VEL\.\$EXT"
)
OFFSET_PATTERN = re.compile(r"#define \$OFF(?P<label>[0-9]{2}) (?P<frames>[0-9]+)")
TUNE_PATTERN = re.compile(r"#define \$TUNE(?P<label>[0-9]{2}) (?P<cents>-?[0-9]+)")


class BuildFailure(Exception):
    def __init__(self, error_id: str, message: str, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.error_id = error_id
        self.message = message


@dataclass(frozen=True)
class LockedFile:
    path: str
    byte_size: int
    git_blob_oid: str
    sha256: str

    def as_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "byte_size": self.byte_size,
            "git_blob_oid": self.git_blob_oid,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class SourceLock:
    acquired_at: str
    repository: str
    revision: str
    tree: str
    acquisition_method: str
    tree_api_uri: str
    blob_api_uri_template: str
    archive_used: bool
    selected_bytes: int
    files: tuple[LockedFile, ...]


@dataclass(frozen=True)
class VerifiedFile:
    contract: LockedFile
    data: bytes


@dataclass(frozen=True)
class RegionMapping:
    region: str
    low_midi: int
    high_midi: int
    source_midi: int
    source_sample: str
    source_offset_frames: int


@dataclass(frozen=True)
class SourceProfile:
    release: bool
    pack_id: str
    pack_version: str
    license_id: str
    sample_suffix: str
    codec_name: str
    input_description: str
    attribution: str


@dataclass(frozen=True)
class BoundTool:
    path: Path
    executable_path: Path
    identity: tuple[int, int, int, int]
    executable_identity: tuple[int, int, int, int]
    sha256: str
    version_output: str

    def as_json(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "version_output": self.version_output,
            "execution": "verified_private_snapshot",
            "execution_sha256": self.sha256,
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_blob_oid(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def _source_failure(diagnostic: str) -> BuildFailure:
    return BuildFailure(
        "source_integrity_failed",
        "Pinned Sound Pack source files failed integrity verification.",
        diagnostic,
    )


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _source_failure(f"Source lock field {field} is invalid.")
    return value


def _required_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _source_failure(f"Source lock field {field} is invalid.")
    return value


def _load_release_lock(source_lock: Path, expected_sha256: str) -> tuple[SourceLock, bytes]:
    try:
        lock_bytes = source_lock.read_bytes()
        parsed: object = json.loads(lock_bytes)
    except (OSError, json.JSONDecodeError) as error:
        raise _source_failure(f"Cannot read source lock: {error}") from error
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise _source_failure("Expected source lock digest is not a lowercase SHA-256.")
    if _sha256(lock_bytes) != expected_sha256:
        raise _source_failure("Approved source lock digest does not match.")
    if not isinstance(parsed, dict) or set(parsed) != LOCK_KEYS:
        raise _source_failure("Source lock has an unexpected contract.")
    if parsed.get("schema_version") != "1.0":
        raise _source_failure("Source lock has an unexpected schema version.")
    raw_files = parsed.get("files")
    if not isinstance(raw_files, list) or len(raw_files) != 37:
        raise _source_failure("Source lock must contain the exact 37 selected files.")
    files: list[LockedFile] = []
    seen_paths: set[str] = set()
    for index, value in enumerate(raw_files):
        if not isinstance(value, dict) or set(value) != LOCK_FILE_KEYS:
            raise _source_failure(f"Source lock file entry {index} is invalid.")
        path = _required_string(value.get("path"), f"files[{index}].path")
        relative = PurePosixPath(path)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(component in {"", ".", ".."} for component in relative.parts)
        ):
            raise _source_failure(f"Source lock path is unsafe: {path}")
        if path in seen_paths:
            raise _source_failure(f"Source lock path is duplicated: {path}")
        seen_paths.add(path)
        byte_size = _required_int(value.get("byte_size"), f"files[{index}].byte_size")
        git_blob_oid = _required_string(value.get("git_blob_oid"), f"files[{index}].git_blob_oid")
        sha256 = _required_string(value.get("sha256"), f"files[{index}].sha256")
        if not re.fullmatch(r"[0-9a-f]{40}", git_blob_oid) or not re.fullmatch(
            r"[0-9a-f]{64}", sha256
        ):
            raise _source_failure(f"Source lock hashes are invalid for: {path}")
        files.append(LockedFile(path, byte_size, git_blob_oid, sha256))
    selected_bytes = _required_int(parsed.get("selected_bytes"), "selected_bytes")
    if selected_bytes != sum(item.byte_size for item in files):
        raise _source_failure("Source lock selected_bytes does not match its file entries.")
    archive_used = parsed.get("archive_used")
    if not isinstance(archive_used, bool):
        raise _source_failure("Source lock field archive_used is invalid.")
    return (
        SourceLock(
            acquired_at=_required_string(parsed.get("acquired_at"), "acquired_at"),
            repository=_required_string(parsed.get("repository"), "repository"),
            revision=_required_string(parsed.get("revision"), "revision"),
            tree=_required_string(parsed.get("tree"), "tree"),
            acquisition_method=_required_string(
                parsed.get("acquisition_method"), "acquisition_method"
            ),
            tree_api_uri=_required_string(parsed.get("tree_api_uri"), "tree_api_uri"),
            blob_api_uri_template=_required_string(
                parsed.get("blob_api_uri_template"), "blob_api_uri_template"
            ),
            archive_used=archive_used,
            selected_bytes=selected_bytes,
            files=tuple(files),
        ),
        lock_bytes,
    )


def _fixture_file(value: object, label: str) -> LockedFile:
    if not isinstance(value, dict) or set(value) != LOCK_FILE_KEYS:
        raise _source_failure(f"Fixture lock {label} is invalid.")
    path = _required_string(value.get("path"), f"{label}.path")
    byte_size = _required_int(value.get("byte_size"), f"{label}.byte_size")
    git_blob_oid = _required_string(value.get("git_blob_oid"), f"{label}.git_blob_oid")
    sha256 = _required_string(value.get("sha256"), f"{label}.sha256")
    if not re.fullmatch(r"[0-9a-f]{40}", git_blob_oid) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise _source_failure(f"Fixture lock hashes are invalid for: {path}")
    return LockedFile(path, byte_size, git_blob_oid, sha256)


def _load_fixture_lock(source_lock: Path, expected_sha256: str) -> tuple[SourceLock, bytes]:
    try:
        lock_bytes = source_lock.read_bytes()
        parsed: object = json.loads(lock_bytes)
    except (OSError, json.JSONDecodeError) as error:
        raise _source_failure(f"Cannot read fixture source lock: {error}") from error
    if _sha256(lock_bytes) != expected_sha256:
        raise _source_failure("Approved fixture source lock digest does not match.")
    expected_keys = {
        "schema_version",
        "profile",
        "license",
        "repository",
        "revision",
        "tree",
        "acquisition_method",
        "sample_template",
        "metadata_files",
        "selected_bytes",
    }
    if not isinstance(parsed, dict) or set(parsed) != expected_keys:
        raise _source_failure("Fixture source lock has an unexpected contract.")
    if parsed.get("schema_version") != "1.0" or parsed.get("profile") != (
        "play-cli-standard-piano-builder-fixture"
    ):
        raise _source_failure("Fixture source lock identity is invalid.")
    license_contract = parsed.get("license")
    if license_contract != {
        "id": "CC0-1.0",
        "file": "LICENSE",
        "sha256": "a2010f343487d3f7618affe54f789f5487602331c0a8d03f49e9a7c547cf0499",
        "complete_text": True,
    }:
        raise _source_failure("Fixture source lock license contract is invalid.")
    metadata = parsed.get("metadata_files")
    if not isinstance(metadata, list) or len(metadata) != 7:
        raise _source_failure("Fixture source lock metadata inventory is invalid.")
    files = [_fixture_file(item, f"metadata_files[{index}]") for index, item in enumerate(metadata)]
    sample_template = parsed.get("sample_template")
    sample_keys = LOCK_FILE_KEYS | {"count"}
    if not isinstance(sample_template, dict) or set(sample_template) != sample_keys:
        raise _source_failure("Fixture source lock sample template is invalid.")
    if sample_template.get("count") != 30:
        raise _source_failure("Fixture source lock must define 30 samples.")
    template = _fixture_file(
        {key: sample_template[key] for key in LOCK_FILE_KEYS}, "sample_template"
    )
    if template.path != "Samples/root{index:02d}v8.wav":
        raise _source_failure("Fixture source lock sample path template is invalid.")
    files.extend(
        LockedFile(
            template.path.format(index=index),
            template.byte_size,
            template.git_blob_oid,
            template.sha256,
        )
        for index in range(1, 31)
    )
    selected_bytes = _required_int(parsed.get("selected_bytes"), "selected_bytes")
    if selected_bytes != sum(item.byte_size for item in files):
        raise _source_failure("Fixture selected_bytes does not match its expanded inventory.")
    return (
        SourceLock(
            acquired_at="2026-09-12",
            repository=_required_string(parsed.get("repository"), "repository"),
            revision=_required_string(parsed.get("revision"), "revision"),
            tree=_required_string(parsed.get("tree"), "tree"),
            acquisition_method=_required_string(
                parsed.get("acquisition_method"), "acquisition_method"
            ),
            tree_api_uri="urn:play-cli:standard-piano-builder-fixture:tree",
            blob_api_uri_template="urn:play-cli:standard-piano-builder-fixture:blob:{git_blob_oid}",
            archive_used=False,
            selected_bytes=selected_bytes,
            files=tuple(files),
        ),
        lock_bytes,
    )


def _load_source_profile(
    source_lock: Path, expected_sha256: str
) -> tuple[SourceProfile, SourceLock, bytes]:
    try:
        resolved = source_lock.resolve(strict=True)
    except OSError as error:
        raise _source_failure(f"Cannot resolve source lock: {error}") from error
    if resolved == SOURCE_LOCK.resolve() and expected_sha256 == APPROVED_SOURCE_LOCK_SHA256:
        lock, lock_bytes = _load_release_lock(resolved, expected_sha256)
        if lock.revision != REQUIRED_REVISION:
            raise _source_failure("Approved source revision does not match.")
        return (
            SourceProfile(
                release=True,
                pack_id=PACK_ID,
                pack_version=PACK_VERSION,
                license_id=LICENSE_ID,
                sample_suffix=".flac",
                codec_name="flac",
                input_description="stereo FLAC, 48000 Hz, 24-bit",
                attribution=(
                    "Salamander Grand Piano V3 recordings by Alexander Holm; SFZ remapping "
                    "by kinwie; Natural mapping from sfzinstruments revision "
                    "3382bf9496bba2486f5ab0de55a264d1dfc38404; Retuned mapping contribution "
                    "by Markus Fiedler was evaluated but omitted."
                ),
            ),
            lock,
            lock_bytes,
        )
    if resolved == FIXTURE_SOURCE_LOCK.resolve() and expected_sha256 == (
        APPROVED_FIXTURE_LOCK_SHA256
    ):
        lock, lock_bytes = _load_fixture_lock(resolved, expected_sha256)
        return (
            SourceProfile(
                release=False,
                pack_id="play-cli-standard-piano-builder-fixture",
                pack_version="0.0.0",
                license_id="CC0-1.0",
                sample_suffix=".wav",
                codec_name="pcm_s24le",
                input_description="test-only stereo PCM_S24LE WAV, 48000 Hz",
                attribution="PLAY CLI contributors; generated from the repository CC0 fixture.",
            ),
            lock,
            lock_bytes,
        )
    raise _source_failure("Source lock is not one of the two repository-approved authorities.")


def _verify_source(source: Path, lock: SourceLock) -> dict[str, VerifiedFile]:
    verified: dict[str, VerifiedFile] = {}
    source_fd: int | None = None
    try:
        source_fd = storage.open_directory(source)
        for item in lock.files:
            data = storage.read_regular_file_at(source_fd, item.path)
            if (
                len(data) != item.byte_size
                or _git_blob_oid(data) != item.git_blob_oid
                or _sha256(data) != item.sha256
            ):
                raise _source_failure(f"Pinned source does not match: {item.path}")
            verified[item.path] = VerifiedFile(item, data)
    except (KeyError, OSError, TypeError) as error:
        raise _source_failure(f"Cannot verify pinned source: {error}") from error
    finally:
        if source_fd is not None:
            os.close(source_fd)
    return verified


def _snapshot_text(verified: dict[str, VerifiedFile], path: str) -> str:
    try:
        return verified[path].data.decode("utf-8")
    except (KeyError, UnicodeDecodeError) as error:
        raise _source_failure(f"Cannot decode pinned mapping: {path}") from error


def _parse_mapping(
    verified: dict[str, VerifiedFile], profile: SourceProfile
) -> list[RegionMapping]:
    notes = _snapshot_text(verified, "Data/notes.txt")
    regions = _snapshot_text(verified, "Data/region.txt")
    velocity = _snapshot_text(verified, "Data/vel_08.txt")
    natural_tuning = _snapshot_text(verified, "Data/tune_nat.txt")
    if 'include "Data/vel_08.txt" lovel=57 hivel=64' not in notes:
        raise _source_failure("Pinned velocity 64 is not mapped to v8.")
    offsets = {
        match.group("label"): int(match.group("frames"))
        for match in OFFSET_PATTERN.finditer(velocity)
    }
    tunes = {
        match.group("label"): int(match.group("cents"))
        for match in TUNE_PATTERN.finditer(natural_tuning)
    }
    mappings: list[RegionMapping] = []
    for match in REGION_PATTERN.finditer(regions):
        label = match.group("label")
        if label not in offsets or tunes.get(label) != 0:
            raise _source_failure(f"Pinned Natural mapping {label} is incomplete or retuned.")
        mappings.append(
            RegionMapping(
                region=label,
                low_midi=int(match.group("low")),
                high_midi=int(match.group("high")),
                source_midi=int(match.group("center")),
                source_sample=f"Samples/{match.group('sample')}v8{profile.sample_suffix}",
                source_offset_frames=offsets[label],
            )
        )
    if len(mappings) != 30 or mappings[0].low_midi != 21 or mappings[-1].high_midi != 108:
        raise _source_failure("Pinned mapping does not cover the 88-key piano range.")
    covered = [
        midi for mapping in mappings for midi in range(mapping.low_midi, mapping.high_midi + 1)
    ]
    if covered != list(range(21, 109)):
        raise _source_failure("Pinned regions are not an exact, ordered MIDI 21-108 mapping.")
    return mappings


def _tool_identity(path: Path) -> tuple[tuple[int, int, int, int], str]:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        identity = os.fstat(descriptor)
        if not identity.st_mode & 0o111:
            raise OSError(f"Conversion tool is not executable: {path}")
        return (
            (identity.st_dev, identity.st_ino, identity.st_size, identity.st_mtime_ns),
            _sha256(storage.read_open_file(descriptor)),
        )
    except OSError as error:
        raise BuildFailure(
            "tool_unavailable", "Required conversion tool is unavailable.", str(error)
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _verify_bound_tool(tool: BoundTool) -> None:
    identity, sha256 = _tool_identity(tool.path)
    executable_identity, executable_sha256 = _tool_identity(tool.executable_path)
    if (
        identity != tool.identity
        or sha256 != tool.sha256
        or executable_identity != tool.executable_identity
        or executable_sha256 != tool.sha256
    ):
        raise BuildFailure(
            "tool_integrity_failed",
            "Conversion tool identity changed during the build.",
            str(tool.path),
        )


def _run_bound_tool(
    tool: BoundTool,
    arguments: list[str],
    *,
    input_bytes: bytes | None = None,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    _verify_bound_tool(tool)
    try:
        result = subprocess.run(
            [str(tool.executable_path), *arguments],
            input=input_bytes,
            capture_output=True,
            check=False,
            pass_fds=pass_fds,
        )
    except OSError as error:
        raise BuildFailure(
            "tool_unavailable", "Required conversion tool is unavailable.", str(error)
        ) from error
    _verify_bound_tool(tool)
    return result


def _bind_tool(executable: Path, workspace: Path, snapshot_name: str) -> BoundTool:
    try:
        resolved = executable.resolve(strict=True)
    except OSError as error:
        raise BuildFailure(
            "tool_unavailable", "Required conversion tool is unavailable.", str(error)
        ) from error
    identity, sha256 = _tool_identity(resolved)
    snapshot_path = workspace / snapshot_name
    source_fd: int | None = None
    snapshot_fd: int | None = None
    try:
        source_fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        source_identity = os.fstat(source_fd)
        source_bytes = storage.read_open_file(source_fd)
        if (
            source_identity.st_dev,
            source_identity.st_ino,
            source_identity.st_size,
            source_identity.st_mtime_ns,
        ) != identity or _sha256(source_bytes) != sha256:
            raise OSError("Conversion tool changed while it was copied.")
        snapshot_fd = os.open(
            snapshot_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o500,
        )
        view = memoryview(source_bytes)
        while view:
            written = os.write(snapshot_fd, view)
            view = view[written:]
        os.fsync(snapshot_fd)
        os.fchmod(snapshot_fd, 0o500)
    except OSError as error:
        raise BuildFailure(
            "tool_unavailable", "Required conversion tool is unavailable.", str(error)
        ) from error
    finally:
        if snapshot_fd is not None:
            os.close(snapshot_fd)
        if source_fd is not None:
            os.close(source_fd)
    executable_identity, executable_sha256 = _tool_identity(snapshot_path)
    if executable_sha256 != sha256:
        raise BuildFailure(
            "tool_integrity_failed",
            "Conversion tool identity changed during the build.",
            str(resolved),
        )
    provisional = BoundTool(
        resolved,
        snapshot_path,
        identity,
        executable_identity,
        sha256,
        "",
    )
    result = _run_bound_tool(provisional, ["-version"])
    if result.returncode != 0 or not result.stdout.splitlines():
        raise BuildFailure(
            "tool_unavailable",
            "Required conversion tool is unavailable.",
            result.stderr.decode(errors="replace"),
        )
    return BoundTool(
        resolved,
        snapshot_path,
        identity,
        executable_identity,
        sha256,
        result.stdout.decode(errors="strict").rstrip(),
    )


def _probe_source(
    ffprobe: BoundTool, source_name: str, source_data: bytes, profile: SourceProfile
) -> None:
    result = _run_bound_tool(
        ffprobe,
        [
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,bits_per_raw_sample",
            "-of",
            "json",
            "pipe:0",
        ],
        input_bytes=source_data,
    )
    try:
        parsed: object = json.loads(result.stdout)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("streams"), list):
            raise TypeError("missing streams")
        streams = parsed["streams"]
        if len(streams) != 1 or not isinstance(streams[0], dict):
            raise TypeError("invalid stream")
        stream = streams[0]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise _source_failure(f"Cannot inspect source audio: {source_name}") from error
    expected = {
        "codec_name": profile.codec_name,
        "sample_rate": "48000",
        "channels": 2,
        "bits_per_raw_sample": "24",
    }
    if result.returncode != 0 or any(stream.get(key) != value for key, value in expected.items()):
        raise _source_failure(f"Source audio format is incompatible: {source_name}")


def _convert_sample(
    ffmpeg: BoundTool,
    source_data: bytes,
    destination_fd: int,
    *,
    offset_frames: int,
    pitch_semitones: int,
) -> None:
    shifted_rate = SAMPLE_RATE * 2 ** (pitch_semitones / 12)
    filters = (
        f"atrim=start_sample={offset_frames},asetpts=PTS-STARTPTS,"
        "pan=mono|c0=0.5*c0+0.5*c1,"
        f"asetrate={shifted_rate:.12f},"
        f"aresample={SAMPLE_RATE}:dither_method=none,"
        "afade=t=in:st=0:d=0.005"
    )
    result = _run_bound_tool(
        ffmpeg,
        [
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-i",
            "pipe:0",
            "-af",
            filters,
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            f"/dev/fd/{destination_fd}",
        ],
        input_bytes=source_data,
        pass_fds=(destination_fd,),
    )
    if result.returncode != 0:
        raise BuildFailure(
            "conversion_failed",
            "Salamander sample conversion failed.",
            result.stderr.decode(errors="replace"),
        )


def _sample_metadata(name: str, descriptor: int) -> tuple[dict[str, object], bytes]:
    data = storage.read_open_file(descriptor)
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            channels = audio.getnchannels()
            width = audio.getsampwidth()
            rate = audio.getframerate()
            frames = audio.getnframes()
            frame_data = audio.readframes(frames)
    except (EOFError, OSError, wave.Error) as error:
        raise BuildFailure(
            "conversion_failed", "Converted WAV is unreadable.", str(error)
        ) from error
    if channels != 1 or width != 2 or rate != SAMPLE_RATE or len(frame_data) != frames * 2:
        raise BuildFailure(
            "conversion_failed",
            "Converted WAV does not satisfy the renderer contract.",
            name,
        )
    return (
        {
            "path": f"samples/{name}",
            "sha256": _sha256(data),
            "byte_size": len(data),
            "format": "PCM_S16LE",
            "sample_rate_hz": SAMPLE_RATE,
            "channels": 1,
            "frames": frames,
        },
        data,
    )


def _json_bytes(value: dict[str, object]) -> bytes:
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return data


def _same_directory(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(left.st_mode)
        and stat.S_ISDIR(right.st_mode)
        and (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
    )


def _verify_output_files(
    root_fd: int,
    samples_fd: int,
    top_files: dict[str, bytes],
    sample_files: dict[str, bytes],
) -> None:
    root_entries = {entry.name: entry for entry in storage.list_directory(root_fd)}
    if set(root_entries) != {*top_files, "samples"}:
        raise BuildFailure(
            "build_integrity_failed", "Built Sound Pack failed verification.", "root"
        )
    samples_stat = storage.stat_at(root_fd, "samples")
    if samples_stat is None or not _same_directory(samples_stat, os.fstat(samples_fd)):
        raise BuildFailure(
            "build_integrity_failed", "Built Sound Pack failed verification.", "samples binding"
        )
    if (
        stat.S_IMODE(os.fstat(root_fd).st_mode) != 0o755
        or stat.S_IMODE(os.fstat(samples_fd).st_mode) != 0o755
    ):
        raise BuildFailure(
            "build_integrity_failed", "Built Sound Pack failed verification.", "directory mode"
        )

    def verify_file(directory_fd: int, name: str, expected: bytes, mode: int) -> None:
        descriptor: int | None = None
        try:
            descriptor = storage.open_regular_file_at(directory_fd, name)
            identity = os.fstat(descriptor)
            if (
                not stat.S_ISREG(identity.st_mode)
                or identity.st_nlink != 1
                or stat.S_IMODE(identity.st_mode) != mode
                or storage.read_open_file(descriptor) != expected
            ):
                raise BuildFailure(
                    "build_integrity_failed",
                    "Built Sound Pack failed verification.",
                    name,
                )
        except OSError as error:
            raise BuildFailure(
                "build_integrity_failed",
                "Built Sound Pack failed verification.",
                f"{name}: {error}",
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)

    for name, data in top_files.items():
        verify_file(root_fd, name, data, 0o600)
    sample_entries = {entry.name for entry in storage.list_directory(samples_fd)}
    if sample_entries != set(sample_files):
        raise BuildFailure(
            "build_integrity_failed", "Built Sound Pack failed verification.", "sample inventory"
        )
    for name, data in sample_files.items():
        verify_file(samples_fd, name, data, 0o644)


def build(
    source: Path,
    output: Path,
    ffmpeg: Path,
    ffprobe: Path,
    source_lock: Path,
    expected_lock_sha256: str,
) -> dict[str, object]:
    output = storage.absolute_path(output)
    if not output.name or output.name in {".", ".."}:
        raise BuildFailure("build_failed", "Standard Piano build failed.", "Invalid output path.")
    profile, lock, lock_bytes = _load_source_profile(source_lock, expected_lock_sha256)
    builder_sha256 = _sha256(Path(__file__).read_bytes())
    verified_files = _verify_source(source, lock)
    mappings = _parse_mapping(verified_files, profile)
    tool_workspace = tempfile.TemporaryDirectory(prefix="playcli-bound-tools-")
    try:
        tool_root = Path(tool_workspace.name)
        bound_ffmpeg = _bind_tool(ffmpeg, tool_root, "ffmpeg")
        bound_ffprobe = _bind_tool(ffprobe, tool_root, "ffprobe")
        for mapping in mappings:
            snapshot = verified_files.get(mapping.source_sample)
            if snapshot is None:
                raise _source_failure(f"Mapped source is absent from lock: {mapping.source_sample}")
            _probe_source(bound_ffprobe, mapping.source_sample, snapshot.data, profile)
    except Exception:
        tool_workspace.cleanup()
        raise

    modifications = (
        [
            "Selected upstream Natural tuning and velocity layer v8 because PLAY CLI "
            "Neutral velocity 64 maps to v8 (57-64).",
            "Applied each v8 region's declared source start offset before conversion.",
            "Expanded 30 minor-third root samples to exact MIDI 21-108 files using at "
            "most one semitone of offline pitch shifting.",
            "Downmixed stereo to mono with equal 0.5/0.5 channel coefficients, converted "
            "24-bit FLAC to PCM S16LE WAV, retained 48 kHz, disabled dither, and applied "
            "a 5 ms fade-in.",
            "Omitted the other 15 velocity layers, Retuned mapping, hammer-release, "
            "string-resonance, and pedal samples; no runtime pedal, resonance, or noise "
            "behavior is represented.",
        ]
        if profile.release
        else [
            "Generated test-only stereo PCM24 source WAVs from the repository CC0 fixture.",
            "Expanded 30 identical test anchors to MIDI 21-108 and converted to the "
            "renderer PCM16 contract; no production audio is present.",
        ]
    )
    source_note = (
        "# PLAY CLI Standard Piano Sound Pack source\n\n"
        f"{profile.attribution}\n\n"
        f"License: {profile.license_id}. Source: {lock.repository} at revision {lock.revision}.\n\n"
        "This is a modified, single-layer PLAY CLI derivative. Modifications:\n\n"
        + "".join(f"- {item}\n" for item in modifications)
    )
    parent_fd: int | None = None
    pending_fd: int | None = None
    samples_fd: int | None = None
    pending_name = f".{output.name}.build-pending-{secrets.token_hex(8)}"
    try:
        parent_fd = storage.open_or_create_directory(output.parent)
        if storage.stat_at(parent_fd, output.name) is not None:
            raise BuildFailure("output_exists", "Output path already exists.", str(output))
        if storage.stat_at(parent_fd, pending_name) is not None:
            raise BuildFailure("output_exists", "Build-pending path already exists.", pending_name)
        os.mkdir(pending_name, dir_fd=parent_fd)
        pending_fd = storage.open_directory_at(parent_fd, pending_name)
        os.fchmod(pending_fd, 0o755)
        os.mkdir("samples", dir_fd=pending_fd)
        samples_fd = storage.open_directory_at(pending_fd, "samples")
        os.fchmod(samples_fd, 0o755)
        pending_identity = os.fstat(pending_fd)
        samples_identity = os.fstat(samples_fd)
        derived_samples: dict[str, object] = {}
        derived_mappings: list[dict[str, object]] = []
        rendered_samples: dict[str, bytes] = {}
        for mapping in mappings:
            snapshot = verified_files[mapping.source_sample]
            for midi in range(mapping.low_midi, mapping.high_midi + 1):
                name = f"midi-{midi}.wav"
                descriptor: int | None = None
                try:
                    descriptor = os.open(
                        name,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL,
                        0o644,
                        dir_fd=samples_fd,
                    )
                    semitones = midi - mapping.source_midi
                    _convert_sample(
                        bound_ffmpeg,
                        snapshot.data,
                        descriptor,
                        offset_frames=mapping.source_offset_frames,
                        pitch_semitones=semitones,
                    )
                    metadata, sample_data = _sample_metadata(name, descriptor)
                    os.fchmod(descriptor, 0o644)
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
                derived_samples[str(midi)] = metadata
                rendered_samples[name] = sample_data
                derived_mappings.append(
                    {
                        "target_midi": midi,
                        "source_midi": mapping.source_midi,
                        "source_sample": mapping.source_sample,
                        "source_sha256": snapshot.contract.sha256,
                        "source_offset_frames": mapping.source_offset_frames,
                        "natural_tune_cents": 0,
                        "pitch_shift_semitones": semitones,
                        "output_sha256": metadata["sha256"],
                    }
                )
        storage.write_new_file_at(pending_fd, "LICENSE", verified_files["LICENSE"].data)
        storage.write_new_file_at(pending_fd, "SOURCE.md", source_note.encode())
        receipt: dict[str, object] = {
            "schema_version": "1.0",
            "builder": {"version": "1.1", "sha256": builder_sha256},
            "source": {
                "repository": lock.repository,
                "revision": lock.revision,
                "tree": lock.tree,
                "acquisition_method": lock.acquisition_method,
                "tree_api_uri": lock.tree_api_uri,
                "blob_api_uri_template": lock.blob_api_uri_template,
                "archive_used": lock.archive_used,
                "source_lock_sha256": _sha256(lock_bytes),
                "selected_bytes": lock.selected_bytes,
                "files": [item.as_json() for item in lock.files],
            },
            "conversion": {
                "ffmpeg": bound_ffmpeg.as_json(),
                "ffprobe": bound_ffprobe.as_json(),
                "platform": platform.platform(),
                "python": sys.version,
                "input": profile.input_description,
                "output": "mono PCM_S16LE WAV, 48000 Hz",
                "downmix": "0.5*left + 0.5*right",
                "dither": "none",
                "fade_in_ms": 5,
                "velocity": 64,
                "velocity_layer": "v8",
                "mappings": derived_mappings,
                "omitted_layers": (
                    [
                        "velocity v1-v7 and v9-v16",
                        "Retuned mapping",
                        "hammer release",
                        "string resonance",
                        "pedal noise",
                    ]
                    if profile.release
                    else []
                ),
            },
            "modifications": modifications,
        }
        receipt_bytes = _json_bytes(receipt)
        storage.write_new_file_at(pending_fd, "SOURCE-RECEIPT.json", receipt_bytes)
        build_record: dict[str, object] = {
            "builder_sha256": builder_sha256,
            "source_repository": lock.repository,
            "source_revision": lock.revision,
            "source_tree": lock.tree,
            "source_acquisition": {
                "method": lock.acquisition_method,
                "tree_api_uri": lock.tree_api_uri,
                "blob_api_uri_template": lock.blob_api_uri_template,
                "archive_used": lock.archive_used,
            },
            "source_lock_sha256": _sha256(lock_bytes),
            "source_receipt_sha256": _sha256(receipt_bytes),
            "selected_source_bytes": lock.selected_bytes,
            "velocity": 64,
            "velocity_layer": "v8",
            "conversion": receipt["conversion"],
        }
        manifest: dict[str, object] = {
            "schema_version": "1.2",
            "id": profile.pack_id,
            "version": profile.pack_version,
            "license": {
                "id": profile.license_id,
                "file": "LICENSE",
                "sha256": verified_files["LICENSE"].contract.sha256,
                "complete_text": True,
            },
            "source": {
                "uri": lock.repository,
                "revision": {"type": "commit", "value": lock.revision},
            },
            "attribution": profile.attribution,
            "modifications": modifications,
            "generator": {
                "command": (
                    "python tools/build_salamander_standard_piano_pack.py "
                    "<source> --output <output>"
                ),
                "version": "1.1",
                "sha256": builder_sha256,
            },
            "build": build_record,
            "samples": derived_samples,
        }
        manifest_bytes = _json_bytes(manifest)
        storage.write_new_file_at(pending_fd, "manifest.json", manifest_bytes)
        top_files = {
            "LICENSE": verified_files["LICENSE"].data,
            "SOURCE.md": source_note.encode(),
            "SOURCE-RECEIPT.json": receipt_bytes,
            "manifest.json": manifest_bytes,
        }

        def commit_guard() -> None:
            pending_binding = storage.stat_at(parent_fd, pending_name)
            if pending_binding is None or not _same_directory(pending_binding, pending_identity):
                raise BuildFailure(
                    "build_integrity_failed",
                    "Built Sound Pack failed verification.",
                    "pending binding",
                )
            _verify_bound_tool(bound_ffmpeg)
            _verify_bound_tool(bound_ffprobe)
            _verify_output_files(pending_fd, samples_fd, top_files, rendered_samples)

        storage.rename_noreplace(
            parent_fd,
            pending_name,
            parent_fd,
            output.name,
            commit_guard=commit_guard,
        )
        output_binding = storage.stat_at(parent_fd, output.name)
        if output_binding is None or not _same_directory(output_binding, pending_identity):
            raise BuildFailure(
                "build_integrity_failed",
                "Built Sound Pack failed verification.",
                "published binding",
            )
        published_fd: int | None = None
        published_samples_fd: int | None = None
        try:
            published_fd = storage.open_directory_at(parent_fd, output.name)
            if not _same_directory(os.fstat(published_fd), pending_identity):
                raise BuildFailure(
                    "build_integrity_failed",
                    "Built Sound Pack failed verification.",
                    "published directory",
                )
            published_samples_fd = storage.open_directory_at(published_fd, "samples")
            if not _same_directory(os.fstat(published_samples_fd), samples_identity):
                raise BuildFailure(
                    "build_integrity_failed",
                    "Built Sound Pack failed verification.",
                    "published samples",
                )
            _verify_output_files(published_fd, published_samples_fd, top_files, rendered_samples)
            final_output_binding = storage.stat_at(parent_fd, output.name)
            if final_output_binding is None or not _same_directory(
                final_output_binding, pending_identity
            ):
                raise BuildFailure(
                    "build_integrity_failed",
                    "Built Sound Pack failed verification.",
                    "final published binding",
                )
            final_samples_binding = storage.stat_at(published_fd, "samples")
            if final_samples_binding is None or not _same_directory(
                final_samples_binding, samples_identity
            ):
                raise BuildFailure(
                    "build_integrity_failed",
                    "Built Sound Pack failed verification.",
                    "final published samples",
                )
            public_parent_fd = storage.open_directory(output.parent)
            try:
                if not _same_directory(os.fstat(public_parent_fd), os.fstat(parent_fd)):
                    raise BuildFailure(
                        "build_integrity_failed",
                        "Built Sound Pack failed verification.",
                        "final published parent",
                    )
            finally:
                os.close(public_parent_fd)
        finally:
            if published_samples_fd is not None:
                os.close(published_samples_fd)
            if published_fd is not None:
                os.close(published_fd)
    finally:
        if samples_fd is not None:
            os.close(samples_fd)
        if pending_fd is not None:
            os.close(pending_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        tool_workspace.cleanup()
    return {
        "schema_version": BUILDER_SCHEMA_VERSION,
        "operation": BUILDER_OPERATION,
        "status": "built",
        "output": str(output),
        "sound_pack": {"id": profile.pack_id, "version": profile.pack_version},
        "source_revision": lock.revision,
        "source_lock_sha256": _sha256(lock_bytes),
        "source_receipt_sha256": _sha256(receipt_bytes),
        "manifest_sha256": _sha256(manifest_bytes),
        "samples": len(derived_samples),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-lock", type=Path, default=SOURCE_LOCK)
    parser.add_argument("--expected-lock-sha256", default=APPROVED_SOURCE_LOCK_SHA256)
    parser.add_argument("--ffmpeg", type=Path, default=Path(shutil.which("ffmpeg") or "ffmpeg"))
    parser.add_argument("--ffprobe", type=Path, default=Path(shutil.which("ffprobe") or "ffprobe"))
    arguments = parser.parse_args()
    try:
        report = build(
            arguments.source,
            arguments.output,
            arguments.ffmpeg,
            arguments.ffprobe,
            arguments.source_lock,
            arguments.expected_lock_sha256,
        )
    except BuildFailure as error:
        print(
            json.dumps(
                {
                    "schema_version": BUILDER_SCHEMA_VERSION,
                    "operation": BUILDER_OPERATION,
                    "status": "failed",
                    "error": {"id": error.error_id, "message": error.message},
                },
                separators=(",", ":"),
            )
        )
        print(f"Standard Piano build failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(
            json.dumps(
                {
                    "schema_version": BUILDER_SCHEMA_VERSION,
                    "operation": BUILDER_OPERATION,
                    "status": "failed",
                    "error": {"id": "build_failed", "message": "Standard Piano build failed."},
                },
                separators=(",", ":"),
            )
        )
        print(f"Standard Piano build failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
