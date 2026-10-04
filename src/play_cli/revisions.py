from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from play_cli import sound_pack_storage as storage
from play_cli.result_bundle import (
    PublishedResultBundle,
    ResultBundleFailure,
    open_published_result_bundle,
)

PerformanceKind = Literal["delivered", "preview"]
_REQUEST_ID = re.compile(r"request-[0-9a-f]{32}")


class RevisionFailure(Exception):
    def __init__(self, error_id: str, message: str, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.error_id = error_id
        self.message = message


@dataclass(frozen=True)
class RevisionPlan:
    previous_result: Path
    request_id: str
    sequence: int
    delivered_revision: int | None
    reason: str
    history: list[dict[str, Any]]
    current_delivered_revision: int
    latest_delivered: dict[str, Any]
    snapshots: tuple[PublishedResultBundle, ...]

    @property
    def parent_fd(self) -> int:
        return self.snapshots[0].parent_fd

    @property
    def parent_inode(self) -> tuple[int, int]:
        return self.snapshots[0].parent_inode

    def confirm(self) -> None:
        try:
            for snapshot in self.snapshots:
                snapshot.confirm()
        except ResultBundleFailure as error:
            raise RevisionFailure(
                "revision_source_invalid",
                "Previous Result Bundle is invalid or incomplete.",
                f"Previous Result Bundle validation failed: {error}.",
            ) from error

    def close(self) -> None:
        for snapshot in self.snapshots:
            snapshot.close()


def plan_revision(
    previous_result: Path | None,
    destination: Path,
    revision_reason: str | None,
    *,
    kind: PerformanceKind,
) -> RevisionPlan | None:
    if previous_result is None:
        if revision_reason is not None:
            raise RevisionFailure(
                "revision_source_required",
                "A previous Result Bundle is required for a revision reason.",
                "--revision-reason requires --previous-result.",
            )
        if kind == "preview":
            raise RevisionFailure(
                "revision_source_required",
                "A previous delivered Performance is required for a preview.",
                "--preview requires --previous-result and --revision-reason.",
            )
        return None

    reason = revision_reason.strip() if revision_reason is not None else ""
    if not reason:
        raise RevisionFailure(
            "revision_reason_required",
            "A non-empty revision reason is required.",
            "--previous-result requires a non-empty --revision-reason.",
        )
    previous = storage.absolute_path(previous_result)
    output = storage.absolute_path(destination)
    if previous == output or previous in output.parents or output in previous.parents:
        raise RevisionFailure(
            "revision_output_invalid",
            "Revision output must be separate from its previous Result Bundle.",
            f"Refusing a self-referential revision from {previous} to {output}.",
        )

    request, snapshots, previous_parent_inode = _open_validated_request(previous)
    output_parent_fd: int | None = None
    try:
        output_parent_fd = storage.open_directory(output.parent)
        output_parent_stat = os.fstat(output_parent_fd)
        if (output_parent_stat.st_dev, output_parent_stat.st_ino) != previous_parent_inode:
            raise OSError("Revision output parent is not the previous Bundle parent")
    except OSError as error:
        for snapshot in snapshots:
            snapshot.close()
        raise RevisionFailure(
            "revision_output_invalid",
            "Revision output must be separate from its previous Result Bundle.",
            f"Revision output must be a safe sibling of {previous}: {error}.",
        ) from error
    finally:
        if output_parent_fd is not None:
            os.close(output_parent_fd)

    sequence = request["current_sequence"] + 1
    current_delivered_revision = request["current_delivered_revision"]
    delivered_revision = current_delivered_revision + 1 if kind == "delivered" else None
    return RevisionPlan(
        previous_result=previous,
        request_id=request["id"],
        sequence=sequence,
        delivered_revision=delivered_revision,
        reason=reason,
        history=list(request["history"]),
        current_delivered_revision=current_delivered_revision,
        latest_delivered=dict(request["latest_delivered"]),
        snapshots=snapshots,
    )


def start_performance_request(
    destination: Path,
    manifest: dict[str, Any],
    *,
    kind: PerformanceKind,
) -> dict[str, Any]:
    request_id = _request_id(destination)
    delivered_revision = 1 if kind == "delivered" else None
    entry = _history_entry(
        request_id,
        sequence=1,
        delivered_revision=delivered_revision,
        reason=None,
        kind=kind,
        destination=destination,
        manifest=manifest,
    )
    return {
        "id": request_id,
        "current_sequence": 1,
        "current_delivered_revision": delivered_revision,
        "latest_delivered": _latest_delivered(entry) if delivered_revision is not None else None,
        "history": [entry],
    }


def complete_performance_request(
    plan: RevisionPlan | None,
    destination: Path,
    manifest: dict[str, Any],
    *,
    kind: PerformanceKind,
) -> dict[str, Any]:
    if plan is None:
        return start_performance_request(destination, manifest, kind=kind)
    plan.confirm()
    entry = _history_entry(
        plan.request_id,
        sequence=plan.sequence,
        delivered_revision=plan.delivered_revision,
        reason=plan.reason,
        kind=kind,
        destination=destination,
        manifest=manifest,
    )
    history = [*plan.history, entry]
    if plan.delivered_revision is None:
        current_delivered_revision = plan.current_delivered_revision
        latest_delivered = plan.latest_delivered
    else:
        current_delivered_revision = plan.delivered_revision
        latest_delivered = _latest_delivered(entry)
    return {
        "id": plan.request_id,
        "current_sequence": plan.sequence,
        "current_delivered_revision": current_delivered_revision,
        "latest_delivered": latest_delivered,
        "history": history,
    }


def _open_validated_request(
    previous: Path,
) -> tuple[dict[str, Any], tuple[PublishedResultBundle, ...], tuple[int, int]]:
    snapshots: list[PublishedResultBundle] = []
    try:
        previous_snapshot = open_published_result_bundle(previous)
        snapshots.append(previous_snapshot)
        request = _request_from_manifest(previous, previous_snapshot.manifest)
        _validate_request_history(request, previous, previous_snapshot.manifest, snapshots)
        return request, tuple(snapshots), previous_snapshot.parent_inode
    except (ResultBundleFailure, KeyError, TypeError, ValueError) as error:
        for snapshot in snapshots:
            snapshot.close()
        raise RevisionFailure(
            "revision_source_invalid",
            "Previous Result Bundle is invalid or incomplete.",
            f"Previous Result Bundle validation failed: {error}.",
        ) from error


def _request_from_manifest(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    request = manifest.get("performance_request")
    result_bundle = manifest.get("result_bundle")
    cli = manifest.get("cli")
    if not isinstance(result_bundle, dict) or not isinstance(cli, dict):
        raise TypeError("Result Bundle revision schema is invalid")
    bundle_schema = result_bundle.get("schema_version")
    if bundle_schema == "1.0":
        if (
            manifest.get("schema_version") != "1.3"
            or cli != {"name": "play-cli", "version": "0.5.0"}
            or request is not None
        ):
            raise ValueError("Result Bundle schema 1.0 must be an exact PLAY CLI 0.5.0 result")
        return start_performance_request(path, manifest, kind="delivered")
    compatible_contracts = {
        ("1.4", "0.6.0"),
        ("1.5", "0.7.0"),
        ("1.5", "0.8.0"),
        ("1.5", "0.8.1"),
        ("1.5", "0.9.0"),
    }
    if bundle_schema == "1.2":
        if (
            manifest.get("schema_version") != "1.6"
            or cli != {"name": "play-cli", "version": "0.10.0"}
            or not isinstance(request, dict)
        ):
            raise TypeError("Invalid voice Bundle Performance Request")
        return request
    if (
        bundle_schema != "1.1"
        or (manifest.get("schema_version"), cli.get("version")) not in compatible_contracts
        or cli.get("name") != "play-cli"
        or set(cli) != {"name", "version"}
        or not isinstance(request, dict)
    ):
        raise TypeError("Result Bundle schema 1.1 Performance Request is invalid")
    return request


def _validate_request_history(
    request: dict[str, Any],
    previous: Path,
    previous_manifest: dict[str, Any],
    snapshots: list[PublishedResultBundle],
) -> None:
    request_id = request.get("id")
    sequence = request.get("current_sequence")
    delivered_revision = request.get("current_delivered_revision")
    latest_delivered = request.get("latest_delivered")
    history = request.get("history")
    if not isinstance(request_id, str) or _REQUEST_ID.fullmatch(request_id) is None:
        raise ValueError("Performance Request ID is invalid")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ValueError("Performance Request sequence is invalid")
    if (
        not isinstance(delivered_revision, int)
        or isinstance(delivered_revision, bool)
        or delivered_revision < 1
    ):
        raise ValueError("Delivered revision is invalid")
    if not isinstance(history, list) or len(history) != sequence:
        raise ValueError("Performance Request history is not complete")

    expected_delivered = 0
    expected_latest: dict[str, Any] | None = None
    for expected_sequence, value in enumerate(history, start=1):
        if not isinstance(value, dict):
            raise TypeError("Revision history entry is not an object")
        entry = value
        entry_sequence = entry.get("sequence")
        if (
            not isinstance(entry_sequence, int)
            or isinstance(entry_sequence, bool)
            or entry_sequence != expected_sequence
        ):
            raise ValueError("Revision history ordering is invalid")
        performance = entry.get("performance")
        interpretation = entry.get("interpretation")
        bundle = entry.get("result_bundle")
        provenance = entry.get("provenance")
        if (
            not isinstance(performance, dict)
            or not isinstance(interpretation, dict)
            or not isinstance(bundle, dict)
            or not isinstance(provenance, dict)
        ):
            raise TypeError("Revision history relationship is invalid")
        kind = performance.get("kind")
        entry_delivered = entry.get("delivered_revision")
        if kind == "delivered":
            expected_delivered += 1
            if (
                not isinstance(entry_delivered, int)
                or isinstance(entry_delivered, bool)
                or entry_delivered != expected_delivered
            ):
                raise ValueError("Delivered revision ordering is invalid")
            expected_latest = _latest_delivered(entry)
        elif kind == "preview":
            if entry_delivered is not None:
                raise ValueError("Preview cannot claim a delivered revision")
        else:
            raise ValueError("Performance kind is invalid")
        reason = entry.get("reason")
        if expected_sequence == 1:
            if reason is not None:
                raise ValueError("First revision reason must be null")
        elif not isinstance(reason, str) or not reason.strip():
            raise ValueError("Later revision reason is missing")
        if interpretation.get("id") != f"{request_id}-interpretation-{expected_sequence:04d}":
            raise ValueError("Score Interpretation ID is invalid")
        interpretation_version = interpretation.get("version")
        if (
            not isinstance(interpretation_version, int)
            or isinstance(interpretation_version, bool)
            or interpretation_version != expected_sequence
        ):
            raise ValueError("Score Interpretation version is invalid")
        if performance.get("id") != f"{request_id}-performance-{expected_sequence:04d}":
            raise ValueError("Performance ID is invalid")
        performance_sequence = performance.get("sequence")
        if (
            not isinstance(performance_sequence, int)
            or isinstance(performance_sequence, bool)
            or performance_sequence != expected_sequence
        ):
            raise ValueError("Performance sequence is invalid")
        if expected_sequence == sequence:
            historical = previous_manifest
        else:
            historical_snapshot = open_published_result_bundle(Path(bundle["path"]))
            snapshots.append(historical_snapshot)
            historical = historical_snapshot.manifest
        _validate_history_relationship(entry, historical)
        _validate_historical_request(
            Path(bundle["path"]),
            historical,
            request_id=request_id,
            sequence=expected_sequence,
            delivered_revision=expected_delivered,
            latest_delivered=expected_latest,
            history=history[:expected_sequence],
        )

    if expected_delivered != delivered_revision or expected_latest != latest_delivered:
        raise ValueError("Latest delivered Performance pointer is invalid")
    last_bundle = history[-1]["result_bundle"]
    if last_bundle.get("path") != str(previous):
        raise ValueError("Previous Result Bundle is not the latest history entry")


def _validate_history_relationship(entry: dict[str, Any], historical: dict[str, Any]) -> None:
    bundle = entry["result_bundle"]
    interpretation = entry["interpretation"]
    performance = entry["performance"]
    provenance = entry["provenance"]
    bundle_path = Path(bundle["path"])
    if bundle.get("manifest") != str(bundle_path / "result.json"):
        raise ValueError("Historical Result Bundle manifest path is invalid")
    if interpretation.get("score") != historical["result_bundle"]["artifacts"]["score"]:
        raise ValueError("Historical score relationship is invalid")
    if interpretation.get("warnings") != historical["warnings"]:
        raise ValueError("Historical warning relationship is invalid")
    if performance.get("wav") != historical["result_bundle"]["artifacts"]["performance"]:
        raise ValueError("Historical Performance relationship is invalid")
    if provenance != {
        "cli": historical["cli"],
        "renderer": historical["renderer"],
        "sound_pack": historical["sound_pack"],
    }:
        raise ValueError("Historical provenance relationship is invalid")


def _validate_historical_request(
    path: Path,
    manifest: dict[str, Any],
    *,
    request_id: str,
    sequence: int,
    delivered_revision: int,
    latest_delivered: dict[str, Any] | None,
    history: list[Any],
) -> None:
    historical_request = _request_from_manifest(path, manifest)
    if historical_request != {
        "id": request_id,
        "current_sequence": sequence,
        "current_delivered_revision": delivered_revision,
        "latest_delivered": latest_delivered,
        "history": history,
    }:
        raise ValueError("Historical Performance Request is not an exact lineage prefix")


def _request_id(destination: Path) -> str:
    seed = f"play-cli-performance-request\0{destination}".encode()
    return f"request-{hashlib.sha256(seed).hexdigest()[:32]}"


def _history_entry(
    request_id: str,
    *,
    sequence: int,
    delivered_revision: int | None,
    reason: str | None,
    kind: PerformanceKind,
    destination: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    artifacts = manifest["result_bundle"]["artifacts"]
    return {
        "sequence": sequence,
        "delivered_revision": delivered_revision,
        "reason": reason,
        "result_bundle": {
            "path": str(destination),
            "manifest": str(destination / "result.json"),
        },
        "interpretation": {
            "id": f"{request_id}-interpretation-{sequence:04d}",
            "version": sequence,
            "score": artifacts["score"],
            "warnings": manifest["warnings"],
        },
        "performance": {
            "id": f"{request_id}-performance-{sequence:04d}",
            "sequence": sequence,
            "kind": kind,
            "wav": artifacts["performance"],
        },
        "provenance": {
            "cli": manifest["cli"],
            "renderer": manifest["renderer"],
            "sound_pack": manifest["sound_pack"],
        },
    }


def _latest_delivered(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "revision": entry["delivered_revision"],
        "sequence": entry["sequence"],
        "performance_id": entry["performance"]["id"],
        "result_bundle": entry["result_bundle"]["path"],
    }
