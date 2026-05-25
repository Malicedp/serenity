"""Audio RAG — flat file store with JSON metadata sidecar.

Storage layout::

    ~/.serenity/audio_rag/
        {uuid}.wav    — raw audio clip
        {uuid}.json   — metadata sidecar

Metadata schema::

    {
        "id":              "uuid4",
        "session_id":      "...",
        "timestamp":       1234567890.0,
        "source":          "mic" | "file",
        "duration_s":      5.2,
        "transcript":      "what Serenity heard",
        "tags":            ["ambient", "speech", ...],
        "clap_embedding":  [0.1, 0.2, ...]   // null if CLAP not available
    }

Query modes:
- Keyword search over transcripts
- CLAP semantic similarity (if embedder available)
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

_BASE = Path.home() / ".serenity" / "audio_rag"


def _base() -> Path:
    _BASE.mkdir(parents=True, exist_ok=True)
    return _BASE


def store(
    audio_path: Path | str,
    transcript: str,
    session_id: str,
    source: str = "mic",
    duration_s: float = 0.0,
    tags: list[str] | None = None,
    clap_embedding: list[float] | None = None,
) -> str:
    """Store an audio clip and its metadata. Returns the entry ID."""
    import shutil

    entry_id = str(uuid.uuid4())
    base = _base()

    # Copy audio file into store
    src = Path(audio_path)
    dest_audio = base / f"{entry_id}{src.suffix}"
    shutil.copy2(src, dest_audio)

    meta = {
        "id": entry_id,
        "session_id": session_id,
        "timestamp": time.time(),
        "source": source,
        "duration_s": duration_s,
        "transcript": transcript,
        "tags": tags or [],
        "clap_embedding": clap_embedding,
    }
    (base / f"{entry_id}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    logger.debug("AudioRAG: stored {} — '{}'", entry_id, transcript[:60])
    return entry_id


def query_keyword(text: str, limit: int = 5) -> list[dict[str, Any]]:
    """Full-text search over stored transcripts."""
    base = _base()
    results = []
    needle = text.lower()
    for meta_file in sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if needle in meta.get("transcript", "").lower():
            results.append(meta)
            if len(results) >= limit:
                break
    return results


def query_semantic(embedding: list[float], limit: int = 5) -> list[dict[str, Any]]:
    """Return entries sorted by cosine similarity to the given CLAP embedding."""
    import math

    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb + 1e-9)

    base = _base()
    scored = []
    for meta_file in base.glob("*.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        stored_emb = meta.get("clap_embedding")
        if not stored_emb:
            continue
        score = _cosine(embedding, stored_emb)
        scored.append((score, meta))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in scored[:limit]]


def recent(limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recently stored entries."""
    base = _base()
    files = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    results = []
    for f in files[:limit]:
        try:
            results.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return results


def count() -> int:
    return len(list(_base().glob("*.json")))
