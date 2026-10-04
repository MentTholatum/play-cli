from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from play_cli import __version__

SCHEMA_VERSION = "1.5"
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION,)


@dataclass(frozen=True)
class OperationContext:
    started_ns: int

    @classmethod
    def start(cls) -> OperationContext:
        return cls(started_ns=time.perf_counter_ns())

    def envelope(
        self,
        operation: str,
        status: str,
        *,
        provenance: dict[str, Any] | None = None,
        timing_scope: str = "through_response_assembly",
    ) -> dict[str, Any]:
        deterministic: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "cli": {"name": "play-cli", "version": __version__},
            "execution": {
                "location": "local",
                "network": "disabled",
                "telemetry": "disabled",
            },
        }
        if provenance is not None:
            deterministic.update(provenance)
        duration_ms = max(0, time.perf_counter_ns() - self.started_ns) / 1_000_000
        return {
            "schema_version": SCHEMA_VERSION,
            "operation": operation,
            "status": status,
            "provenance": deterministic,
            "operation_timing": {
                "duration_ms": round(duration_ms, 3),
                "scope": timing_scope,
            },
        }
