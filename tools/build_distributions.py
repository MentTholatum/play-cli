#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath

BUILD_SCHEMA_VERSION = "1.0"
BUILD_FRONTEND_VERSION = "1.6.0"
BUILD_BACKEND_VERSION = "84.0.0"
PROJECT_ROOT = Path(__file__).parents[1]


class BuildFailure(Exception):
    pass


@dataclass(frozen=True)
class ArchiveEntry:
    name: str
    mode: int
    data: bytes | None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_sdist(path: Path) -> tuple[ArchiveEntry, ...]:
    entries: list[ArchiveEntry] = []
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            seen: set[str] = set()
            for member in archive.getmembers():
                relative = PurePosixPath(member.name)
                if (
                    member.name in seen
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or not member.name
                ):
                    raise BuildFailure("Generated sdist inventory is unsafe.")
                seen.add(member.name)
                if member.isdir():
                    entries.append(ArchiveEntry(member.name, member.mode, None))
                    continue
                if not member.isfile():
                    raise BuildFailure("Generated sdist contains a non-regular entry.")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BuildFailure("Generated sdist file could not be read.")
                entries.append(ArchiveEntry(member.name, member.mode, extracted.read()))
    except (OSError, tarfile.TarError) as error:
        raise BuildFailure("Generated sdist could not be read.") from error
    return tuple(sorted(entries, key=lambda entry: entry.name))


def _write_reproducible_sdist(source: Path, destination: Path, epoch: int) -> None:
    entries = _read_sdist(source)
    try:
        with destination.open("xb") as raw_output:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_output,
                compresslevel=9,
                mtime=epoch,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT
                ) as archive:
                    for entry in entries:
                        member = tarfile.TarInfo(entry.name)
                        member.uid = 0
                        member.gid = 0
                        member.uname = ""
                        member.gname = ""
                        member.mtime = epoch
                        if entry.data is None:
                            member.type = tarfile.DIRTYPE
                            member.mode = 0o755
                            member.size = 0
                            archive.addfile(member)
                            continue
                        member.mode = 0o755 if entry.mode & 0o111 else 0o644
                        member.size = len(entry.data)
                        archive.addfile(member, BytesIO(entry.data))
    except OSError as error:
        raise BuildFailure("Reproducible sdist could not be written.") from error


def _artifact(path: Path, kind: str) -> dict[str, object]:
    return {
        "filename": path.name,
        "kind": kind,
        "byte_size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _require_build_frontend() -> str:
    try:
        frontend_version = importlib.metadata.version("build")
    except importlib.metadata.PackageNotFoundError as error:
        raise BuildFailure("The pinned build frontend is unavailable.") from error
    if frontend_version != BUILD_FRONTEND_VERSION:
        raise BuildFailure(
            f"The build frontend must be build {BUILD_FRONTEND_VERSION}; got {frontend_version}."
        )
    return frontend_version


def _verify_build_tools(wheel: Path, frontend_version: str) -> dict[str, dict[str, str]]:
    try:
        with zipfile.ZipFile(wheel) as archive:
            wheel_metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")
            ]
            if len(wheel_metadata_names) != 1:
                raise BuildFailure("Built wheel metadata is missing or ambiguous.")
            fields = {}
            for line in archive.read(wheel_metadata_names[0]).decode("utf-8").splitlines():
                if ": " in line:
                    key, value = line.split(": ", 1)
                    fields[key] = value
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as error:
        raise BuildFailure("Built wheel metadata could not be read.") from error
    expected_generator = f"setuptools ({BUILD_BACKEND_VERSION})"
    if fields.get("Generator") != expected_generator:
        raise BuildFailure(
            f"The build backend must be {expected_generator}; got {fields.get('Generator')}."
        )
    return {
        "frontend": {"name": "build", "version": frontend_version},
        "backend": {"name": "setuptools", "version": BUILD_BACKEND_VERSION},
    }


def _build(output: Path, epoch: int) -> dict[str, object]:
    if epoch < 315_532_800:
        raise BuildFailure("SOURCE_DATE_EPOCH must be at least 1980-01-01 for wheel ZIP files.")
    if output.exists():
        raise BuildFailure("Build output already exists.")
    frontend_version = _require_build_frontend()
    environment = dict(os.environ)
    environment["SOURCE_DATE_EPOCH"] = str(epoch)
    with tempfile.TemporaryDirectory(prefix="playcli-distributions-") as temporary:
        raw_output = Path(temporary)
        built = subprocess.run(
            [sys.executable, "-m", "build", "--outdir", str(raw_output), str(PROJECT_ROOT)],
            capture_output=True,
            check=False,
            text=True,
            env=environment,
        )
        if built.returncode != 0:
            raise BuildFailure("Python distribution build failed.")
        wheels = tuple(raw_output.glob("*.whl"))
        sdists = tuple(raw_output.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise BuildFailure("Build did not produce exactly one wheel and one sdist.")
        output.mkdir(parents=False)
        wheel = output / wheels[0].name
        sdist = output / sdists[0].name
        shutil.copyfile(wheels[0], wheel)
        _write_reproducible_sdist(sdists[0], sdist, epoch)
    build_tools = _verify_build_tools(wheel, frontend_version)
    return {
        "schema_version": BUILD_SCHEMA_VERSION,
        "operation": "build-distributions",
        "status": "ok",
        "source_date_epoch": epoch,
        "build_python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "build_tools": build_tools,
        "artifacts": [_artifact(wheel, "wheel"), _artifact(sdist, "sdist")],
    }


def _write_json(value: dict[str, object]) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-date-epoch", required=True, type=int)
    arguments = parser.parse_args()
    try:
        report = _build(arguments.output, arguments.source_date_epoch)
    except (BuildFailure, OSError, ValueError) as error:
        _write_json(
            {
                "schema_version": BUILD_SCHEMA_VERSION,
                "operation": "build-distributions",
                "status": "error",
                "error": {"id": "distribution_build_failed", "message": str(error)},
            }
        )
        print(f"Distribution build failed: {error}", file=sys.stderr)
        return 1
    _write_json(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
