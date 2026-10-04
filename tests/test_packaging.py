from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePath

from tests.agent_contract import assert_machine_response

PLAYCLI = Path(sys.executable).with_name("playcli")
ROOT = Path(__file__).parents[1]
BUILD_DISTRIBUTIONS = ROOT / "tools/build_distributions.py"
VERIFY_DISTRIBUTION = ROOT / "tools/verify_platform_distribution.py"
COMPARE_RECORDS = ROOT / "tools/compare_platform_records.py"
MACOS_RECORD = ROOT / "standards/play-cli-0.8.1-macos-arm64.packaging.json"


def _rewrite_wheel(
    source: Path,
    destination: Path,
    transform: Callable[[str, bytes], bytes],
) -> None:
    with zipfile.ZipFile(source) as input_archive:
        with zipfile.ZipFile(destination, mode="x") as output_archive:
            for item in input_archive.infolist():
                data = input_archive.read(item.filename)
                output_archive.writestr(item, transform(item.filename, data))


def _set_linux_platform(record: dict[str, object]) -> None:
    platform_evidence = record["platform"]
    assert isinstance(platform_evidence, dict)
    platform_evidence.update({"description": "Linux", "id": "linux", "machine": "x86_64"})
    dependencies = record["runtime_dependencies"]
    assert isinstance(dependencies, dict)
    target_platform = dependencies["platform"]
    assert isinstance(target_platform, dict)
    target_platform["id"] = "linux"
    dependencies["optional_playback"] = {
        "action": {
            "alternatives": [
                {
                    "argv_template": [candidate, {"required_input": "wav_path"}],
                    "executable": candidate,
                }
                for candidate in ("aplay", "paplay")
            ],
            "id": "provide_playback_command",
            "required_inputs": {
                "wav_path": {
                    "description": "Complete local PCM WAV to play.",
                    "type": "local_path",
                }
            },
        },
        "candidates": ["aplay", "paplay"],
        "player": None,
        "required": False,
        "status": "unavailable",
    }


def test_agent_can_inspect_supported_runtime_dependencies() -> None:
    result = subprocess.run(
        [str(PLAYCLI), "doctor"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    response = assert_machine_response(result, operation="doctor", status="ok")
    assert response["dependencies"]["python"] == {
        "minimum_version": "3.11",
        "status": "available",
        "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    }
    assert response["dependencies"]["platform"] == {
        "id": sys.platform,
        "status": "supported",
        "supported": ["darwin", "linux"],
    }
    assert response["dependencies"]["atomic_noreplace"] == {
        "required_for": ["render", "sound-pack.install"],
        "status": "available",
    }
    assert response["dependencies"]["playback"]["required"] is False
    assert response["dependencies"]["playback"]["status"] in {"available", "unavailable"}


def test_agent_receives_an_actionable_error_when_requested_playback_is_missing(
    tmp_path: Path,
) -> None:
    environment = dict(os.environ)
    environment["PATH"] = str(tmp_path)
    result = subprocess.run(
        [str(PLAYCLI), "doctor", "--require-playback"],
        capture_output=True,
        check=False,
        text=True,
        env=environment,
    )

    assert result.returncode == 5
    response = assert_machine_response(result, operation="doctor", status="error")
    assert response["error"] == {
        "id": "runtime_dependency_missing",
        "message": "Requested playback capability is unavailable.",
    }
    assert response["missing_dependency"] == "playback"
    candidates = ["afplay"] if sys.platform == "darwin" else ["aplay", "paplay"]
    assert response["action"] == {
        "id": "provide_playback_command",
        "alternatives": [
            {
                "argv_template": [candidate, {"required_input": "wav_path"}],
                "executable": candidate,
            }
            for candidate in candidates
        ],
        "required_inputs": {
            "wav_path": {
                "description": "Complete local PCM WAV to play.",
                "type": "local_path",
            }
        },
    }
    assert result.stderr
    assert "Traceback" not in result.stderr


def test_maintainer_can_reproduce_installable_wheel_and_sdist(tmp_path: Path) -> None:
    reports: list[dict[str, object]] = []
    outputs: list[Path] = []
    for name in ("first", "second"):
        output = tmp_path / name
        built = subprocess.run(
            [
                sys.executable,
                str(BUILD_DISTRIBUTIONS),
                "--output",
                str(output),
                "--source-date-epoch",
                "1789142400",
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        assert built.returncode == 0, built.stderr
        assert len(built.stdout.splitlines()) == 1
        report = json.loads(built.stdout)
        assert report["schema_version"] == "1.0"
        assert report["operation"] == "build-distributions"
        assert report["status"] == "ok"
        assert report["source_date_epoch"] == 1_789_142_400
        assert report["build_tools"] == {
            "backend": {"name": "setuptools", "version": "84.0.0"},
            "frontend": {"name": "build", "version": "1.6.0"},
        }
        assert report["build_python"] == {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        }
        reports.append(report)
        outputs.append(output)

    assert reports[0]["artifacts"] == reports[1]["artifacts"]
    for pattern in ("*.whl", "*.tar.gz"):
        first = next(outputs[0].glob(pattern))
        second = next(outputs[1].glob(pattern))
        assert first.name == second.name
        assert (
            hashlib.sha256(first.read_bytes()).digest()
            == hashlib.sha256(second.read_bytes()).digest()
        )

    sdist = next(outputs[0].glob("*.tar.gz"))
    wheel = next(outputs[0].glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert not any(
            name.lower().endswith((".wav", ".flac", ".mp3", ".aif", ".aiff"))
            for name in archive.namelist()
        )
        entry_points = archive.read("play_cli-0.10.0.dist-info/entry_points.txt").decode()
    assert entry_points == "[console_scripts]\nplaycli = play_cli.cli:main\n"
    extracted = tmp_path / "extracted"
    with tarfile.open(sdist, mode="r:gz") as archive:
        audio_names = sorted(
            PurePath(name).as_posix()
            for name in archive.getnames()
            if name.lower().endswith((".wav", ".flac", ".mp3", ".aif", ".aiff"))
        )
        archive.extractall(extracted)
    assert audio_names == [
        "play_cli-0.10.0/tests/fixtures/performance-test-piano-pack/samples/midi-56.wav",
        "play_cli-0.10.0/tests/fixtures/performance-test-piano-pack/samples/midi-60.wav",
        "play_cli-0.10.0/tests/fixtures/performance-test-piano-pack/samples/midi-78.wav",
        "play_cli-0.10.0/tests/fixtures/test-piano-pack/samples/c4.wav",
    ]
    source = next(extracted.iterdir())
    assert (source / "pyproject.toml").is_file()
    rebuilt = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(tmp_path / "rebuilt"),
            str(source),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert rebuilt.returncode == 0, rebuilt.stderr


def test_agent_can_use_the_built_wheel_through_the_complete_platform_contract(
    tmp_path: Path,
) -> None:
    distributions = tmp_path / "distributions"
    built = subprocess.run(
        [
            sys.executable,
            str(BUILD_DISTRIBUTIONS),
            "--output",
            str(distributions),
            "--source-date-epoch",
            "1789142400",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    wheel = next(distributions.glob("*.whl"))
    acceptance_output = tmp_path / "acceptance"
    verified = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/verify_polyphonic_distribution.py"),
            "--wheel",
            str(wheel),
            "--output",
            str(acceptance_output),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr
    report = json.loads(verified.stdout)
    assert json.loads((acceptance_output / "record.json").read_text()) == report
    assert report["status"] == "ok"
    assert report["wheel"] == {
        "filename": "play_cli-0.10.0-py3-none-any.whl",
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }
    assert report["cli"] == {"name": "play-cli", "version": "0.10.0"}
    assert report["event_count"] == 20
    assert {voice["voice"] for voice in report["voices"]} == {"bass", "melody"}
    assert report["wav"]["duration_seconds"] == 9.6
    assert report["revision_verified"] is True
    assert report["human_listening"] == "not_performed"
    assert {op["operation"] for op in report["operations"]} == {
        "doctor",
        "sound-pack.install",
        "sound-pack.list",
        "sound-pack.verify",
        "validate",
        "render",
    }
    assert not (acceptance_output / "runtime/bin/play").exists()


def test_agent_rejects_ambiguous_wheel_dependencies_and_extra_entry_points(
    tmp_path: Path,
) -> None:
    distributions = tmp_path / "distributions"
    built = subprocess.run(
        [
            sys.executable,
            str(BUILD_DISTRIBUTIONS),
            "--output",
            str(distributions),
            "--source-date-epoch",
            "1789142400",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    wheel = next(distributions.glob("*.whl"))

    def add_ambiguous_dependency(name: str, data: bytes) -> bytes:
        if name.endswith(".dist-info/METADATA"):
            return data.replace(
                b"Provides-Extra: dev\n",
                b"Provides-Extra: dev\n"
                b'Requires-Dist: injected; extra == "dev" or python_version >= "3.11"\n',
            )
        return data

    def add_gui_script(name: str, data: bytes) -> bytes:
        if name.endswith(".dist-info/entry_points.txt"):
            return data + b"[gui_scripts]\nhelper = play_cli.cli:main\n"
        return data

    def weaken_python_requirement(name: str, data: bytes) -> bytes:
        if name.endswith(".dist-info/METADATA"):
            return data.replace(b"Requires-Python: >=3.11\n", b"Requires-Python: >=3.10\n")
        return data

    def duplicate_python_requirement(name: str, data: bytes) -> bytes:
        if name.endswith(".dist-info/METADATA"):
            return data.replace(
                b"Requires-Python: >=3.11\n",
                b"Requires-Python: >=3.11\nRequires-Python: >=3.11\n",
            )
        return data

    def make_platform_wheel(name: str, data: bytes) -> bytes:
        if name.endswith(".dist-info/WHEEL"):
            return data.replace(
                b"Root-Is-Purelib: true\nTag: py3-none-any\n",
                b"Root-Is-Purelib: false\nTag: cp311-cp311-macosx_11_0_arm64\n",
            )
        return data

    def add_wheel_tag(name: str, data: bytes) -> bytes:
        if name.endswith(".dist-info/WHEEL"):
            return data.replace(b"Tag: py3-none-any\n", b"Tag: py3-none-any\nTag: py2-none-any\n")
        return data

    for name, transform in (
        ("ambiguous-dependency", add_ambiguous_dependency),
        ("extra-entry-point", add_gui_script),
        ("weaken-python", weaken_python_requirement),
        ("duplicate-python", duplicate_python_requirement),
        ("platform-wheel", make_platform_wheel),
        ("multiple-wheel-tags", add_wheel_tag),
    ):
        candidate_dir = tmp_path / name
        candidate_dir.mkdir()
        candidate = candidate_dir / wheel.name
        _rewrite_wheel(wheel, candidate, transform)
        verified = subprocess.run(
            [
                sys.executable,
                str(VERIFY_DISTRIBUTION),
                str(candidate),
                "--output",
                str(candidate_dir / "acceptance"),
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        assert verified.returncode == 1
        response = json.loads(verified.stdout)
        assert response["status"] == "error"
        assert response["error"]["id"] == "platform_acceptance_failed"
        assert not (candidate_dir / "acceptance").exists()


def test_agent_can_read_the_generated_macos_packaging_record() -> None:
    record = json.loads(MACOS_RECORD.read_text())

    assert record["schema_version"] == "1.0"
    assert record["operation"] == "verify-platform-distribution"
    assert record["status"] == "ok"
    assert record["platform"]["id"] == "darwin"
    assert record["wheel"] == {
        "filename": "play_cli-0.8.1-py3-none-any.whl",
        "sha256": "b9458ddefc7b72e0c145015ba09bd62e6c6e613cf59139f605ab296d7e77f44a",
    }
    assert (
        record["verifier"]["sha256"]
        == "2a92e09ac423a10280093fd2552b59badd4bbb408e8694c6212a2878d0c6e877"
    )
    assert record["entry_points"]["console_scripts"] == {"playcli": "play_cli.cli:main"}
    assert record["entry_points"]["added_executables"] == ["playcli"]
    assert record["entry_points"]["shell_executable"] is True
    assert record["runtime_dependencies"]["required_status"] == "available"
    assert record["contract"]["revision"]["previous_unchanged"] is True
    assert record["contract"]["event_schedule"]["events"]
    assert record["contract"]["provenance"]["sound_pack"]["pack_hash"]
    assert record["contract"]["score_context"]["tempo"] == {
        "bpm": 100.0,
        "source": "default",
    }
    assert len(record["contract"]["warnings"]) == 3


def test_agent_can_compare_semantically_equal_platform_records_with_different_hashes(
    tmp_path: Path,
) -> None:
    macos = json.loads(MACOS_RECORD.read_text())
    linux = copy.deepcopy(macos)
    _set_linux_platform(linux)
    linux["platform"]["implementation"] = "PyPy"
    linux["platform"]["python"] = "3.11.13"
    linux["runtime_dependencies"]["python"]["version"] = "3.11.13"
    for bundle in ("initial", "revision"):
        evidence = linux["result_bundle_evidence"][bundle]
        evidence["manifest_sha256"] = "1" * 64
        evidence["artifact_sha256"]["performance.wav"] = "2" * 64
    left = tmp_path / "macos.json"
    right = tmp_path / "linux.json"
    left.write_text(json.dumps(macos))
    right.write_text(json.dumps(linux))

    compared = subprocess.run(
        [sys.executable, str(COMPARE_RECORDS), str(left), str(right)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert compared.returncode == 0, compared.stderr
    assert len(compared.stdout.splitlines()) == 1
    response = json.loads(compared.stdout)
    assert response["schema_version"] == "1.0"
    assert response["operation"] == "compare-platform-records"
    assert response["status"] == "ok"
    assert response["wheel_sha256"] == macos["wheel"]["sha256"]
    assert response["platforms"] == ["darwin", "linux"]


def test_agent_rejects_platform_records_with_invalid_hash_evidence(tmp_path: Path) -> None:
    original = json.loads(MACOS_RECORD.read_text())
    left = tmp_path / "left.json"
    left.write_text(json.dumps(original))
    candidates = []

    missing = copy.deepcopy(original)
    del missing["result_bundle_evidence"]["revision"]["artifact_sha256"]["performance.wav"]
    candidates.append(missing)

    malformed = copy.deepcopy(original)
    malformed["result_bundle_evidence"]["revision"]["artifact_sha256"]["performance.wav"] = (
        "not-a-sha256"
    )
    candidates.append(malformed)

    unverified = copy.deepcopy(original)
    unverified["contract"]["artifact_hash_contract"]["verified"]["performance.wav"] = False
    candidates.append(unverified)

    for index, candidate in enumerate(candidates):
        right = tmp_path / f"right-{index}.json"
        right.write_text(json.dumps(candidate))
        compared = subprocess.run(
            [sys.executable, str(COMPARE_RECORDS), str(left), str(right)],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        assert compared.returncode == 1
        response = json.loads(compared.stdout)
        assert response["schema_version"] == "1.0"
        assert response["operation"] == "compare-platform-records"
        assert response["status"] == "error"
        assert response["error"]["id"] == "platform_records_invalid"
        assert "Traceback" not in compared.stderr


def test_agent_rejects_platform_records_with_incomplete_acceptance_evidence(
    tmp_path: Path,
) -> None:
    original = json.loads(MACOS_RECORD.read_text())

    def strip_contract(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        record["contract"] = {"artifact_hash_contract": contract["artifact_hash_contract"]}

    def remove_entry_points(record: dict[str, object]) -> None:
        del record["entry_points"]

    def fail_public_operation(record: dict[str, object]) -> None:
        operations = record["public_operations"]
        assert isinstance(operations, dict)
        operations["render"] = "error"

    def add_unknown_field(record: dict[str, object]) -> None:
        record["unexpected"] = True

    def strip_runtime_details(record: dict[str, object]) -> None:
        platform_evidence = record["platform"]
        assert isinstance(platform_evidence, dict)
        record["runtime_dependencies"] = {
            "required_status": "available",
            "python": {"status": "available"},
            "platform": {"id": platform_evidence["id"], "status": "supported"},
            "atomic_noreplace": {"status": "available"},
            "optional_playback": {},
        }

    def empty_expression_context(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        context = contract["score_context"]
        assert isinstance(context, dict)
        context["expression"] = {}

    def invalidate_warning_kind(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        warnings = contract["warnings"]
        assert isinstance(warnings, list)
        warning = warnings[0]
        assert isinstance(warning, dict)
        warning["kind"] = "not-a-default"

    def duplicate_applied_default(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        defaults = contract["applied_score_defaults"]
        assert isinstance(defaults, list)
        defaults.append(copy.deepcopy(defaults[0]))

    def invalidate_renderer(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        provenance = contract["provenance"]
        assert isinstance(provenance, dict)
        provenance["renderer"] = {"id": None, "version": False}

    def use_boolean_sample_midi(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        sound_pack = contract["sound_pack"]
        assert isinstance(sound_pack, dict)
        samples = sound_pack["samples"]
        assert isinstance(samples, list)
        sample = samples[0]
        assert isinstance(sample, dict)
        sample["midi"] = True

    def use_string_event_onset(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        schedule = contract["event_schedule"]
        assert isinstance(schedule, dict)
        events = schedule["events"]
        assert isinstance(events, list)
        event = events[0]
        assert isinstance(event, dict)
        event["onset_quarters"] = "zero"

    def use_boolean_wav_frames(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        wav = contract["wav"]
        assert isinstance(wav, dict)
        wav["frames"] = True
        for revision in ("initial", "revision"):
            evidence = record["result_bundle_evidence"]
            assert isinstance(evidence, dict)
            bundle = evidence[revision]
            assert isinstance(bundle, dict)
            bundle_wav = bundle["wav"]
            assert isinstance(bundle_wav, dict)
            bundle_wav["frames"] = True

    def use_noncanonical_fraction(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        events = contract["event_schedule"]["events"]
        events[0]["onset_quarters_exact"] = {"numerator": 0, "denominator": 2}

    def break_schedule_continuity(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        events = contract["event_schedule"]["events"]
        events[0]["onset_quarters"] = 0.5
        events[0]["onset_quarters_exact"] = {"numerator": 1, "denominator": 2}

    def break_pitch_relationship(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        events = contract["event_schedule"]["events"]
        events[0]["pitch"]["midi"] = 61

    def break_default_relationship(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        warnings = contract["warnings"]
        assert isinstance(warnings, list)
        warnings[0]["applied_value"] = {"fifths": 1, "mode": "major"}

    def change_pack_pin(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        provenance = contract["provenance"]
        sound_pack = contract["sound_pack"]
        provenance["sound_pack"]["pack_hash"] = "3" * 64
        sound_pack["pack_hash"] = "3" * 64

    def break_wav_duration(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        contract["wav"]["duration_seconds"] = 0.5

    def break_revision_relationship(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        contract["revision"]["latest_delivered_sequence"] = 1

    def break_bundle_wav_relationship(record: dict[str, object]) -> None:
        evidence = record["result_bundle_evidence"]
        assert isinstance(evidence, dict)
        evidence["revision"]["wav"]["frames"] = 9599

    def use_boolean_bundle_nlink(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        contract["bundle_file_nlink"] = True

    def use_boolean_wav_channels(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        contract["wav"]["channels"] = True
        evidence = record["result_bundle_evidence"]
        assert isinstance(evidence, dict)
        for revision in ("initial", "revision"):
            evidence[revision]["wav"]["channels"] = True

    def use_integer_purelib(record: dict[str, object]) -> None:
        metadata = record["wheel_metadata"]
        assert isinstance(metadata, dict)
        metadata["root_is_purelib"] = 1

    def use_integer_shell_executable(record: dict[str, object]) -> None:
        entry_points = record["entry_points"]
        assert isinstance(entry_points, dict)
        entry_points["shell_executable"] = 1

    def use_integer_optional_required(record: dict[str, object]) -> None:
        dependencies = record["runtime_dependencies"]
        assert isinstance(dependencies, dict)
        dependencies["optional_playback"]["required"] = 0

    def use_integer_hash_verified(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        contract["artifact_hash_contract"]["verified"]["performance.wav"] = 1

    def use_integer_previous_unchanged(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        contract["revision"]["previous_unchanged"] = 1

    def use_boolean_key_fifths(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        contract["score_context"]["key"]["fifths"] = False
        contract["applied_score_defaults"][0]["value"]["fifths"] = False
        contract["warnings"][0]["applied_value"]["fifths"] = False

    def use_unsupported_python(record: dict[str, object]) -> None:
        platform_evidence = record["platform"]
        dependencies = record["runtime_dependencies"]
        assert isinstance(platform_evidence, dict)
        assert isinstance(dependencies, dict)
        platform_evidence["python"] = "3.10.99"
        dependencies["python"]["version"] = "3.10.99"

    def use_float_event_order(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        contract["event_schedule"]["events"][0]["order"] = 0.0

    def use_float_revision_length(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        contract["revision"]["history_length"] = 2.0

    def use_float_wav_shape(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        contract["wav"]["channels"] = 1.0
        contract["wav"]["sample_width_bytes"] = 2.0
        evidence = record["result_bundle_evidence"]
        assert isinstance(evidence, dict)
        for revision in ("initial", "revision"):
            evidence[revision]["wav"]["channels"] = 1.0
            evidence[revision]["wav"]["sample_width_bytes"] = 2.0

    def use_float_expression_velocity(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        contract["score_context"]["expression"]["velocity"] = 64.0

    def use_float_default_meter(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        contract["applied_score_defaults"][1]["value"]["beats"] = 4.0
        contract["warnings"][1]["applied_value"]["beats"] = 4.0

    def replace_note_with_rest(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        event = contract["event_schedule"]["events"][0]
        event["kind"] = "rest"
        del event["pitch"]
        del event["velocity"]

    def extend_fixed_schedule(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        contract["event_schedule"]["events"].append(
            {
                "duration_quarters": 1,
                "duration_quarters_exact": {"numerator": 1, "denominator": 1},
                "kind": "rest",
                "onset_quarters": 1,
                "onset_quarters_exact": {"numerator": 1, "denominator": 1},
                "order": 1,
            }
        )
        contract["wav"]["duration_seconds"] = 1.2
        contract["wav"]["frames"] = 19200
        evidence = record["result_bundle_evidence"]
        assert isinstance(evidence, dict)
        for revision in ("initial", "revision"):
            evidence[revision]["wav"]["frames"] = 19200

    def use_float_bundle_nlink(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        contract["bundle_file_nlink"] = 1.0

    def use_integer_tempo(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        contract["score_context"]["tempo"]["bpm"] = 100
        contract["applied_score_defaults"][2]["value"]["bpm"] = 100
        contract["warnings"][2]["applied_value"]["bpm"] = 100

    def replace_c4_with_d4(record: dict[str, object]) -> None:
        contract = record["contract"]
        assert isinstance(contract, dict)
        pitch = contract["event_schedule"]["events"][0]["pitch"]
        pitch["step"] = "D"
        pitch["midi"] = 62

    for index, mutation in enumerate(
        (
            strip_contract,
            remove_entry_points,
            fail_public_operation,
            add_unknown_field,
            strip_runtime_details,
            empty_expression_context,
            invalidate_warning_kind,
            duplicate_applied_default,
            invalidate_renderer,
            use_boolean_sample_midi,
            use_string_event_onset,
            use_boolean_wav_frames,
            use_noncanonical_fraction,
            break_schedule_continuity,
            break_pitch_relationship,
            break_default_relationship,
            change_pack_pin,
            break_wav_duration,
            break_revision_relationship,
            break_bundle_wav_relationship,
            use_boolean_bundle_nlink,
            use_boolean_wav_channels,
            use_integer_purelib,
            use_integer_shell_executable,
            use_integer_optional_required,
            use_integer_hash_verified,
            use_integer_previous_unchanged,
            use_boolean_key_fifths,
            use_unsupported_python,
            use_float_event_order,
            use_float_revision_length,
            use_float_wav_shape,
            use_float_expression_velocity,
            use_float_default_meter,
            replace_note_with_rest,
            extend_fixed_schedule,
            use_float_bundle_nlink,
            use_integer_tempo,
            replace_c4_with_d4,
        )
    ):
        macos = copy.deepcopy(original)
        linux = copy.deepcopy(original)
        _set_linux_platform(linux)
        mutation(macos)
        mutation(linux)
        left = tmp_path / f"macos-{index}.json"
        right = tmp_path / f"linux-{index}.json"
        left.write_text(json.dumps(macos))
        right.write_text(json.dumps(linux))

        compared = subprocess.run(
            [sys.executable, str(COMPARE_RECORDS), str(left), str(right)],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )

        assert compared.returncode == 1
        assert len(compared.stdout.splitlines()) == 1
        response = json.loads(compared.stdout)
        assert response["schema_version"] == "1.0"
        assert response["operation"] == "compare-platform-records"
        assert response["status"] == "error"
        assert response["error"]["id"] == "platform_records_invalid"
        assert "Traceback" not in compared.stderr
