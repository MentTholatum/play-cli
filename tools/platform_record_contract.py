from __future__ import annotations

import re
from fractions import Fraction
from typing import Any

SCHEMA_VERSION = "1.0"
VERIFY_OPERATION = "verify-platform-distribution"
COMPARE_OPERATION = "compare-platform-records"
BUNDLE_FILES = (
    "performance.wav",
    "result.json",
    "schedule.json",
    "score.musicxml",
    "validation.json",
)
ARTIFACT_FILENAMES = {
    "score": "score.musicxml",
    "validation": "validation.json",
    "schedule": "schedule.json",
    "performance": "performance.wav",
}
HASH_ARTIFACTS = (
    "performance.wav",
    "schedule.json",
    "score.musicxml",
    "validation.json",
)
HASH_CONTRACT = {
    "algorithm": "sha256",
    "format": "lowercase_hex_64",
    "required_artifacts": list(HASH_ARTIFACTS),
    "verified": {name: True for name in HASH_ARTIFACTS},
}
RECORD_KEYS = {
    "contract",
    "entry_points",
    "operation",
    "platform",
    "public_operations",
    "result_bundle_evidence",
    "runtime_dependencies",
    "schema_version",
    "status",
    "verifier",
    "wheel",
    "wheel_metadata",
}
CONTRACT_KEYS = {
    "applied_score_defaults",
    "artifact_hash_contract",
    "bundle_artifacts",
    "bundle_directory_mode",
    "bundle_file_mode",
    "bundle_file_nlink",
    "bundle_schema_version",
    "bundle_status",
    "event_schedule",
    "provenance",
    "render_status",
    "response_schema_version",
    "revision",
    "score_context",
    "sound_pack",
    "warnings",
    "wav",
}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
EXPECTED_DEV_REQUIREMENTS = [
    'build==1.6.0; extra == "dev"',
    'mypy<2,>=1.11; extra == "dev"',
    'pytest<9,>=8.3; extra == "dev"',
    'ruff<1,>=0.11; extra == "dev"',
]
EXPECTED_ENTRY_POINTS = {
    "added_executables": ["playcli"],
    "console_scripts": {"playcli": "play_cli.cli:main"},
    "installed": [{"group": "console_scripts", "name": "playcli", "value": "play_cli.cli:main"}],
    "shell_executable": True,
}
EXPECTED_PUBLIC_OPERATIONS = {
    "doctor": "ok",
    "render": "ok",
    "sound-pack.install": "ok",
    "sound-pack.list": "ok",
    "sound-pack.verify": "ok",
    "validate": "ok",
}
EXPECTED_SCORE_CONTEXT = {
    "expression": {"mode": "neutral", "source": "default", "velocity": 64},
    "key": {"fifths": 0, "mode": "major", "source": "default"},
    "meter": {"beat_type": 4, "beats": 4, "source": "default"},
    "tempo": {"bpm": 100.0, "source": "default"},
}
EXPECTED_DEFAULTS = [
    {"field": "key", "id": "score_default_key", "value": {"fifths": 0, "mode": "major"}},
    {
        "field": "meter",
        "id": "score_default_meter",
        "value": {"beat_type": 4, "beats": 4},
    },
    {"field": "tempo", "id": "score_default_tempo", "value": {"bpm": 100.0}},
]
EXPECTED_WARNINGS = [
    {
        "applied_value": {"fifths": 0, "mode": "major"},
        "field": "key",
        "id": "score_default_key",
        "kind": "score_default",
        "message": "Key signature was absent; C major was applied.",
    },
    {
        "applied_value": {"beat_type": 4, "beats": 4},
        "field": "meter",
        "id": "score_default_meter",
        "kind": "score_default",
        "message": "Meter was absent; 4/4 was applied.",
    },
    {
        "applied_value": {"bpm": 100.0},
        "field": "tempo",
        "id": "score_default_tempo",
        "kind": "score_default",
        "message": "Tempo was absent; 100 BPM was applied.",
    },
]
EXPECTED_SCORE_SHA256 = "a24cab72e97896f8a0bf3abb30ee49f5c9aae69d0fb957d617a4a9d739b4a9ac"
EXPECTED_PACK_SHA256 = "077374cce49b7be9658d8c82350e4728b89695823403b2be1a6f0c8f6851b094"
EXPECTED_SAMPLES = [
    {
        "midi": 60,
        "sha256": "df4319660da4e6fc9c403a8fc12c3d14067ffd37bf2f7e15db42b6a313ff5f71",
    }
]
EXPECTED_EVENT_SCHEDULE = {
    "events": [
        {
            "duration_quarters": 1,
            "duration_quarters_exact": {"denominator": 1, "numerator": 1},
            "kind": "note",
            "onset_quarters": 0,
            "onset_quarters_exact": {"denominator": 1, "numerator": 0},
            "order": 0,
            "pitch": {"alter": 0, "midi": 60, "octave": 4, "step": "C"},
            "velocity": 64,
        }
    ],
    "schema_version": "1.1",
}
SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


class RecordContractError(Exception):
    pass


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RecordContractError(f"{label} is not an object with string keys.")
    return value


def _sha256(value: object, label: str) -> None:
    if not is_sha256(value):
        raise RecordContractError(f"{label} is not a lowercase SHA-256 value.")


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value: object) -> bool:
    return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)


def _strict_equal(value: object, expected: object) -> bool:
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        assert isinstance(value, dict)
        return set(value) == set(expected) and all(
            _strict_equal(value[key], expected[key]) for key in expected
        )
    if isinstance(expected, list):
        assert isinstance(value, list)
        return len(value) == len(expected) and all(
            _strict_equal(item, expected_item)
            for item, expected_item in zip(value, expected, strict=True)
        )
    return value == expected


def _python_version(value: object, label: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or re.fullmatch(r"\d+\.\d+\.\d+", value) is None:
        raise RecordContractError(f"{label} is invalid.")
    components = value.split(".")
    parsed = tuple(int(component) for component in components)
    if any(str(number) != component for number, component in zip(parsed, components, strict=True)):
        raise RecordContractError(f"{label} is not canonical.")
    assert len(parsed) == 3
    return parsed


def _validate_exact_fraction(value: object, label: str, *, positive: bool) -> Fraction:
    fraction = _object(value, label)
    if (
        set(fraction) != {"numerator", "denominator"}
        or not _integer(fraction.get("numerator"))
        or not _integer(fraction.get("denominator"))
        or fraction["denominator"] <= 0
        or (fraction["numerator"] <= 0 if positive else fraction["numerator"] < 0)
    ):
        raise RecordContractError(f"{label} is invalid.")
    exact = Fraction(fraction["numerator"], fraction["denominator"])
    if exact.numerator != fraction["numerator"] or exact.denominator != fraction["denominator"]:
        raise RecordContractError(f"{label} is not canonical.")
    return exact


def _validate_schedule(contract: dict[str, Any]) -> Fraction:
    schedule = _object(contract.get("event_schedule"), "Event schedule")
    events = schedule.get("events")
    if set(schedule) != {"events", "schema_version"} or schedule.get("schema_version") != "1.1":
        raise RecordContractError("Event schedule schema is invalid.")
    if not isinstance(events, list) or not events:
        raise RecordContractError("Event schedule is empty or invalid.")
    previous_end = Fraction(0)
    for expected_order, value in enumerate(events):
        event = _object(value, "Event")
        kind = event.get("kind")
        common = {
            "duration_quarters",
            "duration_quarters_exact",
            "kind",
            "onset_quarters",
            "onset_quarters_exact",
            "order",
        }
        expected_keys = common | ({"pitch", "velocity"} if kind == "note" else set())
        if kind not in {"note", "rest"} or set(event) != expected_keys:
            raise RecordContractError("Event field inventory is invalid.")
        if not _integer(event.get("order")) or event.get("order") != expected_order:
            raise RecordContractError("Event order is invalid.")
        onset = _validate_exact_fraction(
            event.get("onset_quarters_exact"), "Event onset", positive=False
        )
        duration = _validate_exact_fraction(
            event.get("duration_quarters_exact"), "Event duration", positive=True
        )
        if (
            not _number(event.get("onset_quarters"))
            or not _number(event.get("duration_quarters"))
            or float(onset) != float(event["onset_quarters"])
            or float(duration) != float(event["duration_quarters"])
            or onset != previous_end
        ):
            raise RecordContractError("Event numeric timing is invalid.")
        previous_end = onset + duration
        if kind == "note":
            pitch = _object(event.get("pitch"), "Event pitch")
            step = pitch.get("step")
            alter = pitch.get("alter")
            octave = pitch.get("octave")
            midi = pitch.get("midi")
            if (
                set(pitch) != {"alter", "midi", "octave", "step"}
                or step not in SEMITONES
                or not _integer(alter)
                or not _integer(octave)
                or not _integer(midi)
            ):
                raise RecordContractError("Event pitch is invalid.")
            assert isinstance(step, str)
            assert isinstance(alter, int) and not isinstance(alter, bool)
            assert isinstance(octave, int) and not isinstance(octave, bool)
            assert isinstance(midi, int) and not isinstance(midi, bool)
            if (
                not -2 <= alter <= 2
                or not 0 <= octave <= 9
                or not 21 <= midi <= 108
                or midi != 12 * (octave + 1) + SEMITONES[step] + alter
            ):
                raise RecordContractError("Event pitch is invalid.")
            velocity = event.get("velocity")
            if not _integer(velocity) or velocity != 64:
                raise RecordContractError("Event velocity is invalid.")
    if not _strict_equal(schedule, EXPECTED_EVENT_SCHEDULE):
        raise RecordContractError("Event schedule does not match the fixed score fixture.")
    return previous_end


def _validate_defaults(contract: dict[str, Any]) -> None:
    expected_ids = {
        "key": "score_default_key",
        "meter": "score_default_meter",
        "tempo": "score_default_tempo",
    }
    defaults = contract.get("applied_score_defaults")
    warnings = contract.get("warnings")
    context = _object(contract.get("score_context"), "Score context")
    if not isinstance(defaults, list) or not isinstance(warnings, list):
        raise RecordContractError("Default or warning evidence is invalid.")
    if not all(
        (
            _strict_equal(context, EXPECTED_SCORE_CONTEXT),
            _strict_equal(defaults, EXPECTED_DEFAULTS),
            _strict_equal(warnings, EXPECTED_WARNINGS),
        )
    ):
        raise RecordContractError("Default, warning, or context values are invalid.")
    key_context = _object(context.get("key"), "Key context")
    meter_context = _object(context.get("meter"), "Meter context")
    tempo_context = _object(context.get("tempo"), "Tempo context")
    expression_context = _object(context.get("expression"), "Expression context")
    if (
        set(context) != {"expression", "key", "meter", "tempo"}
        or set(key_context) != {"fifths", "mode", "source"}
        or not _integer(key_context.get("fifths"))
        or not isinstance(key_context.get("mode"), str)
        or key_context.get("source") != "default"
        or set(meter_context) != {"beat_type", "beats", "source"}
        or not _integer(meter_context.get("beat_type"))
        or not _integer(meter_context.get("beats"))
        or meter_context["beat_type"] <= 0
        or meter_context["beats"] <= 0
        or meter_context.get("source") != "default"
        or set(tempo_context) != {"bpm", "source"}
        or not _number(tempo_context.get("bpm"))
        or tempo_context["bpm"] <= 0
        or tempo_context.get("source") != "default"
        or expression_context != {"mode": "neutral", "source": "default", "velocity": 64}
    ):
        raise RecordContractError("Score context values are invalid.")
    warning_by_id: dict[str, dict[str, Any]] = {}
    for value in warnings:
        warning = _object(value, "Warning")
        if set(warning) != {"applied_value", "field", "id", "kind", "message"}:
            raise RecordContractError("Warning field inventory is invalid.")
        warning_id = warning.get("id")
        if (
            not isinstance(warning_id, str)
            or warning_id in warning_by_id
            or warning.get("kind") != "score_default"
            or not isinstance(warning.get("field"), str)
            or not isinstance(warning.get("message"), str)
            or not warning["message"]
            or not isinstance(warning.get("applied_value"), dict)
        ):
            raise RecordContractError("Warning identity is invalid.")
        warning_by_id[warning_id] = warning
    default_ids: set[str] = set()
    default_fields: set[str] = set()
    for value in defaults:
        default = _object(value, "Applied Score Default")
        if set(default) != {"field", "id", "value"}:
            raise RecordContractError("Applied Score Default field inventory is invalid.")
        default_id = default.get("id")
        field = default.get("field")
        if (
            not isinstance(default_id, str)
            or not isinstance(field, str)
            or default_id in default_ids
            or field in default_fields
            or not isinstance(default.get("value"), dict)
            or expected_ids.get(field) != default_id
        ):
            raise RecordContractError("Applied Score Default identity is invalid.")
        field_context = _object(context.get(field), "Applied Score Default context")
        context_value = {key: item for key, item in field_context.items() if key != "source"}
        matching_warning = warning_by_id.get(default_id)
        if (
            matching_warning is None
            or matching_warning.get("field") != field
            or matching_warning.get("applied_value") != default.get("value")
            or field_context.get("source") != "default"
            or context_value != default.get("value")
        ):
            raise RecordContractError("Applied Score Default relationships are invalid.")
        default_ids.add(default_id)
        default_fields.add(field)
    if default_ids != set(warning_by_id) or default_fields != {"key", "meter", "tempo"}:
        raise RecordContractError("Applied Score Defaults and warnings differ.")


def _validate_provenance(contract: dict[str, Any]) -> None:
    provenance = _object(contract.get("provenance"), "Provenance")
    if set(provenance) != {"cli", "execution", "renderer", "schema_version", "score", "sound_pack"}:
        raise RecordContractError("Provenance field inventory is invalid.")
    cli = _object(provenance.get("cli"), "CLI provenance")
    execution = _object(provenance.get("execution"), "Execution provenance")
    renderer = _object(provenance.get("renderer"), "Renderer provenance")
    score = _object(provenance.get("score"), "Score provenance")
    pack = _object(provenance.get("sound_pack"), "Sound Pack provenance")
    if (
        provenance.get("schema_version") != "1.5"
        or cli not in [{"name": "play-cli", "version": version} for version in ("0.8.1", "0.9.0")]
        or not _strict_equal(
            execution, {"location": "local", "network": "disabled", "telemetry": "disabled"}
        )
        or not _strict_equal(renderer, {"id": "pcm-sampler", "version": "2.0"})
        or score.get("format") != "musicxml"
        or set(score) != {"format", "sha256"}
        or score.get("sha256") != EXPECTED_SCORE_SHA256
        or set(pack) != {"id", "pack_hash", "version"}
        or pack.get("id") != "play-cli-performance-test-piano"
        or pack.get("version") != "1.0.0"
        or pack.get("pack_hash") != EXPECTED_PACK_SHA256
    ):
        raise RecordContractError("Provenance values are invalid.")
    _sha256(score.get("sha256"), "Score provenance hash")
    _sha256(pack.get("pack_hash"), "Sound Pack provenance hash")
    sound_pack = _object(contract.get("sound_pack"), "Sound Pack")
    if (
        set(sound_pack) != {"id", "pack_hash", "samples", "version"}
        or sound_pack.get("id") != "play-cli-performance-test-piano"
        or sound_pack.get("version") != "1.0.0"
        or sound_pack.get("pack_hash") != EXPECTED_PACK_SHA256
    ):
        raise RecordContractError("Sound Pack field inventory is invalid.")
    if {key: sound_pack.get(key) for key in ("id", "pack_hash", "version")} != pack:
        raise RecordContractError("Sound Pack provenance does not match the result.")
    samples = sound_pack.get("samples")
    if not _strict_equal(samples, EXPECTED_SAMPLES):
        raise RecordContractError("Sound Pack sample evidence is invalid.")
    assert isinstance(samples, list)
    sample_midis: set[int] = set()
    for value in samples:
        sample = _object(value, "Sound Pack sample")
        midi = sample.get("midi")
        if (
            set(sample) != {"midi", "sha256"}
            or not isinstance(midi, int)
            or isinstance(midi, bool)
            or not 21 <= midi <= 108
            or midi in sample_midis
        ):
            raise RecordContractError("Sound Pack sample fields are invalid.")
        _sha256(sample.get("sha256"), "Sound Pack sample hash")
        sample_midis.add(midi)


def _validate_contract(contract: dict[str, Any]) -> None:
    if set(contract) != CONTRACT_KEYS or not _strict_equal(
        contract.get("artifact_hash_contract"), HASH_CONTRACT
    ):
        raise RecordContractError("Comparable contract field inventory is invalid.")
    if (
        contract.get("response_schema_version") != "1.5"
        or contract.get("render_status") != "ok"
        or contract.get("bundle_schema_version") != "1.1"
        or contract.get("bundle_status") != "complete"
        or contract.get("bundle_artifacts") != list(BUNDLE_FILES)
        or contract.get("bundle_directory_mode") != "0555"
        or contract.get("bundle_file_mode") != "0444"
        or not _integer(contract.get("bundle_file_nlink"))
        or contract.get("bundle_file_nlink") != 1
    ):
        raise RecordContractError("Core Result Bundle contract is invalid.")
    total_quarters = _validate_schedule(contract)
    _validate_defaults(contract)
    _validate_provenance(contract)
    revision = _object(contract.get("revision"), "Revision contract")
    if not _strict_equal(
        revision,
        {
            "current_delivered_revision": 2,
            "current_sequence": 2,
            "history_kinds": ["delivered", "delivered"],
            "history_length": 2,
            "latest_delivered_revision": 2,
            "latest_delivered_sequence": 2,
            "previous_unchanged": True,
        },
    ):
        raise RecordContractError("Revision contract is invalid.")
    wav = _object(contract.get("wav"), "WAV contract")
    context = _object(contract.get("score_context"), "Score context")
    tempo = _object(context.get("tempo"), "Tempo context")
    bpm = tempo.get("bpm")
    if not _number(bpm):
        raise RecordContractError("Tempo value is invalid.")
    assert isinstance(bpm, (int, float)) and not isinstance(bpm, bool)
    if (
        set(wav)
        != {
            "channels",
            "duration_seconds",
            "format",
            "frame_bytes_complete",
            "frames",
            "sample_rate_hz",
            "sample_width_bytes",
        }
        or wav.get("format") != "PCM_S16LE"
        or not _integer(wav.get("channels"))
        or wav.get("channels") != 1
        or not _integer(wav.get("sample_width_bytes"))
        or wav.get("sample_width_bytes") != 2
        or wav.get("frame_bytes_complete") is not True
        or not _integer(wav.get("frames"))
        or not _integer(wav.get("sample_rate_hz"))
        or wav["frames"] <= 0
        or wav["sample_rate_hz"] != 16000
        or not _number(wav.get("duration_seconds"))
        or wav.get("duration_seconds") != wav["frames"] / wav["sample_rate_hz"]
        or wav["duration_seconds"] != float(total_quarters) * 60 / bpm
    ):
        raise RecordContractError("WAV semantic contract is invalid.")


def _validate_bundle_evidence(record: dict[str, Any]) -> None:
    evidence = _object(record.get("result_bundle_evidence"), "Result Bundle evidence")
    if set(evidence) != {"initial", "revision"}:
        raise RecordContractError("Result Bundle evidence revision inventory is invalid.")
    contract = _object(record["contract"], "Comparable contract")
    contract_wav = _object(contract["wav"], "WAV contract")
    artifact_sets: list[dict[str, Any]] = []
    for revision in ("initial", "revision"):
        bundle = _object(evidence[revision], f"{revision} Result Bundle evidence")
        if set(bundle) != {"artifact_sha256", "manifest_sha256", "wav"}:
            raise RecordContractError(f"{revision} Result Bundle evidence fields are invalid.")
        hashes = _object(bundle.get("artifact_sha256"), f"{revision} artifact hashes")
        if set(hashes) != set(HASH_ARTIFACTS):
            raise RecordContractError(f"{revision} artifact hash inventory is invalid.")
        for name in HASH_ARTIFACTS:
            _sha256(hashes[name], f"{revision} {name} hash")
        artifact_sets.append(hashes)
        _sha256(bundle.get("manifest_sha256"), f"{revision} manifest hash")
        wav = _object(bundle.get("wav"), f"{revision} WAV evidence")
        if (
            set(wav)
            != {
                "channels",
                "frame_bytes_complete",
                "frames",
                "sample_rate_hz",
                "sample_width_bytes",
            }
            or not _integer(wav.get("channels"))
            or wav.get("channels") != 1
            or wav.get("frame_bytes_complete") is not True
            or not _integer(wav.get("frames"))
            or wav["frames"] <= 0
            or not _integer(wav.get("sample_rate_hz"))
            or wav["sample_rate_hz"] != 16000
            or not _integer(wav.get("sample_width_bytes"))
            or wav.get("sample_width_bytes") != 2
            or any(wav.get(key) != contract_wav.get(key) for key in wav)
        ):
            raise RecordContractError(f"{revision} WAV evidence is invalid.")
    if artifact_sets[0] != artifact_sets[1]:
        raise RecordContractError("Initial and revision payload hashes differ.")
    provenance = _object(contract.get("provenance"), "Provenance")
    score = _object(provenance.get("score"), "Score provenance")
    if artifact_sets[0]["score.musicxml"] != score.get("sha256"):
        raise RecordContractError("Score artifact and provenance hashes differ.")


def validate_record(record: dict[str, Any]) -> None:
    if (
        set(record) != RECORD_KEYS
        or record.get("schema_version") != SCHEMA_VERSION
        or record.get("operation") != VERIFY_OPERATION
        or record.get("status") != "ok"
    ):
        raise RecordContractError("Platform record envelope or inventory is invalid.")
    wheel = _object(record.get("wheel"), "Wheel")
    version = _object(record.get("wheel_metadata"), "Wheel metadata").get("version")
    if version not in ("0.8.1", "0.9.0"):
        raise RecordContractError("Unsupported wheel version.")
    if set(wheel) != {"filename", "sha256"} or wheel.get("filename") != (
        f"play_cli-{version}-py3-none-any.whl"
    ):
        raise RecordContractError("Wheel identity is invalid.")
    _sha256(wheel.get("sha256"), "Wheel hash")
    platform = _object(record.get("platform"), "Platform")
    if set(platform) != {"description", "id", "implementation", "machine", "python"} or (
        platform.get("id") not in {"darwin", "linux"}
    ):
        raise RecordContractError("Platform evidence is invalid.")
    if not all(isinstance(platform.get(key), str) and platform.get(key) for key in platform):
        raise RecordContractError("Platform evidence values are invalid.")
    platform_python = _python_version(platform["python"], "Platform Python version")
    if platform_python < (3, 11, 0):
        raise RecordContractError("Platform Python version is unsupported.")
    if not _strict_equal(record.get("entry_points"), EXPECTED_ENTRY_POINTS):
        raise RecordContractError("Entry-point evidence is invalid.")
    if record.get("public_operations") != EXPECTED_PUBLIC_OPERATIONS:
        raise RecordContractError("Public operation evidence is invalid.")
    metadata = _object(record.get("wheel_metadata"), "Wheel metadata")
    if set(metadata) != {
        "generator",
        "name",
        "requires_dist",
        "requires_python",
        "root_is_purelib",
        "tag",
        "version",
        "wheel_version",
    } or not _strict_equal(
        metadata,
        {
            "generator": "setuptools (84.0.0)",
            "name": "play-cli",
            "requires_dist": EXPECTED_DEV_REQUIREMENTS,
            "requires_python": ">=3.11",
            "root_is_purelib": True,
            "tag": "py3-none-any",
            "version": version,
            "wheel_version": "1.0",
        },
    ):
        raise RecordContractError("Wheel metadata evidence is invalid.")
    dependencies = _object(record.get("runtime_dependencies"), "Runtime dependencies")
    if (
        set(dependencies)
        != {
            "atomic_noreplace",
            "optional_playback",
            "platform",
            "python",
            "required_status",
        }
        or dependencies.get("required_status") != "available"
    ):
        raise RecordContractError("Runtime dependency evidence is invalid.")
    python = _object(dependencies.get("python"), "Python dependency")
    target = _object(dependencies.get("platform"), "Platform dependency")
    atomic = _object(dependencies.get("atomic_noreplace"), "Atomic dependency")
    playback = _object(dependencies.get("optional_playback"), "Playback dependency")
    candidates = ["afplay"] if platform["id"] == "darwin" else ["aplay", "paplay"]
    expected_playback: dict[str, object] = {
        "action": {
            "alternatives": [
                {
                    "argv_template": [candidate, {"required_input": "wav_path"}],
                    "executable": candidate,
                }
                for candidate in candidates
            ],
            "id": "provide_playback_command",
            "required_inputs": {
                "wav_path": {
                    "description": "Complete local PCM WAV to play.",
                    "type": "local_path",
                }
            },
        },
        "candidates": candidates,
        "player": None,
        "required": False,
        "status": "unavailable",
    }
    if (
        set(python) != {"minimum_version", "status", "version"}
        or python.get("status") != "available"
        or python.get("minimum_version") != "3.11"
        or python.get("version") != platform.get("python")
        or not isinstance(python.get("version"), str)
        or re.fullmatch(r"\d+\.\d+\.\d+", python["version"]) is None
        or set(target) != {"id", "status", "supported"}
        or target.get("status") != "supported"
        or target.get("id") != platform.get("id")
        or target.get("supported") != ["darwin", "linux"]
        or set(atomic) != {"required_for", "status"}
        or atomic.get("status") != "available"
        or atomic.get("required_for") != ["render", "sound-pack.install"]
        or not _strict_equal(playback, expected_playback)
    ):
        raise RecordContractError("Required runtime dependency status is invalid.")
    dependency_python = _python_version(python["version"], "Dependency Python version")
    if dependency_python < (3, 11, 0):
        raise RecordContractError("Dependency Python version is unsupported.")
    verifier = _object(record.get("verifier"), "Verifier")
    if set(verifier) != {"sha256"}:
        raise RecordContractError("Verifier identity fields are invalid.")
    _sha256(verifier.get("sha256"), "Verifier hash")
    contract = _object(record.get("contract"), "Comparable contract")
    _validate_contract(contract)
    if contract["provenance"]["cli"]["version"] != version:
        raise RecordContractError("Wheel and CLI provenance versions differ.")
    _validate_bundle_evidence(record)
