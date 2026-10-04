from __future__ import annotations

import shutil
import sys
from typing import Any

from play_cli import sound_pack_storage as storage

SUPPORTED_PLATFORMS = ("darwin", "linux")


def platform_id() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def playback_candidates() -> tuple[str, ...]:
    return ("afplay",) if platform_id() == "darwin" else ("aplay", "paplay")


def playback_action() -> dict[str, Any]:
    return {
        "id": "provide_playback_command",
        "alternatives": [
            {
                "argv_template": [candidate, {"required_input": "wav_path"}],
                "executable": candidate,
            }
            for candidate in playback_candidates()
        ],
        "required_inputs": {
            "wav_path": {
                "description": "Complete local PCM WAV to play.",
                "type": "local_path",
            }
        },
    }


def required_runtime_action() -> dict[str, Any]:
    return {
        "id": "select_supported_runtime",
        "argv_template": ["playcli", "doctor"],
        "alternatives": [
            {
                "platform": "darwin",
                "minimum_python_version": "3.11",
                "required_native_symbol": "renameatx_np",
            },
            {
                "platform": "linux",
                "minimum_python_version": "3.11",
                "required_native_symbol": "renameat2",
            },
        ],
    }


def inspect_runtime_dependencies() -> dict[str, Any]:
    current_platform = platform_id()
    player = next(
        (path for name in playback_candidates() if (path := shutil.which(name)) is not None),
        None,
    )
    return {
        "python": {
            "minimum_version": "3.11",
            "status": "available" if sys.version_info >= (3, 11) else "unavailable",
            "version": (
                f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            ),
        },
        "platform": {
            "id": current_platform,
            "status": "supported" if current_platform in SUPPORTED_PLATFORMS else "unsupported",
            "supported": list(SUPPORTED_PLATFORMS),
        },
        "atomic_noreplace": {
            "required_for": ["render", "sound-pack.install"],
            "status": "available" if storage.atomic_noreplace_available() else "unavailable",
        },
        "playback": {
            "action": playback_action() if player is None else None,
            "candidates": list(playback_candidates()),
            "player": player,
            "required": False,
            "status": "available" if player is not None else "unavailable",
        },
    }
