"""App index — scans Windows for installed applications and caches results.

Entry types
-----------
  exe   — a direct .exe path  {"name": "Discord", "exe": "C:\\...\\Discord.exe"}
  url   — a protocol/web URL  {"name": "Brawlhalla", "url": "steam://rungameid/291550", "type": "steam"}

Scan sources (priority order, highest first):
  1. Desktop .lnk shortcuts  (%USERPROFILE%/Desktop + %PUBLIC%/Desktop)
  2. Desktop .url shortcuts   (Steam / web protocol links)
  3. Start Menu .lnk shortcuts
  4. Registry App Paths       (HKLM + HKCU \\App Paths)
  5. Registry Uninstall       (HKLM + HKCU \\Uninstall)
  6. Common install dirs      (%LOCALAPPDATA%, %APPDATA%, %ProgramFiles%, %ProgramFiles(x86)%)

Results cached to ~/.serenity/app_index.json
Re-scan: app_index.scan()  or  sera apps scan

Usage
-----
    from serenity.senses.app_index import find, scan
    find("discord")       -> {"name": "Discord", "exe": "C:\\...\\Discord.exe"}
    find("brawlhalla")    -> {"name": "Brawlhalla", "url": "steam://rungameid/291550", "type": "steam"}
    scan()                -> rebuilds the index
"""

from __future__ import annotations

import configparser
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from loguru import logger

_INDEX_PATH = Path.home() / ".serenity" / "app_index.json"

# ── Desktop roots (user + public) ────────────────────────────────────────────
_DESKTOP_ROOTS = [
    Path(os.environ.get("USERPROFILE", "")) / "Desktop",
    Path(os.environ.get("PUBLIC",      "")) / "Desktop",
]

# ── Start Menu roots ─────────────────────────────────────────────────────────
_START_MENU_ROOTS = [
    Path(os.environ.get("APPDATA",     "")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.environ.get("ProgramData", "")) / "Microsoft/Windows/Start Menu/Programs",
]

# ── Common install dirs (walked 2 levels deep) ────────────────────────────────
_SEARCH_ROOTS = [
    os.environ.get("LOCALAPPDATA", ""),
    os.environ.get("APPDATA", ""),
    os.environ.get("ProgramFiles", ""),
    os.environ.get("ProgramFiles(x86)", ""),
    os.environ.get("ProgramW6432", ""),
]

# Words in .exe names that disqualify them as the main executable
_EXE_SKIP_WORDS = ("uninstall", "update", "setup", "helper", "crash", "updater", "repair", "installer")


# ── Public API ────────────────────────────────────────────────────────────────

def find(name: str) -> dict[str, Any] | None:
    """Look up an app by name (fuzzy). Returns entry dict or None.

    Match order (highest priority first):
      1. Exact key match
      2. Space/hyphen-normalized exact match  ("bs manager" == "bsmanager")
      3. Known alias  ("obs" → "obs studio", "vsc" → "visual studio code")
      4. Needle is a whole word in the key  ("obs" in "obs studio" but NOT "obsidian")
      5. Substring match against key or display name
      6. Needle is a whole word in the display name
      7. Key or display name starts with needle
      8. All needle words appear in the key  ("six siege" → "tom clancy's rainbow six siege")
    """
    index = _load_index()
    if not index:
        return None
    needle = name.lower().strip()
    needle_nospace = needle.replace(" ", "").replace("-", "")

    # 1. Exact key match
    if needle in index:
        return index[needle]

    # 2. Space/hyphen-normalized exact match
    for key, entry in index.items():
        key_nospace = key.replace(" ", "").replace("-", "")
        if needle_nospace == key_nospace:
            return entry

    # 3. Known short-name aliases (highest-confidence disambiguation)
    _ALIASES: dict[str, str] = {
        "obs":      "obs studio",
        "vsc":      "visual studio code",
        "vs code":  "visual studio code",
        "vscode":   "visual studio code",
        "ps":       "photoshop",
        "ae":       "after effects",
        "pr":       "premiere pro",
        "ai":       "illustrator",
        "id":       "indesign",
        "fl":       "fl studio",
        "daw":      "fl studio",
    }
    if needle in _ALIASES:
        target = _ALIASES[needle]
        if target in index:
            return index[target]
        # alias target not in index — fall through to fuzzy

    # 4. Needle is a complete whitespace-delimited word inside the key.
    #    "obs" matches "obs studio" (obs is word[0]) but NOT "obsidian".
    for key, entry in index.items():
        if needle in key.split():
            return entry

    # 5. Substring match against key or display name
    for key, entry in index.items():
        if needle in key or needle in entry.get("name", "").lower():
            return entry

    # 6. Needle is a whole word in the display name
    for key, entry in index.items():
        disp_words = entry.get("name", "").lower().split()
        if needle in disp_words:
            return entry

    # 7. Key or display name starts with needle
    for key, entry in index.items():
        if key.startswith(needle) or entry.get("name", "").lower().startswith(needle):
            return entry

    # 8. All needle words appear in the key ("rainbow six" → "tom clancy's rainbow six siege")
    needle_words = needle.split()
    if len(needle_words) >= 2:
        for key, entry in index.items():
            if all(w in key for w in needle_words):
                return entry

    return None


def all_apps() -> dict[str, dict]:
    """Return the full index {key -> entry}."""
    return _load_index()


def scan(verbose: bool = False) -> int:
    """Rebuild the app index. Returns number of entries found."""
    apps: dict[str, dict] = {}

    # Desktop shortcuts first — highest quality, user-curated
    _scan_desktop_lnk(apps, verbose)
    _scan_desktop_url(apps, verbose)
    # Start Menu — broad coverage
    _scan_start_menu(apps, verbose)
    # Registry — catches installs that have no shortcuts
    _scan_registry_app_paths(apps, verbose)
    _scan_registry_uninstall(apps, verbose)
    # Filesystem walk — last resort
    _scan_common_dirs(apps, verbose)

    _INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    _INDEX_PATH.write_text(json.dumps(apps, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("App index: {} entries saved to {}", len(apps), _INDEX_PATH)
    return len(apps)


def _load_index() -> dict[str, dict]:
    if not _INDEX_PATH.exists():
        return {}
    try:
        return json.loads(_INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _add_exe(apps: dict, name: str, exe: str) -> None:
    """Add an exe entry if the file exists and the slot isn't already taken."""
    if not name or not exe:
        return
    exe_path = Path(exe)
    try:
        if not exe_path.exists():
            return
    except OSError:
        return
    # Skip known non-main executables (updaters, installers, etc.)
    stem_lower = exe_path.stem.lower()
    if any(w in stem_lower for w in _EXE_SKIP_WORDS):
        return
    key = name.lower().strip()
    if key not in apps:
        apps[key] = {"name": name, "exe": str(exe_path)}


def _add_url(apps: dict, name: str, url: str, url_type: str = "url") -> None:
    """Add a URL entry (Steam protocol, web link, etc.)."""
    if not name or not url:
        return
    key = name.lower().strip()
    if key not in apps:
        apps[key] = {"name": name, "url": url, "type": url_type}


# ── Desktop .lnk scanner ──────────────────────────────────────────────────────

def _scan_desktop_lnk(apps: dict, verbose: bool) -> None:
    """Resolve .lnk shortcuts on the Desktop(s) via PowerShell WScript.Shell."""
    if sys.platform != "win32":
        return
    _resolve_lnk_files(apps, _iter_lnk_files(_DESKTOP_ROOTS))


# ── Desktop .url scanner ──────────────────────────────────────────────────────

def _scan_desktop_url(apps: dict, verbose: bool) -> None:
    """Parse .url shortcut files on the Desktop — captures Steam / protocol links."""
    for root in _DESKTOP_ROOTS:
        try:
            if not root.exists():
                continue
            entries = list(root.iterdir())
        except OSError:
            continue
        for item in entries:
            try:
                if not item.is_file() or item.suffix.lower() != ".url":
                    continue
            except OSError:
                continue
            _parse_url_file(apps, item)


def _parse_url_file(apps: dict, path: Path) -> None:
    """Parse a Windows .url Internet Shortcut file."""
    try:
        cp = configparser.ConfigParser()
        cp.read(path, encoding="utf-8")
        url = cp.get("InternetShortcut", "URL", fallback="").strip()
        if not url:
            return
        name = path.stem  # filename without .url extension
        if url.startswith("steam://"):
            _add_url(apps, name, url, "steam")
        elif url.startswith(("http://", "https://")):
            _add_url(apps, name, url, "web")
        else:
            # Other protocols (e.g. battle.net://, epicgames://)
            _add_url(apps, name, url, "protocol")
    except Exception:
        pass


# ── Start Menu .lnk scanner ───────────────────────────────────────────────────

def _scan_start_menu(apps: dict, verbose: bool) -> None:
    """Resolve .lnk shortcuts in Start Menu via PowerShell WScript.Shell."""
    if sys.platform != "win32":
        return
    _resolve_lnk_files(apps, _iter_lnk_files(_START_MENU_ROOTS))


# ── Shared .lnk resolver ─────────────────────────────────────────────────────

def _iter_lnk_files(roots: list[Path]):
    """Yield all .lnk file paths under the given roots."""
    for root in roots:
        try:
            if not root.exists():
                continue
            yield from root.rglob("*.lnk")
        except OSError:
            continue


_LNK_BATCH_SIZE = 40  # keep PS command well under WinError 206 limit (~32k chars)


def _resolve_lnk_files(apps: dict, lnk_iter) -> None:
    """Batch-resolve .lnk files to their target .exe via PowerShell WScript.Shell.

    Processes shortcuts in batches of _LNK_BATCH_SIZE to avoid the
    WinError 206 "filename or extension is too long" limit on the PS command.
    """
    lnk_paths = list(lnk_iter)
    if not lnk_paths:
        return

    for i in range(0, len(lnk_paths), _LNK_BATCH_SIZE):
        batch = lnk_paths[i : i + _LNK_BATCH_SIZE]
        _resolve_lnk_batch(apps, batch)


def _resolve_lnk_batch(apps: dict, lnk_paths: list[Path]) -> None:
    ps_lines = ["$sh = New-Object -ComObject WScript.Shell"]
    for lnk in lnk_paths:
        escaped = str(lnk).replace("'", "''")
        name = lnk.stem.replace("'", "''")
        ps_lines.append(
            f"try {{ $t = $sh.CreateShortcut('{escaped}').TargetPath; "
            f"if ($t -and $t.EndsWith('.exe') -and (Test-Path $t)) "
            f"{{ Write-Output ('{name}|' + $t) }} }} catch {{}}"
        )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "\n".join(ps_lines)],
            capture_output=True, text=True, timeout=30,
        )
        for line in result.stdout.splitlines():
            if "|" in line:
                name, exe = line.split("|", 1)
                _add_exe(apps, name.strip(), exe.strip())
    except Exception as e:
        logger.debug("LNK scan error: {}", e)


# ── Registry scanners ─────────────────────────────────────────────────────────

def _scan_registry_app_paths(apps: dict, verbose: bool) -> None:
    """HKLM/HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\*.exe"""
    if sys.platform != "win32":
        return
    import winreg  # type: ignore

    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    ]
    for hive, sub in roots:
        try:
            hkey = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        i = 0
        while True:
            try:
                subkey_name = winreg.EnumKey(hkey, i)
                i += 1
            except OSError:
                break
            try:
                subkey = winreg.OpenKey(hkey, subkey_name)
                exe, _ = winreg.QueryValueEx(subkey, "")
                name = Path(subkey_name).stem
                _add_exe(apps, name, exe.strip('"').strip())
            except OSError:
                pass
        winreg.CloseKey(hkey)


def _scan_registry_uninstall(apps: dict, verbose: bool) -> None:
    """HKLM/HKCU\\...\\Uninstall — picks up InstallLocation + DisplayName."""
    if sys.platform != "win32":
        return
    import winreg  # type: ignore

    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]

    def _reg_str(hkey, name: str) -> str:
        try:
            val, _ = winreg.QueryValueEx(hkey, name)
            return str(val).strip().strip('"')
        except OSError:
            return ""

    for hive, sub in roots:
        try:
            hkey = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        i = 0
        while True:
            try:
                subkey_name = winreg.EnumKey(hkey, i)
                i += 1
            except OSError:
                break
            try:
                subkey = winreg.OpenKey(hkey, subkey_name)
                display_name = _reg_str(subkey, "DisplayName")
                install_loc  = _reg_str(subkey, "InstallLocation")
                display_icon = _reg_str(subkey, "DisplayIcon").split(",")[0]

                if not display_name:
                    continue

                if display_icon and display_icon.lower().endswith(".exe"):
                    _add_exe(apps, display_name, display_icon)

                if install_loc and Path(install_loc).is_dir():
                    _find_exe_in_dir(apps, display_name, Path(install_loc), depth=1)

            except OSError:
                pass
        winreg.CloseKey(hkey)


# ── Filesystem scanner ────────────────────────────────────────────────────────

def _scan_common_dirs(apps: dict, verbose: bool) -> None:
    """Walk common install roots 2 levels deep, collect .exe files."""
    for root_str in _SEARCH_ROOTS:
        if not root_str:
            continue
        root = Path(root_str)
        if not root.exists():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if not child.is_dir() or child.name.startswith("."):
                    continue
            except OSError:
                continue
            _find_exe_in_dir(apps, child.name, child, depth=2)


def _find_exe_in_dir(apps: dict, display_name: str, directory: Path, depth: int = 1) -> None:
    """Find the most likely main .exe inside a directory (up to `depth` levels)."""
    if depth == 0 or not directory.is_dir():
        return

    dir_name_lower = directory.name.lower()
    candidates: list[Path] = []

    try:
        entries = list(directory.iterdir())
    except OSError:
        return

    for item in entries:
        try:
            if item.is_file() and item.suffix.lower() == ".exe":
                low = item.stem.lower()
                if any(x in low for x in _EXE_SKIP_WORDS):
                    continue
                candidates.append(item)
            elif item.is_dir() and depth > 1:
                _find_exe_in_dir(apps, display_name, item, depth - 1)
        except OSError:
            continue

    if not candidates:
        return

    def _score(p: Path) -> int:
        s = p.stem.lower()
        if s == dir_name_lower:
            return 3
        if display_name.lower().split()[0] in s:
            return 2
        if dir_name_lower in s:
            return 1
        return 0

    candidates.sort(key=_score, reverse=True)
    _add_exe(apps, display_name, str(candidates[0]))
