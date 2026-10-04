#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import wave
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

from platform_record_contract import (
    ARTIFACT_FILENAMES,
    BUNDLE_FILES,
    EXPECTED_DEV_REQUIREMENTS,
    HASH_CONTRACT,
    RecordContractError,
    validate_record,
)
from platform_record_contract import (
    SCHEMA_VERSION as ACCEPTANCE_SCHEMA_VERSION,
)

PLAYCLI_SCHEMA_VERSION = "1.5"
SUPPORTED_CLI_VERSIONS = ("0.8.1", "0.9.0", "0.10.0")
PROJECT_ROOT = Path(__file__).parents[1]
SCORE = PROJECT_ROOT / "tests/fixtures/minimal.musicxml"
PACK = PROJECT_ROOT / "tests/fixtures/performance-test-piano-pack"
PACK_ID = "play-cli-performance-test-piano"
PACK_VERSION = "1.0.0"
EXPECTED_ENTRY_POINTS = {"console_scripts": {"playcli": "play_cli.cli:main"}}


class AcceptanceFailure(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(data: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except json.JSONDecodeError as error:
        raise AcceptanceFailure(f"{label} did not return JSON.") from error
    if not isinstance(value, dict):
        raise AcceptanceFailure(f"{label} did not return a JSON object.")
    return value


def _run(
    command: list[str],
    label: str,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        cwd=cwd,
        env=env,
    )
    if completed.returncode != 0:
        raise AcceptanceFailure(f"{label} failed with exit code {completed.returncode}.")
    return completed


def _playcli_response(
    executable: Path,
    arguments: list[str],
    operation: str,
    isolated_cwd: Path,
) -> dict[str, Any]:
    completed = _run(
        [str(executable), *arguments],
        operation,
        cwd=isolated_cwd,
        env={"PATH": str(executable.parent), "PYTHONNOUSERSITE": "1"},
    )
    if len(completed.stdout.splitlines()) != 1:
        raise AcceptanceFailure(f"{operation} stdout is not one JSON value.")
    response = _json_object(completed.stdout, operation)
    if (
        response.get("schema_version") != PLAYCLI_SCHEMA_VERSION
        or response.get("operation") != operation
        or response.get("status") != "ok"
    ):
        raise AcceptanceFailure(f"{operation} response contract is invalid.")
    return response


def _inspect_wheel(wheel: Path) -> tuple[str, dict[str, str], dict[str, object]]:
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            if len(names) != len(set(names)) or any(
                PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
                for name in names
            ):
                raise AcceptanceFailure("Wheel inventory is unsafe.")
            if any(
                name.lower().endswith((".wav", ".flac", ".mp3", ".aif", ".aiff")) for name in names
            ):
                raise AcceptanceFailure("Wheel contains an audio asset.")
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            wheel_metadata_names = [name for name in names if name.endswith(".dist-info/WHEEL")]
            entry_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
            if len(metadata_names) != 1 or len(wheel_metadata_names) != 1 or len(entry_names) != 1:
                raise AcceptanceFailure("Wheel metadata is missing or ambiguous.")
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
            if (
                metadata.get_all("Name") != ["play-cli"]
                or metadata.get_all("Version")
                not in [[version] for version in SUPPORTED_CLI_VERSIONS]
                or metadata.get_all("Requires-Python") != [">=3.11"]
            ):
                raise AcceptanceFailure("Wheel project identity is invalid.")
            requirements = metadata.get_all("Requires-Dist", [])
            if requirements != EXPECTED_DEV_REQUIREMENTS:
                raise AcceptanceFailure(
                    "Wheel dependency metadata is not the approved dev-only set."
                )
            wheel_fields = BytesParser().parsebytes(archive.read(wheel_metadata_names[0]))
            if (
                wheel_fields.get_all("Wheel-Version") != ["1.0"]
                or wheel_fields.get_all("Generator") != ["setuptools (84.0.0)"]
                or wheel_fields.get_all("Root-Is-Purelib") != ["true"]
                or wheel_fields.get_all("Tag") != ["py3-none-any"]
                or wheel.name != f"play_cli-{metadata['Version']}-py3-none-any.whl"
            ):
                raise AcceptanceFailure("Wheel portability metadata is invalid.")
            parser = configparser.ConfigParser()
            parser.read_string(archive.read(entry_names[0]).decode("utf-8"))
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, configparser.Error) as error:
        raise AcceptanceFailure("Wheel could not be inspected.") from error
    entry_points = {section: dict(parser.items(section)) for section in parser.sections()}
    if entry_points != EXPECTED_ENTRY_POINTS:
        raise AcceptanceFailure("Wheel console-script contract is invalid.")
    console_scripts = entry_points["console_scripts"]
    return (
        str(metadata.get("Version")),
        console_scripts,
        {
            "name": str(metadata.get("Name")),
            "version": str(metadata.get("Version")),
            "requires_python": str(metadata.get("Requires-Python")),
            "requires_dist": requirements,
            "wheel_version": str(wheel_fields.get("Wheel-Version")),
            "generator": str(wheel_fields.get("Generator")),
            "root_is_purelib": True,
            "tag": str(wheel_fields.get("Tag")),
        },
    )


def _runtime_metadata(python: Path) -> dict[str, Any]:
    program = (
        "import importlib.metadata as m,json,platform,sys;"
        "d=m.distribution('play-cli');"
        "print(json.dumps({'version':d.version,'python':platform.python_version(),"
        "'implementation':platform.python_implementation(),'platform':sys.platform,"
        "'machine':platform.machine(),'entry_points':sorted("
        "[{'group':e.group,'name':e.name,'value':e.value} for e in d.entry_points],"
        "key=lambda e:(e['group'],e['name'],e['value']))}))"
    )
    completed = _run([str(python), "-I", "-c", program], "installed runtime inspection")
    return _json_object(completed.stdout, "installed runtime inspection")


def _verify_bundle(bundle: Path, response: dict[str, Any]) -> dict[str, Any]:
    names = sorted(item.name for item in bundle.iterdir())
    if names != list(BUNDLE_FILES):
        raise AcceptanceFailure("Result Bundle inventory is not exact.")
    bundle_contract = response.get("result_bundle")
    if (
        not isinstance(bundle_contract, dict)
        or bundle_contract.get("schema_version") != "1.1"
        or bundle_contract.get("status") != "complete"
    ):
        raise AcceptanceFailure("Result Bundle schema is invalid.")
    artifacts = bundle_contract.get("artifacts")
    if not isinstance(artifacts, dict):
        raise AcceptanceFailure("Result Bundle artifact contract is invalid.")
    artifact_hashes: dict[str, str] = {}
    for key, filename in ARTIFACT_FILENAMES.items():
        contract = artifacts.get(key)
        if not isinstance(contract, dict):
            raise AcceptanceFailure("Result Bundle artifact contract is incomplete.")
        actual = _sha256(bundle / filename)
        if (
            contract.get("path") != str((bundle / filename).resolve())
            or contract.get("sha256") != actual
        ):
            raise AcceptanceFailure("Result Bundle artifact hash does not match.")
        artifact_hashes[filename] = actual
    try:
        persisted = _json_object((bundle / "result.json").read_text(), "Result Bundle manifest")
    except OSError as error:
        raise AcceptanceFailure("Result Bundle manifest is unreadable.") from error
    expected_persisted = dict(response)
    expected_persisted.pop("playback", None)
    if persisted != expected_persisted:
        raise AcceptanceFailure("Result Bundle manifest does not match the public response.")
    modes = {stat.S_IMODE((bundle / name).stat().st_mode) for name in BUNDLE_FILES}
    links = {(bundle / name).stat().st_nlink for name in BUNDLE_FILES}
    if modes != {0o444} or links != {1} or stat.S_IMODE(bundle.stat().st_mode) != 0o555:
        raise AcceptanceFailure("Result Bundle permission or link contract is invalid.")
    wav_path = bundle / "performance.wav"
    try:
        with wave.open(str(wav_path), "rb") as performance:
            channels = performance.getnchannels()
            sample_width = performance.getsampwidth()
            frames = performance.getnframes()
            frame_data = performance.readframes(frames)
            trailing = performance.readframes(1)
            sample_rate = performance.getframerate()
    except (OSError, wave.Error) as error:
        raise AcceptanceFailure("Result Bundle WAV is unreadable.") from error
    frame_complete = len(frame_data) == frames * channels * sample_width and trailing == b""
    if channels != 1 or sample_width != 2 or not frame_complete:
        raise AcceptanceFailure("Result Bundle WAV data is incomplete.")
    wav_contract = response.get("wav")
    if not isinstance(wav_contract, dict) or wav_contract != {
        "path": str(wav_path.resolve()),
        "format": "PCM_S16LE",
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "frames": frames,
        "duration_seconds": frames / sample_rate,
    }:
        raise AcceptanceFailure("Result Bundle WAV contract does not match the file.")
    return {
        "artifact_sha256": artifact_hashes,
        "manifest_sha256": _sha256(bundle / "result.json"),
        "wav": {
            "channels": channels,
            "frame_bytes_complete": frame_complete,
            "frames": frames,
            "sample_rate_hz": sample_rate,
            "sample_width_bytes": sample_width,
        },
    }


def _accept(wheel: Path, output: Path) -> dict[str, Any]:
    if output.exists():
        raise AcceptanceFailure("Acceptance output already exists.")
    try:
        wheel = wheel.resolve(strict=True)
    except OSError as error:
        raise AcceptanceFailure("Wheel is not readable.") from error
    version, console_scripts, wheel_metadata = _inspect_wheel(wheel)
    output.mkdir()
    output = output.resolve(strict=True)
    runtime = output / "runtime"
    _run([sys.executable, "-m", "venv", str(runtime)], "runtime creation")
    runtime_python = runtime / "bin/python"
    runtime_bin = runtime / "bin"
    before_install = {item.name for item in runtime_bin.iterdir()}
    _run(
        [
            str(runtime_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            str(wheel),
        ],
        "offline wheel installation",
    )
    playcli = runtime / "bin/playcli"
    after_install = {item.name for item in runtime_bin.iterdir()}
    added_executables = sorted(after_install - before_install)
    try:
        playcli_stat = playcli.lstat()
        with playcli.open("rb") as script:
            shebang = script.readline().decode("utf-8").rstrip("\r\n")
    except (OSError, UnicodeDecodeError) as error:
        raise AcceptanceFailure("Installed playcli entry point could not be inspected.") from error
    if (
        added_executables != ["playcli"]
        or not stat.S_ISREG(playcli_stat.st_mode)
        or not os.access(playcli, os.X_OK)
        or not shebang.startswith("#!/")
        or any(character.isspace() for character in shebang[2:])
        or (runtime / "bin/play").exists()
    ):
        raise AcceptanceFailure("Installed console-script inventory is invalid.")
    try:
        shebang_python = Path(shebang[2:])
        shebang_identity = shebang_python.stat()
        runtime_python_identity = runtime_python.stat()
        same_python = shebang_python.resolve(strict=True) == runtime_python.resolve(
            strict=True
        ) and (shebang_identity.st_dev, shebang_identity.st_ino) == (
            runtime_python_identity.st_dev,
            runtime_python_identity.st_ino,
        )
    except OSError as error:
        raise AcceptanceFailure("Installed playcli shebang target is unavailable.") from error
    if not same_python:
        raise AcceptanceFailure("Installed playcli shebang target is invalid.")
    runtime_metadata = _runtime_metadata(runtime_python)
    if runtime_metadata.get("version") != version:
        raise AcceptanceFailure("Installed distribution version does not match the wheel.")
    installed_entry_points = runtime_metadata.get("entry_points")
    if installed_entry_points != [
        {"group": "console_scripts", "name": "playcli", "value": "play_cli.cli:main"}
    ]:
        raise AcceptanceFailure("Installed entry-point metadata is invalid.")

    doctor = _playcli_response(playcli, ["doctor"], "doctor", output)
    validated = _playcli_response(playcli, ["validate", str(SCORE)], "validate", output)
    store = output / "packs"
    installed = _playcli_response(
        playcli,
        ["sound-pack", "install", str(PACK), "--store", str(store)],
        "sound-pack.install",
        output,
    )
    listed = _playcli_response(
        playcli,
        ["sound-pack", "list", "--store", str(store)],
        "sound-pack.list",
        output,
    )
    verified = _playcli_response(
        playcli,
        [
            "sound-pack",
            "verify",
            PACK_ID,
            "--version",
            PACK_VERSION,
            "--store",
            str(store),
        ],
        "sound-pack.verify",
        output,
    )
    bundle = output / "result"
    rendered = _playcli_response(
        playcli,
        [
            "render",
            str(SCORE),
            "--sound-pack",
            PACK_ID,
            "--sound-pack-version",
            PACK_VERSION,
            "--sound-pack-store",
            str(store),
            "--output",
            str(bundle),
        ],
        "render",
        output,
    )
    first_bundle_evidence = _verify_bundle(bundle, rendered)
    revision_bundle = output / "revision-2"
    revised = _playcli_response(
        playcli,
        [
            "render",
            str(SCORE),
            "--sound-pack",
            PACK_ID,
            "--sound-pack-version",
            PACK_VERSION,
            "--sound-pack-store",
            str(store),
            "--output",
            str(revision_bundle),
            "--previous-result",
            str(bundle),
            "--revision-reason",
            "Cross-platform packaging acceptance revision.",
        ],
        "render",
        output,
    )
    if not isinstance(installed.get("sound_pack"), dict):
        raise AcceptanceFailure("Sound Pack install evidence is invalid.")
    if not isinstance(listed.get("sound_packs"), list) or len(listed["sound_packs"]) != 1:
        raise AcceptanceFailure("Sound Pack list evidence is invalid.")
    if verified.get("sound_pack") != installed.get("sound_pack"):
        raise AcceptanceFailure("Sound Pack verify evidence is inconsistent.")
    revision_bundle_evidence = _verify_bundle(revision_bundle, revised)
    previous_unchanged = _verify_bundle(bundle, rendered) == first_bundle_evidence
    request = revised.get("performance_request")
    if not isinstance(request, dict):
        raise AcceptanceFailure("Revision history is missing.")
    history = request.get("history")
    latest_delivered = request.get("latest_delivered")
    if not isinstance(history, list) or not isinstance(latest_delivered, dict):
        raise AcceptanceFailure("Revision history contract is invalid.")
    history_kinds: list[object] = []
    for entry in history:
        if not isinstance(entry, dict) or not isinstance(entry.get("performance"), dict):
            raise AcceptanceFailure("Revision history entry is invalid.")
        history_kinds.append(entry["performance"].get("kind"))
    warnings = revised.get("warnings")
    if not isinstance(warnings, list) or not all(isinstance(item, dict) for item in warnings):
        raise AcceptanceFailure("Render warnings are invalid.")
    applied_score_defaults = revised.get("applied_score_defaults")
    score_context = revised.get("score_context")
    if (
        not isinstance(applied_score_defaults, list)
        or not all(isinstance(item, dict) for item in applied_score_defaults)
        or not isinstance(score_context, dict)
        or not all(
            isinstance(item.get("id"), str) and isinstance(item.get("field"), str)
            for item in applied_score_defaults
        )
        or {item.get("id") for item in applied_score_defaults}
        != {item.get("id") for item in warnings}
        or any(
            not isinstance(score_context.get(item.get("field")), dict)
            or score_context[item["field"]].get("source") != "default"
            for item in applied_score_defaults
        )
    ):
        raise AcceptanceFailure("Applied Score Defaults are invalid.")
    warnings_by_id = {item["id"]: item for item in warnings}
    for default in applied_score_defaults:
        field = default["field"]
        context = score_context[field]
        context_value = {key: value for key, value in context.items() if key != "source"}
        if (
            not isinstance(default.get("value"), dict)
            or default["value"] != warnings_by_id[default["id"]].get("applied_value")
            or default["value"] != context_value
        ):
            raise AcceptanceFailure("Applied Score Default values are inconsistent.")
    try:
        schedule = _json_object(
            (revision_bundle / "schedule.json").read_text(), "Result Bundle schedule"
        )
    except OSError as error:
        raise AcceptanceFailure("Result Bundle schedule is unreadable.") from error
    if schedule.get("events") != validated.get("events"):
        raise AcceptanceFailure("Validated and rendered event schedules differ.")
    dependencies = doctor.get("dependencies")
    if not isinstance(dependencies, dict):
        raise AcceptanceFailure("Doctor dependency evidence is invalid.")
    dependency_statuses = {
        key: value.get("status") if isinstance(value, dict) else None
        for key, value in dependencies.items()
        if key in {"python", "platform", "atomic_noreplace"}
    }
    if dependency_statuses != {
        "python": "available",
        "platform": "supported",
        "atomic_noreplace": "available",
    }:
        raise AcceptanceFailure("Required runtime dependency evidence is unavailable.")
    report = {
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "operation": "verify-platform-distribution",
        "status": "ok",
        "wheel": {"filename": wheel.name, "sha256": _sha256(wheel)},
        "platform": {
            "id": runtime_metadata.get("platform"),
            "machine": runtime_metadata.get("machine"),
            "python": runtime_metadata.get("python"),
            "implementation": runtime_metadata.get("implementation"),
            "description": platform.platform(),
        },
        "entry_points": {
            "console_scripts": console_scripts,
            "installed": installed_entry_points,
            "added_executables": added_executables,
            "shell_executable": True,
        },
        "wheel_metadata": wheel_metadata,
        "runtime_dependencies": {
            "required_status": "available",
            "python": dependencies.get("python"),
            "platform": dependencies.get("platform"),
            "atomic_noreplace": dependencies.get("atomic_noreplace"),
            "optional_playback": dependencies.get("playback"),
        },
        "public_operations": {
            "doctor": doctor["status"],
            "validate": validated["status"],
            "sound-pack.install": installed["status"],
            "sound-pack.list": listed["status"],
            "sound-pack.verify": verified["status"],
            "render": revised["status"],
        },
        "contract": {
            "applied_score_defaults": applied_score_defaults,
            "response_schema_version": revised["schema_version"],
            "render_status": revised["status"],
            "bundle_schema_version": revised["result_bundle"]["schema_version"],
            "bundle_status": revised["result_bundle"]["status"],
            "bundle_artifacts": list(BUNDLE_FILES),
            "bundle_directory_mode": "0555",
            "bundle_file_mode": "0444",
            "bundle_file_nlink": 1,
            "artifact_hash_contract": HASH_CONTRACT,
            "event_schedule": schedule,
            "provenance": revised.get("provenance"),
            "score_context": score_context,
            "sound_pack": revised.get("sound_pack"),
            "revision": {
                "current_sequence": request.get("current_sequence"),
                "current_delivered_revision": request.get("current_delivered_revision"),
                "history_length": len(history),
                "history_kinds": history_kinds,
                "latest_delivered_revision": latest_delivered.get("revision"),
                "latest_delivered_sequence": latest_delivered.get("sequence"),
                "previous_unchanged": previous_unchanged,
            },
            "warnings": warnings,
            "wav": {
                "format": revised["wav"]["format"],
                "channels": revision_bundle_evidence["wav"]["channels"],
                "sample_rate_hz": revision_bundle_evidence["wav"]["sample_rate_hz"],
                "frames": revision_bundle_evidence["wav"]["frames"],
                "duration_seconds": revised["wav"]["duration_seconds"],
                "sample_width_bytes": revision_bundle_evidence["wav"]["sample_width_bytes"],
                "frame_bytes_complete": revision_bundle_evidence["wav"]["frame_bytes_complete"],
            },
        },
        "result_bundle_evidence": {
            "initial": first_bundle_evidence,
            "revision": revision_bundle_evidence,
        },
        "verifier": {"sha256": _sha256(Path(__file__))},
    }
    try:
        validate_record(report)
    except RecordContractError as error:
        raise AcceptanceFailure(f"Generated platform record is invalid: {error}") from error
    return report


def _write_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--record", type=Path)
    arguments = parser.parse_args()
    try:
        report = _accept(arguments.wheel, arguments.output)
        encoded = _write_json(report)
        if arguments.record is not None:
            with arguments.record.open("x", encoding="utf-8") as record:
                record.write(encoded)
    except (AcceptanceFailure, OSError, KeyError, TypeError, ValueError) as error:
        encoded = _write_json(
            {
                "schema_version": ACCEPTANCE_SCHEMA_VERSION,
                "operation": "verify-platform-distribution",
                "status": "error",
                "error": {"id": "platform_acceptance_failed", "message": str(error)},
            }
        )
        sys.stdout.write(encoded)
        print(f"Platform distribution acceptance failed: {error}", file=sys.stderr)
        return 1
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
