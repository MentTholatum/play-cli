from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_agent_retains_all_defaults_from_the_header_free_numbered_image() -> None:
    root = Path(__file__).parents[1]
    cli = Path(sys.executable).with_name("playcli")
    responses = []
    for filename in ("interpretation.musicxml", "interpretation-defaults.musicxml"):
        result = subprocess.run(
            [str(cli), "validate", str(root / "tests/fixtures/numbered-score" / filename)],
            capture_output=True,
            text=True,
            check=True,
        )
        responses.append(json.loads(result.stdout))
    original, defaults = responses
    assert original["events"] == defaults["events"]
    assert [warning["id"] for warning in defaults["warnings"]] == [
        "score_default_key",
        "score_default_meter",
        "score_default_tempo",
        "score_interpretation_uncertain",
    ]
    assert all(
        defaults["score_context"][field]["source"] == "default"
        for field in ("key", "meter", "tempo")
    )
