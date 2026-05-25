# Copyright © 2026 Daniel T Niamke. All rights reserved.
"""
NNN shim — loaded when the compiled nnn.cp311-win_amd64.pyd is unavailable.

All public functions raise RuntimeError with a clear platform message so
callers can catch the error and degrade gracefully rather than crashing.
"""

from __future__ import annotations

import platform as _platform
import sys as _sys

_PLATFORM_MSG = (
    f"NNN is not available on {_platform.system()} / Python {_sys.version.split()[0]}. "
    "Serenity's compiled memory module currently targets Windows / Python 3.11. "
    "Linux and macOS support is in development — follow https://github.com/Malicedp/serenity."
)


def authorize(token: str) -> None:  # noqa: ARG001
    """Attempt to authorise NNN with the given token."""
    raise RuntimeError(_PLATFORM_MSG)


def encode(text: str, session_id: str = "") -> dict:  # noqa: ARG001
    """Encode text into NNN memory."""
    raise RuntimeError(_PLATFORM_MSG)


def query(text: str, token_budget: int = 1000) -> list:  # noqa: ARG001
    """Query NNN for semantically similar memories."""
    raise RuntimeError(_PLATFORM_MSG)


def status() -> dict:
    """Return NNN status."""
    return {
        "available": False,
        "reason": _PLATFORM_MSG,
    }
