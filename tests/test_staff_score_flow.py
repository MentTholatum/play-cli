from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
PLAYCLI = Path(sys.executable).with_name("playcli")


def test_agent_validates_the_visually_transcribed_staff_score() -> None:
    result = subprocess.run(
        [
            str(PLAYCLI),
            "--schema-version",
            "1.6",
            "validate",
            str(ROOT / "tests/fixtures/staff-score/interpretation.musicxml"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert len(result.stdout.splitlines()) == 1
    response = json.loads(result.stdout)
    assert response["schema_version"] == "1.6"
    assert response["status"] == "ok"
    assert response["warnings"] == []
    assert response["score_context"]["key"] == {"fifths": 1, "mode": "major", "source": "score"}
    assert response["score_context"]["meter"] == {"beats": 3, "beat_type": 4, "source": "score"}
    assert response["score_context"]["tempo"] == {"bpm": 120.0, "source": "score"}
    assert [
        (
            event["pitch"]["midi"] if event["pitch"] else None,
            event["onset_quarters"],
            event["duration_quarters"],
        )
        for event in response["events"]
    ] == [
        (66, 0, 0.5),
        (67, 0.5, 0.5),
        (69, 1, 1.5),
        (71, 2.5, 0.5),
        (72, 3, 2),
        (None, 5, 1),
        (66, 6, 0.5),
        (67, 6.5, 0.5),
        (69, 7, 1.5),
        (71, 8.5, 0.5),
        (72, 9, 2),
        (None, 11, 1),
        (65, 12, 1),
        (65, 13, 1),
        (67, 14, 1),
        (66, 15, 3),
    ]
    assert "validated" in result.stderr.lower()
