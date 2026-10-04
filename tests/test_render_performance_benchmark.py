from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]
PLAYCLI = Path(sys.executable).with_name("playcli")
BENCHMARK = ROOT / "tools/benchmark_render_performance.py"
PACK_SOURCE = ROOT / "tests/fixtures/performance-test-piano-pack"
REFERENCE_RECORD = ROOT / "standards/play-cli-0.8.1-macos-arm64.render-performance.json"
REFERENCE_RECORD_SHA256 = "7ae4a341a51b80fb367f4714e6fda334f4e67f70f479f5efe53fe9ddc7cf529c"


def _benchmark_module() -> ModuleType:
    specification = importlib.util.spec_from_file_location("benchmark_render", BENCHMARK)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _install_pack(root: Path, store: Path) -> dict[str, object]:
    source = root / "benchmark-pack"
    shutil.copytree(PACK_SOURCE, source)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    samples = manifest["samples"]
    for midi in (62, 64, 65, 67, 69, 71, 72):
        target = source / f"samples/midi-{midi}.wav"
        shutil.copyfile(source / "samples/midi-60.wav", target)
        samples[str(midi)] = {
            **samples["60"],
            "path": f"samples/midi-{midi}.wav",
        }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    installed = subprocess.run(
        [
            str(PLAYCLI),
            "sound-pack",
            "install",
            str(source),
            "--store",
            str(store),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    response = json.loads(installed.stdout)
    assert isinstance(response["sound_pack"], dict)
    return response["sound_pack"]


def _benchmark(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    store = tmp_path / "packs"
    _install_pack(tmp_path, store)
    return subprocess.run(
        [
            sys.executable,
            str(BENCHMARK),
            "--playcli",
            str(PLAYCLI),
            "--sound-pack-store",
            str(store),
            "--sound-pack-id",
            "play-cli-performance-test-piano",
            "--sound-pack-version",
            "1.0.0",
            "--output",
            str(tmp_path / "benchmark"),
            "--record",
            str(tmp_path / "benchmark.json"),
            "--duration-seconds",
            "1",
            "--repeat-runs",
            "1",
            *extra,
        ],
        capture_output=True,
        check=False,
        text=True,
    )


def test_agent_can_measure_render_performance_with_complete_bundle_evidence(
    tmp_path: Path,
) -> None:
    result = _benchmark(tmp_path)

    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response == json.loads((tmp_path / "benchmark.json").read_text())
    assert response["schema_version"] == "1.0"
    assert response["operation"] == "benchmark-render-performance"
    assert response["status"] == "ok"
    assert response["measurement_scope"] == {
        "includes": "playcli render subprocess through complete Result Bundle publication",
        "sound_pack_install_included": False,
        "source_score_recognition_included": False,
    }
    assert response["fixture"]["duration_seconds"] == 1
    assert response["fixture"]["events"] == 2
    assert response["fixture"]["tempo_bpm"] == 120
    assert response["summary"]["formal_ten_minute_acceptance"] is False
    assert response["summary"]["target_met"] is True
    assert len(response["runs"]) == 2
    assert [run["cache_state"] for run in response["runs"]] == [
        "first_after_verification",
        "repeat",
    ]
    for run in response["runs"]:
        assert run["status"] == "ok"
        assert type(run["elapsed_seconds_raw"]) is float
        evidence = run["result_bundle"]
        assert evidence["exact_inventory"] == [
            "performance.wav",
            "result.json",
            "schedule.json",
            "score.musicxml",
            "validation.json",
            "voice-0001.wav",
        ]
        assert set(evidence["artifact_sha256"]) == set(evidence["exact_inventory"])
        assert evidence["hashes_verified"] is True
        assert evidence["schedule_verified"] is True
        assert evidence["provenance_verified"] is True
        assert evidence["wav"] == {
            "channels": 1,
            "complete_frame_bytes": True,
            "duration_seconds": 1,
            "format": "PCM_S16LE",
            "frame_bytes": 32_000,
            "frames": 16_000,
            "non_silent": True,
            "sample_rate_hz": 16_000,
            "sample_width_bytes": 2,
            "tail_complete": True,
        }


def test_agent_receives_machine_failure_when_performance_target_is_missed(
    tmp_path: Path,
) -> None:
    result = _benchmark(
        tmp_path,
        "--median-threshold-seconds",
        "0.000001",
        "--max-threshold-seconds",
        "0.000001",
    )

    assert result.returncode == 1
    response = json.loads(result.stdout)
    assert response == json.loads((tmp_path / "benchmark.json").read_text())
    assert response["schema_version"] == "1.0"
    assert response["operation"] == "benchmark-render-performance"
    assert response["status"] == "error"
    assert response["error"]["id"] == "render_performance_target_missed"
    assert response["summary"]["target_met"] is False
    assert response["summary"]["formal_ten_minute_acceptance"] is False
    assert "traceback" not in result.stderr.lower()


def test_agent_cannot_overwrite_an_existing_benchmark_record(tmp_path: Path) -> None:
    record = tmp_path / "benchmark.json"
    record.write_text("owner data\n")

    result = _benchmark(tmp_path)

    assert result.returncode == 1
    assert record.read_text() == "owner data\n"
    response = json.loads(result.stdout)
    assert response["status"] == "error"
    assert response["error"]["id"] == "benchmark_record_failed"
    assert "traceback" not in result.stderr.lower()


def test_agent_cannot_claim_formal_acceptance_with_arbitrary_authority(
    tmp_path: Path,
) -> None:
    store = tmp_path / "packs"
    pack = _install_pack(tmp_path, store)
    qualification = tmp_path / "qualification.json"
    qualification_data = {
        "status": "automated_checks_passed",
        "sound_pack": pack,
    }
    qualification.write_text(json.dumps(qualification_data))
    listening = tmp_path / "listening.json"
    listening.write_text(
        json.dumps(
            {
                "status": "accepted",
                "qualification_record": {
                    "sha256": hashlib.sha256(qualification.read_bytes()).hexdigest()
                },
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            str(BENCHMARK),
            "--playcli",
            str(PLAYCLI),
            "--sound-pack-store",
            str(store),
            "--sound-pack-id",
            "play-cli-performance-test-piano",
            "--sound-pack-version",
            "1.0.0",
            "--output",
            str(tmp_path / "benchmark"),
            "--record",
            str(tmp_path / "benchmark.json"),
            "--duration-seconds",
            "600",
            "--repeat-runs",
            "5",
            "--qualification-record",
            str(qualification),
            "--listening-acceptance-record",
            str(listening),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    response = json.loads(result.stdout)
    assert response["status"] == "error"
    assert response["error"]["id"] == "benchmark_sound_pack_invalid"
    assert "traceback" not in result.stderr.lower()


def test_agent_can_audit_the_committed_ten_minute_reference_record() -> None:
    record_bytes = REFERENCE_RECORD.read_bytes()
    record = json.loads(record_bytes)

    assert hashlib.sha256(record_bytes).hexdigest() == REFERENCE_RECORD_SHA256
    assert record["status"] == "ok"
    assert record["summary"]["formal_ten_minute_acceptance"] is True
    assert record["summary"]["target_met"] is True
    assert record["fixture"] == {
        "duration_seconds": 600,
        "events": 1200,
        "expected_schedule_sha256": (
            "f9f3a918d85ba183bd2748362ead7015e8fdce17ce2856da6a70e1bdbbae6a49"
        ),
        "kind": "generated_monophonic_scale",
        "musicxml_sha256": "76a9f94afac6b1e18adc162c3c031876d3619b39a174011415c2da706b3f9510",
        "tempo_bpm": 120,
    }
    assert record["sound_pack"]["pack_hash"] == (
        "f82e6d9aff03d7162f1d35254f1e7524c7f26a01a86435997b23eb23a0d7ba3e"
    )
    assert record["sound_pack_authority"] == {
        "listening_record_sha256": (
            "08e938dc5b411e6e335aa29e1a1c07880c1a5621c37c861192ad90f78cba9c98"
        ),
        "qualification_record_sha256": (
            "59dd8d18e49dceb1196e46bdb88106b0a4b6cc19964b84caabc729c5987411d0"
        ),
        "verified": True,
    }
    distribution = record["reference_environment"]["distribution"]
    assert distribution["verified"] is True
    assert distribution["wheel"] == {
        "filename": "play_cli-0.8.1-py3-none-any.whl",
        "sha256": "b9458ddefc7b72e0c145015ba09bd62e6c6e613cf59139f605ab296d7e77f44a",
    }
    assert distribution["installed_runtime"]["installed_files"] == 37
    assert len(record["runs"]) == 6
    assert all(run["status"] == "ok" for run in record["runs"])
    assert all(
        run["result_bundle"]["wav"]["frames"] == 28_800_000
        and run["result_bundle"]["wav"]["frame_bytes"] == 57_600_000
        and run["result_bundle"]["wav"]["tail_complete"] is True
        for run in record["runs"]
    )


def test_agent_rejects_malformed_performance_records() -> None:
    module = _benchmark_module()
    original = json.loads(REFERENCE_RECORD.read_text())
    module.validate_record(original)
    candidates = []
    for path, value in (
        (("reference_environment", "hardware"), "forged"),
        (("reference_environment", "python"), None),
        (("reference_environment", "python", "version"), "2.7.18"),
        (("reference_environment", "playcli", "path"), ""),
        (("reference_environment", "os", "name"), "Linux"),
        (("reference_environment", "benchmark_tool_sha256"), "x"),
        (
            (
                "reference_environment",
                "distribution",
                "attempt_platform_acceptance_record_sha256",
            ),
            "x",
        ),
        (("reference_environment", "distribution", "wheel", "sha256"), "z" * 64),
        (
            (
                "reference_environment",
                "distribution",
                "installed_runtime",
                "installed_inventory_sha256",
            ),
            "z" * 64,
        ),
        (("runs", 0, "sequence"), True),
        (("runs", 0, "result_bundle", "wav", "channels"), True),
        (("runs", 0, "result_bundle", "wav", "frame_bytes"), 57_600_000.0),
        (("runs", 0, "result_bundle", "artifact_sha256", "score.musicxml"), "1" * 64),
        (("runs", 1, "result_bundle", "artifact_sha256", "validation.json"), "2" * 64),
    ):
        candidate = copy.deepcopy(original)
        target = candidate
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        candidates.append(candidate)

    negative_timing = copy.deepcopy(original)
    for index, run in enumerate(negative_timing["runs"]):
        run["elapsed_seconds_raw"] = -(index + 1.0)
        run["elapsed_seconds"] = -(index + 1.0)
        run["cli_duration_ms"] = -1.0
    negative_timing["summary"] = {
        "first_run_seconds": -1.0,
        "repeat_median_seconds": -4.0,
        "repeat_max_seconds": -2.0,
        "formal_ten_minute_acceptance": True,
        "target_met": True,
    }
    candidates.append(negative_timing)

    empty_wav = copy.deepcopy(original)
    for run in empty_wav["runs"]:
        wav = run["result_bundle"]["wav"]
        wav["sample_rate_hz"] = 0
        wav["frames"] = 0
        wav["frame_bytes"] = 0
    candidates.append(empty_wav)

    for candidate in candidates:
        with pytest.raises(module.BenchmarkFailure):
            module.validate_record(candidate)


@pytest.mark.parametrize("startup_name", ["sitecustomize.py", "injected.pth"])
def test_agent_runtime_identity_detects_unlisted_startup_code(
    tmp_path: Path, startup_name: str
) -> None:
    module = _benchmark_module()
    runtime = tmp_path / "runtime"
    playcli = runtime / "bin/playcli"
    playcli.parent.mkdir(parents=True)
    runtime = runtime.resolve()
    playcli.write_text(
        f"#!{runtime / 'bin/python'}\n"
        "# -*- coding: utf-8 -*-\n"
        "import re\n"
        "import sys\n"
        "from play_cli.cli import main\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        "    sys.exit(main())\n"
    )
    metadata = runtime / "lib/python3.11/site-packages/play_cli-0.8.1.dist-info"
    metadata.mkdir(parents=True)
    record = metadata / "RECORD"
    record.write_text("../../../bin/playcli,,\nplay_cli-0.8.1.dist-info/RECORD,,\n")

    before = module._runtime_identity(playcli)
    (metadata.parent / startup_name).write_text("MARKER = True\n")

    assert before["complete_runtime_entries"] > 0
    with pytest.raises(module.BenchmarkFailure):
        module._runtime_identity(playcli)


def test_agent_rejects_replaced_bundle_before_record_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _benchmark_module()
    store = tmp_path / "packs"
    _install_pack(tmp_path, store)
    output = tmp_path / "benchmark"
    record = tmp_path / "benchmark.json"
    original = module.storage.rename_noreplace

    def replace_bundle(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
        *,
        commit_guard: object = None,
    ) -> None:
        if destination_name == record.name:
            os.rename(output / "run-00", output / "owner-run-00")
            (output / "run-00").mkdir()
            (output / "run-00/attacker.txt").write_text("attacker\n")
        original(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
            commit_guard=commit_guard,
        )

    monkeypatch.setattr(module.storage, "rename_noreplace", replace_bundle)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(BENCHMARK),
            "--playcli",
            str(PLAYCLI),
            "--sound-pack-store",
            str(store),
            "--sound-pack-id",
            "play-cli-performance-test-piano",
            "--sound-pack-version",
            "1.0.0",
            "--output",
            str(output),
            "--record",
            str(record),
            "--duration-seconds",
            "1",
            "--repeat-runs",
            "1",
        ],
    )

    assert module.main() == 1
    response = json.loads(capsys.readouterr().out)
    assert response["error"]["id"] == "benchmark_record_failed"
    assert not record.exists()
    assert (output / "run-00/attacker.txt").read_text() == "attacker\n"
    assert sorted(item.name for item in (output / "owner-run-00").iterdir()) == [
        "performance.wav",
        "result.json",
        "schedule.json",
        "score.musicxml",
        "validation.json",
        "voice-0001.wav",
    ]


def test_agent_does_not_publish_or_clean_replaced_pending_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _benchmark_module()
    record = tmp_path / "benchmark.json"
    original = module.storage.rename_noreplace

    def replace_pending(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
        *,
        commit_guard: object = None,
    ) -> None:
        os.rename(
            source_name, "owner-record", src_dir_fd=source_parent_fd, dst_dir_fd=source_parent_fd
        )
        descriptor = os.open(
            source_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=source_parent_fd,
        )
        os.write(descriptor, b"attacker\n")
        os.close(descriptor)
        original(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
            commit_guard=commit_guard,
        )

    monkeypatch.setattr(module.storage, "rename_noreplace", replace_pending)

    with pytest.raises(OSError):
        module._write(record, {"status": "ok"})

    assert not record.exists()
    assert (tmp_path / "owner-record").is_file()
    assert (tmp_path / f".{record.name}.pending-{os.getpid()}").read_text() == "attacker\n"


def test_agent_rejects_bundle_replaced_after_native_commit_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _benchmark_module()
    store = tmp_path / "packs"
    _install_pack(tmp_path, store)
    output = tmp_path / "benchmark"
    record = tmp_path / "benchmark.json"
    original = module.storage.rename_noreplace

    def replace_after_guard(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
        *,
        commit_guard: object = None,
    ) -> None:
        if destination_name == record.name:
            assert callable(commit_guard)
            commit_guard()
            os.rename(output / "run-00", output / "owner-run-00")
            (output / "run-00").mkdir()
            (output / "run-00/attacker.txt").write_text("attacker\n")
            original(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
                commit_guard=None,
            )
            return
        original(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
            commit_guard=commit_guard,
        )

    monkeypatch.setattr(module.storage, "rename_noreplace", replace_after_guard)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(BENCHMARK),
            "--playcli",
            str(PLAYCLI),
            "--sound-pack-store",
            str(store),
            "--sound-pack-id",
            "play-cli-performance-test-piano",
            "--sound-pack-version",
            "1.0.0",
            "--output",
            str(output),
            "--record",
            str(record),
            "--duration-seconds",
            "1",
            "--repeat-runs",
            "1",
        ],
    )

    assert module.main() == 1
    response = json.loads(capsys.readouterr().out)
    assert response["error"]["id"] == "benchmark_record_failed"
    assert record.is_file()
    assert (output / "run-00/attacker.txt").read_text() == "attacker\n"
    assert (output / "owner-run-00/result.json").is_file()
