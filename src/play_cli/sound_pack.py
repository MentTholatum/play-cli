from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import stat
import wave
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from play_cli import sound_pack_storage as storage

PACK_MANIFEST_SCHEMA_VERSION = "1.2"
RECEIPT_FILENAME = ".playcli-receipt.json"
RECEIPT_SCHEMA_VERSION = "1.0"
_IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_VERSION_PATTERN = re.compile(
    r"[0-9]+(?:\.[0-9]+)+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
    r"(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?\Z"
)
_COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{7,64}\Z")
_URI_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:[^\s]+\Z")
_MIDI_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,2})\Z")


@dataclass(frozen=True)
class InventoryEntry:
    path: str
    byte_size: int
    sha256: str


@dataclass(frozen=True)
class SourcePin:
    uri: str
    revision_type: str
    revision_value: str


@dataclass(frozen=True)
class LicenseAudit:
    id: str
    file: str
    sha256: str
    complete_text: bool


@dataclass(frozen=True)
class VerifiedSample:
    midi: int
    path: str
    sha256: str
    byte_size: int
    format: str
    sample_rate_hz: int
    channels: int
    frames: int


@dataclass(frozen=True)
class VerifiedSamplePayload:
    sample: VerifiedSample
    data: bytes


@dataclass(frozen=True)
class PackContent:
    path: Path
    id: str
    version: str
    pack_hash: str
    inventory: tuple[InventoryEntry, ...]
    source: SourcePin
    license: LicenseAudit
    attribution: str
    modifications: tuple[str, ...]
    samples: tuple[VerifiedSample, ...]


@dataclass(frozen=True)
class VerifiedSoundPack:
    path: Path
    id: str
    version: str
    pack_hash: str
    receipt_path: Path
    samples: tuple[VerifiedSample, ...]


@dataclass(frozen=True)
class InstallResult:
    sound_pack: VerifiedSoundPack
    result: str


class SoundPackFailure(Exception):
    def __init__(self, error_id: str, message: str, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.error_id = error_id
        self.message = message
        self.diagnostic = diagnostic


def _failure(error_id: str, diagnostic: str) -> SoundPackFailure:
    messages = {
        "sound_pack_invalid": "Sound Pack is invalid.",
        "sound_pack_corrupt": "Installed Sound Pack failed integrity verification.",
        "sound_pack_missing": "Required Sound Pack is not installed.",
        "sound_pack_conflict": "Sound Pack version conflicts with installed content.",
        "sound_pack_source_not_local": "Sound Pack source must be a local directory.",
        "sound_pack_install_failed": "Sound Pack could not be installed.",
        "sound_pack_sample_unavailable": "Sound Pack has no sample for this event.",
    }
    return SoundPackFailure(error_id, messages[error_id], diagnostic)


def _nonempty_text(value: Any, field: str, error_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _failure(error_id, f"Manifest field {field} must be non-empty text.")
    return value


def _identity(value: Any, field: str, error_id: str) -> str:
    text = _nonempty_text(value, field, error_id)
    if _IDENTITY_PATTERN.fullmatch(text) is None:
        raise _failure(error_id, f"Manifest field {field} is not a safe identity.")
    return text


def _relative_path(value: Any, field: str, error_id: str) -> PurePosixPath:
    text = _nonempty_text(value, field, error_id)
    path = PurePosixPath(text)
    if "\\" in text or path.is_absolute() or ".." in path.parts or text in {".", ".."}:
        raise _failure(error_id, f"Manifest field {field} is not a safe relative path.")
    return path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry_json(entry: InventoryEntry) -> dict[str, str | int]:
    return {
        "path": entry.path,
        "byte_size": entry.byte_size,
        "sha256": entry.sha256,
    }


def _pack_hash(inventory: tuple[InventoryEntry, ...]) -> str:
    canonical = json.dumps(
        [_entry_json(entry) for entry in inventory],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return _sha256(canonical)


def _scan_inventory(
    root: Path, error_id: str, *, root_fd: int | None = None
) -> tuple[InventoryEntry, ...]:
    owned_fd: int | None = None
    try:
        if root_fd is None:
            owned_fd = storage.open_directory(root)
            root_fd = owned_fd
        files = storage.scan_regular_files(root_fd)
    except OSError as error:
        raise _failure(error_id, f"Cannot safely inspect Sound Pack inventory: {error}.") from error
    finally:
        if owned_fd is not None:
            os.close(owned_fd)
    return tuple(
        InventoryEntry(item.path, len(item.data), _sha256(item.data))
        for item in files
        if item.path != RECEIPT_FILENAME
    )


def _read_pack_file(
    root: Path, relative: PurePosixPath | str, error_id: str, *, root_fd: int | None = None
) -> bytes:
    try:
        if root_fd is not None:
            return storage.read_regular_file_at(root_fd, relative)
        return storage.read_regular_file(root, relative)
    except OSError as error:
        raise _failure(
            error_id, f"Cannot safely read Sound Pack file {relative}: {error}."
        ) from error


def _load_manifest(root: Path, error_id: str, *, root_fd: int | None = None) -> dict[str, Any]:
    try:
        value = json.loads(
            _read_pack_file(root, "manifest.json", error_id, root_fd=root_fd).decode("utf-8")
        )
    except SoundPackFailure:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _failure(error_id, "Sound Pack manifest.json must be readable JSON.") from error
    if not isinstance(value, dict):
        raise _failure(error_id, "Sound Pack manifest must be a JSON object.")
    return value


def _positive_int(value: Any, field: str, error_id: str) -> int:
    if type(value) is not int or value <= 0:
        raise _failure(error_id, f"Manifest field {field} must be a positive integer.")
    return value


def _source_pin(value: Any, error_id: str) -> SourcePin:
    if not isinstance(value, dict):
        raise _failure(error_id, "Manifest field source must be an object.")
    uri = _nonempty_text(value.get("uri"), "source.uri", error_id)
    if _URI_PATTERN.fullmatch(uri) is None:
        raise _failure(error_id, "Sound Pack source URI must use an explicit URI scheme.")
    revision = value.get("revision")
    if not isinstance(revision, dict):
        raise _failure(error_id, "Manifest field source.revision must be an object.")
    revision_type = revision.get("type")
    revision_value = _nonempty_text(revision.get("value"), "source.revision.value", error_id)
    if revision_type == "version":
        if _VERSION_PATTERN.fullmatch(revision_value) is None:
            raise _failure(error_id, "Sound Pack source version is not a fixed version.")
    elif revision_type == "commit":
        if _COMMIT_PATTERN.fullmatch(revision_value) is None:
            raise _failure(error_id, "Sound Pack source commit is not a fixed hex revision.")
        revision_value = revision_value.lower()
    else:
        raise _failure(error_id, "Sound Pack source revision type is unsupported.")
    return SourcePin(uri=uri, revision_type=revision_type, revision_value=revision_value)


def _license_audit(
    value: Any,
    root: Path,
    inventory: dict[str, InventoryEntry],
    error_id: str,
    *,
    root_fd: int | None = None,
) -> LicenseAudit:
    if not isinstance(value, dict):
        raise _failure(error_id, "Manifest field license must be an object.")
    license_id = _nonempty_text(value.get("id"), "license.id", error_id)
    license_path = _relative_path(value.get("file"), "license.file", error_id)
    declared_hash = _nonempty_text(value.get("sha256"), "license.sha256", error_id)
    if _SHA256_PATTERN.fullmatch(declared_hash) is None:
        raise _failure(error_id, "Sound Pack license SHA-256 is invalid.")
    if value.get("complete_text") is not True:
        raise _failure(error_id, "Sound Pack license must declare complete_text true.")
    license_entry = inventory.get(license_path.as_posix())
    if license_entry is None:
        raise _failure(error_id, "Sound Pack license file is missing.")
    try:
        license_bytes = _read_pack_file(root, license_path, error_id, root_fd=root_fd)
        license_text = license_bytes.decode("utf-8")
    except SoundPackFailure:
        raise
    except UnicodeDecodeError as error:
        raise _failure(error_id, "Sound Pack license must be readable UTF-8 text.") from error
    if not license_text.strip():
        raise _failure(error_id, "Sound Pack license file must not be empty.")
    if license_entry.sha256 != declared_hash:
        raise _failure(error_id, "Sound Pack license SHA-256 does not match its file.")
    return LicenseAudit(
        id=license_id,
        file=license_path.as_posix(),
        sha256=declared_hash,
        complete_text=True,
    )


def _verify_sample_bytes(data: bytes, sample: VerifiedSample, error_id: str) -> None:
    if len(data) != sample.byte_size or _sha256(data) != sample.sha256:
        raise _failure(error_id, f"Sample {sample.midi} integrity metadata does not match.")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            actual = (
                wav.getsampwidth(),
                wav.getcomptype(),
                wav.getframerate(),
                wav.getnchannels(),
                wav.getnframes(),
            )
            frame_data = wav.readframes(sample.frames)
    except (EOFError, wave.Error) as error:
        raise _failure(error_id, f"Sample {sample.midi} is not a readable PCM WAV file.") from error
    expected = (2, "NONE", sample.sample_rate_hz, sample.channels, sample.frames)
    if actual != expected or len(frame_data) != sample.frames * sample.channels * 2:
        raise _failure(error_id, f"Sample {sample.midi} WAV metadata is inconsistent.")


def _validate_sample(
    root: Path,
    midi: str,
    value: Any,
    inventory: dict[str, InventoryEntry],
    error_id: str,
    *,
    root_fd: int | None = None,
) -> VerifiedSample:
    if not isinstance(value, dict):
        raise _failure(error_id, f"Sample {midi} metadata must be an object.")
    path = _relative_path(value.get("path"), f"samples.{midi}.path", error_id)
    relative = path.as_posix()
    if relative not in inventory:
        raise _failure(error_id, f"Sample {midi} file is missing from the pack inventory.")
    digest = _nonempty_text(value.get("sha256"), f"samples.{midi}.sha256", error_id)
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise _failure(error_id, f"Sample {midi} SHA-256 is invalid.")
    byte_size = _positive_int(value.get("byte_size"), f"samples.{midi}.byte_size", error_id)
    if value.get("format") != "PCM_S16LE":
        raise _failure(error_id, f"Sample {midi} format must be PCM_S16LE.")
    sample_rate = _positive_int(
        value.get("sample_rate_hz"), f"samples.{midi}.sample_rate_hz", error_id
    )
    channels = _positive_int(value.get("channels"), f"samples.{midi}.channels", error_id)
    frames = _positive_int(value.get("frames"), f"samples.{midi}.frames", error_id)
    entry = inventory[relative]
    if entry.sha256 != digest or entry.byte_size != byte_size:
        raise _failure(error_id, f"Sample {midi} integrity metadata does not match its file.")
    sample = VerifiedSample(
        midi=int(midi),
        path=relative,
        sha256=digest,
        byte_size=byte_size,
        format="PCM_S16LE",
        sample_rate_hz=sample_rate,
        channels=channels,
        frames=frames,
    )
    sample_bytes = _read_pack_file(root, path, error_id, root_fd=root_fd)
    _verify_sample_bytes(sample_bytes, sample, error_id)
    return sample


def _validate_pack_directory(
    root: Path, error_id: str, *, root_fd: int | None = None
) -> PackContent:
    inventory = _scan_inventory(root, error_id, root_fd=root_fd)
    inventory_by_path = {entry.path: entry for entry in inventory}
    if "manifest.json" not in inventory_by_path:
        raise _failure(error_id, "Sound Pack manifest.json is missing.")
    manifest = _load_manifest(root, error_id, root_fd=root_fd)
    if manifest.get("schema_version") != PACK_MANIFEST_SCHEMA_VERSION:
        raise _failure(error_id, "Sound Pack manifest schema version is unsupported.")
    pack_id = _identity(manifest.get("id"), "id", error_id)
    version = _identity(manifest.get("version"), "version", error_id)
    source = _source_pin(manifest.get("source"), error_id)
    license_audit = _license_audit(
        manifest.get("license"), root, inventory_by_path, error_id, root_fd=root_fd
    )
    attribution = _nonempty_text(manifest.get("attribution"), "attribution", error_id)
    modifications = manifest.get("modifications")
    if not isinstance(modifications, list) or not all(
        isinstance(item, str) and item.strip() for item in modifications
    ):
        raise _failure(error_id, "Manifest field modifications must be a list of text values.")
    samples = manifest.get("samples")
    if not isinstance(samples, dict) or not samples:
        raise _failure(error_id, "Manifest field samples must be a non-empty object.")
    verified_samples: list[VerifiedSample] = []
    for midi, sample in samples.items():
        if not isinstance(midi, str) or _MIDI_PATTERN.fullmatch(midi) is None or int(midi) > 127:
            raise _failure(error_id, "Sound Pack sample keys must be MIDI note numbers.")
        verified_samples.append(
            _validate_sample(root, midi, sample, inventory_by_path, error_id, root_fd=root_fd)
        )
    if any(sample.path == license_audit.file for sample in verified_samples):
        raise _failure(error_id, "Sound Pack license file must not be a sample file.")
    return PackContent(
        path=root.absolute(),
        id=pack_id,
        version=version,
        pack_hash=_pack_hash(inventory),
        inventory=inventory,
        source=source,
        license=license_audit,
        attribution=attribution,
        modifications=tuple(modifications),
        samples=tuple(sorted(verified_samples, key=lambda sample: sample.midi)),
    )


def _receipt_for(content: PackContent) -> dict[str, Any]:
    manifest_entry = next(entry for entry in content.inventory if entry.path == "manifest.json")
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "sound_pack": {"id": content.id, "version": content.version},
        "source": {
            "uri": content.source.uri,
            "revision": {
                "type": content.source.revision_type,
                "value": content.source.revision_value,
            },
        },
        "license": {
            "id": content.license.id,
            "file": content.license.file,
            "sha256": content.license.sha256,
            "complete_text": content.license.complete_text,
        },
        "attribution": content.attribution,
        "modifications": list(content.modifications),
        "manifest_sha256": manifest_entry.sha256,
        "inventory": {
            "algorithm": "sha256",
            "pack_hash": content.pack_hash,
            "files": [_entry_json(entry) for entry in content.inventory],
        },
    }


def _load_receipt(root: Path, *, root_fd: int | None = None) -> dict[str, Any]:
    try:
        value = json.loads(
            _read_pack_file(root, RECEIPT_FILENAME, "sound_pack_corrupt", root_fd=root_fd).decode(
                "utf-8"
            )
        )
    except SoundPackFailure:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _failure(
            "sound_pack_corrupt", "Installed Sound Pack receipt is unreadable."
        ) from error
    if not isinstance(value, dict):
        raise _failure("sound_pack_corrupt", "Installed Sound Pack receipt is invalid.")
    return value


def _absolute_without_symlink_resolution(path: Path) -> Path:
    return storage.absolute_path(path)


def _reject_existing_symlink_components(path: Path, error_id: str) -> None:
    try:
        storage.reject_existing_symlink_components(path)
    except OSError as error:
        raise _failure(error_id, f"Sound Pack path is unsafe: {error}.") from error


def _open_directory(path: Path, error_id: str) -> int:
    try:
        return storage.open_directory(path)
    except OSError as error:
        raise _failure(error_id, f"Cannot open Sound Pack directory safely: {error}.") from error


def _installed_path(store: Path, pack_id: str, version: str, error_id: str) -> Path:
    safe_id = _identity(pack_id, "id", error_id)
    safe_version = _identity(version, "version", error_id)
    absolute_store = _absolute_without_symlink_resolution(store)
    identity_root = absolute_store / safe_id
    destination = identity_root / safe_version
    _reject_existing_symlink_components(destination, error_id)
    return destination


def verify_installed_pack(store: Path, pack_id: str, version: str) -> VerifiedSoundPack:
    destination = _installed_path(store, pack_id, version, "sound_pack_corrupt")
    try:
        destination.lstat()
    except FileNotFoundError:
        raise _failure("sound_pack_missing", f"No installed pack at {destination}.")
    except OSError as error:
        raise _failure(
            "sound_pack_corrupt", f"Cannot inspect installed Sound Pack: {error}."
        ) from error
    content = _validate_pack_directory(destination, "sound_pack_corrupt")
    if content.id != pack_id or content.version != version:
        raise _failure(
            "sound_pack_corrupt", "Installed Sound Pack identity does not match its path."
        )
    receipt = _load_receipt(destination)
    expected = _receipt_for(content)
    if receipt != expected:
        raise _failure(
            "sound_pack_corrupt", "Installed Sound Pack receipt does not match its files."
        )
    return _verified_sound_pack(content, destination)


def _verified_sound_pack(content: PackContent, destination: Path) -> VerifiedSoundPack:
    return VerifiedSoundPack(
        path=destination,
        id=content.id,
        version=content.version,
        pack_hash=content.pack_hash,
        receipt_path=(destination / RECEIPT_FILENAME).absolute(),
        samples=content.samples,
    )


def read_verified_sample(sound_pack: VerifiedSoundPack, midi: int) -> VerifiedSamplePayload:
    sample = next((item for item in sound_pack.samples if item.midi == midi), None)
    if sample is None:
        raise _failure(
            "sound_pack_sample_unavailable", f"Sound Pack has no sample for MIDI {midi}."
        )
    data = _read_pack_file(sound_pack.path, sample.path, "sound_pack_corrupt")
    _verify_sample_bytes(data, sample, "sound_pack_corrupt")
    return VerifiedSamplePayload(sample=sample, data=data)


def _write_receipt(root: Path, content: PackContent, *, root_fd: int | None = None) -> None:
    owned_fd: int | None = None
    try:
        if root_fd is None:
            owned_fd = storage.open_directory(root)
            root_fd = owned_fd
        data = (
            json.dumps(_receipt_for(content), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        storage.write_atomic(root_fd, RECEIPT_FILENAME, data)
    except OSError as error:
        raise _failure(
            "sound_pack_install_failed", f"Cannot write install receipt: {error}."
        ) from error
    finally:
        if owned_fd is not None:
            os.close(owned_fd)


def _entry_mode(root: Path, name: str, error_id: str) -> int | None:
    root_fd = _open_directory(root, error_id)
    try:
        entry = storage.stat_at(root_fd, name)
        return None if entry is None else entry.st_mode
    except OSError as error:
        raise _failure(error_id, f"Cannot inspect Sound Pack entry {name}: {error}.") from error
    finally:
        os.close(root_fd)


def _prepare_store(store: Path) -> tuple[Path, int]:
    absolute = _absolute_without_symlink_resolution(store)
    try:
        store_fd = storage.open_or_create_directory(absolute)
    except OSError as error:
        raise _failure(
            "sound_pack_install_failed", f"Cannot safely create Sound Pack store: {error}."
        ) from error
    return absolute, store_fd


def _open_matching_directory_at(
    parent_fd: int, name: str, expected_inode: tuple[int, int]
) -> int | None:
    directory_fd: int | None = None
    try:
        directory_fd = storage.open_directory_at(parent_fd, name)
        current = os.fstat(directory_fd)
        if (current.st_dev, current.st_ino) != expected_inode:
            os.close(directory_fd)
            directory_fd = None
            return None
        return directory_fd
    except OSError:
        if directory_fd is not None:
            os.close(directory_fd)
        return None


def _same_installed_content(
    destination: Path,
    content: PackContent,
    store_fd: int,
    identity_fd: int,
    identity_stat: os.stat_result,
) -> InstallResult:
    version_fd: int | None = None
    try:
        version_fd = storage.open_directory_at(identity_fd, content.version)
        version_stat = os.fstat(version_fd)
        version_inode = (version_stat.st_dev, version_stat.st_ino)
        try:
            receipt = storage.stat_at(version_fd, RECEIPT_FILENAME)
        except OSError as error:
            raise _failure(
                "sound_pack_install_failed", f"Cannot inspect install receipt: {error}."
            ) from error
        receipt_mode = None if receipt is None else receipt.st_mode
        if receipt_mode is None:
            pending_receipt = f"{RECEIPT_FILENAME}.pending"
            try:
                pending = storage.stat_at(version_fd, pending_receipt)
            except OSError as error:
                raise _failure(
                    "sound_pack_install_failed",
                    f"Cannot inspect pending install receipt: {error}.",
                ) from error
            if pending is not None:
                raise _failure(
                    "sound_pack_install_failed",
                    "Pending Sound Pack receipt requires manual inspection.",
                )
        try:
            installed_content = _validate_pack_directory(
                destination, "sound_pack_corrupt", root_fd=version_fd
            )
        except SoundPackFailure as error:
            raise _failure("sound_pack_conflict", error.diagnostic) from error
        if (
            installed_content.id != content.id
            or installed_content.version != content.version
            or installed_content.pack_hash != content.pack_hash
        ):
            raise _failure("sound_pack_conflict", "Installed Sound Pack has different content.")

        if receipt_mode is None:
            _write_receipt(destination, installed_content, root_fd=version_fd)
            result = "receipt_repaired"
        else:
            if stat.S_ISLNK(receipt_mode) or not stat.S_ISREG(receipt_mode):
                raise _failure("sound_pack_conflict", "Installed Sound Pack receipt is unsafe.")
            try:
                installed_receipt = _load_receipt(destination, root_fd=version_fd)
            except SoundPackFailure as error:
                raise _failure("sound_pack_conflict", error.diagnostic) from error
            if installed_receipt != _receipt_for(installed_content):
                raise _failure(
                    "sound_pack_conflict", "Installed Sound Pack receipt does not match its files."
                )
            result = "already_installed"

        _confirm_public_install_binding(
            store_fd,
            content.id,
            identity_stat,
            content.version,
            version_inode,
        )
        return InstallResult(_verified_sound_pack(installed_content, destination), result)
    except SoundPackFailure:
        raise
    except OSError as error:
        raise _failure(
            "sound_pack_install_failed", f"Cannot inspect installed Sound Pack: {error}."
        ) from error
    finally:
        if version_fd is not None:
            os.close(version_fd)


def _confirm_public_install_binding(
    store_fd: int,
    pack_id: str,
    identity_stat: os.stat_result,
    version: str,
    version_inode: tuple[int, int],
) -> None:
    identity_fd: int | None = None
    version_fd: int | None = None
    try:
        identity_fd = _open_matching_directory_at(
            store_fd,
            pack_id,
            (identity_stat.st_dev, identity_stat.st_ino),
        )
        if identity_fd is None:
            raise _failure(
                "sound_pack_install_failed",
                "Sound Pack identity directory changed before success.",
            )
        version_fd = _open_matching_directory_at(identity_fd, version, version_inode)
        if version_fd is None:
            raise _failure(
                "sound_pack_install_failed",
                "Sound Pack version directory changed before success.",
            )
    except SoundPackFailure:
        raise
    except OSError as error:
        raise _failure(
            "sound_pack_install_failed",
            f"Cannot confirm installed Sound Pack public binding: {error}.",
        ) from error
    finally:
        if version_fd is not None:
            os.close(version_fd)
        if identity_fd is not None:
            os.close(identity_fd)


def _verify_anchored_install(
    destination: Path,
    content: PackContent,
    store_fd: int,
    identity_fd: int,
    identity_stat: os.stat_result,
    version_fd: int,
    installed_inode: tuple[int, int],
) -> VerifiedSoundPack:
    try:
        version_stat = os.fstat(version_fd)
        if (version_stat.st_dev, version_stat.st_ino) != installed_inode:
            raise _failure(
                "sound_pack_install_failed", "Installed Sound Pack inode changed before read-back."
            )
        installed_content = _validate_pack_directory(
            destination, "sound_pack_corrupt", root_fd=version_fd
        )
        if (
            installed_content.id != content.id
            or installed_content.version != content.version
            or installed_content.pack_hash != content.pack_hash
        ):
            raise _failure(
                "sound_pack_install_failed",
                "Installed Sound Pack read-back does not match the explicit source.",
            )
        current_identity_fd = _open_matching_directory_at(
            store_fd,
            content.id,
            (identity_stat.st_dev, identity_stat.st_ino),
        )
        if current_identity_fd is None:
            raise _failure(
                "sound_pack_install_failed",
                "Sound Pack identity directory changed during installation.",
            )
        os.close(current_identity_fd)
        return _verified_sound_pack(installed_content, destination)
    except SoundPackFailure as error:
        if error.error_id == "sound_pack_install_failed":
            raise
        raise _failure("sound_pack_install_failed", error.diagnostic) from error
    except OSError as error:
        raise _failure(
            "sound_pack_install_failed", f"Installed Sound Pack read-back failed: {error}."
        ) from error


def install_local_pack(source_value: str, store: Path) -> InstallResult:
    if "://" in source_value:
        raise _failure("sound_pack_source_not_local", "URL sources are not accepted.")
    source = _absolute_without_symlink_resolution(Path(source_value))
    _reject_existing_symlink_components(source, "sound_pack_invalid")
    for lifecycle_file in (RECEIPT_FILENAME, f"{RECEIPT_FILENAME}.pending"):
        if _entry_mode(source, lifecycle_file, "sound_pack_invalid") is not None:
            raise _failure(
                "sound_pack_invalid", "Source pack must not contain install lifecycle files."
            )
    content = _validate_pack_directory(source, "sound_pack_invalid")
    destination = _installed_path(store, content.id, content.version, "sound_pack_install_failed")
    prepared_store, store_fd = _prepare_store(store)
    identity_root = prepared_store / content.id
    identity_fd: int | None = None
    lock_fd: int | None = None
    temporary_name = f".{content.version}.install-pending"
    staged_fd: int | None = None
    try:
        try:
            os.mkdir(content.id, dir_fd=store_fd)
        except FileExistsError:
            pass
        except OSError as error:
            raise _failure(
                "sound_pack_install_failed", f"Cannot create identity directory: {error}."
            ) from error
        identity_fd = storage.open_directory_at(store_fd, content.id)
        identity_stat = os.fstat(identity_fd)
        lock_fd = storage.open_exclusive_lock_at(identity_fd, f".{content.version}.install.lock")
        current_identity_fd = _open_matching_directory_at(
            store_fd,
            content.id,
            (identity_stat.st_dev, identity_stat.st_ino),
        )
        if current_identity_fd is None:
            raise _failure(
                "sound_pack_install_failed",
                "Sound Pack identity directory changed during installation.",
            )
        os.close(current_identity_fd)
        try:
            existing = storage.stat_at(identity_fd, content.version)
        except OSError as error:
            raise _failure(
                "sound_pack_install_failed", f"Cannot inspect install target: {error}."
            ) from error
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISDIR(existing.st_mode):
                raise _failure("sound_pack_conflict", "Installed Sound Pack path is unsafe.")
            return _same_installed_content(
                destination, content, store_fd, identity_fd, identity_stat
            )

        pending = storage.stat_at(identity_fd, temporary_name)
        if pending is not None:
            raise _failure(
                "sound_pack_install_failed",
                "Pending Sound Pack installation requires manual inspection.",
            )

        try:
            storage.copy_directory_nofollow(source, identity_fd, temporary_name)
        except OSError as error:
            raise _failure(
                "sound_pack_invalid", f"Cannot safely copy Sound Pack: {error}."
            ) from error
        staged = identity_root / temporary_name
        staged_fd = storage.open_directory_at(identity_fd, temporary_name)
        staged_content = _validate_pack_directory(staged, "sound_pack_invalid", root_fd=staged_fd)
        if staged_content.pack_hash != content.pack_hash:
            raise _failure("sound_pack_invalid", "Sound Pack changed while it was copied.")
        staged_check = _validate_pack_directory(staged, "sound_pack_invalid", root_fd=staged_fd)
        if staged_check.pack_hash != content.pack_hash:
            raise _failure("sound_pack_invalid", "Sound Pack changed before installation.")
        staged_stat = os.fstat(staged_fd)
        expected_installed_inode = (staged_stat.st_dev, staged_stat.st_ino)
        try:
            storage.rename_noreplace(identity_fd, temporary_name, identity_fd, content.version)
        except OSError as error:
            if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                current_identity_fd = _open_matching_directory_at(
                    store_fd,
                    content.id,
                    (identity_stat.st_dev, identity_stat.st_ino),
                )
                if current_identity_fd is None:
                    raise _failure(
                        "sound_pack_install_failed",
                        "Sound Pack identity directory changed during installation.",
                    ) from error
                os.close(current_identity_fd)
                return _same_installed_content(
                    destination, content, store_fd, identity_fd, identity_stat
                )
            raise
        destination_stat = storage.stat_at(identity_fd, content.version)
        if (
            destination_stat is None
            or (
                destination_stat.st_dev,
                destination_stat.st_ino,
            )
            != expected_installed_inode
        ):
            raise _failure(
                "sound_pack_install_failed",
                "Installed Sound Pack inode changed after rename.",
            )
        installed = _verify_anchored_install(
            destination,
            content,
            store_fd,
            identity_fd,
            identity_stat,
            staged_fd,
            expected_installed_inode,
        )
        _write_receipt(destination, staged_check, root_fd=staged_fd)
        _confirm_public_install_binding(
            store_fd,
            content.id,
            identity_stat,
            content.version,
            expected_installed_inode,
        )
        return InstallResult(installed, "installed")
    except SoundPackFailure:
        raise
    except OSError as error:
        raise _failure(
            "sound_pack_install_failed", f"Sound Pack installation failed: {error}."
        ) from error
    finally:
        if staged_fd is not None:
            os.close(staged_fd)
        if lock_fd is not None:
            os.close(lock_fd)
        if identity_fd is not None:
            os.close(identity_fd)
        os.close(store_fd)


def list_installed_packs(store: Path) -> list[dict[str, str | None]]:
    absolute = _absolute_without_symlink_resolution(store)
    try:
        store_fd = storage.open_directory_if_present(absolute)
    except OSError as error:
        raise _failure("sound_pack_corrupt", f"Cannot list Sound Pack store: {error}.") from error
    if store_fd is None:
        return []
    summaries: list[dict[str, str | None]] = []
    try:
        try:
            identity_entries = storage.list_directory(store_fd)
        except OSError as error:
            raise _failure(
                "sound_pack_corrupt", f"Cannot list Sound Pack store: {error}."
            ) from error
        for identity_entry in identity_entries:
            if _IDENTITY_PATTERN.fullmatch(identity_entry.name) is None:
                continue
            try:
                identity_mode = identity_entry.mode
            except OSError as error:
                raise _failure(
                    "sound_pack_corrupt", f"Cannot inspect Sound Pack identity: {error}."
                ) from error
            if stat.S_ISLNK(identity_mode) or not stat.S_ISDIR(identity_mode):
                continue
            identity_fd: int | None = None
            try:
                try:
                    identity_fd = storage.open_directory_at(store_fd, identity_entry.name)
                    destinations = storage.list_directory(identity_fd)
                except OSError as error:
                    raise _failure(
                        "sound_pack_corrupt", f"Cannot list Sound Pack identity: {error}."
                    ) from error
                for destination_entry in destinations:
                    if _IDENTITY_PATTERN.fullmatch(destination_entry.name) is None:
                        continue
                    try:
                        destination_mode = destination_entry.mode
                    except OSError as error:
                        raise _failure(
                            "sound_pack_corrupt",
                            f"Cannot inspect installed Sound Pack: {error}.",
                        ) from error
                    if stat.S_ISLNK(destination_mode) or not stat.S_ISDIR(destination_mode):
                        continue
                    destination_fd: int | None = None
                    try:
                        destination_fd = storage.open_directory_at(
                            identity_fd, destination_entry.name
                        )
                        receipt_stat = storage.stat_at(destination_fd, RECEIPT_FILENAME)
                        receipt_mode = None if receipt_stat is None else receipt_stat.st_mode
                    except OSError as error:
                        raise _failure(
                            "sound_pack_corrupt",
                            f"Cannot inspect installed Sound Pack: {error}.",
                        ) from error
                    finally:
                        if destination_fd is not None:
                            os.close(destination_fd)

                    destination = absolute / identity_entry.name / destination_entry.name
                    pack_hash: str | None = None
                    status = "corrupt"
                    receipt_is_regular = receipt_mode is not None and stat.S_ISREG(receipt_mode)
                    if receipt_is_regular:
                        try:
                            verified = verify_installed_pack(
                                absolute, identity_entry.name, destination_entry.name
                            )
                            pack_hash = verified.pack_hash
                            status = "verified"
                        except SoundPackFailure:
                            try:
                                receipt = _load_receipt(destination)
                                inventory = receipt.get("inventory")
                                if isinstance(inventory, dict):
                                    candidate = inventory.get("pack_hash")
                                    if isinstance(candidate, str) and _SHA256_PATTERN.fullmatch(
                                        candidate
                                    ):
                                        pack_hash = candidate
                            except SoundPackFailure:
                                pass
                    summaries.append(
                        {
                            "id": identity_entry.name,
                            "version": destination_entry.name,
                            "pack_hash": pack_hash,
                            "status": status,
                            "path": str(destination.absolute()),
                        }
                    )
            finally:
                if identity_fd is not None:
                    os.close(identity_fd)
    finally:
        os.close(store_fd)
    return summaries
