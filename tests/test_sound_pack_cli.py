from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.agent_contract import stable_response as _stable_response

ROOT = Path(__file__).parents[1]
PLAYCLI = Path(sys.executable).with_name("playcli")
TEST_PACK = ROOT / "tests/fixtures/test-piano-pack"
PACK_ID = "play-cli-test-piano"
PACK_VERSION = "1.0.0"


def _run(*arguments: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(PLAYCLI), *(str(argument) for argument in arguments)],
        capture_output=True,
        check=False,
        text=True,
    )


def _copy_pack(tmp_path: Path, name: str = "pack-source") -> Path:
    source = tmp_path / name
    shutil.copytree(TEST_PACK, source)
    return source


def _install(source: Path, store: Path) -> subprocess.CompletedProcess[str]:
    return _run("sound-pack", "install", source, "--store", store)


def _selector_arguments(store: Path) -> list[str | Path]:
    return [
        "--sound-pack",
        PACK_ID,
        "--sound-pack-version",
        PACK_VERSION,
        "--sound-pack-store",
        store,
    ]


def _assert_pack_summary(summary: dict[str, Any], installed_path: Path) -> str:
    assert summary["id"] == PACK_ID
    assert summary["version"] == PACK_VERSION
    assert summary["status"] == "verified"
    assert summary["path"] == str(installed_path.resolve())
    pack_hash = summary["pack_hash"]
    assert isinstance(pack_hash, str)
    assert re.fullmatch(r"[0-9a-f]{64}", pack_hash)
    return pack_hash


def test_agent_can_install_list_verify_and_idempotently_reinstall_a_pack(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store"
    installed_path = store / PACK_ID / PACK_VERSION

    installed = _install(TEST_PACK, store)

    assert installed.returncode == 0
    installed_response = json.loads(installed.stdout)
    assert installed_response["schema_version"] == "1.6"
    assert installed_response["operation"] == "sound-pack.install"
    assert installed_response["status"] == "ok"
    pack_hash = _assert_pack_summary(installed_response["sound_pack"], installed_path)
    installed_license = (installed_path / "LICENSE").read_text()
    assert "Statement of Purpose" in installed_license
    assert "4. Limitations and Disclaimers." in installed_license
    receipt = json.loads((installed_path / ".playcli-receipt.json").read_text())
    assert receipt["source"] == {
        "uri": "urn:play-cli:test-piano-generator",
        "revision": {"type": "version", "value": "1.1"},
    }
    assert receipt["license"] == {
        "id": "CC0-1.0",
        "file": "LICENSE",
        "sha256": "a2010f343487d3f7618affe54f789f5487602331c0a8d03f49e9a7c547cf0499",
        "complete_text": True,
    }
    assert installed_response["installation"] == {
        "result": "installed",
        "receipt": str((installed_path / ".playcli-receipt.json").resolve()),
    }
    assert installed.stderr
    installed_directories = [entry.name for entry in (store / PACK_ID).iterdir() if entry.is_dir()]
    assert installed_directories == [PACK_VERSION]

    staging = store / PACK_ID / ".1.0.0.install-in-progress"
    staging.mkdir()

    listed = _run("sound-pack", "list", "--store", store)
    assert listed.returncode == 0
    listed_response = _stable_response(listed)
    assert listed_response == {
        "schema_version": "1.6",
        "operation": "sound-pack.list",
        "status": "ok",
        "sound_packs": [installed_response["sound_pack"]],
    }

    verified = _run(
        "sound-pack",
        "verify",
        PACK_ID,
        "--version",
        PACK_VERSION,
        "--store",
        store,
    )
    assert verified.returncode == 0
    assert _stable_response(verified) == {
        "schema_version": "1.6",
        "operation": "sound-pack.verify",
        "status": "ok",
        "sound_pack": installed_response["sound_pack"],
    }

    repeated = _install(TEST_PACK, store)
    assert repeated.returncode == 0
    repeated_response = json.loads(repeated.stdout)
    assert repeated_response["installation"]["result"] == "already_installed"
    assert repeated_response["sound_pack"]["pack_hash"] == pack_hash


def test_agent_receives_sound_packs_in_stable_identity_version_order(tmp_path: Path) -> None:
    store = tmp_path / "store"
    newer = _copy_pack(tmp_path, "newer")
    manifest_path = newer / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = "1.0.1"
    manifest["source"]["revision"]["value"] = "1.1.1"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    assert _install(newer, store).returncode == 0
    assert _install(TEST_PACK, store).returncode == 0

    listed = _run("sound-pack", "list", "--store", store)
    assert listed.returncode == 0
    entries = json.loads(listed.stdout)["sound_packs"]
    assert [(entry["id"], entry["version"]) for entry in entries] == [
        (PACK_ID, "1.0.0"),
        (PACK_ID, "1.0.1"),
    ]


def test_agent_concurrent_first_install_and_precreated_identity_are_idempotent(
    tmp_path: Path,
) -> None:
    command_prefix = [str(PLAYCLI), "sound-pack", "install", str(TEST_PACK), "--store"]
    for index in range(40):
        store = tmp_path / "parallel" / str(index)
        processes = [
            subprocess.Popen(
                [*command_prefix, str(store)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        completed = [process.communicate() for process in processes]
        assert [process.returncode for process in processes] == [0, 0], completed
        assert (
            _run(
                "sound-pack",
                "verify",
                PACK_ID,
                "--version",
                PACK_VERSION,
                "--store",
                store,
            ).returncode
            == 0
        )

    for index in range(30):
        store = tmp_path / "precreated" / str(index)
        (store / PACK_ID).mkdir(parents=True)
        assert _install(TEST_PACK, store).returncode == 0
        assert (
            _run(
                "sound-pack",
                "verify",
                PACK_ID,
                "--version",
                PACK_VERSION,
                "--store",
                store,
            ).returncode
            == 0
        )


def test_agent_cannot_replace_an_installed_identity_version(tmp_path: Path) -> None:
    store = tmp_path / "store"
    conflicting = _copy_pack(tmp_path)
    (conflicting / "SOURCE.md").write_text("different auditable content\n")

    assert _install(TEST_PACK, store).returncode == 0
    conflict = _install(conflicting, store)

    assert conflict.returncode == 4
    assert json.loads(conflict.stdout)["error"] == {
        "id": "sound_pack_conflict",
        "message": "Sound Pack version conflicts with installed content.",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", ""),
        ("id", "."),
        ("id", ".."),
        ("id", "/absolute"),
        ("id", "nested/pack"),
        ("id", "nested\\pack"),
        ("version", ""),
        ("version", "."),
        ("version", ".."),
        ("version", "/absolute"),
        ("version", "nested/version"),
        ("version", "nested\\version"),
    ],
)
def test_agent_rejects_unsafe_pack_identity(field: str, value: str, tmp_path: Path) -> None:
    source = _copy_pack(tmp_path)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    result = _install(source, tmp_path / "store")

    assert result.returncode == 4
    assert json.loads(result.stdout)["error"]["id"] == "sound_pack_invalid"


def test_agent_cannot_install_through_a_store_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    store = tmp_path / "store"
    store.symlink_to(outside, target_is_directory=True)

    result = _install(TEST_PACK, store)

    assert result.returncode == 4
    assert json.loads(result.stdout)["error"]["id"] == "sound_pack_install_failed"
    assert not any(outside.iterdir())


@pytest.mark.parametrize(
    ("operation", "error_id"),
    [
        ("install", "sound_pack_install_failed"),
        ("list", "sound_pack_corrupt"),
        ("verify", "sound_pack_corrupt"),
    ],
)
def test_agent_cannot_use_a_store_through_an_ancestor_symlink(
    operation: str, error_id: str, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    if operation != "install":
        assert _install(TEST_PACK, outside / "store").returncode == 0
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    if operation == "install":
        result = _install(TEST_PACK, linked_parent / "store")
    elif operation == "list":
        result = _run("sound-pack", "list", "--store", linked_parent / "store")
    else:
        result = _run(
            "sound-pack",
            "verify",
            PACK_ID,
            "--version",
            PACK_VERSION,
            "--store",
            linked_parent / "store",
        )

    assert result.returncode == 4
    assert json.loads(result.stdout)["error"]["id"] == error_id
    if operation == "install":
        assert not (outside / "store").exists()


def test_agent_list_rejects_a_symlink_ancestor_when_the_store_leaf_is_missing(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    result = _run("sound-pack", "list", "--store", linked_parent / "missing-store")

    assert result.returncode == 4
    response = json.loads(result.stdout)
    assert response["schema_version"] == "1.6"
    assert response["operation"] == "sound-pack.list"
    assert response["error"]["id"] == "sound_pack_corrupt"
    assert result.stderr


def test_agent_list_returns_empty_for_a_genuinely_missing_store(tmp_path: Path) -> None:
    result = _run("sound-pack", "list", "--store", tmp_path / "missing-store")

    assert result.returncode == 0
    assert _stable_response(result) == {
        "schema_version": "1.6",
        "operation": "sound-pack.list",
        "status": "ok",
        "sound_packs": [],
    }


def test_agent_list_permission_failure_is_a_single_machine_error(tmp_path: Path) -> None:
    store = tmp_path / "store"
    identity_root = store / PACK_ID
    assert _install(TEST_PACK, store).returncode == 0
    identity_root.chmod(0)
    try:
        result = _run("sound-pack", "list", "--store", store)
    finally:
        identity_root.chmod(0o700)

    assert result.returncode == 4
    response = json.loads(result.stdout)
    assert response["schema_version"] == "1.6"
    assert response["operation"] == "sound-pack.list"
    assert response["error"]["id"] == "sound_pack_corrupt"
    assert len(result.stdout.splitlines()) == 1
    assert result.stderr
    assert "traceback" not in result.stderr.lower()


def test_agent_receives_an_explicit_local_install_action_for_a_missing_pack(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store"
    bundle = tmp_path / "bundle"

    missing_action = {
        "id": "install_sound_pack",
        "argv_template": [
            "playcli",
            "sound-pack",
            "install",
            {"required_input": "local_source"},
            "--store",
            str(store.resolve()),
        ],
        "required_inputs": {
            "local_source": {
                "type": "local_path",
                "description": "Explicit local Sound Pack directory.",
            }
        },
    }
    verified = _run(
        "sound-pack",
        "verify",
        PACK_ID,
        "--version",
        PACK_VERSION,
        "--store",
        store,
    )
    assert verified.returncode == 4
    verified_response = json.loads(verified.stdout)
    assert verified_response["error"]["id"] == "sound_pack_missing"
    assert verified_response["action"] == missing_action

    result = _run(
        "render",
        ROOT / "tests/fixtures/renderable-c4.musicxml",
        *_selector_arguments(store),
        "--output",
        bundle,
    )

    assert result.returncode == 4
    response = json.loads(result.stdout)
    assert response["schema_version"] == "1.6"
    assert response["operation"] == "render"
    assert response["error"] == {
        "id": "sound_pack_missing",
        "message": "Required Sound Pack is not installed.",
    }
    assert response["action"] == missing_action
    assert not bundle.exists()


@pytest.mark.parametrize(
    "tampered_path",
    ["manifest.json", "LICENSE", "samples/c4.wav"],
)
def test_agent_cannot_verify_or_render_a_tampered_installed_pack(
    tampered_path: str, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    installed_path = store / PACK_ID / PACK_VERSION
    assert _install(TEST_PACK, store).returncode == 0
    target = installed_path / tampered_path
    target.write_bytes(target.read_bytes() + b"\n")

    verified = _run(
        "sound-pack",
        "verify",
        PACK_ID,
        "--version",
        PACK_VERSION,
        "--store",
        store,
    )
    assert verified.returncode == 4
    assert json.loads(verified.stdout)["error"]["id"] == "sound_pack_corrupt"

    bundle = tmp_path / "bundle"
    rendered = _run(
        "render",
        ROOT / "tests/fixtures/renderable-c4.musicxml",
        *_selector_arguments(store),
        "--output",
        bundle,
    )
    assert rendered.returncode == 4
    assert json.loads(rendered.stdout)["error"]["id"] == "sound_pack_corrupt"
    assert not bundle.exists()


@pytest.mark.parametrize("mutation", ["added", "deleted"])
def test_agent_detects_installed_inventory_changes(mutation: str, tmp_path: Path) -> None:
    store = tmp_path / "store"
    installed_path = store / PACK_ID / PACK_VERSION
    assert _install(TEST_PACK, store).returncode == 0
    if mutation == "added":
        (installed_path / "unexpected.txt").write_text("unexpected\n")
    else:
        os.unlink(installed_path / "SOURCE.md")

    result = _run(
        "sound-pack",
        "verify",
        PACK_ID,
        "--version",
        PACK_VERSION,
        "--store",
        store,
    )

    assert result.returncode == 4
    assert json.loads(result.stdout)["error"]["id"] == "sound_pack_corrupt"


def test_agent_can_detect_and_repair_only_a_missing_install_receipt(tmp_path: Path) -> None:
    store = tmp_path / "store"
    installed_path = store / PACK_ID / PACK_VERSION
    installed = _install(TEST_PACK, store)
    assert installed.returncode == 0
    pack_hash = json.loads(installed.stdout)["sound_pack"]["pack_hash"]
    committed_entries = set(os.listdir(installed_path))
    (installed_path / ".playcli-receipt.json").unlink()

    listed = _run("sound-pack", "list", "--store", store)
    assert listed.returncode == 0
    assert json.loads(listed.stdout)["sound_packs"] == [
        {
            "id": PACK_ID,
            "version": PACK_VERSION,
            "pack_hash": None,
            "status": "corrupt",
            "path": str(installed_path.resolve()),
        }
    ]
    missing_receipt = _run(
        "sound-pack",
        "verify",
        PACK_ID,
        "--version",
        PACK_VERSION,
        "--store",
        store,
    )
    assert missing_receipt.returncode == 4
    assert json.loads(missing_receipt.stdout)["error"]["id"] == "sound_pack_corrupt"

    repaired = _install(TEST_PACK, store)
    assert repaired.returncode == 0
    repaired_response = json.loads(repaired.stdout)
    assert repaired_response["installation"]["result"] == "receipt_repaired"
    assert repaired_response["sound_pack"]["pack_hash"] == pack_hash
    assert set(os.listdir(installed_path)) == committed_entries
    verified = _run(
        "sound-pack",
        "verify",
        PACK_ID,
        "--version",
        PACK_VERSION,
        "--store",
        store,
    )
    assert verified.returncode == 0


def test_agent_rejects_an_unauditable_manifest_combination(tmp_path: Path) -> None:
    source = _copy_pack(tmp_path)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source"] = {
        "uri": "x",
        "revision": {"type": "version", "value": "latest"},
    }
    manifest["license"] = {
        "id": "CC0-1.0",
        "file": "samples/c4.wav",
        "sha256": manifest["samples"]["60"]["sha256"],
        "complete_text": True,
    }
    manifest["attribution"] = "x"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    result = _install(source, tmp_path / "store")

    assert result.returncode == 4
    assert json.loads(result.stdout)["error"]["id"] == "sound_pack_invalid"


@pytest.mark.parametrize(
    ("uri", "revision"),
    [
        ("x", "1.1"),
        ("urn:play-cli:test-piano-generator", "current"),
        ("urn:play-cli:test-piano-generator", "stable"),
    ],
)
def test_agent_rejects_unpinned_source_identifiers(uri: str, revision: str, tmp_path: Path) -> None:
    source = _copy_pack(tmp_path)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source"] = {
        "uri": uri,
        "revision": {"type": "version", "value": revision},
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    result = _install(source, tmp_path / "store")

    assert result.returncode == 4
    assert json.loads(result.stdout)["error"]["id"] == "sound_pack_invalid"


@pytest.mark.parametrize(
    "case",
    [
        "manifest_schema",
        "source",
        "source_revision",
        "license_file",
        "license_hash",
        "license_complete",
        "license_sample",
        "attribution",
        "modifications",
        "sample_hash",
        "sample_format",
        "unicode_midi",
    ],
)
def test_agent_rejects_incomplete_or_invalid_pack_metadata(case: str, tmp_path: Path) -> None:
    source = _copy_pack(tmp_path)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if case == "manifest_schema":
        manifest["schema_version"] = "unsupported"
    elif case in {"source", "attribution"}:
        manifest[case] = ""
    elif case == "source_revision":
        manifest["source"]["revision"]["value"] = "HEAD"
    elif case == "license_file":
        (source / "LICENSE").write_text("")
    elif case == "license_hash":
        manifest["license"]["sha256"] = "0" * 64
    elif case == "license_complete":
        manifest["license"]["complete_text"] = False
    elif case == "license_sample":
        manifest["license"]["file"] = "samples/c4.wav"
        manifest["license"]["sha256"] = manifest["samples"]["60"]["sha256"]
    elif case == "modifications":
        manifest["modifications"] = "none"
    elif case == "sample_hash":
        manifest["samples"]["60"]["sha256"] = "0" * 64
    elif case == "sample_format":
        manifest["samples"]["60"]["format"] = "MP3"
    elif case == "unicode_midi":
        manifest["samples"]["²"] = manifest["samples"].pop("60")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    result = _install(source, tmp_path / "store")

    assert result.returncode == 4
    assert json.loads(result.stdout)["error"]["id"] == "sound_pack_invalid"


@pytest.mark.parametrize("case", ["absolute", "traversal", "symlink", "non_regular"])
def test_agent_rejects_unsafe_pack_files(case: str, tmp_path: Path) -> None:
    source = _copy_pack(tmp_path)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    sample = source / "samples/c4.wav"
    if case == "absolute":
        manifest["samples"]["60"]["path"] = str(sample.resolve())
    elif case == "traversal":
        manifest["samples"]["60"]["path"] = "../outside.wav"
    elif case == "symlink":
        outside = tmp_path / "outside.wav"
        outside.write_bytes(sample.read_bytes())
        sample.unlink()
        sample.symlink_to(outside)
    else:
        sample.unlink()
        sample.mkdir()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    result = _install(source, tmp_path / "store")

    assert result.returncode == 4
    assert json.loads(result.stdout)["error"]["id"] == "sound_pack_invalid"


def test_agent_cannot_install_from_a_url(tmp_path: Path) -> None:
    result = _run(
        "sound-pack",
        "install",
        "https://example.invalid/piano-pack",
        "--store",
        tmp_path / "store",
    )

    assert result.returncode == 4
    assert json.loads(result.stdout)["error"]["id"] == "sound_pack_source_not_local"


def test_agent_cannot_render_from_an_unmanaged_source_pack(tmp_path: Path) -> None:
    result = _run(
        "render",
        ROOT / "tests/fixtures/renderable-c4.musicxml",
        "--sound-pack",
        TEST_PACK,
        "--output",
        tmp_path / "bundle",
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["id"] == "invalid_invocation"


def test_agent_production_code_has_no_network_client_path() -> None:
    source = "\n".join(path.read_text() for path in sorted((ROOT / "src").rglob("*.py")))
    forbidden = [
        "import socket",
        "from socket",
        "import urllib",
        "from urllib",
        "import requests",
        "import httpx",
        "urlopen(",
        "http://",
        "https://",
    ]
    assert all(token not in source for token in forbidden)
