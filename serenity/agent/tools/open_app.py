"""open_app tool — launch any installed application by name.

Consults the app index (~/.serenity/app_index.json) built by `sera apps scan`.
Supports exe paths, Steam protocol links (steam://rungameid/...), and web URLs.
Falls back to a live scan if the index is missing or the entry is stale.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import StringSchema, tool_parameters_schema


@tool_parameters(
    tool_parameters_schema(
        app=StringSchema(
            "Name of the app to open, e.g. 'discord', 'spotify', 'chrome', 'brawlhalla'. "
            "Also accepts an absolute path to an .exe file or a URL/protocol string."
        ),
        args=StringSchema(
            "Optional arguments or URL to pass to the app.",
            nullable=True,
        ),
        required=["app"],
    )
)
class OpenAppTool(Tool):
    """Launch any installed application by name.

    Looks up the app in the cached index (~/.serenity/app_index.json).
    If not found, runs a live search of the registry and desktop shortcuts.
    Run 'sera apps scan' to rebuild the index after installing new apps.
    Supports Steam games, regular executables, and web shortcuts.
    """

    @property
    def name(self) -> str:
        return "open_app"

    @property
    def description(self) -> str:
        return (
            "Open any installed application or game by name — 'discord', 'spotify', 'chrome', "
            "'brawlhalla', 'roblox', 'blender', etc. "
            "Checks the pre-built app index first (desktop shortcuts, Start Menu, registry). "
            "If an app is missing after trying, tell the user to run: sera apps scan. "
            "Also accepts an absolute .exe path or a URL/protocol string as the app argument. "
            "Optional 'args' for command-line arguments or a URL to open."
        )

    async def execute(self, app: str, args: str | None = None, **kwargs: Any) -> str:
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _launch, app.strip(), (args or "").strip())


# ── Launch logic ──────────────────────────────────────────────────────────────

def _launch(app: str, args: str) -> str:
    # 1. Direct absolute exe path
    if _is_absolute_path(app):
        exe = Path(app)
        if exe.exists():
            return _start_exe(exe, args)
        return f"Error: file not found — {app}"

    # 2. Bare URL / protocol (e.g. someone passed "steam://rungameid/291550" directly)
    if _is_url(app):
        return _start_url(app)

    # 3. Check cached index
    from serenity.senses.app_index import find, scan as _scan, _INDEX_PATH

    entry = find(app)

    # 4. Auto-scan once if the index is missing entirely
    if entry is None and not _INDEX_PATH.exists():
        logger.info("App index missing — running initial scan…")
        _scan()
        entry = find(app)

    if entry:
        result = _launch_entry(entry, args)
        if result:
            return result
        # Entry had a stale exe path — rescan and try once more
        logger.info("Stale index entry for '{}' — rescanning…", app)
        _scan()
        entry = find(app)
        if entry:
            result = _launch_entry(entry, args)
            if result:
                return result

    return (
        f"'{app}' not found in the app index.\n"
        f"Run:  sera apps scan  — to rebuild the index after installing new apps.\n"
        f"Or use the full path:  open_app(app='C:\\\\full\\\\path\\\\to\\\\{app}.exe')"
    )


def _launch_entry(entry: dict, args: str) -> str | None:
    """Dispatch to the right launcher based on entry type. Returns result or None if stale."""
    entry_type = entry.get("type", "exe")

    if "exe" in entry:
        exe = Path(entry["exe"])
        try:
            if exe.exists():
                return _start_exe(exe, args)
        except OSError:
            pass
        return None  # stale — signal caller to rescan

    if "url" in entry:
        url = entry["url"]
        if args:
            url = f"{url} {args}"
        return _start_url(url, entry.get("name", url))

    return None


def _start_exe(exe: Path, args: str) -> str:
    cmd = f'start "" "{exe}"'
    if args:
        cmd += f" {args}"
    try:
        subprocess.Popen(cmd, shell=True)
        logger.info("open_app: launched {}", exe.name)
        return f"Opened: {exe.name}"
    except Exception as e:
        return f"Error launching {exe.name}: {e}"


def _start_url(url: str, label: str | None = None) -> str:
    """Launch a URL/protocol string via Windows 'start'."""
    label = label or url
    # Wrap in double quotes unless the URL already contains them
    quoted = url if '"' in url else f'"{url}"'
    cmd = 'start "" ' + quoted
    try:
        subprocess.Popen(cmd, shell=True)
        logger.info("open_app: launched URL {}", url[:80])
        return f"Opened: {label}"
    except Exception as e:
        return f"Error launching {label}: {e}"


def _is_absolute_path(s: str) -> bool:
    return os.path.isabs(s) or (len(s) > 2 and s[1] == ":")


def _is_url(s: str) -> bool:
    return "://" in s


# ── MinimiseAppTool ───────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        app=StringSchema(
            "Name of the app to minimise, e.g. 'discord', 'chrome', 'notepad', 'spotify'. "
            "Can also be a process name like 'chrome.exe'."
        ),
        required=["app"],
    )
)
class MinimiseAppTool(Tool):
    """Minimise a running application window (send to taskbar without closing)."""

    @property
    def name(self) -> str:
        return "minimise_app"

    @property
    def description(self) -> str:
        return (
            "Minimise a running application window to the taskbar — same as clicking the minimise button. "
            "Works with 'discord', 'chrome', 'spotify', 'notepad', 'obs', etc. "
            "Does NOT close the app — it keeps running in the background."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, app: str, **kwargs: Any) -> str:
        import asyncio
        return await asyncio.get_running_loop().run_in_executor(
            None, _minimise, app.strip()
        )


def _minimise(app: str) -> str:
    """Minimise all windows belonging to the given app using Windows user32."""
    import ctypes
    import ctypes.wintypes

    candidates = _resolve_process_name(app)

    # Step 1: get PIDs via tasklist (reliable, no privilege issues)
    target_pids: set[int] = set()
    for proc_name in candidates:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {proc_name}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().splitlines():
                parts = line.strip('"').split('","')
                if len(parts) >= 2:
                    try:
                        target_pids.add(int(parts[1]))
                    except ValueError:
                        pass
        except Exception:
            pass

    if not target_pids:
        return (
            f"No running process found for '{app}'. "
            f"Tried: {', '.join(candidates)}."
        )

    # Step 2: enumerate windows and minimise any that belong to those PIDs
    user32 = ctypes.windll.user32
    SW_MINIMIZE = 6
    minimised: list[int] = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def _callback(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in target_pids:
            user32.ShowWindow(hwnd, SW_MINIMIZE)
            minimised.append(pid.value)
        return True

    user32.EnumWindows(EnumWindowsProc(_callback), 0)

    if minimised:
        return f"Minimised {len(minimised)} window(s) for '{app}'."
    return (
        f"Process is running but no visible windows found for '{app}'. "
        "It may already be minimised or running in the system tray."
    )


# ── CloseAppTool ──────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        app=StringSchema(
            "Name of the app to kill, e.g. 'discord', 'chrome', 'notepad', 'spotify'. "
            "Can also be a process name like 'chrome.exe'."
        ),
        required=["app"],
    )
)
class CloseAppTool(Tool):
    """Force-kill a running application by name (equivalent to End Task in Task Manager)."""

    @property
    def name(self) -> str:
        return "close_app"

    @property
    def description(self) -> str:
        return (
            "Force-kill a running application by name — same as End Task in Task Manager. "
            "Works with 'discord', 'chrome', 'spotify', 'notepad', 'obs', etc. "
            "Uses the app index to resolve names to process names."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, app: str, **kwargs: Any) -> str:
        import asyncio
        return await asyncio.get_running_loop().run_in_executor(
            None, _close, app.strip()
        )


def _resolve_process_name(app: str) -> list[str]:
    """Return candidate process names for an app label.

    Tries the app index first, then falls back to common heuristics.
    """
    candidates: list[str] = []

    # If already looks like a process name, use it directly
    if app.lower().endswith(".exe"):
        candidates.append(app)
        return candidates

    # Check the app index for a known exe path
    try:
        from serenity.senses.app_index import find
        entry = find(app)
        if entry and "exe" in entry:
            exe_name = Path(entry["exe"]).name
            candidates.append(exe_name)
    except Exception:
        pass

    # Always add plain name + .exe as fallbacks
    candidates.append(f"{app}.exe")

    # Common name → process name mappings
    _KNOWN = {
        "chrome": ["chrome.exe"],
        "firefox": ["firefox.exe"],
        "discord": ["discord.exe", "Update.exe"],
        "spotify": ["spotify.exe"],
        "steam": ["steam.exe"],
        "obs": ["obs64.exe", "obs32.exe"],
        "notepad": ["notepad.exe"],
        "explorer": ["explorer.exe"],
        "vscode": ["code.exe"],
        "code": ["code.exe"],
        "vlc": ["vlc.exe"],
        "slack": ["slack.exe"],
        "zoom": ["zoom.exe"],
        "teams": ["teams.exe"],
        "telegram": ["telegram.exe"],
    }
    key = app.lower()
    if key in _KNOWN:
        candidates = _KNOWN[key] + [c for c in candidates if c not in _KNOWN[key]]

    return candidates


def _close(app: str) -> str:
    """Force-kill a process by name using taskkill /F (Windows built-in)."""
    candidates = _resolve_process_name(app)

    for proc_name in candidates:
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", proc_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return f"✅ Killed: {proc_name}"
            # returncode 128 = process not found — try next candidate
            if result.returncode != 128:
                err = result.stderr.strip() or result.stdout.strip()
                return f"❌ Failed to kill '{proc_name}': {err}"
        except subprocess.TimeoutExpired:
            return f"❌ Timed out trying to kill '{proc_name}'."
        except Exception as exc:
            return f"❌ Error: {exc}"

    return (
        f"❌ No running process found for '{app}'. "
        f"Tried: {', '.join(candidates)}. "
        "Check the exact process name in Task Manager."
    )
