from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from play_cli import sound_pack_storage as storage
from play_cli.audio import PcmWavValidationError, validate_pcm16_mono_wav
from play_cli.voice_audio import MAX_OUTPUT_BYTES, MAX_VOICES, validate_voice

BUNDLE_SCHEMA_VERSION = "1.2"
PAYLOAD_ARTIFACTS = (
    "score.musicxml",
    "validation.json",
    "schedule.json",
    "performance.wav",
)
REQUIRED_ARTIFACTS = (*PAYLOAD_ARTIFACTS, "result.json")
_MANIFEST_ARTIFACT_NAMES = {
    "score": "score.musicxml",
    "validation": "validation.json",
    "schedule": "schedule.json",
    "performance": "performance.wav",
}
_SEALED_FILE_MODE = 0o444
_SEALED_DIRECTORY_MODE = 0o555


class ResultBundleFailure(Exception):
    def __init__(self, error_id: str, message: str, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.error_id = error_id
        self.message = message


@dataclass(frozen=True)
class ArtifactIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass
class PublishedResultBundle:
    path: Path
    manifest: dict[str, Any]
    parent_fd: int
    directory_fd: int
    parent_inode: tuple[int, int]
    bundle_inode: tuple[int, int]
    descriptors: dict[str, int]
    identities: dict[str, ArtifactIdentity]
    artifacts: dict[str, bytes]
    _closed: bool = False

    def confirm(self) -> None:
        try:
            _confirm_retained_snapshot(self)
        except ResultBundleFailure:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ResultBundleFailure(
                "result_bundle_failed",
                "Result Bundle could not be read.",
                f"Published Result Bundle validation failed: {error}.",
            ) from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in self.descriptors.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        for descriptor in (self.directory_fd, self.parent_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


@dataclass
class PendingResultBundle:
    path: Path
    destination: Path
    parent_fd: int
    pending_fd: int
    parent_inode: tuple[int, int]
    pending_inode: tuple[int, int]
    created_artifacts: dict[str, ArtifactIdentity] = field(default_factory=dict)
    _closed: bool = False

    def write_bytes(self, name: str, data: bytes) -> Path:
        if name not in PAYLOAD_ARTIFACTS and not re.fullmatch(r"voice-\d{4}\.wav", name):
            raise ResultBundleFailure(
                "result_bundle_failed",
                "Result Bundle could not be completed.",
                f"Unexpected Result Bundle payload artifact: {name}.",
            )
        try:
            created = storage.write_new_file_at(self.pending_fd, name, data)
            self.created_artifacts[name] = _artifact_identity(created)
        except OSError as error:
            raise ResultBundleFailure(
                "result_bundle_failed",
                "Result Bundle could not be completed.",
                f"Cannot write Result Bundle artifact {name}: {error}.",
            ) from error
        return self.path / name

    def publish(
        self,
        manifest: dict[str, Any],
        *,
        commit_guard: Callable[[], None] | None = None,
    ) -> Path:
        try:
            _validate_bundle_paths(manifest, self.destination)
            manifest_data = encode_json(manifest)
            created = storage.write_new_file_at(self.pending_fd, "result.json", manifest_data)
            self.created_artifacts["result.json"] = _artifact_identity(created)
            _seal_complete_bundle(
                self.pending_fd,
                manifest,
                manifest_data,
                self.created_artifacts,
            )
            self._publish_sealed_directory(commit_guard)
            self._confirm_published_binding()
        except ResultBundleFailure:
            raise
        except OSError as error:
            raise ResultBundleFailure(
                "result_bundle_failed",
                "Result Bundle could not be completed.",
                f"Cannot publish Result Bundle: {error}.",
            ) from error
        return self.destination

    def _publish_sealed_directory(self, commit_guard: Callable[[], None] | None) -> None:
        if _inode(os.fstat(self.pending_fd)) != self.pending_inode:
            raise OSError("Pending Result Bundle descriptor changed")
        if stat.S_IMODE(os.fstat(self.pending_fd).st_mode) != _SEALED_DIRECTORY_MODE:
            raise OSError("Pending Result Bundle is not sealed")
        pending_entry = storage.stat_at(self.parent_fd, self.path.name)
        if (
            pending_entry is None
            or not stat.S_ISDIR(pending_entry.st_mode)
            or _inode(pending_entry) != self.pending_inode
            or stat.S_IMODE(pending_entry.st_mode) != _SEALED_DIRECTORY_MODE
        ):
            raise OSError("Pending Result Bundle binding changed")
        _confirm_public_directory(self.destination.parent, self.parent_inode)
        if storage.stat_at(self.parent_fd, self.destination.name) is not None:
            raise ResultBundleFailure(
                "result_bundle_exists",
                "Result Bundle output already exists.",
                f"Refusing to replace existing output {self.destination}.",
            )
        if commit_guard is None:
            storage.rename_noreplace(
                self.parent_fd,
                self.path.name,
                self.parent_fd,
                self.destination.name,
            )
        else:
            storage.rename_noreplace(
                self.parent_fd,
                self.path.name,
                self.parent_fd,
                self.destination.name,
                commit_guard=commit_guard,
            )

    def _confirm_published_binding(self) -> None:
        public_fd = _open_public_bundle(
            self.destination,
            self.parent_inode,
            self.pending_inode,
        )
        try:
            if stat.S_IMODE(os.fstat(public_fd).st_mode) != _SEALED_DIRECTORY_MODE:
                raise OSError("Published Result Bundle is not sealed")
        finally:
            os.close(public_fd)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in (self.pending_fd, self.parent_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


def encode_json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _read_bundle_file(descriptor: int, name: str, expected_size: int | None = None) -> bytes:
    limit = MAX_OUTPUT_BYTES if name.endswith(".wav") else 16 * 1024 * 1024
    size = os.fstat(descriptor).st_size
    if size > limit or (expected_size is not None and size != expected_size):
        raise OSError("Bundle artifact size differs or exceeds resource limit")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            raise OSError("Bundle artifact was truncated during read")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise OSError("Bundle artifact grew during read")
    return b"".join(chunks)


def prepare_result_bundle(destination: Path) -> PendingResultBundle:
    absolute = storage.absolute_path(destination)
    parent_fd: int | None = None
    try:
        parent_fd = storage.open_or_create_directory(absolute.parent)
        parent_inode = _inode(os.fstat(parent_fd))
    except OSError as error:
        if parent_fd is not None:
            os.close(parent_fd)
        raise ResultBundleFailure(
            "result_bundle_failed",
            "Result Bundle could not be completed.",
            f"Cannot prepare Result Bundle: {error}.",
        ) from error
    return _prepare_result_bundle_at(absolute, parent_fd, parent_inode)


def prepare_result_bundle_in_parent(
    destination: Path,
    retained_parent_fd: int,
    expected_parent_inode: tuple[int, int],
) -> PendingResultBundle:
    absolute = storage.absolute_path(destination)
    parent_fd: int | None = None
    try:
        parent_fd = os.dup(retained_parent_fd)
        if _inode(os.fstat(parent_fd)) != expected_parent_inode:
            raise OSError("Retained Result Bundle parent identity changed")
    except OSError as error:
        if parent_fd is not None:
            os.close(parent_fd)
        raise ResultBundleFailure(
            "result_bundle_failed",
            "Result Bundle could not be completed.",
            f"Cannot retain Result Bundle parent: {error}.",
        ) from error
    return _prepare_result_bundle_at(absolute, parent_fd, expected_parent_inode)


def _prepare_result_bundle_at(
    absolute: Path,
    parent_fd: int,
    parent_inode: tuple[int, int],
) -> PendingResultBundle:
    pending = absolute.parent / f".{absolute.name}.pending"
    pending_fd: int | None = None
    try:
        if storage.stat_at(parent_fd, absolute.name) is not None:
            raise ResultBundleFailure(
                "result_bundle_exists",
                "Result Bundle output already exists.",
                f"Refusing to replace existing output {absolute}.",
            )
        if storage.stat_at(parent_fd, pending.name) is not None:
            raise ResultBundleFailure(
                "result_bundle_pending",
                "Result Bundle has an incomplete pending attempt.",
                f"Pending Result Bundle requires inspection: {pending}.",
            )
        os.mkdir(pending.name, dir_fd=parent_fd)
        pending_fd = storage.open_directory_at(parent_fd, pending.name)
        pending_inode = _inode(os.fstat(pending_fd))
    except ResultBundleFailure:
        if pending_fd is not None:
            os.close(pending_fd)
        os.close(parent_fd)
        raise
    except OSError as error:
        if pending_fd is not None:
            os.close(pending_fd)
        os.close(parent_fd)
        raise ResultBundleFailure(
            "result_bundle_failed",
            "Result Bundle could not be completed.",
            f"Cannot prepare Result Bundle: {error}.",
        ) from error
    assert pending_fd is not None
    return PendingResultBundle(
        path=pending,
        destination=absolute,
        parent_fd=parent_fd,
        pending_fd=pending_fd,
        parent_inode=parent_inode,
        pending_inode=pending_inode,
    )


def open_published_result_bundle(path: Path) -> PublishedResultBundle:
    absolute = storage.absolute_path(path)
    parent_fd: int | None = None
    directory_fd: int | None = None
    descriptors: dict[str, int] = {}
    try:
        parent_fd = storage.open_directory(absolute.parent)
        parent_inode = _inode(os.fstat(parent_fd))
        directory_fd = storage.open_directory_at(parent_fd, absolute.name)
        bundle_inode = _inode(os.fstat(directory_fd))

        identities: dict[str, ArtifactIdentity] = {}
        artifacts: dict[str, bytes] = {}
        for name in REQUIRED_ARTIFACTS:
            descriptor = storage.open_regular_file_at(directory_fd, name)
            descriptors[name] = descriptor
            artifact_stat = os.fstat(descriptor)
            identities[name] = _artifact_identity(artifact_stat)
            artifacts[name] = _read_bundle_file(descriptor, name)

        manifest_value = json.loads(artifacts["result.json"])
        if not isinstance(manifest_value, dict):
            raise OSError("Published Result Bundle manifest is not an object")
        manifest: dict[str, Any] = manifest_value
        for name, voice in _voice_contracts(manifest).items():
            descriptor = storage.open_regular_file_at(directory_fd, name)
            descriptors[name] = descriptor
            artifact_stat = os.fstat(descriptor)
            if artifact_stat.st_size > MAX_OUTPUT_BYTES:
                raise OSError("Voice artifact exceeds resource limit")
            identities[name] = _artifact_identity(artifact_stat)
            artifacts[name] = _read_bundle_file(descriptor, name, 56 + voice["frames"] * 8)
        snapshot = PublishedResultBundle(
            path=absolute,
            manifest=manifest,
            parent_fd=parent_fd,
            directory_fd=directory_fd,
            parent_inode=parent_inode,
            bundle_inode=bundle_inode,
            descriptors=descriptors,
            identities=identities,
            artifacts=artifacts,
        )
        snapshot.confirm()
        return snapshot
    except BaseException as error:
        for descriptor in descriptors.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        for optional_descriptor in (directory_fd, parent_fd):
            if optional_descriptor is not None:
                try:
                    os.close(optional_descriptor)
                except OSError:
                    pass
        if isinstance(error, ResultBundleFailure):
            raise
        if isinstance(error, (OSError, UnicodeDecodeError, json.JSONDecodeError)):
            raise ResultBundleFailure(
                "result_bundle_failed",
                "Result Bundle could not be read.",
                f"Published Result Bundle validation failed: {error}.",
            ) from error
        raise


def read_published_result_bundle(path: Path) -> dict[str, Any]:
    snapshot = open_published_result_bundle(path)
    try:
        return snapshot.manifest
    finally:
        snapshot.close()


def _confirm_retained_snapshot(snapshot: PublishedResultBundle) -> None:
    if _inode(os.fstat(snapshot.parent_fd)) != snapshot.parent_inode:
        raise OSError("Published Result Bundle parent descriptor changed")
    directory_stat = os.fstat(snapshot.directory_fd)
    if (
        _inode(directory_stat) != snapshot.bundle_inode
        or stat.S_IMODE(directory_stat.st_mode) != _SEALED_DIRECTORY_MODE
    ):
        raise OSError("Published Result Bundle directory identity or seal changed")
    public_entry = storage.stat_at(snapshot.parent_fd, snapshot.path.name)
    if (
        public_entry is None
        or not stat.S_ISDIR(public_entry.st_mode)
        or _inode(public_entry) != snapshot.bundle_inode
        or stat.S_IMODE(public_entry.st_mode) != _SEALED_DIRECTORY_MODE
    ):
        raise OSError("Published Result Bundle binding changed")

    expected_names = set(REQUIRED_ARTIFACTS) | set(_voice_contracts(snapshot.manifest))
    entries = storage.list_directory(snapshot.directory_fd)
    if {entry.name for entry in entries} != expected_names or any(
        not stat.S_ISREG(entry.mode) or stat.S_IMODE(entry.mode) != _SEALED_FILE_MODE
        for entry in entries
    ):
        raise OSError("Published Result Bundle sealed inventory changed")

    current_artifacts: dict[str, bytes] = {}
    for name, expected_identity in snapshot.identities.items():
        current = storage.stat_at(snapshot.directory_fd, name)
        descriptor_stat = os.fstat(snapshot.descriptors[name])
        if (
            current is None
            or not stat.S_ISREG(current.st_mode)
            or stat.S_IMODE(current.st_mode) != _SEALED_FILE_MODE
            or current.st_nlink != 1
            or _artifact_identity(current) != expected_identity
            or _artifact_identity(descriptor_stat) != expected_identity
        ):
            raise OSError(f"Published Result Bundle artifact binding changed: {name}")
        current_artifacts[name] = _read_bundle_file(
            snapshot.descriptors[name], name, expected_identity.size
        )
    if current_artifacts != snapshot.artifacts:
        raise OSError("Published Result Bundle artifact content changed")

    manifest_value = json.loads(current_artifacts["result.json"])
    if not isinstance(manifest_value, dict) or manifest_value != snapshot.manifest:
        raise OSError("Published Result Bundle manifest changed")
    _validate_published_manifest_contract(manifest_value)
    _validate_bundle_paths(manifest_value, snapshot.path)
    _validate_bundle_data(current_artifacts, manifest_value, encode_json(manifest_value))
    _confirm_public_directory(snapshot.path.parent, snapshot.parent_inode)
    rebound_fd = _open_public_bundle(
        snapshot.path,
        snapshot.parent_inode,
        snapshot.bundle_inode,
    )
    os.close(rebound_fd)


def _inode(value: os.stat_result) -> tuple[int, int]:
    return (value.st_dev, value.st_ino)


def _artifact_identity(value: os.stat_result) -> ArtifactIdentity:
    return ArtifactIdentity(
        device=value.st_dev,
        inode=value.st_ino,
        size=value.st_size,
        modified_ns=value.st_mtime_ns,
        changed_ns=value.st_ctime_ns,
    )


def _confirm_public_directory(path: Path, expected_inode: tuple[int, int]) -> None:
    descriptor = storage.open_directory(path)
    try:
        if _inode(os.fstat(descriptor)) != expected_inode:
            raise OSError(f"Result Bundle parent binding changed: {path}")
    finally:
        os.close(descriptor)


def _open_public_bundle(
    destination: Path,
    expected_parent_inode: tuple[int, int],
    expected_bundle_inode: tuple[int, int],
) -> int:
    parent_fd = storage.open_directory(destination.parent)
    bundle_fd: int | None = None
    try:
        if _inode(os.fstat(parent_fd)) != expected_parent_inode:
            raise OSError(f"Result Bundle parent binding changed: {destination.parent}")
        bundle_fd = storage.open_directory_at(parent_fd, destination.name)
        if _inode(os.fstat(bundle_fd)) != expected_bundle_inode:
            raise OSError(f"Result Bundle binding changed: {destination}")
        public_entry = storage.stat_at(parent_fd, destination.name)
        if (
            public_entry is None
            or not stat.S_ISDIR(public_entry.st_mode)
            or _inode(public_entry) != expected_bundle_inode
        ):
            raise OSError(f"Result Bundle public binding changed: {destination}")
        return bundle_fd
    except BaseException:
        if bundle_fd is not None:
            os.close(bundle_fd)
        raise
    finally:
        os.close(parent_fd)


def _artifact_contract(manifest: dict[str, Any], key: str) -> dict[str, Any]:
    try:
        contract = manifest["result_bundle"]["artifacts"][key]
    except (KeyError, TypeError) as error:
        raise OSError(f"Result manifest has no {key} artifact contract") from error
    if not isinstance(contract, dict):
        raise OSError(f"Result manifest {key} artifact contract is invalid")
    return contract


def _voice_contracts(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    bundle = manifest.get("result_bundle", {})
    if not isinstance(bundle, dict):
        raise OSError("Invalid Bundle contract")
    voices = bundle.get("voices")
    if bundle.get("schema_version") != "1.2":
        if voices is not None:
            raise OSError("Legacy Bundle cannot declare voice artifacts")
        return {}
    if (manifest.get("schema_version"), manifest.get("cli")) != (
        "1.6",
        {"name": "play-cli", "version": "0.10.0"},
    ):
        raise OSError("Incompatible voice Bundle contract")
    if not isinstance(voices, list) or not 1 <= len(voices) <= MAX_VOICES:
        raise OSError("Invalid voice inventory")
    wav = manifest.get("wav", {})
    if not isinstance(wav, dict):
        raise OSError("Invalid voice Bundle WAV contract")
    result: dict[str, dict[str, Any]] = {}
    identities: set[str] = set()
    for index, voice in enumerate(voices, 1):
        name = f"voice-{index:04d}.wav"
        if not isinstance(voice, dict):
            raise OSError("Invalid voice artifact")
        identity = voice.get("voice")
        frames = voice.get("frames")
        rate = voice.get("sample_rate_hz")
        if (
            voice.get("relative_path") != name
            or voice.get("format") != "IEEE_FLOAT64_LE"
            or voice.get("channels") != 1
            or not isinstance(identity, str)
            or not identity
            or identity in identities
            or type(frames) is not int
            or not 0 <= frames <= 28_800_000
            or type(rate) is not int
            or not 0 < rate <= 192000
            or frames != wav.get("frames")
            or rate != wav.get("sample_rate_hz")
        ):
            raise OSError("Invalid voice identity, path or timeline")
        identities.add(identity)
        result[name] = voice
    if (
        sum(56 + item["frames"] * 8 for item in result.values()) + wav["frames"] * 2 + 44
        > MAX_OUTPUT_BYTES
    ):
        raise OSError("Voice inventory exceeds resource limit")
    return result


def _validate_published_manifest_contract(manifest: dict[str, Any]) -> None:
    result_bundle = manifest.get("result_bundle")
    cli = manifest.get("cli")
    if (
        manifest.get("schema_version") not in {"1.3", "1.4", "1.5", "1.6"}
        or manifest.get("operation") != "render"
        or manifest.get("status") != "ok"
        or not isinstance(result_bundle, dict)
        or result_bundle.get("schema_version") not in {"1.0", "1.1", "1.2"}
        or result_bundle.get("status") != "complete"
        or not isinstance(cli, dict)
        or cli.get("name") != "play-cli"
        or not isinstance(cli.get("version"), str)
        or not isinstance(manifest.get("renderer"), dict)
        or not isinstance(manifest.get("sound_pack"), dict)
        or not isinstance(manifest.get("warnings"), list)
    ):
        raise OSError("Published Result Bundle manifest contract is invalid")
    schema_version = manifest["schema_version"]
    if schema_version in {"1.5", "1.6"}:
        _validate_machine_metadata(manifest)
    elif "provenance" in manifest or "operation_timing" in manifest:
        raise OSError("Published Result Bundle mixes machine-contract versions")


def _validate_machine_metadata(manifest: dict[str, Any]) -> None:
    provenance = manifest.get("provenance")
    operation_timing = manifest.get("operation_timing")
    cli = manifest["cli"]
    renderer = manifest["renderer"]
    sound_pack = manifest["sound_pack"]
    if (
        not isinstance(provenance, dict)
        or set(provenance)
        != {"schema_version", "cli", "execution", "score", "renderer", "sound_pack"}
        or provenance.get("schema_version") != manifest.get("schema_version")
        or provenance.get("cli") != cli
        or provenance.get("execution")
        != {"location": "local", "network": "disabled", "telemetry": "disabled"}
        or provenance.get("renderer") != renderer
        or not isinstance(sound_pack, dict)
        or provenance.get("sound_pack")
        != {key: sound_pack.get(key) for key in ("id", "version", "pack_hash")}
        or provenance.get("score")
        != {
            "format": "musicxml",
            "sha256": _artifact_contract(manifest, "score").get("sha256"),
        }
        or not isinstance(operation_timing, dict)
        or set(operation_timing) != {"duration_ms", "scope"}
        or operation_timing.get("scope") != "through_manifest_payload_assembly"
    ):
        raise OSError("Published Result Bundle machine metadata is invalid")
    duration_ms = operation_timing["duration_ms"]
    if (
        not isinstance(duration_ms, (int, float))
        or isinstance(duration_ms, bool)
        or not math.isfinite(duration_ms)
        or duration_ms < 0
    ):
        raise OSError("Published Result Bundle operation timing is invalid")


def _validate_bundle_paths(manifest: dict[str, Any], destination: Path) -> None:
    try:
        bundle = manifest["result_bundle"]
        if not isinstance(bundle, dict):
            raise OSError("Result manifest Bundle contract is invalid")
        if bundle.get("path") != str(destination):
            raise OSError("Result manifest Bundle path is invalid")
        if bundle.get("manifest") != str(destination / "result.json"):
            raise OSError("Result manifest path is invalid")
        for key, name in _MANIFEST_ARTIFACT_NAMES.items():
            if _artifact_contract(manifest, key).get("path") != str(destination / name):
                raise OSError(f"Result manifest {key} artifact path is invalid")
    except (KeyError, TypeError) as error:
        raise OSError("Result manifest path contract is invalid") from error


def _validate_bundle_data(
    artifacts: dict[str, bytes], manifest: dict[str, Any], manifest_data: bytes
) -> None:
    try:
        for name, voice in _voice_contracts(manifest).items():
            data = artifacts[name]
            if hashlib.sha256(data).hexdigest() != voice.get("sha256"):
                raise OSError("Voice artifact integrity mismatch")
            validate_voice(data, voice["sample_rate_hz"], voice["frames"])
        for key, name in _MANIFEST_ARTIFACT_NAMES.items():
            data = artifacts[name]
            if not data:
                raise OSError(f"Artifact is empty: {name}")
            expected_hash = _artifact_contract(manifest, key).get("sha256")
            if expected_hash != hashlib.sha256(data).hexdigest():
                raise OSError(f"Artifact integrity mismatch: {name}")
        if artifacts["result.json"] != manifest_data:
            raise OSError("Result manifest read-back differs")
        if json.loads(artifacts["result.json"]) != manifest:
            raise OSError("Result manifest content differs")
        validation = json.loads(artifacts["validation.json"])
        schedule = json.loads(artifacts["schedule.json"])
        bundle = manifest["result_bundle"]
        if (
            not isinstance(validation, dict)
            or not isinstance(schedule, dict)
            or not isinstance(bundle, dict)
            or validation.get("schema_version") != bundle.get("schema_version")
            or schedule.get("schema_version") != bundle.get("schema_version")
        ):
            raise OSError("Result Bundle payload schemas are inconsistent")
        _validate_wav(artifacts["performance.wav"], manifest)
    except (
        KeyError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        PcmWavValidationError,
    ) as error:
        raise ResultBundleFailure(
            "result_bundle_failed",
            "Result Bundle could not be completed.",
            f"Result Bundle read-back failed: {error}.",
        ) from error


def _seal_complete_bundle(
    directory_fd: int,
    manifest: dict[str, Any],
    manifest_data: bytes,
    created_artifacts: dict[str, ArtifactIdentity],
) -> None:
    descriptors: dict[str, int] = {}
    try:
        expected_names = set(REQUIRED_ARTIFACTS) | set(_voice_contracts(manifest))
        if set(created_artifacts) != expected_names:
            raise OSError("Result Bundle creation inventory is incomplete")
        entries = storage.list_directory(directory_fd)
        if {entry.name for entry in entries} != expected_names or any(
            not stat.S_ISREG(entry.mode) for entry in entries
        ):
            raise OSError("Result Bundle inventory is not exact")
        for name in sorted(expected_names):
            descriptor = storage.open_regular_file_at(directory_fd, name)
            artifact_stat = os.fstat(descriptor)
            if (
                artifact_stat.st_nlink != 1
                or _artifact_identity(artifact_stat) != created_artifacts[name]
            ):
                os.close(descriptor)
                raise OSError(f"Result Bundle artifact was replaced or linked: {name}")
            descriptors[name] = descriptor
        _validate_bundle_data(
            {
                name: _read_bundle_file(descriptor, name, created_artifacts[name].size)
                for name, descriptor in descriptors.items()
            },
            manifest,
            manifest_data,
        )
        for descriptor in descriptors.values():
            os.fchmod(descriptor, _SEALED_FILE_MODE)
        os.fchmod(directory_fd, _SEALED_DIRECTORY_MODE)

        sealed_data: dict[str, bytes] = {}
        sealed_identities: dict[str, ArtifactIdentity] = {}
        for name, descriptor in descriptors.items():
            artifact_stat = os.fstat(descriptor)
            if (
                stat.S_IMODE(artifact_stat.st_mode) != _SEALED_FILE_MODE
                or artifact_stat.st_nlink != 1
            ):
                raise OSError(f"Result Bundle artifact is not sealed: {name}")
            sealed_data[name] = _read_bundle_file(descriptor, name, created_artifacts[name].size)
            sealed_identities[name] = _artifact_identity(os.fstat(descriptor))
        _validate_bundle_data(sealed_data, manifest, manifest_data)
        if stat.S_IMODE(os.fstat(directory_fd).st_mode) != _SEALED_DIRECTORY_MODE:
            raise OSError("Result Bundle directory is not sealed")
        entries = storage.list_directory(directory_fd)
        if {entry.name for entry in entries} != expected_names or any(
            not stat.S_ISREG(entry.mode) or stat.S_IMODE(entry.mode) != _SEALED_FILE_MODE
            for entry in entries
        ):
            raise OSError("Sealed Result Bundle inventory is not exact")
        for name, expected_identity in sealed_identities.items():
            current = storage.stat_at(directory_fd, name)
            if (
                current is None
                or not stat.S_ISREG(current.st_mode)
                or stat.S_IMODE(current.st_mode) != _SEALED_FILE_MODE
                or current.st_nlink != 1
                or _artifact_identity(current) != expected_identity
            ):
                raise OSError(f"Sealed Result Bundle artifact binding changed: {name}")
    except ResultBundleFailure:
        raise
    except OSError as error:
        raise ResultBundleFailure(
            "result_bundle_failed",
            "Result Bundle could not be completed.",
            f"Result Bundle sealing failed: {error}.",
        ) from error
    finally:
        for descriptor in descriptors.values():
            try:
                os.close(descriptor)
            except OSError:
                pass


def _validate_wav(data: bytes, manifest: dict[str, Any]) -> None:
    wav_contract = manifest.get("wav")
    if not isinstance(wav_contract, dict):
        raise OSError("Result manifest WAV contract is invalid")
    sample_rate = wav_contract.get("sample_rate_hz")
    frames = wav_contract.get("frames")
    if (
        wav_contract.get("channels") != 1
        or not isinstance(sample_rate, int)
        or isinstance(sample_rate, bool)
        or sample_rate <= 0
        or not isinstance(frames, int)
        or isinstance(frames, bool)
        or frames < 0
    ):
        raise PcmWavValidationError("Performance WAV contract is invalid")
    validate_pcm16_mono_wav(
        data,
        expected_sample_rate_hz=sample_rate,
        expected_frames=frames,
    )
