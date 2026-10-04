from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PLAYCLI = Path(sys.executable).with_name("playcli")


def test_agent_validates_the_visually_transcribed_numbered_score() -> None:
    result = subprocess.run(
        [
            str(PLAYCLI),
            "validate",
            str(ROOT / "tests/fixtures/numbered-score/interpretation.musicxml"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    response = json.loads(result.stdout)
    phrase = [
        (60, 0, 1),
        (62, 1, 1.5),
        (64, 2.5, 0.5),
        (None, 3, 1),
        (66, 4, 0.5),
        (67, 4.5, 0.5),
        (72, 5, 3),
    ]
    expected = phrase + [(pitch, onset + 8, duration) for pitch, onset, duration in phrase]
    expected += [(60, 16, 1), (59, 17, 1), (60, 18, 2)]
    assert [
        (
            event["pitch"]["midi"] if event["pitch"] else None,
            event["onset_quarters"],
            event["duration_quarters"],
        )
        for event in response["events"]
    ] == expected
    assert [warning["id"] for warning in response["warnings"]] == [
        "score_default_tempo",
        "score_interpretation_uncertain",
    ]


@pytest.mark.parametrize("namespace", ["", ' xmlns="http://www.musicxml.org/ns/musicxml"'])
def test_agent_retains_interpretation_uncertainty_in_validation(
    tmp_path: Path, namespace: str,
) -> None:
    score = tmp_path / "score.musicxml"
    original = (ROOT / "tests/fixtures/minimal.musicxml").read_text().replace(
        "<score-partwise ", f"<score-partwise{namespace} "
    )
    score.write_text(
        original.replace(
            "<part-list>",
            "<identification><miscellaneous>"
            '<miscellaneous-field name="play-cli:uncertainty">'
            '{"location":"page 1, final symbol","message":"Octave dot is faint",'
            '"chosen_interpretation":"C4"}'
            "</miscellaneous-field></miscellaneous></identification><part-list>",
        )
    )
    result = subprocess.run(
        [str(PLAYCLI), "validate", str(score)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    warnings = json.loads(result.stdout)["warnings"]
    assert warnings[-1] == {
        "id": "score_interpretation_uncertain",
        "kind": "uncertainty",
        "location": "page 1, final symbol",
        "message": "Octave dot is faint",
        "chosen_interpretation": "C4",
    }
    store = tmp_path / "packs"
    subprocess.run(
        [
            str(PLAYCLI),
            "sound-pack",
            "install",
            str(ROOT / "tests/fixtures/test-piano-pack"),
            "--store",
            str(store),
        ],
        capture_output=True,
        check=True,
    )
    previous = None
    for name in ("first", "revision"):
        bundle = tmp_path / name
        argv = [
            str(PLAYCLI),
            "render",
            str(score),
            "--sound-pack",
            "play-cli-test-piano",
            "--sound-pack-version",
            "1.0.0",
            "--sound-pack-store",
            str(store),
            "--output",
            str(bundle),
        ]
        if previous:
            argv += ["--previous-result", str(previous), "--revision-reason", "Recheck ending"]
        rendered = subprocess.run(argv, capture_output=True, text=True, check=False)
        assert rendered.returncode == 0, rendered.stderr
        manifest = json.loads((bundle / "result.json").read_text())
        assert json.loads(rendered.stdout)["warnings"] == warnings
        assert manifest["warnings"] == warnings
        assert len(manifest["applied_score_defaults"]) == 3
        assert json.loads((bundle / "validation.json").read_text())["warnings"] == warnings
        for artifact in manifest["result_bundle"]["artifacts"].values():
            assert (
                hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest()
                == artifact["sha256"]
            )
        assert (bundle / "score.musicxml").read_bytes() == score.read_bytes()
        assert {item.name for item in bundle.iterdir()} == {
            "score.musicxml",
            "validation.json",
            "schedule.json",
            "performance.wav",
            "result.json",
            "voice-0001.wav",
        }
        assert (
            json.loads((bundle / "schedule.json").read_text())["events"]
            == json.loads(result.stdout)["events"]
        )
        with wave.open(str(bundle / "performance.wav")) as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
            assert wav.getnframes() == manifest["wav"]["frames"] == 9600
            pcm = wav.readframes(wav.getnframes())
            assert len(pcm) == 19200
            assert any(pcm[:4800])
            assert pcm[-2:] == b"\x00\x00"
        voice = manifest["result_bundle"]["voices"][0]
        raw = (bundle / voice["relative_path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == voice["sha256"]
        assert struct.unpack_from("<HHI", raw, 20) == (3, 1, 16000)
        assert voice["frames"] == 9600
        assert len(raw) == 56 + 9600 * 8
        assert [round(value * 32768) for value in struct.unpack("<9600d", raw[56:])] == list(
            struct.unpack("<9600h", pcm)
        )
        previous = bundle
    assert manifest["performance_request"]["history"][0]["interpretation"]["warnings"] == warnings


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        pytest.param("[" * 1100 + "0" + "]" * 1100, id="excessive-json-depth"),
        "{}",
        '{"location":3}',
        '{"location":"x","message":" ","chosen_interpretation":"C4"}',
        '{"location":"x","message":"y","chosen_interpretation":"C4","extra":true}',
    ],
)
@pytest.mark.parametrize("namespace", ["", ' xmlns="http://www.musicxml.org/ns/musicxml"'])
def test_agent_rejects_malformed_uncertainty(
    tmp_path: Path, payload: str, namespace: str,
) -> None:
    score = tmp_path / "invalid.musicxml"
    score.write_text(
        (ROOT / "tests/fixtures/minimal.musicxml")
        .read_text()
        .replace("<score-partwise ", f"<score-partwise{namespace} ")
        .replace(
            "<part-list>",
            "<identification><miscellaneous>"
            '<miscellaneous-field name="play-cli:uncertainty">'
            + payload
            + "</miscellaneous-field></miscellaneous></identification><part-list>",
        )
    )
    result = subprocess.run(
        [str(PLAYCLI), "validate", str(score)], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert len(result.stdout.splitlines()) == 1
    response = json.loads(result.stdout)
    assert response["schema_version"] == "1.6"
    assert response["error"]["id"] == "musicxml_invalid"
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("namespace", ["", ' xmlns="http://www.musicxml.org/ns/musicxml"'])
def test_agent_rejects_uncertainty_outside_root_identification(
    tmp_path: Path, namespace: str,
) -> None:
    score = tmp_path / "misplaced.musicxml"
    score.write_text(
        (ROOT / "tests/fixtures/minimal.musicxml")
        .read_text()
        .replace("<score-partwise ", f"<score-partwise{namespace} ")
        .replace(
            "<part-name>Piano</part-name>",
            "<identification><miscellaneous>"
            '<miscellaneous-field name="play-cli:uncertainty">'
            '{"location":"page 1","message":"Faint dot","chosen_interpretation":"C4"}'
            "</miscellaneous-field></miscellaneous></identification>"
            "<part-name>Piano</part-name>",
        )
    )
    result = subprocess.run(
        [str(PLAYCLI), "validate", str(score)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)["error"]["id"] == "musicxml_invalid"
    assert result.stderr and "Traceback" not in result.stderr
