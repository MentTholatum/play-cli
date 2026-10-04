from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any

import pytest

from play_cli import cli
from play_cli import result_bundle as bundle_module

ROOT = Path(__file__).parents[1]
PLAYCLI = Path(sys.executable).with_name("playcli")
PERFORMANCE_PACK = ROOT / "tests/fixtures/performance-test-piano-pack"
PACK_ID = "play-cli-performance-test-piano"
PACK_VERSION = "1.0.0"
LEGACY_PACK = ROOT / "tests/fixtures/test-piano-pack"
LEGACY_PACK_ID = "play-cli-test-piano"


def _install_pack(source: Path, store: Path) -> dict[str, Any]:
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
    return json.loads(installed.stdout)


def _install_performance_pack(tmp_path: Path) -> Path:
    store = tmp_path / "packs"
    _install_pack(PERFORMANCE_PACK, store)
    return store


def _render_command(
    score: Path,
    store: Path,
    result_bundle: Path,
    *,
    pack_id: str = PACK_ID,
    pack_version: str = PACK_VERSION,
    play: bool = False,
) -> list[str]:
    command = [
        str(PLAYCLI),
        "render",
        str(score),
        "--sound-pack",
        pack_id,
        "--sound-pack-version",
        pack_version,
        "--sound-pack-store",
        str(store),
        "--output",
        str(result_bundle),
    ]
    if play:
        command.append("--play")
    return command


def _run_render(
    score: Path,
    store: Path,
    result_bundle: Path,
    *,
    pack_id: str = PACK_ID,
    pack_version: str = PACK_VERSION,
    play: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _render_command(
            score,
            store,
            result_bundle,
            pack_id=pack_id,
            pack_version=pack_version,
            play=play,
        ),
        capture_output=True,
        check=False,
        text=True,
        env=env,
    )


def _capture_prepared_bundles(
    monkeypatch: pytest.MonkeyPatch,
) -> list[bundle_module.PendingResultBundle]:
    original_prepare = cli.prepare_result_bundle
    prepared: list[bundle_module.PendingResultBundle] = []

    def capture(destination: Path) -> bundle_module.PendingResultBundle:
        bundle = original_prepare(destination)
        prepared.append(bundle)
        return bundle

    monkeypatch.setattr(cli, "prepare_result_bundle", capture)
    return prepared


def test_agent_rechecks_lineage_immediately_before_atomic_publish(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    store = _install_performance_pack(tmp_path)
    previous = tmp_path / "revision-1"
    first = _run_render(ROOT / "tests/fixtures/renderable-c4.musicxml", store, previous)
    assert first.returncode == 0, first.stderr
    original_rename = bundle_module.storage.rename_noreplace
    external_snapshot: dict[str, tuple[bytes, int]] = {}

    def rename_after_lineage_tamper(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
        *,
        commit_guard: Any = None,
    ) -> None:
        if destination_name == "revision-2":
            previous.chmod(0o755)
            (previous / "EXTRA").write_text("external asset\n")
            previous.chmod(0o555)
            external_snapshot.update(
                {
                    item.name: (item.read_bytes(), stat.S_IMODE(item.stat().st_mode))
                    for item in previous.iterdir()
                }
            )
        if commit_guard is None:
            original_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
        else:
            original_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
                commit_guard=commit_guard,
            )

    monkeypatch.setattr(bundle_module.storage, "rename_noreplace", rename_after_lineage_tamper)
    descriptor_count_before = len(tuple(Path("/dev/fd").iterdir()))
    output = tmp_path / "revision-2"
    return_code = cli._render(
        ROOT / "tests/fixtures/minimal.musicxml",
        PACK_ID,
        PACK_VERSION,
        store,
        output,
        "pcm-sampler",
        False,
        previous,
        "Agent User reported a mismatch.",
    )
    captured = capsys.readouterr()

    assert return_code == 3
    assert json.loads(captured.out)["error"]["id"] == "revision_source_invalid"
    assert {
        item.name: (item.read_bytes(), stat.S_IMODE(item.stat().st_mode))
        for item in previous.iterdir()
    } == external_snapshot
    assert not output.exists()
    pending = tmp_path / ".revision-2.pending"
    assert {item.name for item in pending.iterdir()} == (
        set(bundle_module.REQUIRED_ARTIFACTS) | {"voice-0001.wav"}
    )
    assert stat.S_IMODE(pending.stat().st_mode) == 0o555
    descriptor_count_after = len(tuple(Path("/dev/fd").iterdir()))
    assert descriptor_count_after <= descriptor_count_before + 2


def _copy_pack_with_sample_rate(tmp_path: Path, *, midi: int, sample_rate: int) -> Path:
    source = tmp_path / f"sample-rate-{sample_rate}-pack"
    shutil.copytree(PERFORMANCE_PACK, source)
    sample_path = source / f"samples/midi-{midi}.wav"
    with wave.open(str(sample_path), "rb") as original:
        frames = original.readframes(original.getnframes())
        frame_count = original.getnframes()
    with wave.open(str(sample_path), "wb") as changed:
        changed.setnchannels(1)
        changed.setsampwidth(2)
        changed.setframerate(sample_rate)
        changed.writeframes(frames)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["samples"][str(midi)]["sample_rate_hz"] = sample_rate
    manifest["samples"][str(midi)]["frames"] = frame_count
    manifest["samples"][str(midi)]["sha256"] = hashlib.sha256(sample_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return source


def test_agent_can_render_the_complete_supported_monophonic_fixture(tmp_path: Path) -> None:
    store = tmp_path / "packs"
    pack_hash = _install_pack(PERFORMANCE_PACK, store)["sound_pack"]["pack_hash"]

    result_bundle = tmp_path / "result"
    rendered = _run_render(
        ROOT / "tests/fixtures/supported-monophonic.musicxml",
        store,
        result_bundle,
    )

    assert rendered.returncode == 0, rendered.stderr
    response = json.loads(rendered.stdout)
    assert response["schema_version"] == "1.6"
    assert response["status"] == "ok"
    assert response["cli"] == {"name": "play-cli", "version": "0.10.0"}
    assert response["renderer"] == {"id": "pcm-sampler", "version": "3.0"}
    assert response["sound_pack"] == {
        "id": PACK_ID,
        "version": PACK_VERSION,
        "pack_hash": pack_hash,
        "samples": [
            {
                "midi": 56,
                "sha256": "e37d9e468c10182a626879148bc1a391c6a2f5b5ccbe2cd02f5692c223747dce",
            },
            {
                "midi": 60,
                "sha256": "df4319660da4e6fc9c403a8fc12c3d14067ffd37bf2f7e15db42b6a313ff5f71",
            },
            {
                "midi": 78,
                "sha256": "9a94722e78aa8f3f7b8a0bbfe3360f67c126394c1ce79849cb29a81df4b5013c",
            },
        ],
    }
    assert response["warnings"] == []
    assert response["applied_score_defaults"] == []
    assert response["playback"] == {"requested": False, "status": "not_requested"}

    score_path = result_bundle / "score.musicxml"
    validation_path = result_bundle / "validation.json"
    schedule_path = result_bundle / "schedule.json"
    wav_path = result_bundle / "performance.wav"
    manifest_path = result_bundle / "result.json"
    assert stat.S_IMODE(result_bundle.stat().st_mode) == 0o555
    assert all(stat.S_IMODE(item.stat().st_mode) == 0o444 for item in result_bundle.iterdir())
    assert {key: value for key, value in response["result_bundle"].items() if key != "voices"} == {
        "schema_version": "1.2",
        "status": "complete",
        "path": str(result_bundle.resolve()),
        "manifest": str(manifest_path.resolve()),
        "artifacts": {
            "score": {
                "path": str(score_path.resolve()),
                "sha256": hashlib.sha256(score_path.read_bytes()).hexdigest(),
            },
            "validation": {
                "path": str(validation_path.resolve()),
                "sha256": hashlib.sha256(validation_path.read_bytes()).hexdigest(),
            },
            "schedule": {
                "path": str(schedule_path.resolve()),
                "sha256": hashlib.sha256(schedule_path.read_bytes()).hexdigest(),
            },
            "performance": {
                "path": str(wav_path.resolve()),
                "sha256": hashlib.sha256(wav_path.read_bytes()).hexdigest(),
            },
        },
    }
    assert (
        score_path.read_bytes()
        == (ROOT / "tests/fixtures/supported-monophonic.musicxml").read_bytes()
    )
    validation = json.loads(validation_path.read_text())
    schedule = json.loads(schedule_path.read_text())
    assert validation == {
        "schema_version": "1.2",
        "valid": True,
        "score_context": response["score_context"],
        "warnings": [],
        "applied_score_defaults": [],
    }
    assert schedule["schema_version"] == "1.2"
    assert schedule["events"] == [
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
            "duration_quarters": 1,
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
            "onset_quarters": 1,
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
            "onset_quarters": 3,
            "onset_quarters_exact": {"numerator": 3, "denominator": 1},
            "duration_quarters": 5,
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
            "onset_quarters": 8,
            "onset_quarters_exact": {"numerator": 8, "denominator": 1},
            "duration_quarters": 1,
            "duration_quarters_exact": {"numerator": 1, "denominator": 1},
            "pitch": None,
            "velocity": 0,
        },
    ]
    persisted_response = dict(response)
    persisted_response.pop("playback")
    assert json.loads(manifest_path.read_text()) == persisted_response

    assert response["wav"] == {
        "path": str(wav_path.resolve()),
        "format": "PCM_S16LE",
        "sample_rate_hz": 16_000,
        "channels": 1,
        "frames": 72_000,
        "duration_seconds": 4.5,
    }
    with wave.open(str(wav_path), "rb") as performance:
        assert performance.getnchannels() == 1
        assert performance.getsampwidth() == 2
        assert performance.getframerate() == 16_000
        assert performance.getnframes() == 72_000
        frame_data = performance.readframes(72_000)
    samples = struct.unpack("<72000h", frame_data)
    assert max(abs(sample) for sample in samples) < 32_767
    assert sum(sample * sample for sample in samples[0:8_000]) > 0
    assert all(sample == 0 for sample in samples[8_000:12_000])
    assert sum(sample * sample for sample in samples[12_000:24_000]) > 0
    assert abs(samples[23_999]) <= 32
    assert sum(sample * sample for sample in samples[24_000:64_000]) > 0
    assert all(sample == 0 for sample in samples[64_000:72_000])


def test_agent_optional_playback_uses_the_persisted_performance(tmp_path: Path) -> None:
    store = _install_performance_pack(tmp_path)

    player_bin = tmp_path / "bin"
    player_bin.mkdir()
    playback_log = tmp_path / "played-path.txt"
    player_script = '#!/bin/sh\nprintf \'%s\' "$1" > "$PLAYCLI_PLAYBACK_LOG"\n'
    for player_name in ("afplay", "aplay", "paplay"):
        player = player_bin / player_name
        player.write_text(player_script)
        player.chmod(0o755)

    result_bundle = tmp_path / "result"
    environment = dict(os.environ)
    environment["PATH"] = f"{player_bin}:{environment.get('PATH', '')}"
    environment["PLAYCLI_PLAYBACK_LOG"] = str(playback_log)
    rendered = _run_render(
        ROOT / "tests/fixtures/supported-monophonic.musicxml",
        store,
        result_bundle,
        play=True,
        env=environment,
    )

    assert rendered.returncode == 0, rendered.stderr
    response = json.loads(rendered.stdout)
    assert response["status"] == "ok"
    assert response["playback"]["requested"] is True
    assert response["playback"]["status"] == "played"
    assert Path(response["playback"]["player"]).name in {"afplay", "aplay", "paplay"}
    assert playback_log.read_text() == str((result_bundle / "performance.wav").resolve())
    assert (result_bundle / "result.json").is_file()


@pytest.mark.parametrize("swap_target", ["pending", "ancestor"])
def test_agent_bundle_publish_rejects_replaced_filesystem_bindings(
    swap_target: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    store = _install_performance_pack(tmp_path)

    original_prepare = cli.prepare_result_bundle
    replacement_pendings: list[Path] = []

    def prepare_then_swap(destination: Path) -> bundle_module.PendingResultBundle:
        bundle = original_prepare(destination)
        if swap_target == "pending":
            orphan = bundle.path.with_name(f"{bundle.path.name}.orphan")
            bundle.path.rename(orphan)
            bundle.path.mkdir()
            replacement = bundle.path
        else:
            public_parent = bundle.destination.parent
            orphan_parent = public_parent.with_name(f"{public_parent.name}.orphan")
            public_parent.rename(orphan_parent)
            public_parent.mkdir()
            replacement = public_parent / bundle.path.name
            replacement.mkdir()
        (replacement / "KEEP").write_text("attacker asset\n")
        replacement_pendings.append(replacement)
        return bundle

    monkeypatch.setattr(cli, "prepare_result_bundle", prepare_then_swap)
    descriptor_count_before = len(tuple(Path("/dev/fd").iterdir()))
    for attempt in range(5):
        destination = tmp_path / f"{swap_target}-{attempt}" / "result"
        return_code = cli._render(
            ROOT / "tests/fixtures/supported-monophonic.musicxml",
            PACK_ID,
            PACK_VERSION,
            store,
            destination,
            "pcm-sampler",
            False,
        )
        captured = capsys.readouterr()

        assert return_code == 3
        assert json.loads(captured.out)["error"]["id"] == "result_bundle_failed"
        assert not destination.exists()
        replacement = replacement_pendings[-1]
        assert {item.name for item in replacement.iterdir()} == {"KEEP"}
        assert (replacement / "KEEP").read_text() == "attacker asset\n"
    descriptor_count_after = len(tuple(Path("/dev/fd").iterdir()))
    assert descriptor_count_after <= descriptor_count_before + 2


@pytest.mark.parametrize("tamper", ["extra", "hardlink", "single-link-replacement"])
def test_agent_bundle_rejects_inventory_not_created_by_this_render(
    tamper: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    store = _install_performance_pack(tmp_path)
    original_publish = bundle_module.PendingResultBundle.publish
    original_score = (ROOT / "tests/fixtures/supported-monophonic.musicxml").read_bytes()
    external = tmp_path / "external-score.musicxml"
    external.write_bytes(original_score)
    external.chmod(0o600)

    def publish_with_changed_inventory(
        bundle: bundle_module.PendingResultBundle, manifest: dict[str, Any]
    ) -> Path:
        score = bundle.path / "score.musicxml"
        if tamper == "extra":
            (bundle.path / "UNDECLARED").write_text("unexpected\n")
        elif tamper == "hardlink":
            score.unlink()
            os.link(external, score)
        else:
            replacement = bundle.path / "replacement-score.tmp"
            replacement.write_bytes(original_score)
            os.replace(replacement, score)
        return original_publish(bundle, manifest)

    monkeypatch.setattr(
        bundle_module.PendingResultBundle,
        "publish",
        publish_with_changed_inventory,
    )
    descriptor_count_before = len(tuple(Path("/dev/fd").iterdir()))
    result_bundle = tmp_path / "result"
    return_code = cli._render(
        ROOT / "tests/fixtures/supported-monophonic.musicxml",
        PACK_ID,
        PACK_VERSION,
        store,
        result_bundle,
        "pcm-sampler",
        False,
    )
    captured = capsys.readouterr()

    assert return_code == 3
    assert json.loads(captured.out)["error"]["id"] == "result_bundle_failed"
    assert not result_bundle.exists()
    assert external.read_bytes() == original_score
    assert stat.S_IMODE(external.stat().st_mode) == 0o600
    assert "traceback" not in captured.err.lower()
    descriptor_count_after = len(tuple(Path("/dev/fd").iterdir()))
    assert descriptor_count_after <= descriptor_count_before + 2


@pytest.mark.parametrize("replacement_kind", ["empty", "not-wav", "truncated"])
def test_agent_sealed_bundle_blocks_payload_changes_before_atomic_publish(
    replacement_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    store = _install_performance_pack(tmp_path)

    replacements = {
        "empty": b"",
        "not-wav": b"NOT-A-WAV",
        "truncated": (PERFORMANCE_PACK / "samples/midi-60.wav").read_bytes()[:44],
    }
    replacement = replacements[replacement_kind]
    original_rename = bundle_module.storage.rename_noreplace
    prepared = _capture_prepared_bundles(monkeypatch)
    attempted: set[Path] = set()
    blocked: set[Path] = set()

    def replace_wav_before_publish(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        active_pending = prepared[-1].path
        if source_name == active_pending.name and active_pending not in attempted:
            attempted.add(active_pending)
            try:
                (active_pending / "performance.wav").write_bytes(replacement)
            except PermissionError:
                blocked.add(active_pending)
        original_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        )

    monkeypatch.setattr(bundle_module.storage, "rename_noreplace", replace_wav_before_publish)
    descriptor_count_before = len(tuple(Path("/dev/fd").iterdir()))
    for attempt in range(3):
        destination = tmp_path / f"race-{replacement_kind}-{attempt}"
        return_code = cli._render(
            ROOT / "tests/fixtures/supported-monophonic.musicxml",
            PACK_ID,
            PACK_VERSION,
            store,
            destination,
            "pcm-sampler",
            False,
        )
        captured = capsys.readouterr()

        assert return_code == 0, captured.err
        active_pending = prepared[-1].path
        assert active_pending in attempted
        assert active_pending in blocked
        response = json.loads(captured.out)
        wav_data = (destination / "performance.wav").read_bytes()
        assert (
            hashlib.sha256(wav_data).hexdigest()
            == response["result_bundle"]["artifacts"]["performance"]["sha256"]
        )
        assert stat.S_IMODE(destination.stat().st_mode) == 0o555
        assert all(stat.S_IMODE(item.stat().st_mode) == 0o444 for item in destination.iterdir())
    descriptor_count_after = len(tuple(Path("/dev/fd").iterdir()))
    assert descriptor_count_after <= descriptor_count_before + 2


@pytest.mark.parametrize("mutation", ["create", "delete"])
def test_agent_sealed_bundle_blocks_directory_mutation_before_atomic_publish(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    store = _install_performance_pack(tmp_path)
    original_rename = bundle_module.storage.rename_noreplace
    prepared = _capture_prepared_bundles(monkeypatch)
    blocked = False

    def mutate_directory_before_publish(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal blocked
        active_pending = prepared[-1].path
        if source_name == active_pending.name:
            try:
                if mutation == "create":
                    (active_pending / "EXTRA").write_text("unexpected\n")
                else:
                    (active_pending / "score.musicxml").unlink()
            except PermissionError:
                blocked = True
        original_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        )

    monkeypatch.setattr(bundle_module.storage, "rename_noreplace", mutate_directory_before_publish)
    result_bundle = tmp_path / "result"
    return_code = cli._render(
        ROOT / "tests/fixtures/supported-monophonic.musicxml",
        PACK_ID,
        PACK_VERSION,
        store,
        result_bundle,
        "pcm-sampler",
        False,
    )
    captured = capsys.readouterr()

    assert blocked
    assert return_code == 0, captured.err
    assert {item.name for item in result_bundle.iterdir()} == (
        set(bundle_module.REQUIRED_ARTIFACTS) | {"voice-0001.wav"}
    )


@pytest.mark.parametrize("swap_moment", ["before-rename", "after-rename"])
def test_agent_bundle_publish_rejects_a_replaced_sealed_directory(
    swap_moment: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    store = _install_performance_pack(tmp_path)
    original_rename = bundle_module.storage.rename_noreplace
    prepared = _capture_prepared_bundles(monkeypatch)
    injected: set[Path] = set()
    orphans: dict[Path, Path] = {}

    def replace_sealed_directory(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        active_bundle = prepared[-1]
        destination = active_bundle.destination
        pending = active_bundle.path
        if source_name != pending.name or destination in injected:
            original_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            return
        orphan = destination.with_name(f".{destination.name}.{swap_moment}.orphan")
        if swap_moment == "before-rename":
            pending.rename(orphan)
            pending.mkdir()
            (pending / "KEEP").write_text("replacement asset\n")
            original_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
        else:
            original_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            destination.rename(orphan)
            destination.mkdir()
            (destination / "KEEP").write_text("replacement asset\n")
        injected.add(destination)
        orphans[destination] = orphan

    monkeypatch.setattr(bundle_module.storage, "rename_noreplace", replace_sealed_directory)
    descriptor_count_before = len(tuple(Path("/dev/fd").iterdir()))
    for attempt in range(5):
        result_bundle = tmp_path / f"result-{attempt}"
        return_code = cli._render(
            ROOT / "tests/fixtures/supported-monophonic.musicxml",
            PACK_ID,
            PACK_VERSION,
            store,
            result_bundle,
            "pcm-sampler",
            False,
        )
        captured = capsys.readouterr()

        assert result_bundle in injected
        assert return_code == 3
        assert json.loads(captured.out)["error"]["id"] == "result_bundle_failed"
        assert {item.name for item in result_bundle.iterdir()} == {"KEEP"}
        assert (result_bundle / "KEEP").read_text() == "replacement asset\n"
        assert (orphans[result_bundle] / "result.json").is_file()
        assert stat.S_IMODE(orphans[result_bundle].stat().st_mode) == 0o555
    descriptor_count_after = len(tuple(Path("/dev/fd").iterdir()))
    assert descriptor_count_after <= descriptor_count_before + 2


def test_agent_discloses_score_defaults_and_pads_a_short_sample(tmp_path: Path) -> None:
    store = _install_performance_pack(tmp_path)

    result_bundle = tmp_path / "result"
    rendered = _run_render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        result_bundle,
    )

    assert rendered.returncode == 0, rendered.stderr
    response = json.loads(rendered.stdout)
    assert [warning["id"] for warning in response["warnings"]] == [
        "score_default_key",
        "score_default_meter",
        "score_default_tempo",
    ]
    assert response["applied_score_defaults"] == [
        {
            "id": "score_default_key",
            "field": "key",
            "value": {"fifths": 0, "mode": "major"},
        },
        {
            "id": "score_default_meter",
            "field": "meter",
            "value": {"beats": 4, "beat_type": 4},
        },
        {"id": "score_default_tempo", "field": "tempo", "value": {"bpm": 100.0}},
    ]
    assert response["wav"]["frames"] == 9_600
    with wave.open(str(result_bundle / "performance.wav"), "rb") as performance:
        samples = struct.unpack("<9600h", performance.readframes(9_600))
    assert sum(sample * sample for sample in samples[:8_000]) > 0
    assert all(sample == 0 for sample in samples[8_000:])


@pytest.mark.parametrize("tempo", ["0.099", "1e-101"])
def test_agent_rejects_performances_beyond_the_renderer_resource_limit(
    tempo: str, tmp_path: Path
) -> None:
    store = _install_performance_pack(tmp_path)

    score = tmp_path / "slow.musicxml"
    score.write_text(
        (ROOT / "tests/fixtures/renderable-c4.musicxml")
        .read_text()
        .replace('tempo="120"', f'tempo="{tempo}"')
        .replace("<per-minute>120</per-minute>", f"<per-minute>{tempo}</per-minute>")
    )
    result_bundle = tmp_path / "result"
    command = _render_command(score, store, result_bundle)
    for _attempt in range(2):
        rendered = subprocess.run(command, capture_output=True, check=False, text=True)

        assert rendered.returncode == 3
        response = json.loads(rendered.stdout)
        assert response["error"]["id"] == "renderer_duration_unsupported"
        assert "traceback" not in rendered.stderr.lower()
        assert not result_bundle.exists()
        assert not (tmp_path / ".result.pending").exists()


def test_agent_rejects_a_note_too_short_to_render_a_non_silent_fade(tmp_path: Path) -> None:
    store = _install_performance_pack(tmp_path)
    score = tmp_path / "too-short.musicxml"
    score.write_text(
        (ROOT / "tests/fixtures/renderable-c4.musicxml")
        .read_text()
        .replace('tempo="120"', 'tempo="480000"')
        .replace("<per-minute>120</per-minute>", "<per-minute>480000</per-minute>")
    )
    result_bundle = tmp_path / "result"
    rendered = _run_render(score, store, result_bundle)

    assert rendered.returncode == 3
    response = json.loads(rendered.stdout)
    assert response["error"]["id"] == "renderer_timing_unrepresentable"
    assert "traceback" not in rendered.stderr.lower()
    assert not result_bundle.exists()
    assert not (tmp_path / ".result.pending").exists()


@pytest.mark.parametrize(
    ("sample_rate", "expected_frames", "expected_size"),
    [(16_000, 9_600_000, 19_200_044), (48_000, 28_800_000, 57_600_044)],
)
def test_agent_can_render_a_performance_at_the_ten_minute_limit(
    sample_rate: int,
    expected_frames: int,
    expected_size: int,
    tmp_path: Path,
) -> None:
    store = tmp_path / "packs"
    source = (
        PERFORMANCE_PACK
        if sample_rate == 16_000
        else _copy_pack_with_sample_rate(tmp_path, midi=60, sample_rate=sample_rate)
    )
    _install_pack(source, store)

    score = tmp_path / "ten-minutes.musicxml"
    score.write_text(
        (ROOT / "tests/fixtures/renderable-c4.musicxml")
        .read_text()
        .replace('tempo="120"', 'tempo="0.1"')
        .replace("<per-minute>120</per-minute>", "<per-minute>0.1</per-minute>")
    )
    result_bundle = tmp_path / "result"
    rendered = _run_render(score, store, result_bundle)

    assert rendered.returncode == 0, rendered.stderr
    response = json.loads(rendered.stdout)
    assert response["wav"]["duration_seconds"] == 600
    assert response["wav"]["sample_rate_hz"] == sample_rate
    assert response["wav"]["frames"] == expected_frames
    assert (result_bundle / "performance.wav").stat().st_size == expected_size


@pytest.mark.parametrize(
    ("player_mode", "expected_status"), [("missing", "unavailable"), ("failure", "failed")]
)
def test_agent_playback_failure_keeps_the_completed_bundle(
    player_mode: str, expected_status: str, tmp_path: Path
) -> None:
    store = _install_performance_pack(tmp_path)

    player_bin = tmp_path / "bin"
    player_bin.mkdir()
    if player_mode == "failure":
        for player_name in ("afplay", "aplay", "paplay"):
            player = player_bin / player_name
            player.write_text("#!/bin/sh\nexit 7\n")
            player.chmod(0o755)
    environment = dict(os.environ)
    environment["PATH"] = str(player_bin)
    result_bundle = tmp_path / "result"
    rendered = _run_render(
        ROOT / "tests/fixtures/supported-monophonic.musicxml",
        store,
        result_bundle,
        play=True,
        env=environment,
    )

    assert rendered.returncode == 0, rendered.stderr
    response = json.loads(rendered.stdout)
    assert response["status"] == "ok"
    assert response["playback"]["status"] == expected_status
    if player_mode == "missing":
        candidates = ["afplay"] if sys.platform == "darwin" else ["aplay", "paplay"]
        assert response["playback"]["error"] == {
            "id": "runtime_dependency_missing",
            "message": "Requested playback capability is unavailable.",
        }
        assert response["playback"]["action"] == {
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
    assert expected_status in rendered.stderr.lower()
    assert (result_bundle / "performance.wav").is_file()
    assert (result_bundle / "result.json").is_file()
    assert "playback" not in json.loads((result_bundle / "result.json").read_text())


def test_agent_renderer_preflight_failure_leaves_the_output_retriable(tmp_path: Path) -> None:
    store = tmp_path / "packs"
    _install_pack(LEGACY_PACK, store)

    result_bundle = tmp_path / "result"
    command = _render_command(
        ROOT / "tests/fixtures/supported-monophonic.musicxml",
        store,
        result_bundle,
        pack_id=LEGACY_PACK_ID,
    )
    failed = subprocess.run(command, capture_output=True, check=False, text=True)

    assert failed.returncode == 3
    assert json.loads(failed.stdout)["error"]["id"] == "sound_pack_sample_unavailable"
    assert not result_bundle.exists()
    pending = tmp_path / ".result.pending"
    assert not pending.exists()

    retried = subprocess.run(command, capture_output=True, check=False, text=True)
    assert retried.returncode == 3
    assert json.loads(retried.stdout)["error"]["id"] == "sound_pack_sample_unavailable"
    assert not pending.exists()


def test_agent_never_overwrites_an_existing_result_bundle(tmp_path: Path) -> None:
    store = _install_performance_pack(tmp_path)
    result_bundle = tmp_path / "result"
    result_bundle.mkdir()
    keep = result_bundle / "KEEP"
    keep.write_text("user asset\n")

    rendered = _run_render(
        ROOT / "tests/fixtures/supported-monophonic.musicxml",
        store,
        result_bundle,
    )

    assert rendered.returncode == 3
    assert json.loads(rendered.stdout)["error"]["id"] == "result_bundle_exists"
    assert keep.read_text() == "user asset\n"
    assert not (tmp_path / ".result.pending").exists()


def test_agent_rejects_mixed_sample_rates_without_publishing_a_bundle(tmp_path: Path) -> None:
    source = _copy_pack_with_sample_rate(tmp_path, midi=78, sample_rate=8_000)

    store = tmp_path / "packs"
    _install_pack(source, store)
    result_bundle = tmp_path / "result"
    rendered = _run_render(
        ROOT / "tests/fixtures/supported-monophonic.musicxml",
        store,
        result_bundle,
    )

    assert rendered.returncode == 3
    assert json.loads(rendered.stdout)["error"]["id"] == "renderer_sample_incompatible"
    assert not result_bundle.exists()
