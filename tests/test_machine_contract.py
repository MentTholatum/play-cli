from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.agent_contract import assert_machine_response as _assert_contract

ROOT = Path(__file__).parents[1]
PLAYCLI = Path(sys.executable).with_name("playcli")
PACK = ROOT / "tests/fixtures/performance-test-piano-pack"
PACK_ID = "play-cli-performance-test-piano"
PACK_VERSION = "1.0.0"


def _run(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(PLAYCLI), *arguments],
        capture_output=True,
        check=False,
        text=True,
        env=env,
    )


def _install_pack(tmp_path: Path) -> Path:
    store = tmp_path / "packs"
    installed = _run("sound-pack", "install", str(PACK), "--store", str(store))
    assert installed.returncode == 0, installed.stderr
    _assert_contract(installed, operation="sound-pack.install", status="ok")
    return store


def _render(score: Path, store: Path, output: Path, *, env: dict[str, str] | None = None):
    return _run(
        "render",
        str(score),
        "--sound-pack",
        PACK_ID,
        "--sound-pack-version",
        PACK_VERSION,
        "--sound-pack-store",
        str(store),
        "--output",
        str(output),
        env=env,
    )


def test_agent_can_negotiate_the_default_and_explicit_supported_schema() -> None:
    score = ROOT / "tests/fixtures/minimal.musicxml"
    default = _run("validate", str(score))
    explicit = _run("validate", str(score), "--schema-version", "1.6")

    assert default.returncode == explicit.returncode == 0
    default_response = _assert_contract(default, operation="validate", status="ok")
    explicit_response = _assert_contract(explicit, operation="validate", status="ok")
    assert default_response["events"] == explicit_response["events"]
    assert default_response["provenance"]["score"] == {
        "format": "musicxml",
        "sha256": hashlib.sha256(score.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize("encoding", ["X-UNKNOWN", "UTF-32"])
def test_agent_receives_json_when_score_encoding_is_unsupported(
    tmp_path: Path, encoding: str,
) -> None:
    score = tmp_path / "score.musicxml"
    score.write_text(
        (ROOT / "tests/fixtures/minimal.musicxml")
        .read_text()
        .replace('encoding="UTF-8"', f'encoding="{encoding}"')
    )
    output = tmp_path / "result"
    store = tmp_path / "missing-store"
    for result, operation in (
        (_run("validate", str(score)), "validate"),
        (_render(score, store, output), "render"),
    ):
        assert result.returncode == 2, result.stderr
        response = _assert_contract(result, operation=operation, status="error")
        assert response["error"]["id"] == "musicxml_invalid"
        assert response["provenance"]["score"]["sha256"] == hashlib.sha256(
            score.read_bytes()
        ).hexdigest()
        assert result.stderr and "Traceback" not in result.stderr
    assert not output.exists()
    assert not store.exists()


@pytest.mark.parametrize("tempo", ["1e999", "1e-999"])
def test_agent_rejects_tempo_outside_the_json_numeric_range(
    tmp_path: Path, tempo: str,
) -> None:
    score = tmp_path / "score.musicxml"
    score.write_text(
        (ROOT / "tests/fixtures/renderable-c4.musicxml")
        .read_text()
        .replace("120", tempo)
    )
    store = tmp_path / "missing-store"
    output = tmp_path / "result"
    for result, operation in (
        (_run("validate", str(score)), "validate"),
        (_render(score, store, output), "render"),
    ):
        assert result.returncode == 2, result.stderr
        response = _assert_contract(result, operation=operation, status="error")
        assert response["error"]["id"] == "musicxml_invalid"
        assert "Infinity" not in result.stdout and "NaN" not in result.stdout
        assert result.stderr and "Traceback" not in result.stderr
    assert not output.exists()
    assert not store.exists()


def test_agent_rejects_an_unsupported_schema_before_any_operation_side_effect(
    tmp_path: Path,
) -> None:
    output = tmp_path / "must-not-exist"
    result = _run(
        "--schema-version",
        "9.9",
        "render",
        str(tmp_path / "missing-score.musicxml"),
        "--sound-pack",
        PACK_ID,
        "--sound-pack-version",
        PACK_VERSION,
        "--sound-pack-store",
        str(tmp_path / "missing-store"),
        "--output",
        str(output),
    )

    assert result.returncode == 2
    response = _assert_contract(result, operation="render", status="error")
    assert response["error"] == {
        "id": "schema_version_unsupported",
        "message": "Requested response schema version is unsupported.",
    }
    assert response["schema"] == {
        "requested": "9.9",
        "supported": ["1.6"],
        "default": "1.6",
    }
    assert result.stderr
    assert not output.exists()
    assert not (tmp_path / "missing-store").exists()


def test_agent_receives_a_uniform_contract_for_every_valid_operation(tmp_path: Path) -> None:
    store = _install_pack(tmp_path)
    invalid_score = ROOT / "tests/fixtures/invalid-malformed.musicxml"
    invalid_result = _run("validate", str(invalid_score))
    invalid_render = _render(invalid_score, store, tmp_path / "invalid-render")
    operations = [
        (_run("validate", str(ROOT / "tests/fixtures/minimal.musicxml")), "validate", "ok"),
        (_run("sound-pack", "list", "--store", str(store)), "sound-pack.list", "ok"),
        (
            _run(
                "sound-pack",
                "verify",
                PACK_ID,
                "--version",
                PACK_VERSION,
                "--store",
                str(store),
            ),
            "sound-pack.verify",
            "ok",
        ),
        (invalid_result, "validate", "error"),
        (invalid_render, "render", "error"),
        (
            _render(
                ROOT / "tests/fixtures/minimal.musicxml",
                store,
                tmp_path / "unavailable-renderer",
            ),
            "render",
            "ok",
        ),
    ]

    for result, operation, status in operations:
        assert result.returncode == (0 if status == "ok" else 2), result.stderr
        _assert_contract(result, operation=operation, status=status)
    invalid_response = _assert_contract(invalid_result, operation="validate", status="error")
    assert invalid_response["provenance"]["score"] == {
        "format": "musicxml",
        "sha256": hashlib.sha256(invalid_score.read_bytes()).hexdigest(),
    }
    invalid_render_response = _assert_contract(
        invalid_render,
        operation="render",
        status="error",
    )
    assert invalid_render_response["provenance"]["score"] == invalid_response["provenance"]["score"]
    assert not (tmp_path / "invalid-render").exists()


def test_agent_receives_a_stable_error_for_an_unwritable_result_destination(
    tmp_path: Path,
) -> None:
    store = _install_pack(tmp_path)
    not_a_directory = tmp_path / "not-a-directory"
    not_a_directory.write_text("blocks directory traversal\n")
    result = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        not_a_directory / "result",
    )

    assert result.returncode == 3
    response = _assert_contract(result, operation="render", status="error")
    assert response["error"]["id"] == "result_bundle_failed"
    assert not (not_a_directory / "result").exists()


def test_agent_receives_stable_renderer_and_corrupt_pack_failures(tmp_path: Path) -> None:
    store = _install_pack(tmp_path)
    unavailable = _run(
        "render",
        str(ROOT / "tests/fixtures/minimal.musicxml"),
        "--sound-pack",
        PACK_ID,
        "--sound-pack-version",
        PACK_VERSION,
        "--sound-pack-store",
        str(store),
        "--output",
        str(tmp_path / "unavailable"),
        "--renderer",
        "not-installed",
    )
    assert unavailable.returncode == 3
    unavailable_response = _assert_contract(unavailable, operation="render", status="error")
    assert unavailable_response["error"]["id"] == "renderer_unavailable"

    installed_sample = store / PACK_ID / PACK_VERSION / "samples/midi-60.wav"
    installed_sample.write_bytes(installed_sample.read_bytes() + b"tampered")
    corrupt = _run(
        "sound-pack",
        "verify",
        PACK_ID,
        "--version",
        PACK_VERSION,
        "--store",
        str(store),
    )
    assert corrupt.returncode == 4
    corrupt_response = _assert_contract(
        corrupt,
        operation="sound-pack.verify",
        status="error",
    )
    assert corrupt_response["error"]["id"] == "sound_pack_corrupt"


def test_agent_records_reproducible_schedule_and_deterministic_provenance(
    tmp_path: Path,
) -> None:
    store = _install_pack(tmp_path)
    score = ROOT / "tests/fixtures/supported-monophonic.musicxml"
    first = _render(score, store, tmp_path / "first")
    second = _render(score, store, tmp_path / "second")

    assert first.returncode == second.returncode == 0
    first_response = _assert_contract(first, operation="render", status="ok")
    second_response = _assert_contract(second, operation="render", status="ok")
    assert (
        json.loads((tmp_path / "first/schedule.json").read_text())["events"]
        == json.loads((tmp_path / "second/schedule.json").read_text())["events"]
    )
    assert first_response["provenance"] == second_response["provenance"]
    assert first_response["provenance"]["score"] == {
        "format": "musicxml",
        "sha256": first_response["result_bundle"]["artifacts"]["score"]["sha256"],
    }
    assert first_response["provenance"]["renderer"] == first_response["renderer"]
    assert first_response["provenance"]["sound_pack"] == {
        key: first_response["sound_pack"][key] for key in ("id", "version", "pack_hash")
    }


def test_agent_validation_and_rendering_make_no_runtime_network_attempt(tmp_path: Path) -> None:
    store = _install_pack(tmp_path)
    hook_directory = tmp_path / "network-guard"
    hook_directory.mkdir()
    marker = tmp_path / "network-attempted"
    loaded_marker = tmp_path / "network-guard-loaded"
    (hook_directory / "sitecustomize.py").write_text(
        """
import os
import socket

marker = os.environ["PLAYCLI_NETWORK_MARKER"]
with open(os.environ["PLAYCLI_NETWORK_GUARD_LOADED"], "a", encoding="utf-8") as output:
    output.write("loaded\\n")

def blocked(*args, **kwargs):
    with open(marker, "a", encoding="utf-8") as output:
        output.write("network attempted\\n")
    raise AssertionError("PLAY CLI attempted network access")

class GuardedSocket(socket.socket):
    def connect(self, *args, **kwargs):
        return blocked(*args, **kwargs)

    def connect_ex(self, *args, **kwargs):
        blocked(*args, **kwargs)
        return 1

    def sendto(self, *args, **kwargs):
        return blocked(*args, **kwargs)

socket.socket = GuardedSocket
socket.create_connection = blocked
socket.getaddrinfo = blocked
""".lstrip()
    )
    environment = dict(os.environ)
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(hook_directory)
        if not existing_python_path
        else os.pathsep.join((str(hook_directory), existing_python_path))
    )
    environment["PLAYCLI_NETWORK_MARKER"] = str(marker)
    environment["PLAYCLI_NETWORK_GUARD_LOADED"] = str(loaded_marker)

    validated = _run(
        "validate",
        str(ROOT / "tests/fixtures/minimal.musicxml"),
        env=environment,
    )
    rendered = _render(
        ROOT / "tests/fixtures/minimal.musicxml",
        store,
        tmp_path / "offline-result",
        env=environment,
    )

    assert validated.returncode == rendered.returncode == 0
    _assert_contract(validated, operation="validate", status="ok")
    _assert_contract(rendered, operation="render", status="ok")
    assert loaded_marker.read_text().splitlines() == ["loaded", "loaded"]
    assert not marker.exists()


def test_agent_runtime_package_declares_no_network_dependency_or_client_path() -> None:
    project = json.loads(
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json,tomllib,pathlib;"
                    "print(json.dumps(tomllib.loads(pathlib.Path('pyproject.toml').read_text())"
                    "['project']['dependencies']))"
                ),
            ],
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    )
    source = "\n".join(path.read_text() for path in sorted((ROOT / "src").rglob("*.py")))
    assert project == []
    assert all(
        token not in source
        for token in (
            "import socket",
            "from socket",
            "import urllib",
            "from urllib",
            "import requests",
            "import httpx",
            "import http.client",
            "import ftplib",
            "urlopen(",
        )
    )
