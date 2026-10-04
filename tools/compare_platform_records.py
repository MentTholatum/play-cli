#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from platform_record_contract import (
    COMPARE_OPERATION as OPERATION,
)
from platform_record_contract import (
    SCHEMA_VERSION,
    RecordContractError,
    validate_record,
)


class ComparisonFailure(Exception):
    pass


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ComparisonFailure(f"{label} is not a JSON object with string keys.")
    return value


def _load_record(path: Path, label: str) -> dict[str, Any]:
    try:
        record = _object(json.loads(path.read_text()), label)
    except (OSError, json.JSONDecodeError) as error:
        raise ComparisonFailure(f"{label} could not be read.") from error
    try:
        validate_record(record)
    except RecordContractError as error:
        raise ComparisonFailure(f"{label} is invalid: {error}") from error
    return record


def _compare(left_path: Path, right_path: Path) -> dict[str, object]:
    left = _load_record(left_path, "First record")
    right = _load_record(right_path, "Second record")
    left_wheel = _object(left["wheel"], "First record wheel")
    right_wheel = _object(right["wheel"], "Second record wheel")
    if left_wheel["sha256"] != right_wheel["sha256"]:
        raise ComparisonFailure("Platform records do not bind the same wheel.")
    if left["contract"] != right["contract"]:
        raise ComparisonFailure("Platform records do not have the same semantic contract.")
    if left["verifier"] != right["verifier"]:
        raise ComparisonFailure("Platform records were not produced by the same verifier.")
    left_platform = _object(left["platform"], "First record platform")
    right_platform = _object(right["platform"], "Second record platform")
    if {left_platform["id"], right_platform["id"]} != {"darwin", "linux"}:
        raise ComparisonFailure("Comparison requires one macOS and one Linux record.")
    encoded_contract = json.dumps(left["contract"], sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": OPERATION,
        "status": "ok",
        "wheel_sha256": left_wheel["sha256"],
        "contract_sha256": hashlib.sha256(encoded_contract).hexdigest(),
        "platforms": [left_platform["id"], right_platform["id"]],
    }


def _write_json(value: dict[str, object]) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    arguments = parser.parse_args()
    try:
        response = _compare(arguments.first, arguments.second)
    except (ComparisonFailure, KeyError, TypeError, ValueError) as error:
        _write_json(
            {
                "schema_version": SCHEMA_VERSION,
                "operation": OPERATION,
                "status": "error",
                "error": {"id": "platform_records_invalid", "message": str(error)},
            }
        )
        print(f"Platform record comparison failed: {error}", file=sys.stderr)
        return 1
    _write_json(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
