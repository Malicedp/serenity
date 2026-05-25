# Copyright © 2026 Daniel T Niamke. All rights reserved.
"""
serenity_nnn — Neural Node Network memory system.

On Windows / Python 3.11, the compiled .pyd extensions are loaded automatically
and take precedence over these Python stubs.

On all other platforms, these stubs are used and NNN operations will raise
RuntimeError with a clear message rather than crashing the agent.
"""

from __future__ import annotations

import platform as _platform
import sys as _sys

_PLATFORM_MSG = (
    f"NNN is not available on {_platform.system()} / Python {_sys.version.split()[0]}. "
    "Serenity's compiled memory module currently targets Windows / Python 3.11. "
    "Linux and macOS support is in development — follow https://github.com/Malicedp/serenity."
)


def get_state() -> dict:
    """Return the current NNN state (bundle count, authorisation status, etc.)."""
    return {
        "available": False,
        "authorised": False,
        "bundle_count": 0,
        "reason": _PLATFORM_MSG,
    }


def encode(text: str, session_id: str = "") -> dict:  # noqa: ARG001
    """Encode text into NNN memory."""
    raise RuntimeError(_PLATFORM_MSG)


def query(text: str, token_budget: int = 1000) -> list:  # noqa: ARG001
    """Query NNN for semantically similar memories."""
    raise RuntimeError(_PLATFORM_MSG)


def simulate_plan(actions: list, initial_state: str) -> list:  # noqa: ARG001
    """Simulate a multi-step plan using NNN causal memory."""
    raise RuntimeError(_PLATFORM_MSG)
