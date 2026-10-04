from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.agent_contract import assert_machine_response

ROOT = Path(__file__).parents[1]
PLAYCLI = Path(sys.executable).with_name("playcli")
PACK_ID = "play-cli-performance-test-piano"
PACK_VERSION = "1.0.0"
PACK = ROOT / "tests/fixtures/performance-test-piano-pack"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(PLAYCLI), *arguments],
        capture_output=True,
        check=False,
        text=True,
    )


def _install_pack(tmp_path: Path) -> Path:
    store = tmp_path / "packs"
    installed = _run("sound-pack", "install", str(PACK), "--store", str(store))
    assert installed.returncode == 0, installed.stderr
    return store


def _render(
    score: Path,
    store: Path,
    output: Path,
    *revision_arguments: str,
) -> subprocess.CompletedProcess[str]:
    return _run(
        "render",
        str(score),
        "--sound-pack",
        PACK_ID,
        "--sound-pack-version",
        PACK_VERSION,
        "--sound-pack-store",
        str(store),
        "--output",
        str(output),
        *revision_arguments,
    )


def _response(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stdout)


def _start_revision_waiting_for_score(
    tmp_path: Path,
    store: Path,
    previous: Path,
    output: Path,
) -> tuple[subprocess.Popen[str], int]:
    blocked_score = tmp_path / f"{output.name}-blocked.musicxml"
    os.mkfifo(blocked_score)
    command = [
        str(PLAYCLI),
        "render",
        str(blocked_score),
        "--sound-pack",
        PACK_ID,
        "--sound-pack-version",
        PACK_VERSION,
        "--sound-pack-store",
        str(store),
        "--output",
        str(output),
        "--previous-result",
        str(previous),
        "--revision-reason",
        "Agent User reported a mismatch.",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 5
    while True:
        try:
            writer = os.open(blocked_score, os.O_WRONLY | os.O_NONBLOCK)
            return process, writer
        except OSError as error:
            if error.errno != errno.ENXIO:
                raise
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.005)


def _finish_blocked_revision(
    process: subprocess.Popen[str],
    writer: int,
) -> tuple[str, str]:
    try:
        os.write(writer, (ROOT / "tests/fixtures/minimal.musicxml").read_bytes())
    finally:
        os.close(writer)
    return process.communicate(timeout=10)


def _encoded_json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _convert_bundle_to_cli_060(bundle: Path) -> None:
    _convert_to_five_artifacts(bundle)
    manifest_path = bundle / "result.json"
    bundle.chmod(0o755)
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("provenance")
    manifest.pop("operation_timing")
    manifest["schema_version"] = "1.4"
    manifest["cli"]["version"] = "0.6.0"
    for entry in manifest["performance_request"]["history"]:
        entry["provenance"]["cli"]["version"] = "0.6.0"
    manifest_path.write_bytes(_encoded_json(manifest))
    manifest_path.chmod(0o444)
    bundle.chmod(0o555)


def _convert_bundle_to_cli_070(bundle: Path) -> None:
    _convert_to_five_artifacts(bundle)
    bundle.chmod(0o755)
    manifest_path = bundle / "result.json"
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text())
    manifest["cli"]["version"] = "0.7.0"
    manifest["schema_version"] = "1.5"
    manifest["provenance"]["schema_version"] = "1.5"
    manifest["provenance"]["cli"]["version"] = "0.7.0"
    for entry in manifest["performance_request"]["history"]:
        entry["provenance"]["cli"]["version"] = "0.7.0"
    manifest_path.write_bytes(_encoded_json(manifest))
    manifest_path.chmod(0o444)
    bundle.chmod(0o555)


def _convert_to_five_artifacts(bundle: Path) -> None:
    bundle.chmod(0o755)
    manifest_path = bundle / "result.json"
    manifest = json.loads(manifest_path.read_text())
    for voice in manifest["result_bundle"].pop("voices"):
        (bundle / voice["relative_path"]).unlink()
    manifest.pop("mix")
    manifest["result_bundle"]["schema_version"] = "1.1"
    for key, name in (("validation", "validation.json"), ("schedule", "schedule.json")):
        path = bundle / name
        data = json.loads(path.read_text())
        data["schema_version"] = "1.1"
        if key == "schedule":
            for event in data["events"]:
                for field in ("voice", "staff", "staves", "sources", "event_id", "pages"):
                    event.pop(field)
        path.chmod(0o644)
        path.write_bytes(_encoded_json(data))
        path.chmod(0o444)
        manifest["result_bundle"]["artifacts"][key]["sha256"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    manifest_path.chmod(0o644)
    manifest_path.write_bytes(_encoded_json(manifest))
    manifest_path.chmod(0o444)
    bundle.chmod(0o555)


def test_agent_receives_identifiable_first_interpretation_and_performance(
    tmp_path: Path,
) -> None:
    store = _install_pack(tmp_path)
    bundle = tmp_path / "revision-1"

    rendered = _render(ROOT / "tests/fixtures/renderable-c4.musicxml", store, bundle)

    assert rendered.returncode == 0, rendered.stderr
    response = _response(rendered)
    assert response["schema_version"] == "1.6"
    assert response["cli"] == {"name": "play-cli", "version": "0.10.0"}
    assert response["result_bundle"]["schema_version"] == "1.2"
    request = response["performance_request"]
    assert re.fullmatch(r"request-[0-9a-f]{32}", request["id"])
    assert request["current_sequence"] == 1
    assert request["current_delivered_revision"] == 1
    assert len(request["history"]) == 1
    revision = request["history"][0]
    assert revision == {
        "sequence": 1,
        "delivered_revision": 1,
        "reason": None,
        "result_bundle": {
            "path": str(bundle.resolve()),
            "manifest": str((bundle / "result.json").resolve()),
        },
        "interpretation": {
            "id": f"{request['id']}-interpretation-0001",
            "version": 1,
            "score": response["result_bundle"]["artifacts"]["score"],
            "warnings": [],
        },
        "performance": {
            "id": f"{request['id']}-performance-0001",
            "sequence": 1,
            "kind": "delivered",
            "wav": response["result_bundle"]["artifacts"]["performance"],
        },
        "provenance": {
            "cli": response["cli"],
            "renderer": response["renderer"],
            "sound_pack": response["sound_pack"],
        },
    }
    assert request["latest_delivered"] == {
        "revision": 1,
        "sequence": 1,
        "performance_id": revision["performance"]["id"],
        "result_bundle": str(bundle.resolve()),
    }
    persisted = json.loads((bundle / "result.json").read_text())
    assert persisted["performance_request"] == request


def test_agent_can_render_a_revision_without_changing_the_previous_result(
    tmp_path: Path,
) -> None:
    store = _install_pack(tmp_path)
    first_bundle = tmp_path / "revision-1"
    second_bundle = tmp_path / "revision-2"
    first = _render(
        ROOT / "tests/fixtures/supported-monophonic.musicxml",
        store,
        first_bundle,
    )
    assert first.returncode == 0, first.stderr
    first_response = _response(first)
    first_snapshot = {
        item.name: (item.read_bytes(), item.stat().st_ino, stat.S_IMODE(item.stat().st_mode))
        for item in first_bundle.iterdir()
    }

    second = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        second_bundle,
        "--previous-result",
        str(first_bundle),
        "--revision-reason",
        "Agent User said the later part sounded wrong; interpretation was re-examined.",
    )

    assert second.returncode == 0, second.stderr
    response = _response(second)
    request = response["performance_request"]
    assert request["id"] == first_response["performance_request"]["id"]
    assert request["current_sequence"] == 2
    assert request["current_delivered_revision"] == 2
    assert [entry["sequence"] for entry in request["history"]] == [1, 2]
    assert [entry["delivered_revision"] for entry in request["history"]] == [1, 2]
    assert request["history"][0] == first_response["performance_request"]["history"][0]
    current = request["history"][1]
    assert current["reason"] == (
        "Agent User said the later part sounded wrong; interpretation was re-examined."
    )
    assert current["interpretation"]["warnings"] == response["warnings"]
    assert current["interpretation"]["score"] == response["result_bundle"]["artifacts"]["score"]
    assert current["performance"]["kind"] == "delivered"
    assert current["performance"]["wav"] == response["result_bundle"]["artifacts"]["performance"]
    assert current["provenance"] == {
        "cli": response["cli"],
        "renderer": response["renderer"],
        "sound_pack": response["sound_pack"],
    }
    assert (
        request["history"][0]["interpretation"]["score"]["sha256"]
        != current["interpretation"]["score"]["sha256"]
    )
    assert (
        request["history"][0]["performance"]["wav"]["sha256"]
        != current["performance"]["wav"]["sha256"]
    )
    assert request["latest_delivered"] == {
        "revision": 2,
        "sequence": 2,
        "performance_id": current["performance"]["id"],
        "result_bundle": str(second_bundle.resolve()),
    }

    assert {
        item.name: (item.read_bytes(), item.stat().st_ino, stat.S_IMODE(item.stat().st_mode))
        for item in first_bundle.iterdir()
    } == first_snapshot
    for entry in request["history"]:
        bundle = Path(entry["result_bundle"]["path"])
        assert json.loads(Path(entry["result_bundle"]["manifest"]).read_text())["status"] == "ok"
        score = Path(entry["interpretation"]["score"]["path"])
        wav = Path(entry["performance"]["wav"]["path"])
        assert score.parent == bundle
        assert wav.parent == bundle
        assert (
            hashlib.sha256(score.read_bytes()).hexdigest()
            == entry["interpretation"]["score"]["sha256"]
        )
        assert hashlib.sha256(wav.read_bytes()).hexdigest() == entry["performance"]["wav"]["sha256"]
        assert {item.name for item in bundle.iterdir()} == {
            "score.musicxml",
            "validation.json",
            "schedule.json",
            "performance.wav",
            "result.json",
            "voice-0001.wav",
        }
        assert stat.S_IMODE(bundle.stat().st_mode) == 0o555
        assert all(stat.S_IMODE(item.stat().st_mode) == 0o444 for item in bundle.iterdir())
        assert all(item.stat().st_nlink == 1 for item in bundle.iterdir())


def test_agent_must_supply_a_non_empty_reason_for_a_later_render(tmp_path: Path) -> None:
    store = _install_pack(tmp_path)
    first_bundle = tmp_path / "revision-1"
    second_bundle = tmp_path / "revision-2"
    first = _render(ROOT / "tests/fixtures/renderable-c4.musicxml", store, first_bundle)
    assert first.returncode == 0, first.stderr
    old_manifest = (first_bundle / "result.json").read_bytes()

    missing = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        second_bundle,
        "--previous-result",
        str(first_bundle),
    )
    blank = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        second_bundle,
        "--previous-result",
        str(first_bundle),
        "--revision-reason",
        "   ",
    )
    preview_without_feedback = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        second_bundle,
        "--previous-result",
        str(first_bundle),
        "--preview",
    )

    for failed in (missing, blank, preview_without_feedback):
        assert failed.returncode == 3
        assert _response(failed)["error"] == {
            "id": "revision_reason_required",
            "message": "A non-empty revision reason is required.",
        }
        assert "revision" in failed.stderr.lower()
    assert (first_bundle / "result.json").read_bytes() == old_manifest
    assert not second_bundle.exists()
    assert not (tmp_path / ".revision-2.pending").exists()


def test_agent_cannot_start_revision_or_preview_metadata_without_a_previous_result(
    tmp_path: Path,
) -> None:
    store = _install_pack(tmp_path)
    output = tmp_path / "result"

    reason_without_previous = _render(
        ROOT / "tests/fixtures/renderable-c4.musicxml",
        store,
        output,
        "--revision-reason",
        "Agent User reported a mismatch.",
    )
    preview_without_previous = _render(
        ROOT / "tests/fixtures/renderable-c4.musicxml",
        store,
        output,
        "--preview",
    )

    for failed in (reason_without_previous, preview_without_previous):
        assert failed.returncode == 3
        assert _response(failed)["error"]["id"] == "revision_source_required"
    assert not output.exists()


def test_agent_can_distinguish_a_preview_from_the_latest_delivered_performance(
    tmp_path: Path,
) -> None:
    store = _install_pack(tmp_path)
    delivered_1 = tmp_path / "delivered-1"
    preview_bundle = tmp_path / "preview-2"
    delivered_2 = tmp_path / "delivered-3"
    first = _render(ROOT / "tests/fixtures/renderable-c4.musicxml", store, delivered_1)
    assert first.returncode == 0, first.stderr
    first_response = _response(first)

    preview = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        preview_bundle,
        "--previous-result",
        str(delivered_1),
        "--revision-reason",
        "Agent User reported that the later part sounded wrong; testing one interpretation.",
        "--preview",
    )

    assert preview.returncode == 0, preview.stderr
    assert "rendered complete playback candidate" in preview.stderr.lower()
    assert "rendered complete performance" not in preview.stderr.lower()
    preview_request = _response(preview)["performance_request"]
    assert preview_request["current_sequence"] == 2
    assert preview_request["current_delivered_revision"] == 1
    assert [entry["delivered_revision"] for entry in preview_request["history"]] == [1, None]
    assert preview_request["history"][-1]["performance"]["kind"] == "preview"
    assert preview_request["latest_delivered"] == {
        "revision": 1,
        "sequence": 1,
        "performance_id": first_response["performance_request"]["history"][0]["performance"]["id"],
        "result_bundle": str(delivered_1.resolve()),
    }

    final = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        delivered_2,
        "--previous-result",
        str(preview_bundle),
        "--revision-reason",
        "Agent User preferred the preview; delivering that revised interpretation.",
    )

    assert final.returncode == 0, final.stderr
    final_request = _response(final)["performance_request"]
    assert final_request["current_sequence"] == 3
    assert final_request["current_delivered_revision"] == 2
    assert [entry["sequence"] for entry in final_request["history"]] == [1, 2, 3]
    assert [entry["delivered_revision"] for entry in final_request["history"]] == [1, None, 2]
    assert [entry["performance"]["kind"] for entry in final_request["history"]] == [
        "delivered",
        "preview",
        "delivered",
    ]
    assert final_request["history"][-1]["reason"] == (
        "Agent User preferred the preview; delivering that revised interpretation."
    )
    assert final_request["latest_delivered"] == {
        "revision": 2,
        "sequence": 3,
        "performance_id": final_request["history"][-1]["performance"]["id"],
        "result_bundle": str(delivered_2.resolve()),
    }
    assert (preview_bundle / "performance.wav").is_file()
    assert stat.S_IMODE(preview_bundle.stat().st_mode) == 0o555


def test_agent_failed_revision_preserves_every_previous_artifact(tmp_path: Path) -> None:
    store = _install_pack(tmp_path)
    first_bundle = tmp_path / "revision-1"
    failed_bundle = tmp_path / "revision-2"
    first = _render(ROOT / "tests/fixtures/renderable-c4.musicxml", store, first_bundle)
    assert first.returncode == 0, first.stderr
    old_snapshot = {
        item.name: (item.read_bytes(), item.stat().st_ino, stat.S_IMODE(item.stat().st_mode))
        for item in first_bundle.iterdir()
    }

    failed = _render(
        ROOT / "tests/fixtures/invalid-malformed.musicxml",
        store,
        failed_bundle,
        "--previous-result",
        str(first_bundle),
        "--revision-reason",
        "Agent User reported a mismatch; the replacement interpretation is malformed.",
    )

    assert failed.returncode == 2
    assert _response(failed)["error"]["id"] == "musicxml_invalid"
    assert {
        item.name: (item.read_bytes(), item.stat().st_ino, stat.S_IMODE(item.stat().st_mode))
        for item in first_bundle.iterdir()
    } == old_snapshot
    assert not failed_bundle.exists()
    assert not (tmp_path / ".revision-2.pending").exists()


def test_agent_rejects_a_fifo_in_revision_history_without_waiting_for_a_writer(
    tmp_path: Path,
) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    score = previous / "score.musicxml"
    os.mkfifo(score)
    keep = previous / "KEEP"
    keep.write_bytes(b"retained user asset")
    original_entries = {item.name: item.lstat() for item in previous.iterdir()}
    output = tmp_path / "revision"
    store = tmp_path / "missing-store"
    result = subprocess.run(
        [
            str(PLAYCLI), "render", str(ROOT / "tests/fixtures/minimal.musicxml"),
            "--sound-pack", PACK_ID, "--sound-pack-version", PACK_VERSION,
            "--sound-pack-store", str(store), "--output", str(output),
            "--previous-result", str(previous), "--revision-reason", "Recheck ending",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 3, result.stderr
    response = assert_machine_response(result, operation="render", status="error")
    assert response["error"]["id"] == "revision_source_invalid"
    assert result.stderr and "Traceback" not in result.stderr
    assert not output.exists()
    assert not (tmp_path / ".revision.pending").exists()
    assert not store.exists()
    assert {item.name: item.lstat() for item in previous.iterdir()} == original_entries
    assert stat.S_ISFIFO(score.lstat().st_mode)
    assert keep.read_bytes() == b"retained user asset"


def test_agent_rejects_self_referential_revision_outputs(tmp_path: Path) -> None:
    store = _install_pack(tmp_path)
    first_bundle = tmp_path / "revision-1"
    first = _render(ROOT / "tests/fixtures/renderable-c4.musicxml", store, first_bundle)
    assert first.returncode == 0, first.stderr
    old_manifest = (first_bundle / "result.json").read_bytes()

    for output in (first_bundle, first_bundle / "nested-revision"):
        failed = _render(
            ROOT / "tests/fixtures/minimal.musicxml",
            store,
            output,
            "--previous-result",
            str(first_bundle),
            "--revision-reason",
            "Agent User reported a mismatch.",
        )
        assert failed.returncode == 3
        assert _response(failed)["error"] == {
            "id": "revision_output_invalid",
            "message": "Revision output must be separate from its previous Result Bundle.",
        }

    assert (first_bundle / "result.json").read_bytes() == old_manifest
    assert {item.name for item in first_bundle.iterdir()} == {
        "score.musicxml",
        "validation.json",
        "schedule.json",
        "performance.wav",
        "result.json",
        "voice-0001.wav",
    }


def test_agent_rejects_a_revision_output_outside_the_previous_bundle_parent(
    tmp_path: Path,
) -> None:
    store = _install_pack(tmp_path)
    first_parent = tmp_path / "request"
    second_parent = tmp_path / "other"
    first_parent.mkdir()
    second_parent.mkdir()
    previous = first_parent / "revision-1"
    output = second_parent / "revision-2"
    first = _render(ROOT / "tests/fixtures/renderable-c4.musicxml", store, previous)
    assert first.returncode == 0, first.stderr

    failed = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        output,
        "--previous-result",
        str(previous),
        "--revision-reason",
        "Agent User reported a mismatch.",
    )

    assert failed.returncode == 3
    assert _response(failed)["error"] == {
        "id": "revision_output_invalid",
        "message": "Revision output must be separate from its previous Result Bundle.",
    }
    assert not output.exists()


@pytest.mark.parametrize("tamper", ["extra", "missing", "replacement"])
def test_agent_revalidates_history_after_rendering_before_publication(
    tamper: str,
    tmp_path: Path,
) -> None:
    store = _install_pack(tmp_path)
    previous = tmp_path / "revision-1"
    output = tmp_path / "revision-2"
    first = _render(ROOT / "tests/fixtures/renderable-c4.musicxml", store, previous)
    assert first.returncode == 0, first.stderr
    replacement_source = tmp_path / "unrelated-result"
    if tamper == "replacement":
        replacement = _render(
            ROOT / "tests/fixtures/minimal.musicxml",
            store,
            replacement_source,
        )
        assert replacement.returncode == 0, replacement.stderr
    process, writer = _start_revision_waiting_for_score(tmp_path, store, previous, output)
    try:
        moved = tmp_path / "moved-revision-1"
        if tamper == "extra":
            previous.chmod(0o755)
            (previous / "EXTRA").write_text("unexpected\n")
            previous.chmod(0o555)
            watched = [previous]
        elif tamper == "missing":
            previous.rename(moved)
            watched = [moved]
        else:
            previous.rename(moved)
            replacement_source.rename(previous)
            watched = [moved, previous]
        tampered_snapshot = {
            str(bundle): {item.name: item.read_bytes() for item in bundle.iterdir()}
            for bundle in watched
        }
    finally:
        stdout, stderr = _finish_blocked_revision(process, writer)

    assert process.returncode == 3, stderr
    assert json.loads(stdout)["error"]["id"] == "revision_source_invalid"
    assert {
        str(bundle): {item.name: item.read_bytes() for item in bundle.iterdir()}
        for bundle in watched
    } == tampered_snapshot
    assert not output.exists()


def test_agent_rejects_a_nonprefix_manifest_in_earlier_history(tmp_path: Path) -> None:
    store = _install_pack(tmp_path)
    first_bundle = tmp_path / "revision-1"
    second_bundle = tmp_path / "revision-2"
    third_bundle = tmp_path / "revision-3"
    first = _render(ROOT / "tests/fixtures/renderable-c4.musicxml", store, first_bundle)
    second = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        second_bundle,
        "--previous-result",
        str(first_bundle),
        "--revision-reason",
        "Agent User reported a mismatch.",
    )
    assert first.returncode == second.returncode == 0
    manifest_path = first_bundle / "result.json"
    first_bundle.chmod(0o755)
    manifest_path.chmod(0o644)
    malformed = json.loads(manifest_path.read_text())
    malformed.pop("performance_request")
    manifest_path.write_bytes(_encoded_json(malformed))
    manifest_path.chmod(0o444)
    first_bundle.chmod(0o555)
    second_snapshot = {item.name: item.read_bytes() for item in second_bundle.iterdir()}

    failed = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        third_bundle,
        "--previous-result",
        str(second_bundle),
        "--revision-reason",
        "Agent User supplied new feedback.",
    )

    assert failed.returncode == 3
    assert _response(failed)["error"]["id"] == "revision_source_invalid"
    assert {item.name: item.read_bytes() for item in second_bundle.iterdir()} == second_snapshot
    assert not third_bundle.exists()


def test_agent_does_not_touch_a_rebound_output_parent(tmp_path: Path) -> None:
    store = _install_pack(tmp_path)
    request_parent = tmp_path / "request"
    request_parent.mkdir()
    previous = request_parent / "revision-1"
    output = request_parent / "revision-2"
    first = _render(ROOT / "tests/fixtures/renderable-c4.musicxml", store, previous)
    assert first.returncode == 0, first.stderr
    previous_snapshot = {item.name: item.read_bytes() for item in previous.iterdir()}
    process, writer = _start_revision_waiting_for_score(tmp_path, store, previous, output)

    moved_parent = tmp_path / "moved-request"
    request_parent.rename(moved_parent)
    request_parent.mkdir()
    (request_parent / "KEEP").write_text("external asset\n")
    stdout, stderr = _finish_blocked_revision(process, writer)

    assert process.returncode == 3, stderr
    assert json.loads(stdout)["error"]["id"] == "revision_source_invalid"
    assert {item.name for item in request_parent.iterdir()} == {"KEEP"}
    assert (request_parent / "KEEP").read_text() == "external asset\n"
    assert {
        item.name: item.read_bytes() for item in (moved_parent / "revision-1").iterdir()
    } == previous_snapshot
    assert not output.exists()


def test_agent_can_extend_a_legacy_bundle_without_rewriting_it(tmp_path: Path) -> None:
    store = _install_pack(tmp_path)
    legacy_bundle = tmp_path / "legacy-result"
    revised_bundle = tmp_path / "revision-2"
    rendered = _render(ROOT / "tests/fixtures/renderable-c4.musicxml", store, legacy_bundle)
    assert rendered.returncode == 0, rendered.stderr

    legacy_bundle.chmod(0o755)
    _convert_to_five_artifacts(legacy_bundle)
    legacy_bundle.chmod(0o755)
    for name in ("validation.json", "schedule.json"):
        path = legacy_bundle / name
        path.chmod(0o644)
        payload = json.loads(path.read_text())
        payload["schema_version"] = "1.0"
        path.write_bytes(_encoded_json(payload))
        path.chmod(0o444)
    manifest_path = legacy_bundle / "result.json"
    manifest_path.chmod(0o644)
    legacy_manifest = json.loads(manifest_path.read_text())
    legacy_manifest.pop("performance_request")
    legacy_manifest.pop("provenance")
    legacy_manifest.pop("operation_timing")
    legacy_manifest["schema_version"] = "1.3"
    legacy_manifest["cli"]["version"] = "0.5.0"
    legacy_manifest["result_bundle"]["schema_version"] = "1.0"
    for key, name in (("validation", "validation.json"), ("schedule", "schedule.json")):
        legacy_manifest["result_bundle"]["artifacts"][key]["sha256"] = hashlib.sha256(
            (legacy_bundle / name).read_bytes()
        ).hexdigest()
    manifest_path.write_bytes(_encoded_json(legacy_manifest))
    manifest_path.chmod(0o444)
    legacy_bundle.chmod(0o555)
    legacy_snapshot = {item.name: item.read_bytes() for item in legacy_bundle.iterdir()}

    revised = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        revised_bundle,
        "--previous-result",
        str(legacy_bundle),
        "--revision-reason",
        "Agent User reported that the first interpretation sounded incomplete.",
    )

    assert revised.returncode == 0, revised.stderr
    request = _response(revised)["performance_request"]
    assert request["current_sequence"] == 2
    assert request["current_delivered_revision"] == 2
    assert request["history"][0]["provenance"]["cli"]["version"] == "0.5.0"
    assert request["history"][0]["result_bundle"]["path"] == str(legacy_bundle.resolve())
    assert {item.name: item.read_bytes() for item in legacy_bundle.iterdir()} == legacy_snapshot


def test_agent_can_extend_a_cli_060_schema_14_bundle_without_rewriting_it(
    tmp_path: Path,
) -> None:
    store = _install_pack(tmp_path)
    previous = tmp_path / "cli-060-result"
    output = tmp_path / "cli-070-revision"
    rendered = _render(ROOT / "tests/fixtures/renderable-c4.musicxml", store, previous)
    assert rendered.returncode == 0, rendered.stderr
    _convert_bundle_to_cli_060(previous)
    previous_snapshot = {item.name: item.read_bytes() for item in previous.iterdir()}

    revised = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        output,
        "--previous-result",
        str(previous),
        "--revision-reason",
        "Agent User reported that the previous interpretation sounded incomplete.",
    )

    assert revised.returncode == 0, revised.stderr
    history = _response(revised)["performance_request"]["history"]
    assert [entry["provenance"]["cli"]["version"] for entry in history] == [
        "0.6.0",
        "0.10.0",
    ]
    assert {item.name: item.read_bytes() for item in previous.iterdir()} == previous_snapshot


def test_agent_can_extend_a_cli_070_schema_15_bundle_without_rewriting_it(
    tmp_path: Path,
) -> None:
    store = _install_pack(tmp_path)
    previous = tmp_path / "cli-070-result"
    output = tmp_path / "cli-080-revision"
    rendered = _render(ROOT / "tests/fixtures/renderable-c4.musicxml", store, previous)
    assert rendered.returncode == 0, rendered.stderr
    _convert_bundle_to_cli_070(previous)
    previous_snapshot = {item.name: item.read_bytes() for item in previous.iterdir()}

    revised = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        output,
        "--previous-result",
        str(previous),
        "--revision-reason",
        "Agent User reported that the previous interpretation sounded incomplete.",
    )

    assert revised.returncode == 0, revised.stderr
    history = _response(revised)["performance_request"]["history"]
    assert [entry["provenance"]["cli"]["version"] for entry in history] == [
        "0.7.0",
        "0.10.0",
    ]
    assert {item.name: item.read_bytes() for item in previous.iterdir()} == previous_snapshot


def test_agent_rejects_a_missing_or_replaced_previous_bundle(tmp_path: Path) -> None:
    store = _install_pack(tmp_path)
    previous = tmp_path / "revision-1"
    replacement_source = tmp_path / "unrelated-result"
    output = tmp_path / "revision-2"

    missing = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        output,
        "--previous-result",
        str(previous),
        "--revision-reason",
        "Agent User reported a mismatch.",
    )
    assert missing.returncode == 3
    assert _response(missing)["error"]["id"] == "revision_source_invalid"
    assert not output.exists()

    original = _render(ROOT / "tests/fixtures/renderable-c4.musicxml", store, previous)
    replacement = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        replacement_source,
    )
    assert original.returncode == 0, original.stderr
    assert replacement.returncode == 0, replacement.stderr
    previous.rename(tmp_path / "moved-revision-1")
    replacement_source.rename(previous)
    replacement_snapshot = {item.name: item.read_bytes() for item in previous.iterdir()}

    replaced = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        output,
        "--previous-result",
        str(previous),
        "--revision-reason",
        "Agent User reported a mismatch.",
    )

    assert replaced.returncode == 3
    assert _response(replaced)["error"]["id"] == "revision_source_invalid"
    assert {item.name: item.read_bytes() for item in previous.iterdir()} == replacement_snapshot
    assert not output.exists()
    assert not (tmp_path / ".revision-2.pending").exists()


@pytest.mark.parametrize(
    "tamper",
    [
        "extra",
        "unsealed",
        "hardlink",
        "truncated-wav",
        "missing-new-history",
        "legacy-schema-with-new-history",
        "new-schema-with-legacy-cli",
        "previous-schema-with-current-cli",
        "unknown-schema",
        "invalid-status",
        "missing-provenance",
        "invalid-operation-timing",
        "boolean-history-number",
    ],
)
def test_agent_rejects_an_invalid_previous_result_bundle(
    tamper: str,
    tmp_path: Path,
) -> None:
    store = _install_pack(tmp_path)
    previous = tmp_path / "revision-1"
    output = tmp_path / "revision-2"
    rendered = _render(ROOT / "tests/fixtures/renderable-c4.musicxml", store, previous)
    assert rendered.returncode == 0, rendered.stderr

    if tamper == "extra":
        previous.chmod(0o755)
        (previous / "EXTRA").write_text("unexpected\n")
        previous.chmod(0o555)
    elif tamper == "unsealed":
        (previous / "score.musicxml").chmod(0o644)
    elif tamper == "hardlink":
        os.link(previous / "score.musicxml", tmp_path / "external-score-link.musicxml")
    elif tamper == "truncated-wav":
        wav_path = previous / "performance.wav"
        wav_path.chmod(0o644)
        wav_path.write_bytes(wav_path.read_bytes()[:44])
        wav_path.chmod(0o444)
        manifest_path = previous / "result.json"
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text())
        manifest["result_bundle"]["artifacts"]["performance"]["sha256"] = hashlib.sha256(
            wav_path.read_bytes()
        ).hexdigest()
        manifest_path.write_bytes(_encoded_json(manifest))
        manifest_path.chmod(0o444)
    elif tamper == "missing-new-history":
        manifest_path = previous / "result.json"
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text())
        manifest.pop("performance_request")
        manifest_path.write_bytes(_encoded_json(manifest))
        manifest_path.chmod(0o444)
    elif tamper == "legacy-schema-with-new-history":
        manifest_path = previous / "result.json"
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text())
        manifest["result_bundle"]["schema_version"] = "1.0"
        manifest_path.write_bytes(_encoded_json(manifest))
        manifest_path.chmod(0o444)
    elif tamper == "new-schema-with-legacy-cli":
        manifest_path = previous / "result.json"
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text())
        manifest["cli"]["version"] = "0.5.0"
        manifest_path.write_bytes(_encoded_json(manifest))
        manifest_path.chmod(0o444)
    elif tamper == "previous-schema-with-current-cli":
        manifest_path = previous / "result.json"
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text())
        manifest["schema_version"] = "1.4"
        manifest_path.write_bytes(_encoded_json(manifest))
        manifest_path.chmod(0o444)
    elif tamper == "unknown-schema":
        manifest_path = previous / "result.json"
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text())
        manifest["result_bundle"]["schema_version"] = "9.9"
        manifest_path.write_bytes(_encoded_json(manifest))
        manifest_path.chmod(0o444)
    elif tamper == "invalid-status":
        manifest_path = previous / "result.json"
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text())
        manifest["status"] = "error"
        manifest_path.write_bytes(_encoded_json(manifest))
        manifest_path.chmod(0o444)
    elif tamper == "missing-provenance":
        manifest_path = previous / "result.json"
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text())
        manifest.pop("provenance")
        manifest_path.write_bytes(_encoded_json(manifest))
        manifest_path.chmod(0o444)
    elif tamper == "invalid-operation-timing":
        manifest_path = previous / "result.json"
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text())
        manifest["operation_timing"]["duration_ms"] = float("nan")
        manifest_path.write_bytes(_encoded_json(manifest))
        manifest_path.chmod(0o444)
    else:
        manifest_path = previous / "result.json"
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text())
        manifest["performance_request"]["history"][0]["sequence"] = True
        manifest_path.write_bytes(_encoded_json(manifest))
        manifest_path.chmod(0o444)
    snapshot = {
        item.name: (
            item.read_bytes(),
            item.stat().st_ino,
            stat.S_IMODE(item.stat().st_mode),
            item.stat().st_nlink,
        )
        for item in previous.iterdir()
    }

    failed = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        output,
        "--previous-result",
        str(previous),
        "--revision-reason",
        "Agent User reported a mismatch.",
    )

    assert failed.returncode == 3
    assert _response(failed)["error"]["id"] == "revision_source_invalid"
    assert not output.exists()
    assert not (tmp_path / ".revision-2.pending").exists()
    assert {
        item.name: (
            item.read_bytes(),
            item.stat().st_ino,
            stat.S_IMODE(item.stat().st_mode),
            item.stat().st_nlink,
        )
        for item in previous.iterdir()
    } == snapshot


def test_agent_cannot_extend_history_when_an_earlier_bundle_is_unreachable(
    tmp_path: Path,
) -> None:
    store = _install_pack(tmp_path)
    first_bundle = tmp_path / "revision-1"
    second_bundle = tmp_path / "revision-2"
    third_bundle = tmp_path / "revision-3"
    first = _render(ROOT / "tests/fixtures/renderable-c4.musicxml", store, first_bundle)
    assert first.returncode == 0, first.stderr
    second = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        second_bundle,
        "--previous-result",
        str(first_bundle),
        "--revision-reason",
        "Agent User reported a mismatch in the first interpretation.",
    )
    assert second.returncode == 0, second.stderr
    second_snapshot = {item.name: item.read_bytes() for item in second_bundle.iterdir()}
    first_bundle.rename(tmp_path / "moved-revision-1")

    failed = _render(
        ROOT / "tests/fixtures/renderable-c4.musicxml",
        store,
        third_bundle,
        "--previous-result",
        str(second_bundle),
        "--revision-reason",
        "Agent User supplied new feedback for another correction.",
    )

    assert failed.returncode == 3
    assert _response(failed)["error"]["id"] == "revision_source_invalid"
    assert {item.name: item.read_bytes() for item in second_bundle.iterdir()} == second_snapshot
    assert not third_bundle.exists()
