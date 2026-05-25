"""voice_clone.py — Shared voice-clone audio locator for all TTS providers.

Drop any audio file (WAV / MP3 / FLAC / OGG / M4A, ideally 5–30 s) into:

  Runtime  →  ~/.serenity/voice_clone/      (always checked first)
  Dev      →  <repo_root>/sense/voice_clone/ (fallback when running from source)

``get_clone_audio()`` returns the Path to the most recently modified file,
or ``None`` when the drop zone is empty.

``resample_to_24k(src, dst)`` converts any audio file to 24 kHz mono WAV —
needed by Qwen3-TTS (Base) and Coqui XTTS-v2.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# ── Supported audio extensions ────────────────────────────────────────────────
CLONE_EXTS: frozenset[str] = frozenset(
    {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}
)

# ── Path resolution ───────────────────────────────────────────────────────────

def _runtime_clone_dir() -> Path:
    """~/.serenity/voice_clone/ — portable across machines."""
    return Path.home() / ".serenity" / "voice_clone"


def _dev_clone_dir() -> Path:
    """<repo_root>/sense/voice_clone/ — used when running from source."""
    # This file lives at:  serenity/senses/voice_clone.py
    #   parent            = serenity/senses/
    #   parent.parent     = serenity/
    #   parent.parent.parent = <repo_root>/
    return Path(__file__).resolve().parent.parent.parent / "sense" / "voice_clone"


def _scan_dir(d: Path) -> Path | None:
    """Return the most recently modified audio file in d, or None."""
    if not d.is_dir():
        return None
    candidates = [
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in CLONE_EXTS
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# ── Public API ────────────────────────────────────────────────────────────────

def get_clone_audio() -> Path | None:
    """Return the voice-clone reference audio file, or None if none is found.

    Search order:
      1. ~/.serenity/voice_clone/
      2. <repo_root>/sense/voice_clone/

    Returns the most recently modified audio file from whichever directory
    has one first.
    """
    for d in (_runtime_clone_dir(), _dev_clone_dir()):
        found = _scan_dir(d)
        if found is not None:
            return found
    return None


def file_hash(path: Path) -> str:
    """Return a short SHA-256 hex digest of the file (first 64 KB).

    Used to detect whether the clone reference has changed between calls
    without re-reading the entire file each time.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(65_536))
    return h.hexdigest()[:16]


def resample_to_24k(src: Path, dst: Path | None = None) -> Path:
    """Resample *src* to 24 kHz mono WAV and write to *dst*.

    If *dst* is None a sibling file ``<src_stem>_24k.wav`` is created next to
    *src*.  Returns the path that was written.

    Requires ``soundfile`` and ``scipy`` (both installed by install_senses).
    Falls back to returning *src* unchanged if neither is available.
    """
    if dst is None:
        dst = src.with_name(src.stem + "_24k.wav")

    if dst.exists():
        return dst  # already converted

    try:
        import numpy as np
        import soundfile as sf
        from scipy.signal import resample_poly
        from math import gcd

        data, sr = sf.read(str(src), always_2d=False)

        # Mono-mix if stereo
        if data.ndim == 2:
            data = data.mean(axis=1)

        # Resample if needed
        target = 24_000
        if sr != target:
            g = gcd(sr, target)
            data = resample_poly(data, target // g, sr // g).astype(np.float32)

        dst.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(dst), data, target, subtype="PCM_16")
        return dst

    except Exception:
        # If dependencies missing, return original — provider will try it raw
        return src
