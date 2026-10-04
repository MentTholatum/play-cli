from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from play_cli.runtime_dependencies import playback_action, playback_candidates


@dataclass(frozen=True)
class PlaybackResult:
    requested: bool
    status: str
    player: str | None = None
    exit_code: int | None = None

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "requested": self.requested,
            "status": self.status,
        }
        if self.player is not None:
            result["player"] = self.player
        if self.exit_code is not None:
            result["exit_code"] = self.exit_code
        if self.status == "unavailable":
            result["error"] = {
                "id": "runtime_dependency_missing",
                "message": "Requested playback capability is unavailable.",
            }
            result["action"] = playback_action()
        return result


def play_performance(wav_path: Path, requested: bool) -> PlaybackResult:
    if not requested:
        return PlaybackResult(requested=False, status="not_requested")
    candidates = playback_candidates()
    player = next((path for name in candidates if (path := shutil.which(name)) is not None), None)
    if player is None:
        return PlaybackResult(requested=True, status="unavailable")
    try:
        completed = subprocess.run(
            [player, str(wav_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return PlaybackResult(requested=True, status="failed", player=player)
    if completed.returncode != 0:
        return PlaybackResult(
            requested=True,
            status="failed",
            player=player,
            exit_code=completed.returncode,
        )
    return PlaybackResult(requested=True, status="played", player=player, exit_code=0)
