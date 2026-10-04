from __future__ import annotations

import json
import math
import subprocess
from typing import Any


def parse_single_json_response(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert len(result.stdout.splitlines()) == 1
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def assert_machine_response(
    result: subprocess.CompletedProcess[str],
    *,
    operation: str,
    status: str,
) -> dict[str, Any]:
    response = parse_single_json_response(result)
    _assert_machine_payload(response, operation=operation, status=status)
    return response


def _assert_machine_payload(
    response: dict[str, Any],
    *,
    operation: str,
    status: str,
) -> None:
    assert response["schema_version"] == "1.6"
    assert response["operation"] == operation
    assert response["status"] == status
    assert response["provenance"]["schema_version"] == "1.6"
    assert response["provenance"]["cli"] == {"name": "play-cli", "version": "0.10.0"}
    assert response["provenance"]["execution"] == {
        "location": "local",
        "network": "disabled",
        "telemetry": "disabled",
    }
    duration = response["operation_timing"]["duration_ms"]
    assert isinstance(duration, (int, float)) and not isinstance(duration, bool)
    assert math.isfinite(duration) and duration >= 0
    assert response["operation_timing"]["scope"] in {
        "through_response_assembly",
        "through_manifest_payload_assembly",
    }


def stable_response(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    response = parse_single_json_response(result)
    if response.get("operation") is None:
        return response
    _assert_machine_payload(
        response,
        operation=response["operation"],
        status=response["status"],
    )
    stable = dict(response)
    stable.pop("provenance")
    stable.pop("operation_timing")
    return stable
