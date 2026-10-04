"""Test-only trust binding; production qualification never imports this module."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/qualification-baseline"
AUTHORITY_SHA256 = "a94f03c093d334b66546bc2b8990517b1eecfc34b8ecc659b758be56db36a04d"


def load_qualifier() -> ModuleType:
    # Load a separate module object: importing this helper must not change the
    # production module's authority or bypass any of its integrity checks.
    spec = importlib.util.spec_from_file_location(
        "_playcli_test_qualifier", ROOT / "tools/qualify_standard_piano_pack.py"
    )
    assert spec is not None and spec.loader is not None
    qualifier = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = qualifier
    spec.loader.exec_module(qualifier)
    qualifier.WHEEL_AUTHORITY = FIXTURE / "wheel.lock.json"
    qualifier.APPROVED_WHEEL_AUTHORITY_SHA256 = AUTHORITY_SHA256
    return qualifier


if __name__ == "__main__":
    raise SystemExit(load_qualifier().main())
