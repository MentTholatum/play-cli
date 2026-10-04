from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import wave
import zipfile
from pathlib import Path

import pytest

from tests.qualification_baseline import load_qualifier

ROOT = Path(__file__).parents[1]
PLAYCLI = Path(sys.executable).with_name("playcli")
QUALIFIER = ROOT / "tools/qualify_standard_piano_pack.py"
TEST_QUALIFIER = ROOT / "tests/qualification_baseline.py"
BASELINE_SOURCE = ROOT / "tests/fixtures/qualification-baseline/source"
BUILDER = ROOT / "tools/build_salamander_standard_piano_pack.py"
FIXTURE_LOCK = ROOT / "standards/standard-piano-builder-fixture.lock.json"
QUALIFICATION_RECORD = (
    ROOT / "standards/salamander-3382bf9496bba2486f5ab0de55a264d1dfc38404.qualification.json"
)
LISTENING_ACCEPTANCE_RECORD = (
    ROOT / "standards/salamander-3382bf9496bba2486f5ab0de55a264d1dfc38404.listening-acceptance.json"
)


def _write_sample(path: Path, midi: int) -> dict[str, int | str]:
    sample_rate = 48_000
    frame_count = 24_000
    frequency = 440.0 * 2 ** ((midi - 69) / 12)
    frames = []
    for index in range(frame_count):
        attack = min(1.0, index / 480)
        release = min(1.0, (frame_count - 1 - index) / 480)
        sample = round(
            0.2
            * 32_767
            * attack
            * release
            * math.sin(2 * math.pi * frequency * index / sample_rate)
        )
        frames.append(struct.pack("<h", sample))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"".join(frames))
    data = path.read_bytes()
    return {
        "path": path.relative_to(path.parents[1]).as_posix(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_size": len(data),
        "format": "PCM_S16LE",
        "sample_rate_hz": sample_rate,
        "channels": 1,
        "frames": frame_count,
    }


def _write_complete_pack(root: Path) -> None:
    license_bytes = (ROOT / "tests/fixtures/test-piano-pack/LICENSE").read_bytes()
    (root / "LICENSE").parent.mkdir(parents=True)
    (root / "LICENSE").write_bytes(license_bytes)
    (root / "SOURCE.md").write_text("Generated qualification fixture.\n")
    builder_sha256 = hashlib.sha256(BUILDER.read_bytes()).hexdigest()
    receipt_bytes = (
        json.dumps(
            {
                "schema_version": "1.0",
                "builder": {"version": "1.1", "sha256": builder_sha256},
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    (root / "SOURCE-RECEIPT.json").write_bytes(receipt_bytes)
    samples = {
        str(midi): _write_sample(root / f"samples/midi-{midi}.wav", midi) for midi in range(21, 109)
    }
    manifest = {
        "schema_version": "1.2",
        "id": "qualification-fixture-piano",
        "version": "1.0.0",
        "license": {
            "id": "CC0-1.0",
            "file": "LICENSE",
            "sha256": hashlib.sha256(license_bytes).hexdigest(),
            "complete_text": True,
        },
        "source": {
            "uri": "urn:play-cli:qualification-fixture",
            "revision": {"type": "version", "value": "1.0.0"},
        },
        "attribution": "PLAY CLI contributors",
        "modifications": ["Deterministic test-only sine samples."],
        "generator": {"version": "1.1", "sha256": builder_sha256},
        "build": {
            "builder_sha256": builder_sha256,
            "source_revision": "1.0.0",
            "source_tree": "0000000000000000000000000000000000000000",
            "source_lock_sha256": "0" * 64,
            "source_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "conversion": {
                "ffmpeg": {
                    "path": str(Path(sys.executable).resolve()),
                    "sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
                    "version_output": "qualification fixture",
                },
                "ffprobe": {
                    "path": str(Path(sys.executable).resolve()),
                    "sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
                    "version_output": "qualification fixture",
                },
                "platform": sys.platform,
                "python": sys.version,
                "input": "generated PCM16",
                "output": "mono PCM_S16LE WAV, 48000 Hz",
                "downmix": "not required",
                "dither": "none",
                "fade_in_ms": 0,
                "velocity": 64,
                "velocity_layer": "fixture",
                "omitted_layers": [],
            },
        },
        "samples": samples,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def _write_builder_source(root: Path, tmp_path: Path) -> tuple[Path, str, Path]:
    fixture_wav = ROOT / "tests/fixtures/test-piano-pack/samples/c4.wav"
    with wave.open(str(fixture_wav), "rb") as audio:
        samples = struct.unpack(f"<{audio.getnframes()}h", audio.readframes(audio.getnframes()))
    source_buffer = io.BytesIO()
    with wave.open(source_buffer, "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(3)
        audio.setframerate(48_000)
        frames = bytearray()
        for sample in samples:
            encoded = int(sample << 8).to_bytes(3, "little", signed=True)
            frames.extend(encoded)
            frames.extend(encoded)
        audio.writeframes(bytes(frames))
    source_sample = source_buffer.getvalue()
    alternate_sample = tmp_path / "alternate.wav"
    alternate_sample.write_bytes(fixture_wav.read_bytes())
    (root / "Data").mkdir(parents=True)
    (root / "Samples").mkdir()
    (root / "LICENSE").write_bytes((ROOT / "tests/fixtures/test-piano-pack/LICENSE").read_bytes())
    (root / "README.md").write_text("Qualification fixture.\n")
    (root / "Salamander Grand Piano V3.sfz").write_text("Qualification fixture.\n")
    (root / "Data/notes.txt").write_text('include "Data/vel_08.txt" lovel=57 hivel=64\n')
    region_lines = []
    offset_lines = []
    tune_lines = []
    roots = list(range(21, 109, 3))
    for index, center in enumerate(roots, start=1):
        label = f"{index:02d}"
        low = center if index == 1 else center - 1
        high = center if index == len(roots) else center + 1
        sample = f"root{label}"
        region_lines.append(
            f"<region> region_label={label} lokey={low} hikey={high} "
            f"pitch_keycenter={center} sample={sample}$VEL.$EXT"
        )
        offset_lines.append(f"#define $OFF{label} 0")
        tune_lines.append(f"#define $TUNE{label} 0")
        (root / f"Samples/{sample}v8.wav").write_bytes(source_sample)
    (root / "Data/region.txt").write_text("\n".join(region_lines) + "\n")
    (root / "Data/vel_08.txt").write_text("\n".join(offset_lines) + "\n")
    (root / "Data/tune_nat.txt").write_text("\n".join(tune_lines) + "\n")
    lock_bytes = FIXTURE_LOCK.read_bytes()
    return FIXTURE_LOCK, hashlib.sha256(lock_bytes).hexdigest(), alternate_sample


def _run_builder(
    source: Path,
    output: Path,
    lock_path: Path,
    lock_sha256: str,
    *,
    ffmpeg: Path | None = None,
    ffprobe: Path | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(BUILDER),
        str(source),
        "--output",
        str(output),
        "--source-lock",
        str(lock_path),
        "--expected-lock-sha256",
        lock_sha256,
    ]
    if ffmpeg is not None:
        command.extend(("--ffmpeg", str(ffmpeg)))
    if ffprobe is not None:
        command.extend(("--ffprobe", str(ffprobe)))
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        env=environment,
    )


def _install_wheel_runtime(tmp_path: Path, wheel_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    created = subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(runtime)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert created.returncode == 0, created.stderr
    installed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "--python",
            str(runtime / "bin/python"),
            "install",
            "--no-index",
            "--no-deps",
            str(wheel_path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    return runtime / "bin/playcli"


def _installed_wheel_runtime(tmp_path: Path) -> tuple[Path, Path]:
    source_root = tmp_path / "approved-source"
    shutil.copytree(
        BASELINE_SOURCE,
        source_root,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "build", "*.egg-info"),
    )
    for extracted in source_root.rglob("*"):
        extracted.chmod(extracted.stat().st_mode & ~0o022)
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir(parents=True)
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = "1789142400"
    built = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(wheel_dir),
        ],
        cwd=source_root,
        capture_output=True,
        check=False,
        text=True,
        env=environment,
    )
    assert built.returncode == 0, built.stderr
    wheel_path = next(wheel_dir.glob("*.whl"))
    qualifier = load_qualifier()
    qualifier._verified_wheel(wheel_path, qualifier.APPROVED_PLAYCLI_GIT_COMMIT)
    return _install_wheel_runtime(tmp_path, wheel_path), wheel_path


def test_test_baseline_does_not_change_production_trust(tmp_path: Path) -> None:
    from tools import qualify_standard_piano_pack as production

    authority_path = production.WHEEL_AUTHORITY
    authority_hash = production.APPROVED_WHEEL_AUTHORITY_SHA256
    _, wheel_path = _installed_wheel_runtime(tmp_path)
    fixture = load_qualifier()
    assert fixture is not production
    assert production.WHEEL_AUTHORITY == authority_path
    assert production.APPROVED_WHEEL_AUTHORITY_SHA256 == authority_hash
    assert hashlib.sha256(authority_path.read_bytes()).hexdigest() == authority_hash
    with pytest.raises(production.QualificationFailure, match="approved git baseline"):
        production._verified_wheel(wheel_path, production.APPROVED_PLAYCLI_GIT_COMMIT)


def test_test_baseline_rejects_modified_authority(tmp_path: Path) -> None:
    qualifier = load_qualifier()
    modified = tmp_path / "wheel.lock.json"
    modified.write_bytes(qualifier.WHEEL_AUTHORITY.read_bytes() + b"\n")
    qualifier.WHEEL_AUTHORITY = modified
    with pytest.raises(qualifier.QualificationFailure, match="authority integrity failed"):
        qualifier._load_wheel_authority()


def _forge_wheel_with_pth(wheel_path: Path, forged_path: Path, payload: bytes) -> None:
    with zipfile.ZipFile(wheel_path) as source:
        files = {
            name: source.read(name)
            for name in source.namelist()
            if not name.endswith("/") and not name.endswith(".dist-info/RECORD")
        }
        record_name = next(name for name in source.namelist() if name.endswith(".dist-info/RECORD"))
    files["play_cli_baseline_probe.pth"] = payload
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for name, data in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow((name, f"sha256={digest}", len(data)))
    writer.writerow((record_name, "", ""))
    files[record_name] = record.getvalue().encode()
    with zipfile.ZipFile(forged_path, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name, data in sorted(files.items()):
            output.writestr(name, data)


def test_agent_can_automatically_qualify_a_complete_standard_piano_mapping(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-pack"
    _write_complete_pack(source)
    playcli, wheel_path = _installed_wheel_runtime(tmp_path)
    output = tmp_path / "qualification"
    record_path = tmp_path / "qualification-record.json"

    result = subprocess.run(
        [
            sys.executable,
            str(TEST_QUALIFIER),
            str(source),
            "--playcli",
            str(playcli),
            "--output",
            str(output),
            "--record",
            str(record_path),
            "--playcli-git-commit",
            "2860da46c1e58dfbd27f5276fc042711c4433161",
            "--distribution",
            str(wheel_path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["schema_version"] == "1.0"
    assert report["operation"] == "qualify-standard-piano"
    assert report["status"] == "automated_checks_passed"
    attempt = Path(report["qualification_output"])
    assert attempt.parent == output
    assert report["public_operations"] == {
        "sound-pack.install": "ok",
        "sound-pack.list": "ok",
        "sound-pack.verify": "ok",
        "render.coverage": "ok",
        "render.audition": "ok",
    }
    assert report["coverage"] == {
        "midi_min": 21,
        "midi_max": 108,
        "expected_notes": 88,
        "responsive_notes": 88,
        "missing_notes": [],
    }
    assert report["pitch"]["tested_midi"] == [21, 33, 45, 57, 60, 69, 81, 93]
    assert report["pitch"]["max_absolute_cents"] <= 15
    assert report["timing"]["expected_frames"] == report["timing"]["actual_frames"]
    assert report["audio_integrity"]["clipped_samples"] == 0
    assert report["audio_integrity"]["release_failures"] == []
    assert report["audio_integrity"]["boundary_click_failures"] == []
    assert report["listening_acceptance"] == {
        "status": "pending_human_listening",
        "content": {
            "representative_single_notes_midi": [21, 33, 45, 57, 60, 69, 81, 93, 108],
            "monophonic_scale_midi": [60, 62, 64, 65, 67, 69, 71, 72],
            "monophonic_arpeggio_midi": [60, 64, 67, 72],
            "contains_chords": False,
            "representative_note_duration_seconds": 3.0,
            "representative_silence_seconds": 1.5,
            "scale_note_duration_seconds": 0.375,
            "scale_silence_seconds": 0.1875,
        },
        "required_checks": [
            "recognizable_natural_acoustic_piano",
            "reliable_note_response",
            "clean_releases",
            "no_audible_clicks_or_clipping",
        ],
        "wav": str((attempt / "audition-result/performance.wav").resolve()),
    }
    record = json.loads(record_path.read_text())
    assert record == report["qualification_record"]
    assert record["tested_playcli"]["git_commit"] == ("2860da46c1e58dfbd27f5276fc042711c4433161")
    assert (
        record["tested_playcli"]["executable_sha256"]
        == hashlib.sha256((attempt / "runtime/bin/playcli").read_bytes()).hexdigest()
    )
    assert (
        record["tested_distribution"]["sha256"]
        == hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    )
    assert set(record["result_bundles"]) == {"coverage", "audition"}
    for bundle in record["result_bundles"].values():
        assert set(bundle["artifact_sha256"]) == {
            "score.musicxml",
            "validation.json",
            "schedule.json",
            "performance.wav",
        }
    with wave.open(report["listening_acceptance"]["wav"], "rb") as audition:
        frames = audition.readframes(audition.getnframes())
        assert (
            len(frames) == audition.getnframes() * audition.getnchannels() * audition.getsampwidth()
        )


def test_agent_qualifier_rejects_an_unbound_distribution_claim(tmp_path: Path) -> None:
    source = tmp_path / "source-pack"
    _write_complete_pack(source)
    fake_wheel = tmp_path / "fake.whl"
    fake_wheel.write_text("not a wheel\n")

    result = subprocess.run(
        [
            sys.executable,
            str(QUALIFIER),
            str(source),
            "--playcli",
            str(PLAYCLI),
            "--output",
            str(tmp_path / "qualification"),
            "--playcli-git-commit",
            "2860da46c1e58dfbd27f5276fc042711c4433161",
            "--distribution",
            str(fake_wheel),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["id"] == "qualification_failed"
    assert "Traceback" not in result.stderr


def test_agent_qualifier_rejects_a_false_git_claim_for_a_real_wheel(tmp_path: Path) -> None:
    source = tmp_path / "source-pack"
    _write_complete_pack(source)
    playcli, wheel_path = _installed_wheel_runtime(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(TEST_QUALIFIER),
            str(source),
            "--playcli",
            str(playcli),
            "--output",
            str(tmp_path / "qualification"),
            "--playcli-git-commit",
            "deadbeef",
            "--distribution",
            str(wheel_path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["id"] == "qualification_failed"
    assert "git commit is not the approved" in result.stderr
    assert "Traceback" not in result.stderr


def test_agent_qualifier_rejects_a_record_valid_wheel_with_executable_pth(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-pack"
    _write_complete_pack(source)
    _, wheel_path = _installed_wheel_runtime(tmp_path / "clean")
    marker = tmp_path / "pth-executed"
    forged = tmp_path / "play_cli-0.7.0-py3-none-any.whl"
    _forge_wheel_with_pth(
        wheel_path,
        forged,
        f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n".encode(),
    )
    playcli = _install_wheel_runtime(tmp_path / "forged", forged)

    result = subprocess.run(
        [
            sys.executable,
            str(TEST_QUALIFIER),
            str(source),
            "--playcli",
            str(playcli),
            "--output",
            str(tmp_path / "qualification"),
            "--playcli-git-commit",
            "2860da46c1e58dfbd27f5276fc042711c4433161",
            "--distribution",
            str(forged),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["id"] == "qualification_failed"
    assert "wheel does not match the approved git baseline" in result.stderr
    assert not marker.exists()


def test_agent_qualifier_rejects_unrecorded_runtime_startup_code_before_execution(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-pack"
    _write_complete_pack(source)
    playcli, wheel_path = _installed_wheel_runtime(tmp_path)
    site_packages = next((playcli.parents[1] / "lib").glob("python*/site-packages"))
    marker = tmp_path / "sitecustomize-executed"
    (site_packages / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(TEST_QUALIFIER),
            str(source),
            "--playcli",
            str(playcli),
            "--output",
            str(tmp_path / "qualification"),
            "--playcli-git-commit",
            "2860da46c1e58dfbd27f5276fc042711c4433161",
            "--distribution",
            str(wheel_path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["id"] == "qualification_failed"
    assert "startup code" in result.stderr
    assert not marker.exists()


def test_agent_qualifier_rejects_unrecorded_installed_files(tmp_path: Path) -> None:
    source = tmp_path / "source-pack"
    _write_complete_pack(source)
    playcli, wheel_path = _installed_wheel_runtime(tmp_path)
    site_packages = next((playcli.parents[1] / "lib").glob("python*/site-packages"))
    (site_packages / "play_cli/extra.py").write_text("UNAPPROVED = True\n")

    result = subprocess.run(
        [
            sys.executable,
            str(TEST_QUALIFIER),
            str(source),
            "--playcli",
            str(playcli),
            "--output",
            str(tmp_path / "qualification"),
            "--playcli-git-commit",
            "2860da46c1e58dfbd27f5276fc042711c4433161",
            "--distribution",
            str(wheel_path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["id"] == "qualification_failed"
    assert "filesystem inventory is not exact" in result.stderr


def test_agent_qualifier_rejects_replaced_entry_before_public_operations(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-pack"
    _write_complete_pack(source)
    playcli, wheel_path = _installed_wheel_runtime(tmp_path)
    marker = tmp_path / "wrapper-executed"
    original = playcli.with_name("playcli-original")
    playcli.rename(original)
    playcli.write_text(f'#!/bin/sh\ntouch {str(marker)!r}\nexec {str(original)!r} "$@"\n')
    playcli.chmod(0o755)

    result = subprocess.run(
        [
            sys.executable,
            str(TEST_QUALIFIER),
            str(source),
            "--playcli",
            str(playcli),
            "--output",
            str(tmp_path / "qualification"),
            "--playcli-git-commit",
            "2860da46c1e58dfbd27f5276fc042711c4433161",
            "--distribution",
            str(wheel_path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["id"] == "qualification_failed"
    assert "entry point content is not canonical" in result.stderr
    assert not marker.exists()


def test_agent_qualifier_rechecks_private_runtime_before_every_public_operation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-pack"
    _write_complete_pack(source)
    playcli, wheel_path = _installed_wheel_runtime(tmp_path)
    marker = tmp_path / "replacement-executed"
    runner = tmp_path / "runtime-replacer.py"
    runner.write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from tests.qualification_baseline import load_qualifier\n"
        "qualifier = load_qualifier()\n"
        "original = qualifier._run_playcli\n"
        "replaced = False\n"
        "def replace_after_first_operation(runtime, operation, *arguments):\n"
        "    global replaced\n"
        "    response = original(runtime, operation, *arguments)\n"
        "    if not replaced:\n"
        "        os.rename(runtime.playcli.path, str(runtime.playcli.path) + '.original')\n"
        "        with open(runtime.playcli.path, 'w') as replacement:\n"
        "            replacement.write('#!/bin/sh\\ntouch ' + repr(os.environ['MARKER']) + '\\n')\n"
        "        os.chmod(runtime.playcli.path, 0o755)\n"
        "        replaced = True\n"
        "    return response\n"
        "qualifier._run_playcli = replace_after_first_operation\n"
        "raise SystemExit(qualifier.main())\n"
    )
    environment = os.environ.copy()
    environment["MARKER"] = str(marker)

    result = subprocess.run(
        [
            sys.executable,
            str(runner),
            str(source),
            "--playcli",
            str(playcli),
            "--output",
            str(tmp_path / "qualification"),
            "--playcli-git-commit",
            "2860da46c1e58dfbd27f5276fc042711c4433161",
            "--distribution",
            str(wheel_path),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["id"] == "qualification_failed"
    assert "runtime identity changed" in result.stderr
    assert not marker.exists()


def test_agent_qualifier_rechecks_exact_runtime_inventory_before_every_operation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-pack"
    _write_complete_pack(source)
    playcli, wheel_path = _installed_wheel_runtime(tmp_path)
    marker = tmp_path / "shadow-module-executed"
    runner = tmp_path / "runtime-inventory-shadow.py"
    runner.write_text(
        "import os, pathlib, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from tests.qualification_baseline import load_qualifier\n"
        "qualifier = load_qualifier()\n"
        "original = qualifier._run_playcli\n"
        "inserted = False\n"
        "def insert_after_first_operation(runtime, operation, *arguments):\n"
        "    global inserted\n"
        "    response = original(runtime, operation, *arguments)\n"
        "    if not inserted:\n"
        "        shadow = runtime.site_packages / 'play_cli/cli'\n"
        "        shadow.mkdir()\n"
        "        (shadow / '__init__.py').write_text(\n"
        "            'from pathlib import Path\\nPath(' + repr(os.environ['MARKER']) "
        "            + ').touch()\\n'\n"
        "        )\n"
        "        inserted = True\n"
        "    return response\n"
        "qualifier._run_playcli = insert_after_first_operation\n"
        "raise SystemExit(qualifier.main())\n"
    )
    environment = os.environ.copy()
    environment["MARKER"] = str(marker)

    result = subprocess.run(
        [
            sys.executable,
            str(runner),
            str(source),
            "--playcli",
            str(playcli),
            "--output",
            str(tmp_path / "qualification"),
            "--playcli-git-commit",
            "2860da46c1e58dfbd27f5276fc042711c4433161",
            "--distribution",
            str(wheel_path),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["id"] == "qualification_failed"
    assert "runtime inventory changed" in result.stderr
    assert not marker.exists()


def test_agent_qualifier_rejects_receipt_without_matching_builder_provenance(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-pack"
    _write_complete_pack(source)
    receipt_bytes = b'{"schema_version":"1.0"}\n'
    (source / "SOURCE-RECEIPT.json").write_bytes(receipt_bytes)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["build"]["source_receipt_sha256"] = hashlib.sha256(receipt_bytes).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    playcli, wheel_path = _installed_wheel_runtime(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(TEST_QUALIFIER),
            str(source),
            "--playcli",
            str(playcli),
            "--output",
            str(tmp_path / "qualification"),
            "--playcli-git-commit",
            "2860da46c1e58dfbd27f5276fc042711c4433161",
            "--distribution",
            str(wheel_path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["id"] == "qualification_failed"
    assert "source receipt builder is not a JSON object" in result.stderr
    assert "Traceback" not in result.stderr


def test_agent_builder_rejects_source_files_that_do_not_match_the_pinned_lock(
    tmp_path: Path,
) -> None:
    source = tmp_path / "untrusted-source"
    source.mkdir()
    (source / "LICENSE").write_text("changed license\n")
    output = tmp_path / "must-not-exist"

    result = subprocess.run(
        [sys.executable, str(BUILDER), str(source), "--output", str(output)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "schema_version": "1.0",
        "operation": "build-standard-piano",
        "status": "failed",
        "error": {
            "id": "source_integrity_failed",
            "message": "Pinned Sound Pack source files failed integrity verification.",
        },
    }
    assert result.stderr
    assert not output.exists()


def test_agent_builder_rejects_modified_lock_authority_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    lock_path, expected_digest, _ = _write_builder_source(source, tmp_path)
    modified_lock = tmp_path / "modified.lock.json"
    lock = json.loads(lock_path.read_text())
    lock["repository"] = "https://example.invalid/tampered-source"
    modified_lock.write_text(json.dumps(lock, indent=2) + "\n")

    result = _run_builder(
        source,
        tmp_path / "must-not-exist",
        modified_lock,
        expected_digest,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["id"] == "source_integrity_failed"
    assert "two repository-approved authorities" in result.stderr
    assert not (tmp_path / "must-not-exist").exists()


def test_agent_builder_uses_a_verified_snapshot_if_source_is_replaced(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    lock_path, expected_digest, alternate_sample = _write_builder_source(source, tmp_path)
    control = tmp_path / "control"
    control_result = _run_builder(source, control, lock_path, expected_digest)
    assert control_result.returncode == 0, control_result.stderr

    wrapper = tmp_path / "racing-ffprobe"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if '-version' not in sys.argv and not os.path.exists(os.environ['RACE_MARKER']):\n"
        "    open(os.environ['RACE_MARKER'], 'w').close()\n"
        "    os.replace(os.environ['RACE_ALTERNATE'], os.environ['RACE_SOURCE'])\n"
        "os.execv(os.environ['REAL_FFPROBE'], [os.environ['REAL_FFPROBE'], *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "RACE_MARKER": str(tmp_path / "race-fired"),
            "RACE_SOURCE": str(source / "Samples/root01v8.wav"),
            "RACE_ALTERNATE": str(alternate_sample),
            "REAL_FFPROBE": shutil.which("ffprobe") or "ffprobe",
        }
    )
    raced = tmp_path / "raced"

    raced_result = _run_builder(
        source,
        raced,
        lock_path,
        expected_digest,
        ffprobe=wrapper,
        environment=environment,
    )

    assert raced_result.returncode == 0, raced_result.stderr
    assert (tmp_path / "race-fired").exists()
    assert (raced / "samples/midi-21.wav").read_bytes() == (
        control / "samples/midi-21.wav"
    ).read_bytes()
    assert json.loads((raced / "manifest.json").read_text())["id"] == (
        "play-cli-standard-piano-builder-fixture"
    )


def test_agent_builder_never_replaces_a_competing_output_directory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    lock_path, expected_digest, _ = _write_builder_source(source, tmp_path)
    output = tmp_path / "competitor"
    wrapper = tmp_path / "competing-ffmpeg"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if '-version' not in sys.argv and not os.path.exists(os.environ['RACE_MARKER']):\n"
        "    os.mkdir(os.environ['RACE_OUTPUT'])\n"
        "    open(os.path.join(os.environ['RACE_OUTPUT'], 'owned-by-competitor'), 'w').close()\n"
        "    open(os.environ['RACE_MARKER'], 'w').close()\n"
        "os.execv(os.environ['REAL_FFMPEG'], [os.environ['REAL_FFMPEG'], *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "RACE_MARKER": str(tmp_path / "race-fired"),
            "RACE_OUTPUT": str(output),
            "REAL_FFMPEG": shutil.which("ffmpeg") or "ffmpeg",
        }
    )

    result = _run_builder(
        source,
        output,
        lock_path,
        expected_digest,
        ffmpeg=wrapper,
        environment=environment,
    )

    assert result.returncode == 1
    assert (output / "owned-by-competitor").is_file()
    assert list(output.iterdir()) == [output / "owned-by-competitor"]


def test_agent_builder_rejects_a_rebound_pending_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    lock_path, expected_digest, _ = _write_builder_source(source, tmp_path)
    output = tmp_path / "published"
    moved = tmp_path / "moved-real-pending"
    wrapper = tmp_path / "pending-rebinder"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import glob, os, sys\n"
        "if '-version' not in sys.argv and not os.path.exists(os.environ['RACE_MARKER']):\n"
        "    pending = glob.glob(os.environ['RACE_PARENT'] + '/.published.build-pending*')[0]\n"
        "    os.rename(pending, os.environ['RACE_MOVED'])\n"
        "    os.mkdir(pending)\n"
        "    open(os.path.join(pending, 'owned-by-attacker'), 'w').close()\n"
        "    open(os.environ['RACE_MARKER'], 'w').close()\n"
        "os.execv(os.environ['REAL_FFMPEG'], [os.environ['REAL_FFMPEG'], *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "RACE_MARKER": str(tmp_path / "race-fired"),
            "RACE_PARENT": str(tmp_path),
            "RACE_MOVED": str(moved),
            "REAL_FFMPEG": shutil.which("ffmpeg") or "ffmpeg",
        }
    )

    result = _run_builder(
        source,
        output,
        lock_path,
        expected_digest,
        ffmpeg=wrapper,
        environment=environment,
    )

    assert result.returncode == 1
    assert not output.exists()
    assert moved.is_dir()
    assert any(path.name == "owned-by-attacker" for path in tmp_path.glob(".*/*"))


def test_agent_builder_rejects_output_rebound_during_post_publish_readback(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    lock_path, expected_digest, _ = _write_builder_source(source, tmp_path)
    output = tmp_path / "published"
    moved = tmp_path / "moved-published"
    runner = tmp_path / "post-publish-rebinder.py"
    runner.write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from tools import build_salamander_standard_piano_pack as builder\n"
        "original = builder._verify_output_files\n"
        "calls = 0\n"
        "def rebound(*args):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    if calls == 2:\n"
        "        os.rename(os.environ['RACE_OUTPUT'], os.environ['RACE_MOVED'])\n"
        "        os.mkdir(os.environ['RACE_OUTPUT'])\n"
        "        open(os.path.join(os.environ['RACE_OUTPUT'], 'owned-by-attacker'), 'w').close()\n"
        "    return original(*args)\n"
        "builder._verify_output_files = rebound\n"
        "raise SystemExit(builder.main())\n"
    )
    environment = os.environ.copy()
    environment.update({"RACE_OUTPUT": str(output), "RACE_MOVED": str(moved)})
    result = subprocess.run(
        [
            sys.executable,
            str(runner),
            str(source),
            "--output",
            str(output),
            "--source-lock",
            str(lock_path),
            "--expected-lock-sha256",
            expected_digest,
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert result.stdout, result.stderr
    assert json.loads(result.stdout)["error"]["id"] == "build_integrity_failed"
    assert list(output.iterdir()) == [output / "owned-by-attacker"]
    assert (moved / "manifest.json").is_file()


def test_agent_builder_rejects_an_unapproved_custom_lock(tmp_path: Path) -> None:
    source = tmp_path / "source"
    lock_path, expected_digest, _ = _write_builder_source(source, tmp_path)
    copied_lock = tmp_path / "caller-approved.lock.json"
    shutil.copyfile(lock_path, copied_lock)

    result = _run_builder(source, tmp_path / "output", copied_lock, expected_digest)

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["id"] == "source_integrity_failed"
    assert not (tmp_path / "output").exists()


def test_agent_builder_executes_the_conversion_tool_it_hashed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    lock_path, expected_digest, _ = _write_builder_source(source, tmp_path)
    marker = tmp_path / "swapped-tool-ran"
    link = tmp_path / "ffmpeg"
    original = tmp_path / "ffmpeg-original"
    replacement = tmp_path / "ffmpeg-replacement"
    original.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if '-version' in sys.argv and os.path.lexists(os.environ['TOOL_LINK']):\n"
        "    os.unlink(os.environ['TOOL_LINK'])\n"
        "    os.symlink(os.environ['TOOL_REPLACEMENT'], os.environ['TOOL_LINK'])\n"
        "os.execv(os.environ['REAL_FFMPEG'], [os.environ['REAL_FFMPEG'], *sys.argv[1:]])\n"
    )
    replacement.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if '-version' not in sys.argv:\n"
        "    open(os.environ['TOOL_MARKER'], 'w').close()\n"
        "os.execv(os.environ['REAL_FFMPEG'], [os.environ['REAL_FFMPEG'], *sys.argv[1:]])\n"
    )
    original.chmod(0o755)
    replacement.chmod(0o755)
    link.symlink_to(original)
    environment = os.environ.copy()
    environment.update(
        {
            "TOOL_LINK": str(link),
            "TOOL_REPLACEMENT": str(replacement),
            "TOOL_MARKER": str(marker),
            "REAL_FFMPEG": shutil.which("ffmpeg") or "ffmpeg",
        }
    )

    result = _run_builder(
        source,
        tmp_path / "output",
        lock_path,
        expected_digest,
        ffmpeg=link,
        environment=environment,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_agent_builder_never_executes_a_resolved_tool_replaced_after_precheck(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    lock_path, expected_digest, _ = _write_builder_source(source, tmp_path)
    marker = tmp_path / "replacement-ran"
    tool = tmp_path / "ffmpeg-under-test"
    replacement = tmp_path / "ffmpeg-replacement"
    shutil.copyfile(shutil.which("ffmpeg") or "ffmpeg", tool)
    tool.chmod(0o755)
    replacement.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "open(os.environ['TOOL_MARKER'], 'w').close()\n"
        "os.execv(os.environ['REAL_FFMPEG'], [os.environ['REAL_FFMPEG'], *sys.argv[1:]])\n"
    )
    replacement.chmod(0o755)
    runner = tmp_path / "tool-replacer.py"
    runner.write_text(
        "import os, pathlib, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from tools import build_salamander_standard_piano_pack as builder\n"
        "original = builder._verify_bound_tool\n"
        "replaced = False\n"
        "def replace_after_precheck(bound):\n"
        "    global replaced\n"
        "    original(bound)\n"
        "    if not replaced and bound.path == pathlib.Path(os.environ['RACE_TOOL']):\n"
        "        os.replace(os.environ['TOOL_REPLACEMENT'], os.environ['RACE_TOOL'])\n"
        "        replaced = True\n"
        "builder._verify_bound_tool = replace_after_precheck\n"
        "raise SystemExit(builder.main())\n"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "RACE_TOOL": str(tool),
            "TOOL_REPLACEMENT": str(replacement),
            "TOOL_MARKER": str(marker),
            "REAL_FFMPEG": shutil.which("ffmpeg") or "ffmpeg",
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            str(runner),
            str(source),
            "--output",
            str(tmp_path / "output"),
            "--source-lock",
            str(lock_path),
            "--expected-lock-sha256",
            expected_digest,
            "--ffmpeg",
            str(tool),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["id"] == "tool_integrity_failed"
    assert not marker.exists()
    assert not (tmp_path / "output").exists()


def test_agent_builder_returns_a_versioned_error_for_malformed_probe_json(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    lock_path, expected_digest, _ = _write_builder_source(source, tmp_path)
    wrapper = tmp_path / "malformed-ffprobe"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if '-version' in sys.argv:\n"
        "    os.execv(os.environ['REAL_FFPROBE'], [os.environ['REAL_FFPROBE'], *sys.argv[1:]])\n"
        "print('{\"streams\":[1]}')\n"
    )
    wrapper.chmod(0o755)
    environment = os.environ.copy()
    environment["REAL_FFPROBE"] = shutil.which("ffprobe") or "ffprobe"

    result = _run_builder(
        source,
        tmp_path / "output",
        lock_path,
        expected_digest,
        ffprobe=wrapper,
        environment=environment,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "schema_version": "1.0",
        "operation": "build-standard-piano",
        "status": "failed",
        "error": {
            "id": "source_integrity_failed",
            "message": "Pinned Sound Pack source files failed integrity verification.",
        },
    }
    assert "Traceback" not in result.stderr


def test_agent_can_read_the_audio_free_standard_piano_qualification_record() -> None:
    record = json.loads(QUALIFICATION_RECORD.read_text())

    assert record["schema_version"] == "1.0"
    assert record["status"] == "automated_checks_passed"
    assert record["tested_playcli"] == {
        "git_commit": "2860da46c1e58dfbd27f5276fc042711c4433161",
        "executable_sha256": record["tested_playcli"]["executable_sha256"],
        "cli": {"name": "play-cli", "version": "0.7.0"},
        "response_schema_version": "1.5",
        "renderer": {"id": "pcm-sampler", "version": "2.0"},
    }
    assert len(record["tested_playcli"]["executable_sha256"]) == 64
    assert record["tested_distribution"]["path_name"].endswith(".whl")
    assert len(record["tested_distribution"]["sha256"]) == 64
    assert record["tested_distribution"]["source_manifest_sha256"] == (
        "2315a3d602b7d8a24a6a63661a1c68b684300be5107ac39cdacf4370ff56f715"
    )
    assert len(record["tested_distribution"]["runtime_python_sha256"]) == 64
    assert (
        record["qualification_tools"]["builder_sha256"]
        == hashlib.sha256(BUILDER.read_bytes()).hexdigest()
    )
    assert (
        record["qualification_tools"]["qualifier_sha256"]
        == hashlib.sha256(QUALIFIER.read_bytes()).hexdigest()
    )
    assert record["sound_pack"] == {
        "id": "play-cli-standard-piano-salamander",
        "version": "1.0.0",
        "pack_hash": "f82e6d9aff03d7162f1d35254f1e7524c7f26a01a86435997b23eb23a0d7ba3e",
    }
    derived = record["derived_pack"]
    assert derived | {"conversion_environment": None} == {
        "source_revision": "3382bf9496bba2486f5ab0de55a264d1dfc38404",
        "source_tree": "77a68329fec9a01d1cdb965f02709bb188d1dbae",
        "source_lock_sha256": ("c115197af805e4fd1b6e9f6a568042ae901243f22cd2a8dfa1fb6441a9db5922"),
        "source_receipt_sha256": (
            "2d77bad7b7f6390740ef70db5ebc2e2b3bc7bc6382b0c01a034831598567d40c"
        ),
        "builder_sha256": hashlib.sha256(BUILDER.read_bytes()).hexdigest(),
        "manifest_sha256": ("d59ba371830bfcf2b5d9f5fc203461e6371c3c939c86027d8d1eddaba5af958c"),
        "pack_hash": "f82e6d9aff03d7162f1d35254f1e7524c7f26a01a86435997b23eb23a0d7ba3e",
        "files": 92,
        "samples": 88,
        "sample_bytes": 118_794_380,
        "total_bytes": 118_904_706,
        "sample_inventory_sha256": (
            "ede2ef0ddac4803f6479ca5eca1a0bf9714cc626e7cf7a65600f2192b8476dc5"
        ),
        "conversion_environment": None,
    }
    conversion = derived["conversion_environment"]
    assert set(conversion) == {
        "ffmpeg",
        "ffprobe",
        "platform",
        "python",
        "input",
        "output",
        "downmix",
        "dither",
        "fade_in_ms",
        "velocity",
        "velocity_layer",
        "omitted_layers",
    }
    for name in ("ffmpeg", "ffprobe"):
        assert len(conversion[name]["sha256"]) == 64
        assert "configuration:" in conversion[name]["version_output"]
    assert record["public_operations"] == {
        "sound-pack.install": "ok",
        "sound-pack.list": "ok",
        "sound-pack.verify": "ok",
        "render.coverage": "ok",
        "render.audition": "ok",
    }
    assert set(record["result_bundles"]) == {"coverage", "audition"}
    for bundle in record["result_bundles"].values():
        assert len(bundle["manifest_sha256"]) == 64
        assert set(bundle["artifact_sha256"]) == {
            "score.musicxml",
            "validation.json",
            "schedule.json",
            "performance.wav",
        }
        assert all(len(value) == 64 for value in bundle["artifact_sha256"].values())
    assert record["automated_checks"]["responsive_notes"] == 88
    assert record["automated_checks"]["expected_notes"] == 88
    assert record["automated_checks"]["timing_expected_frames"] == 1_584_000
    assert record["automated_checks"]["timing_actual_frames"] == 1_584_000
    assert record["automated_checks"]["max_absolute_cents"] == 12.608
    assert record["automated_checks"]["clipped_samples"] == 0
    assert record["automated_checks"]["boundary_click_failures"] == []
    assert record["automated_checks"]["release_failures"] == []
    assert record["automated_checks"]["silent_ending"] is True
    assert record["listening_acceptance"] == {
        "status": "pending_human_listening",
        "audition_wav_sha256": ("589c795b2cd172be6e3ab6b432ddaa57a2173c5114ff1b6e91631e59decca127"),
        "audition_wav_bytes": 4_536_044,
        "audition_wav_frames": 2_268_000,
        "audition_wav_duration_seconds": 47.25,
    }
    assert record["distribution_approval"] == "blocked_pending_human_listening"
    source_lock = ROOT / "standards/salamander-3382bf9496bba2486f5ab0de55a264d1dfc38404.lock.json"
    assert hashlib.sha256(source_lock.read_bytes()).hexdigest() == derived["source_lock_sha256"]
    assert not any(
        path.suffix.lower() in {".wav", ".flac"} for path in (ROOT / "standards").iterdir()
    )


def test_agent_can_audit_human_listening_acceptance_without_mutating_qualification() -> None:
    qualification_bytes = QUALIFICATION_RECORD.read_bytes()
    qualification = json.loads(qualification_bytes)
    acceptance_bytes = LISTENING_ACCEPTANCE_RECORD.read_bytes()
    acceptance = json.loads(acceptance_bytes)

    assert hashlib.sha256(acceptance_bytes).hexdigest() == (
        "08e938dc5b411e6e335aa29e1a1c07880c1a5621c37c861192ad90f78cba9c98"
    )
    assert acceptance == {
        "schema_version": "1.0",
        "record_type": "human-listening-acceptance",
        "status": "accepted",
        "decision_date": "2026-09-12",
        "decision_source": "agent_user_feedback",
        "conclusion_verbatim": "听感通过",
        "acceptance_scope": {
            "audience": "non_music_professional_agent_users",
            "assessment": "experience_listening_acceptance",
            "professional_acoustic_evaluation": False,
        },
        "audition_wav_sha256": ("589c795b2cd172be6e3ab6b432ddaa57a2173c5114ff1b6e91631e59decca127"),
        "qualification_record": {
            "path": (
                "standards/salamander-3382bf9496bba2486f5ab0de55a264d1dfc38404.qualification.json"
            ),
            "sha256": "59dd8d18e49dceb1196e46bdb88106b0a4b6cc19964b84caabc729c5987411d0",
        },
        "distribution": {
            "approval": "approved_for_separate_distribution",
            "external_publication": "not_published",
            "bundled_with_play_cli": False,
        },
    }
    assert (
        acceptance["qualification_record"]["sha256"]
        == hashlib.sha256(qualification_bytes).hexdigest()
    )
    assert qualification["status"] == "automated_checks_passed"
    assert qualification["listening_acceptance"]["status"] == "pending_human_listening"
    assert qualification["distribution_approval"] == "blocked_pending_human_listening"
    assert (
        acceptance["audition_wav_sha256"]
        == qualification["listening_acceptance"]["audition_wav_sha256"]
    )
    assert (
        acceptance["audition_wav_sha256"]
        == qualification["result_bundles"]["audition"]["artifact_sha256"]["performance.wav"]
    )


def test_agent_qualifier_rejects_malformed_success_without_traceback(
    tmp_path: Path,
) -> None:
    source = tmp_path / "unused-pack"
    source.mkdir()
    fake_playcli = tmp_path / "fake-playcli"
    fake_playcli.write_text("#!/bin/sh\nprintf '%s\\n' \"$FAKE_RESPONSE\"\n")
    fake_playcli.chmod(0o755)
    output = tmp_path / "qualification"

    responses = (
        "{",
        "[]",
        '{"status":"ok"}',
        '{"schema_version":"1.5","operation":"sound-pack.install","status":"error"}',
    )
    for response in responses:
        environment = os.environ.copy()
        environment["FAKE_RESPONSE"] = response
        result = subprocess.run(
            [
                sys.executable,
                str(QUALIFIER),
                str(source),
                "--playcli",
                str(fake_playcli),
                "--output",
                str(output),
            ],
            capture_output=True,
            check=False,
            text=True,
            env=environment,
        )

        assert result.returncode == 1
        assert json.loads(result.stdout) == {
            "schema_version": "1.0",
            "operation": "qualify-standard-piano",
            "status": "failed",
            "error": {
                "id": "qualification_failed",
                "message": "Standard Piano qualification failed.",
            },
        }
        assert "Traceback" not in result.stderr
        assert output.is_dir()
    attempts = sorted(output.glob("attempt-*"))
    assert len(attempts) == len(responses)
    assert all((attempt / "qualification-failure.json").is_file() for attempt in attempts)


def test_agent_qualifier_rejects_a_render_bundle_with_tampered_schedule(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-pack"
    _write_complete_pack(source)
    playcli, wheel_path = _installed_wheel_runtime(tmp_path)
    runner = tmp_path / "tampering-runner.py"
    runner.write_text(
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from tests.qualification_baseline import load_qualifier\n"
        "qualifier = load_qualifier()\n"
        "original = qualifier._run_playcli\n"
        "tampered = False\n"
        "def tamper(runtime, operation, *arguments):\n"
        "    global tampered\n"
        "    response = original(runtime, operation, *arguments)\n"
        "    if operation == 'render' and not tampered:\n"
        "        path = response['result_bundle']['artifacts']['schedule']['path']\n"
        "        os.chmod(path, 0o644)\n"
        "        schedule = json.load(open(path))\n"
        "        schedule['events'] = [e for e in schedule['events'] if e['kind'] != 'rest']\n"
        "        open(path, 'w').write(json.dumps(schedule))\n"
        "        tampered = True\n"
        "    return response\n"
        "qualifier._run_playcli = tamper\n"
        "raise SystemExit(qualifier.main())\n"
    )
    output = tmp_path / "qualification"

    result = subprocess.run(
        [
            sys.executable,
            str(runner),
            str(source),
            "--playcli",
            str(playcli),
            "--output",
            str(output),
            "--playcli-git-commit",
            "2860da46c1e58dfbd27f5276fc042711c4433161",
            "--distribution",
            str(wheel_path),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["id"] == "qualification_failed"
    assert "artifact hash does not match" in result.stderr
    assert any(output.glob("attempt-*/qualification-failure.json"))
