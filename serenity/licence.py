# Copyright © 2026 Daniel T Niamke. All rights reserved.
"""
Licence shim — loaded on platforms where the compiled licence module
(licence.cp311-win_amd64.pyd) is not available (Linux, macOS, Python 3.12+).

On Windows Python 3.11, the compiled .pyd takes precedence automatically.
On all other platforms, this file provides safe stub implementations so
Serenity starts cleanly and shows a clear message instead of crashing with
an ImportError.

If you are a developer building a port for Linux/macOS, implement these
functions in a platform-appropriate extension module and place it alongside
this file.
"""

from __future__ import annotations

import platform as _platform
import sys as _sys

_IS_WIN311 = _platform.system() == "Windows" and _sys.version_info[:2] == (3, 11)

_UNSUPPORTED_MSG = (
    "The Serenity licence module is currently compiled for Windows / Python 3.11 only.\n"
    f"  Your platform: {_platform.system()} / Python {_sys.version.split()[0]}\n\n"
    "  Linux and macOS support is in development.\n"
    "  Follow https://github.com/Malicedp/serenity for updates."
)


def generate_nnn_token(key: str) -> str:  # noqa: ARG001
    """Generate an NNN authorisation token from a licence key.

    On unsupported platforms this returns an empty string — NNN will
    remain locked and the agent will still run in unlicensed mode.
    """
    if not _IS_WIN311:
        return ""
    # Should never reach here — .pyd overrides this file on Win/3.11
    raise ImportError(_UNSUPPORTED_MSG)


def is_master_key_active() -> bool:
    """Return True if a master development key is active.

    Always returns False on unsupported platforms.
    """
    return False


def check_grace_period(
    last_validated_iso: str,
    grace_days: int = 7,
) -> bool:
    """Check whether the offline grace period is still valid.

    Returns True if the grace period is still active, False if expired.

    The caller uses this as a bool: `if check_grace_period(...):` — so the
    shim returns False (expired) to force an offline-block on unsupported
    platforms rather than silently allowing unlimited offline use.

    On Windows Python 3.11 the compiled .pyd overrides this file entirely.
    """
    if not _IS_WIN311:
        return False
    raise ImportError(_UNSUPPORTED_MSG)
