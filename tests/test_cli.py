from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from tests.agent_contract import stable_response as _stable_response

ROOT = Path(__file__).parents[1]
PLAYCLI = Path(sys.executable).with_name("playcli")
TEST_PACK = ROOT / "tests/fixtures/test-piano-pack"
PACK_ID = "play-cli-test-piano"
PACK_VERSION = "1.0.0"


def _install_test_pack(tmp_path: Path) -> tuple[Path, str]:
    store = tmp_path / "sound-pack-store"
    result = subprocess.run(
        [str(PLAYCLI), "sound-pack", "install", str(TEST_PACK), "--store", str(store)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0
    return store, json.loads(result.stdout)["sound_pack"]["pack_hash"]


def _sound_pack_arguments(store: Path) -> list[str]:
    return [
        "--sound-pack",
        PACK_ID,
        "--sound-pack-version",
        PACK_VERSION,
        "--sound-pack-store",
        str(store),
    ]


def test_agent_can_validate_a_minimal_playable_score() -> None:
    result = subprocess.run(
        [str(PLAYCLI), "validate", str(ROOT / "tests/fixtures/minimal.musicxml")],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0
    response = _stable_response(result)
    assert response == {
        "schema_version": "1.6",
        "operation": "validate",
        "status": "ok",
        "score_context": {
            "key": {"fifths": 0, "mode": "major", "source": "default"},
            "meter": {"beats": 4, "beat_type": 4, "source": "default"},
            "tempo": {"bpm": 100.0, "source": "default"},
            "expression": {"mode": "neutral", "velocity": 64, "source": "default"},
        },
        "events": [
            {
                "order": 0,
                "voice": "1",
                "staff": 1,
                "staves": [1],
                "pages": [1],
                "event_id": "event-000001",
                "sources": ["measure:1"],
                "kind": "note",
                "onset_quarters": 0.0,
                "onset_quarters_exact": {"numerator": 0, "denominator": 1},
                "duration_quarters": 1.0,
                "duration_quarters_exact": {"numerator": 1, "denominator": 1},
                "pitch": {"step": "C", "alter": 0, "octave": 4, "midi": 60},
                "velocity": 64,
            }
        ],
        "warnings": [
            {
                "id": "score_default_key",
                "kind": "score_default",
                "field": "key",
                "message": "Key signature was absent; C major was applied.",
                "applied_value": {"fifths": 0, "mode": "major"},
            },
            {
                "id": "score_default_meter",
                "kind": "score_default",
                "field": "meter",
                "message": "Meter was absent; 4/4 was applied.",
                "applied_value": {"beats": 4, "beat_type": 4},
            },
            {
                "id": "score_default_tempo",
                "kind": "score_default",
                "field": "tempo",
                "message": "Tempo was absent; 100 BPM was applied.",
                "applied_value": {"bpm": 100.0},
            },
        ],
    }
    assert "validated 1 normalized event" in result.stderr.lower()


def test_agent_can_validate_the_supported_monophonic_schedule() -> None:
    result = subprocess.run(
        [
            str(PLAYCLI),
            "validate",
            str(ROOT / "tests/fixtures/supported-monophonic.musicxml"),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0
    assert _stable_response(result) == {
        "schema_version": "1.6",
        "operation": "validate",
        "status": "ok",
        "score_context": {
            "key": {"fifths": 2, "mode": "major", "source": "score"},
            "meter": {"beats": 3, "beat_type": 4, "source": "score"},
            "tempo": {"bpm": 120.0, "source": "score"},
            "expression": {"mode": "neutral", "velocity": 64, "source": "default"},
        },
        "events": [
            {
                "order": 0,
                "voice": "1",
                "staff": 1,
                "staves": [1],
                "pages": [1],
                "event_id": "event-000001",
                "sources": ["measure:1"],
                "kind": "note",
                "onset_quarters": 0.0,
                "onset_quarters_exact": {"numerator": 0, "denominator": 1},
                "duration_quarters": 1.0,
                "duration_quarters_exact": {"numerator": 1, "denominator": 1},
                "pitch": {"step": "C", "alter": 0, "octave": 4, "midi": 60},
                "velocity": 64,
            },
            {
                "order": 1,
                "voice": "1",
                "staff": 1,
                "staves": [1],
                "pages": [1],
                "event_id": "event-000002",
                "sources": ["measure:1"],
                "kind": "rest",
                "onset_quarters": 1.0,
                "onset_quarters_exact": {"numerator": 1, "denominator": 1},
                "duration_quarters": 0.5,
                "duration_quarters_exact": {"numerator": 1, "denominator": 2},
                "pitch": None,
                "velocity": 0,
            },
            {
                "order": 2,
                "voice": "1",
                "staff": 1,
                "staves": [1],
                "pages": [1],
                "event_id": "event-000003",
                "sources": ["measure:1"],
                "kind": "note",
                "onset_quarters": 1.5,
                "onset_quarters_exact": {"numerator": 3, "denominator": 2},
                "duration_quarters": 1.5,
                "duration_quarters_exact": {"numerator": 3, "denominator": 2},
                "pitch": {"step": "F", "alter": 1, "octave": 5, "midi": 78},
                "velocity": 64,
            },
            {
                "order": 3,
                "voice": "1",
                "staff": 1,
                "staves": [1],
                "pages": [1],
                "event_id": "event-000004",
                "sources": ["measure:2", "measure:3", "measure:3"],
                "kind": "note",
                "onset_quarters": 3.0,
                "onset_quarters_exact": {"numerator": 3, "denominator": 1},
                "duration_quarters": 5.0,
                "duration_quarters_exact": {"numerator": 5, "denominator": 1},
                "pitch": {"step": "A", "alter": -1, "octave": 3, "midi": 56},
                "velocity": 64,
            },
            {
                "order": 4,
                "voice": "1",
                "staff": 1,
                "staves": [1],
                "pages": [1],
                "event_id": "event-000005",
                "sources": ["measure:3"],
                "kind": "rest",
                "onset_quarters": 8.0,
                "onset_quarters_exact": {"numerator": 8, "denominator": 1},
                "duration_quarters": 1.0,
                "duration_quarters_exact": {"numerator": 1, "denominator": 1},
                "pitch": None,
                "velocity": 0,
            },
        ],
        "warnings": [],
    }
    assert "validated 5 normalized events" in result.stderr.lower()


def test_agent_accepts_direction_voice_and_official_tied_layout_attributes() -> None:
    result = subprocess.run(
        [
            str(PLAYCLI),
            "validate",
            str(ROOT / "tests/fixtures/supported-monophonic.musicxml"),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert response["status"] == "ok"
    assert response["events"][3]["duration_quarters_exact"] == {
        "numerator": 5,
        "denominator": 1,
    }


def test_agent_receives_exact_rational_timing() -> None:
    result = subprocess.run(
        [
            str(PLAYCLI),
            "validate",
            str(ROOT / "tests/fixtures/fractional-duration.musicxml"),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert response["schema_version"] == "1.6"
    assert response["events"] == [
        {
            "order": 0,
            "voice": "1",
            "staff": 1,
            "staves": [1],
            "pages": [1],
            "event_id": "event-000001",
            "sources": ["measure:1"],
            "kind": "note",
            "onset_quarters": 0,
            "onset_quarters_exact": {"numerator": 0, "denominator": 1},
            "duration_quarters": 0.3333333333333333,
            "duration_quarters_exact": {"numerator": 1, "denominator": 3},
            "pitch": {"step": "C", "alter": 0, "octave": 4, "midi": 60},
            "velocity": 64,
        }
    ]


def test_agent_receives_a_stable_error_for_an_unreadable_score() -> None:
    missing_score = ROOT / "tests/fixtures/does-not-exist.musicxml"

    result = subprocess.run(
        [str(PLAYCLI), "validate", str(missing_score)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert _stable_response(result) == {
        "schema_version": "1.6",
        "operation": "validate",
        "status": "error",
        "error": {
            "id": "input_unreadable",
            "message": "Playable Score could not be read.",
        },
    }
    assert "does-not-exist.musicxml" in result.stderr


@pytest.mark.parametrize(
    "fixture_name",
    [
        "invalid-malformed.musicxml",
        "invalid-structure.musicxml",
        "invalid-tie-dangling.musicxml",
        "invalid-tie-pitch.musicxml",
        "invalid-tie-rest.musicxml",
        "invalid-dot-duration.musicxml",
    ],
)
def test_agent_receives_a_stable_error_for_invalid_musicxml(fixture_name: str) -> None:
    result = subprocess.run(
        [str(PLAYCLI), "validate", str(ROOT / "tests/fixtures" / fixture_name)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert _stable_response(result) == {
        "schema_version": "1.6",
        "operation": "validate",
        "status": "error",
        "error": {
            "id": "musicxml_invalid",
            "message": "Playable Score MusicXML is invalid.",
        },
    }
    assert "musicxml" in result.stderr.lower()
    assert "traceback" not in result.stderr.lower()


@pytest.mark.parametrize(
    ("fixture_name", "diagnostic"),
    [
        ("invalid-empty-alter.musicxml", "no value: alter"),
        ("invalid-missing-part-list.musicxml", "part-list"),
        ("invalid-part-id-mismatch.musicxml", "part id"),
        ("invalid-duplicate-step.musicxml", "step under pitch"),
        ("invalid-empty-direction.musicxml", "direction-type under direction"),
        ("invalid-duplicate-sound.musicxml", "sound under direction"),
        ("invalid-empty-direction-voice.musicxml", "no value: voice"),
        ("invalid-direction-order.musicxml", "child order under direction"),
    ],
)
def test_agent_rejects_invalid_supported_subset_structure(
    fixture_name: str, diagnostic: str
) -> None:
    result = subprocess.run(
        [str(PLAYCLI), "validate", str(ROOT / "tests/fixtures" / fixture_name)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["id"] == "musicxml_invalid"
    assert diagnostic in result.stderr.lower()
    assert "traceback" not in result.stderr.lower()


@pytest.mark.parametrize(
    ("original", "replacement", "element_name"),
    [
        ("<divisions>1</divisions>", "<divisions/>", "divisions"),
        (
            "</attributes>",
            "<key><fifths>0</fifths><mode/></key></attributes>",
            "mode",
        ),
        ("<type>quarter</type>", "<type/>", "type"),
        ("<type>quarter</type>", "<voice/><type>quarter</type>", "voice"),
    ],
)
def test_agent_rejects_empty_optional_semantic_values(
    original: str, replacement: str, element_name: str, tmp_path: Path
) -> None:
    score = tmp_path / f"empty-{element_name}.musicxml"
    score.write_text(
        (ROOT / "tests/fixtures/minimal.musicxml").read_text().replace(original, replacement, 1)
    )

    result = subprocess.run(
        [str(PLAYCLI), "validate", str(score)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["id"] == "musicxml_invalid"
    assert f"no value: {element_name}" in result.stderr.lower()
    assert "traceback" not in result.stderr.lower()


@pytest.mark.parametrize(
    ("fixture_name", "diagnostic"),
    [
        ("unsupported-extra-parts.musicxml", "part"),
        ("unsupported-grace.musicxml", "grace"),
        ("unsupported-unpitched.musicxml", "unpitched"),
        ("unsupported-repeat.musicxml", "repeat"),
        ("unsupported-context-change.musicxml", "tempo"),
        ("unsupported-dynamics.musicxml", "dynamics"),
        ("unsupported-words.musicxml", "words under direction-type"),
        (
            "unsupported-unknown-element.musicxml",
            "bogus-performance-mark under measure",
        ),
        ("unsupported-duration-attribute.musicxml", "bogus attribute on duration"),
        ("unsupported-direction-voice-mismatch.musicxml", "absent voice"),
        ("unsupported-tied-time-only.musicxml", "time-only attribute on tied"),
        ("unsupported-tied-line-shape.musicxml", "line-shape attribute on tied"),
    ],
)
def test_agent_receives_a_stable_error_for_unsupported_notation(
    fixture_name: str, diagnostic: str
) -> None:
    result = subprocess.run(
        [str(PLAYCLI), "validate", str(ROOT / "tests/fixtures" / fixture_name)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert _stable_response(result) == {
        "schema_version": "1.6",
        "operation": "validate",
        "status": "error",
        "error": {
            "id": "notation_unsupported",
            "message": "Playable Score contains unsupported notation.",
        },
    }
    assert diagnostic in result.stderr.lower()
    assert "traceback" not in result.stderr.lower()


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("attack", "1"),
        ("release", "1"),
        ("dynamics", "80"),
        ("end-dynamics", "80"),
        ("pizzicato", "yes"),
        ("time-only", "1"),
        ("bogus-performance-attribute", "yes"),
    ],
)
def test_agent_receives_an_error_for_unsupported_note_attributes(
    attribute: str, value: str, tmp_path: Path
) -> None:
    score = tmp_path / "unsupported-note-attribute.musicxml"
    score.write_text(
        (ROOT / "tests/fixtures/minimal.musicxml")
        .read_text()
        .replace("<note>", f'<note {attribute}="{value}">', 1)
    )

    result = subprocess.run(
        [str(PLAYCLI), "validate", str(score)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert _stable_response(result) == {
        "schema_version": "1.6",
        "operation": "validate",
        "status": "error",
        "error": {
            "id": "notation_unsupported",
            "message": "Playable Score contains unsupported notation.",
        },
    }
    assert f"{attribute} attribute on note" in result.stderr.lower()
    assert "traceback" not in result.stderr.lower()


def test_agent_receives_a_stable_error_for_an_invalid_invocation() -> None:
    result = subprocess.run(
        [str(PLAYCLI)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout) == {
        "schema_version": "1.6",
        "operation": None,
        "status": "error",
        "error": {
            "id": "invalid_invocation",
            "message": "PLAY CLI invocation is invalid.",
        },
    }
    assert "usage:" in result.stderr.lower()


@pytest.mark.parametrize("arguments", [["--help"], ["validate", "--help"], ["render", "--help"]])
def test_agent_receives_a_machine_error_when_help_is_requested(
    arguments: list[str],
) -> None:
    result = subprocess.run(
        [str(PLAYCLI), *arguments],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout) == {
        "schema_version": "1.6",
        "operation": None,
        "status": "error",
        "error": {
            "id": "invalid_invocation",
            "message": "PLAY CLI invocation is invalid.",
        },
    }
    assert "usage:" in result.stderr.lower()


def test_agent_can_render_a_complete_audible_performance(tmp_path: Path) -> None:
    result_bundle = tmp_path / "result-bundle"
    sound_pack_store, pack_hash = _install_test_pack(tmp_path)

    result = subprocess.run(
        [
            str(PLAYCLI),
            "render",
            str(ROOT / "tests/fixtures/renderable-c4.musicxml"),
            *_sound_pack_arguments(sound_pack_store),
            "--output",
            str(result_bundle),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0
    response = json.loads(result.stdout)
    expected_wav = result_bundle / "performance.wav"
    expected_manifest = result_bundle / "result.json"
    expected_artifacts = {
        "score": result_bundle / "score.musicxml",
        "validation": result_bundle / "validation.json",
        "schedule": result_bundle / "schedule.json",
        "performance": expected_wav,
    }
    assert {item.name for item in result_bundle.iterdir()} == {
        "score.musicxml",
        "validation.json",
        "schedule.json",
        "performance.wav",
        "result.json",
        "voice-0001.wav",
    }
    assert response["schema_version"] == "1.6"
    assert response["operation"] == "render"
    assert response["status"] == "ok"
    assert response["cli"] == {"name": "play-cli", "version": "0.10.0"}
    assert response["result_bundle"]["schema_version"] == "1.2"
    assert response["result_bundle"]["status"] == "complete"
    assert response["result_bundle"]["path"] == str(result_bundle.resolve())
    assert response["result_bundle"]["manifest"] == str(expected_manifest.resolve())
    for key, path in expected_artifacts.items():
        data = path.read_bytes()
        assert data
        assert response["result_bundle"]["artifacts"][key] == {
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    assert response["wav"] == {
        "path": str(expected_wav.resolve()),
        "format": "PCM_S16LE",
        "sample_rate_hz": 16_000,
        "channels": 1,
        "frames": 8_000,
        "duration_seconds": 0.5,
    }
    assert response["renderer"] == {"id": "pcm-sampler", "version": "3.0"}
    assert response["sound_pack"] == {
        "id": "play-cli-test-piano",
        "version": "1.0.0",
        "pack_hash": pack_hash,
        "samples": [
            {
                "midi": 60,
                "sha256": "a8443684b60318df7975e7d2fbc7fa3cf10a715ee2d946e2d84b0c34c9911dcd",
            }
        ],
    }
    assert response["warnings"] == []
    assert response["applied_score_defaults"] == []
    assert response["playback"] == {"requested": False, "status": "not_requested"}
    persisted_response = dict(response)
    persisted_response.pop("playback")
    assert json.loads(expected_manifest.read_text()) == persisted_response
    assert "rendered complete performance" in result.stderr.lower()

    with wave.open(str(expected_wav), "rb") as performance:
        assert performance.getnchannels() == 1
        assert performance.getsampwidth() == 2
        assert performance.getframerate() == 16_000
        assert performance.getnframes() == 8_000
        frame_data = performance.readframes(performance.getnframes())
        assert len(frame_data) == (
            performance.getnframes() * performance.getnchannels() * performance.getsampwidth()
        )

    samples = struct.unpack(f"<{len(frame_data) // 2}h", frame_data)
    assert sum(sample * sample for sample in samples) > 0
    assert max(abs(sample) for sample in samples) > 1_000
    assert max(abs(sample) for sample in samples) < 32_767
    silent_start = len(samples) - 800
    assert all(sample == 0 for sample in samples[silent_start:])

    fade = samples[silent_start - 320 : silent_start]
    assert abs(fade[-1] - samples[silent_start]) <= 32
    fade_window_energy = [
        sum(sample * sample for sample in fade[start : start + 80])
        for start in range(0, len(fade), 80)
    ]
    assert all(
        earlier > later for earlier, later in zip(fade_window_energy, fade_window_energy[1:])
    )
    assert fade_window_energy[-1] < fade_window_energy[0] * 0.15


@pytest.mark.parametrize(
    ("fixture_name", "expected_frames"),
    [("minimal.musicxml", 9_600), ("long-tie.musicxml", 64_000)],
)
def test_agent_can_pad_a_single_event_when_the_sample_is_shorter(
    fixture_name: str, expected_frames: int, tmp_path: Path
) -> None:
    result_bundle = tmp_path / "result-bundle"
    sound_pack_store, _ = _install_test_pack(tmp_path)

    result = subprocess.run(
        [
            str(PLAYCLI),
            "render",
            str(ROOT / "tests/fixtures" / fixture_name),
            *_sound_pack_arguments(sound_pack_store),
            "--output",
            str(result_bundle),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["status"] == "ok"
    assert response["wav"]["frames"] == expected_frames
    assert (result_bundle / "performance.wav").is_file()


def test_agent_receives_an_error_instead_of_a_partial_multi_event_render(
    tmp_path: Path,
) -> None:
    result_bundle = tmp_path / "result-bundle"
    sound_pack_store, _ = _install_test_pack(tmp_path)

    result = subprocess.run(
        [
            str(PLAYCLI),
            "render",
            str(ROOT / "tests/fixtures/supported-monophonic.musicxml"),
            *_sound_pack_arguments(sound_pack_store),
            "--output",
            str(result_bundle),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 3
    assert _stable_response(result) == {
        "schema_version": "1.6",
        "operation": "render",
        "status": "error",
        "error": {
            "id": "sound_pack_sample_unavailable",
            "message": "Sound Pack has no sample for this event.",
        },
    }
    assert "midi 78" in result.stderr.lower()
    assert not result_bundle.exists()


def test_agent_receives_an_error_without_artifacts_when_renderer_is_unavailable(
    tmp_path: Path,
) -> None:
    result_bundle = tmp_path / "result-bundle"
    sound_pack_store = tmp_path / "sound-pack-store"

    result = subprocess.run(
        [
            str(PLAYCLI),
            "render",
            str(ROOT / "tests/fixtures/minimal.musicxml"),
            *_sound_pack_arguments(sound_pack_store),
            "--output",
            str(result_bundle),
            "--renderer",
            "not-installed",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 3
    assert _stable_response(result) == {
        "schema_version": "1.6",
        "operation": "render",
        "status": "error",
        "error": {
            "id": "renderer_unavailable",
            "message": "Requested renderer is unavailable.",
        },
    }
    assert "not-installed" in result.stderr
    assert not result_bundle.exists()
