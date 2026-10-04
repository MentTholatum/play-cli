"""Replay a synthetic correction fixture, never actual user feedback or automatic OCR."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from workflow_evidence import SCHEMA_VERSION, read_bundle

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/listening-correction"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playcli", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--feedback", type=Path)
    args = parser.parse_args()
    feedback = json.loads(args.feedback.read_text()) if args.feedback else None
    if feedback is not None and feedback != {
        "kind": "synthetic_test_feedback",
        "id": "feedback-001",
        "text": "后面听起来不对",
        "applies_to_delivered_revision": 1,
    }:
        parser.error("This fixture replay accepts only its explicitly synthetic feedback file.")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    operations = []

    def run(*arguments: str) -> dict[str, Any]:
        command = [str(args.playcli.resolve()), "--schema-version", "1.6", *arguments]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        value: dict[str, Any] = json.loads(result.stdout)
        assert len(result.stdout.splitlines()) == 1
        assert value["schema_version"] == "1.6" and value["status"] == "ok"
        operations.append({"argv": command, "response": value, "stderr": result.stderr})
        return value

    pack = [
        "--sound-pack",
        "play-cli-standard-piano-salamander",
        "--sound-pack-version",
        "1.0.0",
        "--sound-pack-store",
        str(args.store.resolve()),
    ]
    run(
        "sound-pack",
        "verify",
        "play-cli-standard-piano-salamander",
        "--version",
        "1.0.0",
        "--store",
        str(args.store.resolve()),
    )
    initial_score = FIXTURE / "initial.musicxml"
    initial_validation = run("validate", str(initial_score))
    first = output / "revision-1"
    initial = run("render", str(initial_score), *pack, "--output", str(first))
    initial_hashes = read_bundle(
        first, initial_score, initial_validation, 576000, ((86400, 115200), (316800, 345600))
    )
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "synthetic-listening-correction",
        "human_feedback": False,
        "human_listening": "not_performed",
        "feedback": feedback,
        "state": "awaiting_new_feedback",
        "delivered_revisions": 1,
        "initial_hashes": initial_hashes,
        "operations": operations,
        "source": {
            "path": str(ROOT / "tests/fixtures/numbered-score/source.svg.png"),
            "sha256": hashlib.sha256(
                (ROOT / "tests/fixtures/numbered-score/source.svg.png").read_bytes()
            ).hexdigest(),
        },
    }
    if feedback is not None:
        corrected = FIXTURE / "revised.musicxml"
        validation = run("validate", str(corrected))
        before, after = initial_validation["events"], validation["events"]
        assert len(before) == len(after) == 17
        assert [index for index in range(17) if before[index] != after[index]] == [15]
        assert before[15]["pitch"]["midi"] == 71 and after[15]["pitch"]["midi"] == 59
        assert before[15]["onset_quarters"] == after[15]["onset_quarters"] == 17
        assert before[15]["duration_quarters"] == after[15]["duration_quarters"] == 1
        assert initial_validation["warnings"] == validation["warnings"]
        reason = ET.parse(corrected).findtext(
            './identification/miscellaneous/miscellaneous-field[@name="play-cli:revision-reason"]'
        )
        assert reason and feedback["text"] in reason and feedback["id"] in reason
        second = output / "revision-2"
        revised = run(
            "render",
            str(corrected),
            *pack,
            "--output",
            str(second),
            "--previous-result",
            str(first),
            "--revision-reason",
            reason,
        )
        revised_hashes = read_bundle(
            second, corrected, validation, 576000, ((86400, 115200), (316800, 345600))
        )
        persisted = json.loads((second / "result.json").read_text())
        assert persisted["performance_request"] == revised["performance_request"]
        assert (
            read_bundle(
                first,
                initial_score,
                initial_validation,
                576000,
                ((86400, 115200), (316800, 345600)),
            )
            == initial_hashes
        )
        history = revised["performance_request"]["history"]
        assert revised["performance_request"]["id"] == initial["performance_request"]["id"]
        assert len(history) == 2 and history[0] == initial["performance_request"]["history"][0]
        assert history[1]["reason"] == reason
        assert revised["performance_request"]["current_delivered_revision"] == 2
        assert revised["performance_request"]["latest_delivered"]["result_bundle"] == str(second)
        assert history[1]["interpretation"]["warnings"] == validation["warnings"]
        assert revised_hashes["performance.wav"] != initial_hashes["performance.wav"]
        record.update(
            {
                "delivered_revisions": 2,
                "revised_hashes": revised_hashes,
                "consumed_feedback_id": feedback["id"],
                "changed_event_indices": [15],
            }
        )
    assert not (output / "revision-3").exists()
    if feedback is None:
        assert not (output / "revision-2").exists()
    (output / "evidence.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "ok",
                "state": record["state"],
                "evidence": str(output / "evidence.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
