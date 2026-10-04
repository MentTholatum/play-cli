from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
PLAYCLI = Path(sys.executable).with_name("playcli")


def test_agent_correction_changes_only_the_source_supported_octave() -> None:
    responses = []
    for name in ("initial", "revised"):
        result = subprocess.run(
            [
                str(PLAYCLI),
                "validate",
                str(ROOT / f"tests/fixtures/listening-correction/{name}.musicxml"),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        responses.append(json.loads(result.stdout))
    before, after = responses
    assert len(before["events"]) == len(after["events"]) == 17
    assert before["warnings"] == after["warnings"]
    assert [i for i in range(17) if before["events"][i] != after["events"][i]] == [15]
    assert before["events"][15]["pitch"] == {"step": "B", "alter": 0, "octave": 4, "midi": 71}
    assert after["events"][15]["pitch"] == {"step": "B", "alter": 0, "octave": 3, "midi": 59}
    for response in responses:
        assert response["events"][15]["onset_quarters"] == 17
        assert response["events"][15]["duration_quarters"] == 1
        assert response["events"][-1]["onset_quarters"] == 18
        assert response["events"][-1]["duration_quarters"] == 2
