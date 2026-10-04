from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from play_cli import cli, sound_pack
from play_cli import sound_pack_storage as storage

ROOT = Path(__file__).parents[1]
TEST_PACK = ROOT / "tests/fixtures/test-piano-pack"
PACK_ID = "play-cli-test-piano"
PLAYCLI = Path(sys.executable).with_name("playcli")


def test_agent_failed_post_install_readback_keeps_the_final_orphan_uncommitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    destination = store / "play-cli-test-piano" / "1.0.0"
    bundle = tmp_path / "bundle"

    def fail_readback(
        destination: Path,
        content: sound_pack.PackContent,
        store_fd: int,
        identity_fd: int,
        identity_stat: os.stat_result,
        version_fd: int,
        installed_inode: tuple[int, int],
    ) -> sound_pack.VerifiedSoundPack:
        raise sound_pack.SoundPackFailure(
            "sound_pack_corrupt",
            "Installed Sound Pack failed integrity verification.",
            f"Injected anchored read-back failure for {destination}.",
        )

    monkeypatch.setattr(sound_pack, "_verify_anchored_install", fail_readback)

    with pytest.raises(sound_pack.SoundPackFailure):
        sound_pack.install_local_pack(str(TEST_PACK), store)

    assert (destination / "manifest.json").is_file()
    assert not (destination / ".playcli-receipt.json").exists()

    listed = subprocess.run(
        [str(PLAYCLI), "sound-pack", "list", "--store", str(store)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert listed.returncode == 0
    assert json.loads(listed.stdout)["sound_packs"] == [
        {
            "id": PACK_ID,
            "version": "1.0.0",
            "pack_hash": None,
            "status": "corrupt",
            "path": str(destination),
        }
    ]

    verified = subprocess.run(
        [
            str(PLAYCLI),
            "sound-pack",
            "verify",
            PACK_ID,
            "--version",
            "1.0.0",
            "--store",
            str(store),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert verified.returncode == 4
    assert json.loads(verified.stdout)["error"]["id"] == "sound_pack_corrupt"

    rendered = subprocess.run(
        [
            str(PLAYCLI),
            "render",
            str(ROOT / "tests/fixtures/renderable-c4.musicxml"),
            "--sound-pack",
            PACK_ID,
            "--sound-pack-version",
            "1.0.0",
            "--sound-pack-store",
            str(store),
            "--output",
            str(bundle),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert rendered.returncode == 4
    assert json.loads(rendered.stdout)["error"]["id"] == "sound_pack_corrupt"
    assert not bundle.exists()


def test_agent_failed_staging_preserves_a_replaced_file_without_path_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    identity = store / PACK_ID
    original_rename = storage.rename_noreplace

    def replace_file_then_fail(
        source_parent_fd: int,
        source: str,
        destination_parent_fd: int,
        destination: str,
    ) -> None:
        if destination == "1.0.0":
            pending = identity / source
            (pending / "SOURCE.md").rename(pending / "SOURCE.original")
            (pending / "SOURCE.md").write_text("competitor replacement\n")
            raise OSError("injected version commit failure after replacement")
        original_rename(source_parent_fd, source, destination_parent_fd, destination)

    monkeypatch.setattr(storage, "rename_noreplace", replace_file_then_fail)

    with pytest.raises(sound_pack.SoundPackFailure):
        sound_pack.install_local_pack(str(TEST_PACK), store)

    orphan_directories = [entry for entry in identity.iterdir() if entry.is_dir()]
    assert len(orphan_directories) == 1
    orphan = orphan_directories[0]
    assert (orphan / "SOURCE.md").read_text() == "competitor replacement\n"
    assert (orphan / "SOURCE.original").is_file()


def test_agent_sample_is_rechecked_after_pack_verification(tmp_path: Path) -> None:
    store = tmp_path / "store"
    installed = sound_pack.install_local_pack(str(TEST_PACK), store).sound_pack
    sample_path = installed.path / "samples/c4.wav"
    sample_path.write_bytes(sample_path.read_bytes() + b"\n")

    with pytest.raises(sound_pack.SoundPackFailure) as failure:
        sound_pack.read_verified_sample(installed, 60)

    assert failure.value.error_id == "sound_pack_corrupt"


def test_agent_sample_symlink_swap_between_check_and_read_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    installed = sound_pack.install_local_pack(str(TEST_PACK), store).sound_pack
    sample_path = installed.path / "samples/c4.wav"
    outside = tmp_path / "outside.wav"
    outside.write_bytes(sample_path.read_bytes())
    backup = tmp_path / "sample-backup.wav"
    original_lstat = Path.lstat
    original_open = os.open
    target_lstat_calls = 0
    swapped = False

    def swap_sample() -> None:
        nonlocal swapped
        sample_path.rename(backup)
        sample_path.symlink_to(outside)
        swapped = True

    def swapping_lstat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal target_lstat_calls
        result = original_lstat(path, *args, **kwargs)
        if path == sample_path:
            target_lstat_calls += 1
            if target_lstat_calls == 2 and not swapped:
                swap_sample()
        return result

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "c4.wav" and not swapped:
            swap_sample()
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(Path, "lstat", swapping_lstat)
    monkeypatch.setattr(os, "open", swapping_open)

    with pytest.raises(sound_pack.SoundPackFailure) as failure:
        sound_pack.read_verified_sample(installed, 60)

    assert swapped
    assert failure.value.error_id == "sound_pack_corrupt"


def test_agent_list_rejects_identity_swapped_to_external_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    outside_store = tmp_path / "outside-store"
    sound_pack.install_local_pack(str(TEST_PACK), store)
    sound_pack.install_local_pack(str(TEST_PACK), outside_store)
    identity_root = store / PACK_ID
    backup = tmp_path / "identity-backup"
    outside_identity = outside_store / PACK_ID
    original_open = os.open
    swapped = False

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == PACK_ID and dir_fd is not None and not swapped:
            identity_root.rename(backup)
            identity_root.symlink_to(outside_identity, target_is_directory=True)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping_open)

    with pytest.raises(sound_pack.SoundPackFailure) as failure:
        sound_pack.list_installed_packs(store)

    assert swapped
    assert failure.value.error_id == "sound_pack_corrupt"


def test_agent_repeated_list_failures_do_not_leak_identity_descriptors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    sound_pack.install_local_pack(str(TEST_PACK), store)
    identity_stat = (store / PACK_ID).stat()
    identity_inode = (identity_stat.st_dev, identity_stat.st_ino)
    original_list = storage.list_directory

    def fail_identity_list(directory_fd: int) -> tuple[storage.DirectoryEntry, ...]:
        opened = os.fstat(directory_fd)
        if (opened.st_dev, opened.st_ino) == identity_inode:
            raise OSError("injected identity list failure")
        return original_list(directory_fd)

    monkeypatch.setattr(storage, "list_directory", fail_identity_list)
    before = len(os.listdir("/dev/fd"))
    for _ in range(100):
        with pytest.raises(sound_pack.SoundPackFailure) as failure:
            sound_pack.list_installed_packs(store)
        assert failure.value.error_id == "sound_pack_corrupt"
    after = len(os.listdir("/dev/fd"))

    assert after <= before + 2


def test_agent_install_rejects_identity_replaced_after_atomic_rename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    victim_store = tmp_path / "victim-store"
    attacker_store = tmp_path / "attacker-store"
    attacker_source = tmp_path / "attacker-source"
    shutil.copytree(TEST_PACK, attacker_source)
    manifest_path = attacker_source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["attribution"] = "reviewer replacement pack"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    attacker = sound_pack.install_local_pack(str(attacker_source), attacker_store).sound_pack
    attacker_hash = attacker.pack_hash
    original_rename = storage.rename_noreplace
    old_identity = tmp_path / "old-identity"
    victim_identity = victim_store / PACK_ID
    attacker_identity = attacker_store / PACK_ID
    swapped = False

    def swapping_rename(
        source_parent_fd: int,
        source: str,
        destination_parent_fd: int,
        destination: str,
    ) -> None:
        nonlocal swapped
        original_rename(source_parent_fd, source, destination_parent_fd, destination)
        if source.startswith(".1.0.0.install-") and destination == "1.0.0" and not swapped:
            victim_identity.rename(old_identity)
            attacker_identity.rename(victim_identity)
            swapped = True

    monkeypatch.setattr(storage, "rename_noreplace", swapping_rename)

    with pytest.raises(sound_pack.SoundPackFailure) as failure:
        sound_pack.install_local_pack(str(TEST_PACK), victim_store)

    assert swapped
    assert failure.value.error_id == "sound_pack_install_failed"
    assert (old_identity / "1.0.0" / "manifest.json").is_file()
    installed_attacker = sound_pack.verify_installed_pack(victim_store, PACK_ID, "1.0.0")
    assert installed_attacker.pack_hash == attacker_hash


def test_agent_receipt_commit_rechecks_public_binding_without_descriptor_leaks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attacker_source = tmp_path / "attacker-source"
    shutil.copytree(TEST_PACK, attacker_source)
    manifest_path = attacker_source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["attribution"] = "receipt-window replacement pack"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    template_store = tmp_path / "attacker-template"
    attacker_hash = sound_pack.install_local_pack(
        str(attacker_source), template_store
    ).sound_pack.pack_hash
    template_identity = template_store / PACK_ID
    attacker_identities: dict[Path, Path] = {}
    original_write = sound_pack._write_receipt
    (tmp_path / "orphans").mkdir()

    for index in range(100):
        attacker_identity = tmp_path / "attackers" / str(index) / PACK_ID
        shutil.copytree(template_identity, attacker_identity)
        attacker_identities[tmp_path / "victims" / str(index) / PACK_ID] = attacker_identity

    def swap_before_receipt(
        root: Path,
        content: sound_pack.PackContent,
        *,
        root_fd: int | None = None,
    ) -> None:
        victim_identity = root.parent
        attacker_identity = attacker_identities[victim_identity]
        victim_identity.rename(tmp_path / "orphans" / victim_identity.parent.name)
        attacker_identity.rename(victim_identity)
        original_write(root, content, root_fd=root_fd)

    monkeypatch.setattr(sound_pack, "_write_receipt", swap_before_receipt)
    before = len(os.listdir("/dev/fd"))
    for index in range(100):
        victim_store = tmp_path / "victims" / str(index)
        with pytest.raises(sound_pack.SoundPackFailure) as failure:
            sound_pack.install_local_pack(str(TEST_PACK), victim_store)
        assert failure.value.error_id == "sound_pack_install_failed"
        installed_attacker = sound_pack.verify_installed_pack(victim_store, PACK_ID, "1.0.0")
        assert installed_attacker.pack_hash == attacker_hash
    after = len(os.listdir("/dev/fd"))

    assert after <= before + 2


def test_agent_receipt_repair_rechecks_public_binding_without_mutating_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    victim_template_store = tmp_path / "victim-template"
    victim_template = sound_pack.install_local_pack(
        str(TEST_PACK), victim_template_store
    ).sound_pack.path.parent
    (victim_template / "1.0.0" / sound_pack.RECEIPT_FILENAME).unlink()

    attacker_source = tmp_path / "attacker-source"
    shutil.copytree(TEST_PACK, attacker_source)
    manifest_path = attacker_source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["attribution"] = "receipt-repair replacement pack"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    attacker_template_store = tmp_path / "attacker-template"
    attacker_template = sound_pack.install_local_pack(
        str(attacker_source), attacker_template_store
    ).sound_pack.path.parent

    swaps: dict[Path, tuple[Path, Path]] = {}
    expected_attackers: dict[Path, tuple[set[str], dict[str, bytes], str]] = {}
    original_write = sound_pack._write_receipt

    for has_receipt in (False, True):
        mode = "verified" if has_receipt else "missing-receipt"
        for index in range(50):
            victim_identity = tmp_path / "victims" / mode / str(index) / PACK_ID
            attacker_identity = tmp_path / "attackers" / mode / str(index) / PACK_ID
            orphan_identity = tmp_path / "orphans" / mode / str(index) / PACK_ID
            shutil.copytree(victim_template, victim_identity)
            shutil.copytree(attacker_template, attacker_identity)
            if not has_receipt:
                (attacker_identity / "1.0.0" / sound_pack.RECEIPT_FILENAME).unlink()

            entries = {
                item.relative_to(attacker_identity).as_posix()
                for item in attacker_identity.rglob("*")
            }
            files = {
                item.relative_to(attacker_identity).as_posix(): item.read_bytes()
                for item in attacker_identity.rglob("*")
                if item.is_file()
            }
            expected_status = "verified" if has_receipt else "sound_pack_corrupt"
            swaps[victim_identity] = (attacker_identity, orphan_identity)
            expected_attackers[victim_identity] = (entries, files, expected_status)

    def swap_before_repair_receipt(
        root: Path,
        content: sound_pack.PackContent,
        *,
        root_fd: int | None = None,
    ) -> None:
        victim_identity = root.parent
        attacker_identity, orphan_identity = swaps[victim_identity]
        orphan_identity.parent.mkdir(parents=True)
        victim_identity.rename(orphan_identity)
        attacker_identity.rename(victim_identity)
        original_write(root, content, root_fd=root_fd)

    monkeypatch.setattr(sound_pack, "_write_receipt", swap_before_repair_receipt)
    before = len(os.listdir("/dev/fd"))
    for victim_identity, (_, orphan_identity) in swaps.items():
        victim_store = victim_identity.parent
        with pytest.raises(sound_pack.SoundPackFailure) as failure:
            sound_pack.install_local_pack(str(TEST_PACK), victim_store)
        assert failure.value.error_id == "sound_pack_install_failed"
        assert (orphan_identity / "1.0.0" / "manifest.json").is_file()

        expected_entries, expected_files, expected_status = expected_attackers[victim_identity]
        current_entries = {
            item.relative_to(victim_identity).as_posix() for item in victim_identity.rglob("*")
        }
        current_files = {
            item.relative_to(victim_identity).as_posix(): item.read_bytes()
            for item in victim_identity.rglob("*")
            if item.is_file()
        }
        assert current_entries == expected_entries
        assert current_files == expected_files
        try:
            sound_pack.verify_installed_pack(victim_store, PACK_ID, "1.0.0")
            actual_status = "verified"
        except sound_pack.SoundPackFailure as error:
            actual_status = error.error_id
        assert actual_status == expected_status
    after = len(os.listdir("/dev/fd"))

    assert after <= before + 2


def test_agent_install_never_replaces_a_racing_empty_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    destination = store / PACK_ID / "1.0.0"
    original_rename = storage.rename_noreplace
    destination_inode: tuple[int, int] | None = None

    def racing_rename(
        source_parent_fd: int,
        source: str,
        destination_parent_fd: int,
        target: str,
    ) -> None:
        nonlocal destination_inode
        if source.startswith(".1.0.0.install-") and target == "1.0.0" and destination_inode is None:
            destination.mkdir()
            created = destination.stat()
            destination_inode = (created.st_dev, created.st_ino)
        original_rename(source_parent_fd, source, destination_parent_fd, target)

    monkeypatch.setattr(storage, "rename_noreplace", racing_rename)

    with pytest.raises(sound_pack.SoundPackFailure) as failure:
        sound_pack.install_local_pack(str(TEST_PACK), store)

    current = destination.stat()
    assert failure.value.error_id == "sound_pack_conflict"
    assert destination_inode == (current.st_dev, current.st_ino)
    assert not any(destination.iterdir())


def test_agent_preserves_replacement_inserted_before_installed_inode_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    destination = store / PACK_ID / "1.0.0"
    original_install = tmp_path / "original-install"
    original_rename = storage.rename_noreplace
    keep = destination / "KEEP"

    def swapping_rename(
        source_parent_fd: int,
        source: str,
        destination_parent_fd: int,
        target: str,
    ) -> None:
        original_rename(source_parent_fd, source, destination_parent_fd, target)
        destination.rename(original_install)
        destination.mkdir()
        keep.write_text("competitor")

    monkeypatch.setattr(storage, "rename_noreplace", swapping_rename)

    with pytest.raises(sound_pack.SoundPackFailure) as failure:
        sound_pack.install_local_pack(str(TEST_PACK), store)

    assert failure.value.error_id == "sound_pack_install_failed"
    assert keep.read_text() == "competitor"
    assert (original_install / "manifest.json").is_file()


def test_agent_post_rename_failure_never_attempts_path_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    destination = store / PACK_ID / "1.0.0"
    orphan = tmp_path / "original-orphan"
    keep = destination / "KEEP"
    original_rmdir = os.rmdir
    swapped = False

    def fail_readback(
        destination: Path,
        content: sound_pack.PackContent,
        store_fd: int,
        identity_fd: int,
        identity_stat: os.stat_result,
        version_fd: int,
        installed_inode: tuple[int, int],
    ) -> sound_pack.VerifiedSoundPack:
        raise sound_pack.SoundPackFailure(
            "sound_pack_install_failed",
            "Sound Pack could not be installed.",
            "Injected anchored read-back failure.",
        )

    def swapping_rmdir(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if os.fsdecode(path) == "1.0.0" and dir_fd is not None and not swapped:
            destination.rename(orphan)
            destination.mkdir()
            keep.write_text("competitor")
            swapped = True
        original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(sound_pack, "_verify_anchored_install", fail_readback)
    monkeypatch.setattr(os, "rmdir", swapping_rmdir)

    with pytest.raises(sound_pack.SoundPackFailure):
        sound_pack.install_local_pack(str(TEST_PACK), store)

    assert not swapped
    assert (destination / "manifest.json").is_file()
    assert not keep.exists()
    assert not orphan.exists()


def test_agent_store_creation_rejects_symlink_inserted_after_missing_check(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    racing_parent = tmp_path / "racing-parent"
    store = racing_parent / "packs"
    original_mkdir = os.mkdir
    inserted = False

    def racing_mkdir(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal inserted
        if Path(os.fsdecode(path)).name == racing_parent.name and not inserted:
            racing_parent.symlink_to(outside, target_is_directory=True)
            inserted = True
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", racing_mkdir)
    monkeypatch.setattr(
        sys,
        "argv",
        ["playcli", "sound-pack", "install", str(TEST_PACK), "--store", str(store)],
    )

    result = cli.main()
    output = capsys.readouterr()

    assert inserted
    assert result == 4
    assert json.loads(output.out)["error"]["id"] == "sound_pack_install_failed"
    assert output.err
    assert not (outside / "packs").exists()


def test_agent_repeated_install_failure_keeps_only_one_pending_pack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    identity = store / PACK_ID
    original_rename = storage.rename_noreplace

    def fail_version_commit(
        source_parent_fd: int,
        source: str,
        destination_parent_fd: int,
        destination: str,
    ) -> None:
        if destination == "1.0.0":
            raise OSError("injected version commit failure")
        original_rename(source_parent_fd, source, destination_parent_fd, destination)

    monkeypatch.setattr(storage, "rename_noreplace", fail_version_commit)

    with pytest.raises(sound_pack.SoundPackFailure) as failure:
        sound_pack.install_local_pack(str(TEST_PACK), store)
    assert failure.value.error_id == "sound_pack_install_failed"
    entries_after_first_failure = set(os.listdir(identity))

    for _ in range(9):
        with pytest.raises(sound_pack.SoundPackFailure) as failure:
            sound_pack.install_local_pack(str(TEST_PACK), store)
        assert failure.value.error_id == "sound_pack_install_failed"
        assert set(os.listdir(identity)) == entries_after_first_failure

    orphan_directories = [entry for entry in identity.iterdir() if entry.is_dir()]
    assert len(orphan_directories) == 1
    assert (orphan_directories[0] / "manifest.json").is_file()
    listed = subprocess.run(
        [str(PLAYCLI), "sound-pack", "list", "--store", str(store)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert listed.returncode == 0
    assert json.loads(listed.stdout)["sound_packs"] == []


def test_agent_repeated_receipt_failure_keeps_only_one_pending_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    destination = store / PACK_ID / "1.0.0"
    original_rename = storage.rename_noreplace

    def fail_receipt_commit(
        source_parent_fd: int,
        source: str,
        destination_parent_fd: int,
        target: str,
    ) -> None:
        if target == sound_pack.RECEIPT_FILENAME:
            raise OSError("injected receipt commit failure")
        original_rename(source_parent_fd, source, destination_parent_fd, target)

    monkeypatch.setattr(storage, "rename_noreplace", fail_receipt_commit)

    with pytest.raises(sound_pack.SoundPackFailure) as failure:
        sound_pack.install_local_pack(str(TEST_PACK), store)
    assert failure.value.error_id == "sound_pack_install_failed"
    identity_entries = set(os.listdir(destination.parent))
    destination_entries = set(os.listdir(destination))

    for _ in range(9):
        with pytest.raises(sound_pack.SoundPackFailure) as failure:
            sound_pack.install_local_pack(str(TEST_PACK), store)
        assert failure.value.error_id == "sound_pack_install_failed"
        assert set(os.listdir(destination.parent)) == identity_entries
        assert set(os.listdir(destination)) == destination_entries

    assert destination.is_dir()
    assert not (destination / sound_pack.RECEIPT_FILENAME).exists()
    listed = subprocess.run(
        [str(PLAYCLI), "sound-pack", "list", "--store", str(store)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert listed.returncode == 0
    assert json.loads(listed.stdout)["sound_packs"][0]["status"] == "corrupt"
    verified = subprocess.run(
        [
            str(PLAYCLI),
            "sound-pack",
            "verify",
            PACK_ID,
            "--version",
            "1.0.0",
            "--store",
            str(store),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert verified.returncode == 4
    assert json.loads(verified.stdout)["error"]["id"] == "sound_pack_corrupt"
