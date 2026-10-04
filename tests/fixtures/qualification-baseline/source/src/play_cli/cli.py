from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

from play_cli import __version__
from play_cli.contract import event_json, score_context_json, warning_json
from play_cli.machine_contract import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    OperationContext,
)
from play_cli.playback import play_performance
from play_cli.renderer import RENDERER_ID, RENDERER_VERSION, RenderFailure, render_pcm_schedule
from play_cli.result_bundle import (
    BUNDLE_SCHEMA_VERSION,
    PendingResultBundle,
    ResultBundleFailure,
    encode_json,
    prepare_result_bundle,
    prepare_result_bundle_in_parent,
)
from play_cli.revisions import (
    PerformanceKind,
    RevisionFailure,
    RevisionPlan,
    complete_performance_request,
    plan_revision,
)
from play_cli.score import ScoreFailure, parse_score_bytes
from play_cli.sound_pack import (
    InstallResult,
    SoundPackFailure,
    VerifiedSoundPack,
    install_local_pack,
    list_installed_packs,
    verify_installed_pack,
)


class _InvocationError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        raise _InvocationError(message)


def _write_response(response: dict[str, Any]) -> None:
    json.dump(response, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


def _write_error(
    *,
    operation: str | None,
    error_id: str,
    message: str,
    action: dict[str, Any] | None = None,
    context: OperationContext | None = None,
    schema: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "error": {
            "id": error_id,
            "message": message,
        }
    }
    if schema is not None:
        payload["schema"] = schema
    if action is not None:
        payload["action"] = action
    if context is None or operation is None:
        response = {
            "schema_version": SCHEMA_VERSION,
            "operation": operation,
            "status": "error",
            **payload,
        }
    else:
        response = {
            **context.envelope(operation, "error", provenance=provenance),
            **payload,
        }
    _write_response(response)


def _sound_pack_summary(sound_pack: VerifiedSoundPack) -> dict[str, str]:
    return {
        "id": sound_pack.id,
        "version": sound_pack.version,
        "pack_hash": sound_pack.pack_hash,
        "status": "verified",
        "path": str(sound_pack.path),
    }


def _install_action(store: Path) -> dict[str, Any]:
    return {
        "id": "install_sound_pack",
        "argv_template": [
            "playcli",
            "sound-pack",
            "install",
            {"required_input": "local_source"},
            "--store",
            str(store.resolve()),
        ],
        "required_inputs": {
            "local_source": {
                "type": "local_path",
                "description": "Explicit local Sound Pack directory.",
            }
        },
    }


def _sound_pack_failure(
    operation: str,
    error: SoundPackFailure,
    install_action_store: Path | None = None,
    *,
    context: OperationContext | None = None,
    provenance: dict[str, Any] | None = None,
) -> int:
    action = (
        _install_action(install_action_store)
        if error.error_id == "sound_pack_missing" and install_action_store is not None
        else None
    )
    _write_error(
        operation=operation,
        error_id=error.error_id,
        message=error.message,
        action=action,
        context=context,
        provenance=provenance,
    )
    print(f"Sound Pack operation failed: {error.diagnostic}", file=sys.stderr)
    return 4


def _install_sound_pack(source: str, store: Path, context: OperationContext | None = None) -> int:
    context = context or OperationContext.start()
    try:
        result: InstallResult = install_local_pack(source, store)
    except SoundPackFailure as error:
        return _sound_pack_failure("sound-pack.install", error, context=context)
    payload = {
        "sound_pack": _sound_pack_summary(result.sound_pack),
        "installation": {
            "result": result.result,
            "receipt": str(result.sound_pack.receipt_path),
        },
    }
    _write_response(
        {
            **context.envelope("sound-pack.install", "ok"),
            **payload,
        }
    )
    print(
        f"Sound Pack {result.result}: {result.sound_pack.id}@{result.sound_pack.version}.",
        file=sys.stderr,
    )
    return 0


def _list_sound_packs(store: Path, context: OperationContext | None = None) -> int:
    context = context or OperationContext.start()
    try:
        sound_packs = list_installed_packs(store)
    except SoundPackFailure as error:
        return _sound_pack_failure("sound-pack.list", error, context=context)
    payload = {"sound_packs": sound_packs}
    _write_response(
        {
            **context.envelope("sound-pack.list", "ok"),
            **payload,
        }
    )
    return 0


def _verify_sound_pack(
    pack_id: str,
    version: str,
    store: Path,
    context: OperationContext | None = None,
) -> int:
    context = context or OperationContext.start()
    try:
        sound_pack = verify_installed_pack(store, pack_id, version)
    except SoundPackFailure as error:
        return _sound_pack_failure("sound-pack.verify", error, store, context=context)
    payload = {"sound_pack": _sound_pack_summary(sound_pack)}
    _write_response(
        {
            **context.envelope("sound-pack.verify", "ok"),
            **payload,
        }
    )
    return 0


def _validate(score_path: Path, context: OperationContext | None = None) -> int:
    context = context or OperationContext.start()
    try:
        score_bytes = score_path.read_bytes()
    except OSError as error:
        _write_error(
            operation="validate",
            error_id="input_unreadable",
            message="Playable Score could not be read.",
            context=context,
        )
        print(f"Could not read {score_path}: {error}", file=sys.stderr)
        return 2
    score_provenance = _score_provenance(score_bytes)
    try:
        score = parse_score_bytes(score_bytes)
    except ScoreFailure as error:
        _write_error(
            operation="validate",
            error_id=error.error_id,
            message=error.message,
            context=context,
            provenance=score_provenance,
        )
        print(f"MusicXML validation failed: {error.diagnostic}", file=sys.stderr)
        return 2

    payload = {
        "score_context": score_context_json(score.context),
        "events": [event_json(event) for event in score.events],
        "warnings": [warning_json(warning) for warning in score.warnings],
    }
    _write_response({**context.envelope("validate", "ok", provenance=score_provenance), **payload})
    print(f"Validated {len(score.events)} normalized events.", file=sys.stderr)
    return 0


def _applied_score_defaults(warnings: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "id": warning["id"],
            "field": warning["field"],
            "value": warning["applied_value"],
        }
        for warning in warnings
    ]


def _score_provenance(score_bytes: bytes) -> dict[str, Any]:
    return {
        "score": {
            "format": "musicxml",
            "sha256": hashlib.sha256(score_bytes).hexdigest(),
        }
    }


def _close_revision_plan(plan: RevisionPlan | None) -> None:
    if plan is not None:
        plan.close()


def _render(
    score_path: Path,
    sound_pack_id: str,
    sound_pack_version: str,
    sound_pack_store: Path,
    result_bundle: Path,
    renderer: str,
    play: bool,
    previous_result: Path | None = None,
    revision_reason: str | None = None,
    preview: bool = False,
    context: OperationContext | None = None,
) -> int:
    context = context or OperationContext.start()
    kind: PerformanceKind = "preview" if preview else "delivered"
    try:
        revision_plan = plan_revision(
            previous_result,
            result_bundle,
            revision_reason,
            kind=kind,
        )
    except RevisionFailure as error:
        _write_error(
            operation="render",
            error_id=error.error_id,
            message=error.message,
            context=context,
        )
        print(f"Revision failed: {error}", file=sys.stderr)
        return 3

    if renderer != RENDERER_ID:
        _write_error(
            operation="render",
            error_id="renderer_unavailable",
            message="Requested renderer is unavailable.",
            context=context,
        )
        print(f"Renderer is unavailable: {renderer}", file=sys.stderr)
        _close_revision_plan(revision_plan)
        return 3

    try:
        score_bytes = score_path.read_bytes()
    except OSError as error:
        _write_error(
            operation="render",
            error_id="input_unreadable",
            message="Playable Score could not be read.",
            context=context,
        )
        print(f"Could not read {score_path}: {error}", file=sys.stderr)
        _close_revision_plan(revision_plan)
        return 2
    score_provenance = _score_provenance(score_bytes)
    try:
        score = parse_score_bytes(score_bytes)
    except ScoreFailure as error:
        _write_error(
            operation="render",
            error_id=error.error_id,
            message=error.message,
            context=context,
            provenance=score_provenance,
        )
        print(f"MusicXML validation failed: {error.diagnostic}", file=sys.stderr)
        _close_revision_plan(revision_plan)
        return 2

    renderer_provenance = {"id": RENDERER_ID, "version": RENDERER_VERSION}
    score_renderer_provenance = {
        **score_provenance,
        "renderer": renderer_provenance,
    }
    try:
        sound_pack = verify_installed_pack(sound_pack_store, sound_pack_id, sound_pack_version)
    except SoundPackFailure as error:
        result = _sound_pack_failure(
            "render",
            error,
            sound_pack_store,
            context=context,
            provenance=score_renderer_provenance,
        )
        _close_revision_plan(revision_plan)
        return result

    sound_pack_provenance = {
        "id": sound_pack.id,
        "version": sound_pack.version,
        "pack_hash": sound_pack.pack_hash,
    }
    render_provenance = {
        **score_renderer_provenance,
        "sound_pack": sound_pack_provenance,
    }
    try:
        performance = render_pcm_schedule(
            score.events,
            score.context.tempo.bpm,
            sound_pack,
        )
    except RenderFailure as error:
        _write_error(
            operation="render",
            error_id=error.error_id,
            message=error.message,
            context=context,
            provenance=render_provenance,
        )
        print(f"Render failed: {error}", file=sys.stderr)
        _close_revision_plan(revision_plan)
        return 3
    except (MemoryError, OverflowError):
        _write_error(
            operation="render",
            error_id="renderer_resource_unavailable",
            message="Renderer resources are unavailable for this Performance.",
            context=context,
            provenance=render_provenance,
        )
        print("Render failed: renderer resources are unavailable.", file=sys.stderr)
        _close_revision_plan(revision_plan)
        return 3

    pending_bundle: PendingResultBundle | None = None
    try:
        if revision_plan is None:
            pending_bundle = prepare_result_bundle(result_bundle)
        else:
            revision_plan.confirm()
            pending_bundle = prepare_result_bundle_in_parent(
                result_bundle,
                revision_plan.parent_fd,
                revision_plan.parent_inode,
            )
        destination = pending_bundle.destination
        score_context = score_context_json(score.context)
        events = [event_json(event) for event in score.events]
        warnings = [warning_json(warning) for warning in score.warnings]
        applied_defaults = _applied_score_defaults(warnings)
        validation_data = encode_json(
            {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "valid": True,
                "score_context": score_context,
                "warnings": warnings,
                "applied_score_defaults": applied_defaults,
            }
        )
        schedule_data = encode_json({"schema_version": BUNDLE_SCHEMA_VERSION, "events": events})
        pending_bundle.write_bytes("score.musicxml", score_bytes)
        pending_bundle.write_bytes("validation.json", validation_data)
        pending_bundle.write_bytes("schedule.json", schedule_data)
        pending_bundle.write_bytes("performance.wav", performance.wav_data)
        manifest_path = destination / "result.json"
        score_artifact = destination / "score.musicxml"
        validation_artifact = destination / "validation.json"
        schedule_artifact = destination / "schedule.json"
        wav_artifact = destination / "performance.wav"
        manifest = {
            "cli": {"name": "play-cli", "version": __version__},
            "score_context": score_context,
            "result_bundle": {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "status": "complete",
                "path": str(destination),
                "manifest": str(manifest_path),
                "artifacts": {
                    "score": {
                        "path": str(score_artifact),
                        "sha256": hashlib.sha256(score_bytes).hexdigest(),
                    },
                    "validation": {
                        "path": str(validation_artifact),
                        "sha256": hashlib.sha256(validation_data).hexdigest(),
                    },
                    "schedule": {
                        "path": str(schedule_artifact),
                        "sha256": hashlib.sha256(schedule_data).hexdigest(),
                    },
                    "performance": {
                        "path": str(wav_artifact),
                        "sha256": hashlib.sha256(performance.wav_data).hexdigest(),
                    },
                },
            },
            "wav": {
                "path": str(wav_artifact),
                "format": performance.wav.format,
                "sample_rate_hz": performance.wav.sample_rate_hz,
                "channels": performance.wav.channels,
                "frames": performance.wav.frames,
                "duration_seconds": performance.wav.frames / performance.wav.sample_rate_hz,
            },
            "renderer": renderer_provenance,
            "sound_pack": {
                "id": performance.sound_pack.id,
                "version": performance.sound_pack.version,
                "pack_hash": performance.sound_pack.pack_hash,
                "samples": [
                    {"midi": sample.midi, "sha256": sample.sha256}
                    for sample in performance.sound_pack.samples
                ],
            },
            "warnings": warnings,
            "applied_score_defaults": applied_defaults,
        }
        manifest["performance_request"] = complete_performance_request(
            revision_plan,
            destination,
            manifest,
            kind=kind,
        )
        manifest = {
            **context.envelope(
                "render",
                "ok",
                provenance=render_provenance,
                timing_scope="through_manifest_payload_assembly",
            ),
            **manifest,
        }
        if revision_plan is None:
            pending_bundle.publish(manifest)
        else:
            pending_bundle.publish(manifest, commit_guard=revision_plan.confirm)
    except (RenderFailure, ResultBundleFailure, RevisionFailure) as error:
        error_id = error.error_id
        message = error.message
        _write_error(
            operation="render",
            error_id=error_id,
            message=message,
            context=context,
            provenance=render_provenance,
        )
        print(f"Render failed: {error}", file=sys.stderr)
        return 3
    finally:
        if pending_bundle is not None:
            pending_bundle.close()
        _close_revision_plan(revision_plan)

    playback = play_performance(wav_artifact, play)
    response = {**manifest, "playback": playback.to_json()}
    _write_response(response)
    if playback.status in {"unavailable", "failed"}:
        print(f"Playback {playback.status}; persisted WAV remains available.", file=sys.stderr)
    completion = "Playback Candidate" if kind == "preview" else "Performance"
    print(f"Rendered complete {completion}.", file=sys.stderr)
    return 0


def main() -> int:
    schema_parser = _ArgumentParser(prog="playcli", add_help=False)
    schema_parser.add_argument("--schema-version", action="append")
    try:
        schema_arguments, remaining_arguments = schema_parser.parse_known_args()
        requested_versions = schema_arguments.schema_version or []
        if len(requested_versions) > 1:
            raise _InvocationError("--schema-version may be supplied only once")
    except _InvocationError as error:
        _write_error(
            operation=None,
            error_id="invalid_invocation",
            message="PLAY CLI invocation is invalid.",
        )
        print(f"Invalid invocation: {error}", file=sys.stderr)
        return 2

    parser = _ArgumentParser(prog="playcli", add_help=False)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    validate_parser = subparsers.add_parser("validate", add_help=False)
    validate_parser.add_argument("score", type=Path)
    render_parser = subparsers.add_parser("render", add_help=False)
    render_parser.add_argument("score", type=Path)
    render_parser.add_argument("--sound-pack", required=True)
    render_parser.add_argument("--sound-pack-version", required=True)
    render_parser.add_argument("--sound-pack-store", required=True, type=Path)
    render_parser.add_argument("--output", required=True, type=Path)
    render_parser.add_argument("--renderer", default=RENDERER_ID)
    render_parser.add_argument("--play", action="store_true")
    render_parser.add_argument("--previous-result", type=Path)
    render_parser.add_argument("--revision-reason")
    render_parser.add_argument("--preview", action="store_true")
    sound_pack_parser = subparsers.add_parser("sound-pack", add_help=False)
    sound_pack_subparsers = sound_pack_parser.add_subparsers(
        dest="sound_pack_operation", required=True
    )
    install_parser = sound_pack_subparsers.add_parser("install", add_help=False)
    install_parser.add_argument("source")
    install_parser.add_argument("--store", required=True, type=Path)
    list_parser = sound_pack_subparsers.add_parser("list", add_help=False)
    list_parser.add_argument("--store", required=True, type=Path)
    verify_parser = sound_pack_subparsers.add_parser("verify", add_help=False)
    verify_parser.add_argument("id")
    verify_parser.add_argument("--version", required=True)
    verify_parser.add_argument("--store", required=True, type=Path)
    try:
        arguments = parser.parse_args(remaining_arguments)
    except _InvocationError as error:
        _write_error(
            operation=None,
            error_id="invalid_invocation",
            message="PLAY CLI invocation is invalid.",
        )
        print(f"Invalid invocation: {error}", file=sys.stderr)
        return 2

    if arguments.operation == "sound-pack":
        operation = f"sound-pack.{arguments.sound_pack_operation}"
    else:
        operation = arguments.operation
    context = OperationContext.start()
    requested_schema = requested_versions[0] if requested_versions else SCHEMA_VERSION
    if requested_schema not in SUPPORTED_SCHEMA_VERSIONS:
        _write_error(
            operation=operation,
            error_id="schema_version_unsupported",
            message="Requested response schema version is unsupported.",
            context=context,
            schema={
                "requested": requested_schema,
                "supported": list(SUPPORTED_SCHEMA_VERSIONS),
                "default": SCHEMA_VERSION,
            },
        )
        print(f"Unsupported response schema version: {requested_schema}", file=sys.stderr)
        return 2

    if arguments.operation == "validate":
        return _validate(arguments.score, context)
    if arguments.operation == "render":
        return _render(
            arguments.score,
            arguments.sound_pack,
            arguments.sound_pack_version,
            arguments.sound_pack_store,
            arguments.output,
            arguments.renderer,
            arguments.play,
            arguments.previous_result,
            arguments.revision_reason,
            arguments.preview,
            context,
        )
    if arguments.sound_pack_operation == "install":
        return _install_sound_pack(arguments.source, arguments.store, context)
    if arguments.sound_pack_operation == "list":
        return _list_sound_packs(arguments.store, context)
    return _verify_sound_pack(arguments.id, arguments.version, arguments.store, context)
