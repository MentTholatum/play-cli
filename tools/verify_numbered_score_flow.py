"""Replay the retained Agent transcription; this tool does not recognize images."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from workflow_evidence import SCHEMA_VERSION, read_bundle

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playcli", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--notation", choices=("numbered", "staff"), default="numbered")
    parser.add_argument("--all-defaults", action="store_true")
    args = parser.parse_args()
    if args.all_defaults and args.notation != "numbered":
        parser.error("--all-defaults is a numbered-score fixture variant.")
    fixture = ROOT / f"tests/fixtures/{args.notation}-score"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    operations = []

    def run(*argv: str) -> dict[str, Any]:
        command = [str(args.playcli.resolve()), "--schema-version", "1.6", *argv]
        result = subprocess.run(command, text=True, capture_output=True, check=True)
        value: dict[str, Any] = json.loads(result.stdout)
        assert value["schema_version"] == "1.6" and value["status"] == "ok"
        operations.append({"argv": command, "response": value, "stderr": result.stderr})
        return value

    run(
        "sound-pack",
        "verify",
        "play-cli-standard-piano-salamander",
        "--version",
        "1.0.0",
        "--store",
        str(args.store.resolve()),
    )
    suffix = "-defaults" if args.all_defaults else ""
    score = fixture / f"interpretation{suffix}.musicxml"
    validation = run("validate", str(score))
    phrase = [(60, 1), (62, 1.5), (64, 0.5), (None, 1), (66, 0.5), (67, 0.5), (72, 3)]
    expected = phrase * 2 + [(60, 1), (59, 1), (60, 2)]
    warning_ids = ["score_default_tempo", "score_interpretation_uncertain"]
    if args.all_defaults:
        warning_ids = ["score_default_key", "score_default_meter", *warning_ids]
    frames = 576000
    rest_frames = ((86400, 115200), (316800, 345600))
    if args.notation == "staff":
        phrase = [(66, 0.5), (67, 0.5), (69, 1.5), (71, 0.5), (72, 2), (None, 1)]
        expected = phrase * 2 + [(65, 1), (65, 1), (67, 1), (66, 3)]
        warning_ids = []
        frames = 432000
        rest_frames = ((120000, 144000), (264000, 288000))
        assert validation["score_context"]["key"] == {
            "fifths": 1,
            "mode": "major",
            "source": "score",
        }
        assert validation["score_context"]["meter"] == {
            "beats": 3,
            "beat_type": 4,
            "source": "score",
        }
        assert validation["score_context"]["tempo"] == {"bpm": 120.0, "source": "score"}
    assert len(validation["events"]) == len(expected)
    cursor = 0.0
    for event, (midi, duration) in zip(validation["events"], expected, strict=True):
        assert (event["pitch"]["midi"] if event["pitch"] else None) == midi
        assert event["onset_quarters"] == cursor
        assert event["duration_quarters"] == duration
        cursor += duration
    assert [warning["id"] for warning in validation["warnings"]] == warning_ids
    result = run(
        "render",
        str(score),
        "--sound-pack",
        "play-cli-standard-piano-salamander",
        "--sound-pack-version",
        "1.0.0",
        "--sound-pack-store",
        str(args.store.resolve()),
        "--output",
        str(output / "bundle"),
    )
    bundle = output / "bundle"
    manifest = json.loads((bundle / "result.json").read_text())
    assert manifest["warnings"] == validation["warnings"] == result["warnings"]
    hashes = read_bundle(bundle, score, validation, frames, rest_frames)
    record = {
        "schema_version": SCHEMA_VERSION,
        "kind": f"agent-authored-{args.notation}-score-workflow",
        "recognition_benchmark": False,
        "human_listening": "not_performed",
        "operations": operations,
        "artifact_hashes": hashes,
        "source_hashes": {
            name: hashlib.sha256((fixture / name).read_bytes()).hexdigest()
            for name in (f"source{suffix}.svg", f"source{suffix}.svg.png", score.name)
        },
        "result_manifest_sha256": hashlib.sha256((bundle / "result.json").read_bytes()).hexdigest(),
        "expected_events": len(expected),
        "duration_seconds": frames / 48000,
    }
    (output / "evidence.json").write_text(json.dumps(record, indent=2) + "\n")
    print(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "ok",
                "evidence": str(output / "evidence.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
