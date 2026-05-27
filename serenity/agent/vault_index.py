"""Semantic index of Obsidian vault notes — ChromaDB + nomic-embed-text.

Every note written via VaultWriteTool is embedded and stored here.
Context builder uses semantic search instead of grep, so paraphrased
queries find relevant notes even when the exact words don't match.

Public API:
    index_note(path, content)   — called by VaultWriteTool on every write
    remove_note(path)           — called if a note is deleted
    search(query, n=4)          — semantic search, returns list of hits
    reindex_all(workspace)      — bootstrap existing notes (run once)
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from loguru import logger


_COLLECTION_NAME = "serenity_vault"
_DATA_DIR        = Path.home() / ".serenity" / "serenity_vault_data"
_MAX_CHUNK       = 1500   # chars stored per note — covers most notes fully
_MIN_SCORE       = 0.35   # cosine similarity floor — below this is noise

# System dirs and filenames never indexed (same list as _search_vault)
_SKIP_DIRS  = frozenset({"Agent", "memory", "sessions", "cron", "state",
                          "skills", ".git", ".obsidian", "User"})
_SKIP_FILES = frozenset({"SOUL.md", "AGENTS.md", "HEARTBEAT.md", "TOOLS.md",
                          "USER.md", "MEMORY.md", "SKILLS.md", "BOOTSTRAP.md",
                          "IDENTITY.md", "Experience.md"})

_col = None   # cached ChromaDB collection
_index_lock = threading.Lock()  # serialises concurrent upsert/query calls


def _collection():
    global _col
    if _col is not None:
        return _col
    import chromadb
    from chromadb.config import Settings
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(
        path=str(_DATA_DIR),
        settings=Settings(anonymized_telemetry=False),
    )
    _col = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    return _col


def _embed(text: str) -> list[float]:
    from serenity_nnn.embedder import embed
    return embed(text)


def _note_id(path: Path) -> str:
    """Stable, unique ID for a note derived from its absolute path."""
    return hashlib.md5(str(path.resolve()).encode()).hexdigest()


def _best_snippet(content: str) -> str:
    """First readable non-frontmatter, non-heading line from a note."""
    in_fm = False
    for line in content.splitlines():
        s = line.strip()
        if s == "---":
            in_fm = not in_fm
            continue
        if in_fm or not s or s.startswith("#"):
            continue
        return s[:120]
    return content[:120].replace("\n", " ")


# ── Public API ────────────────────────────────────────────────────────────────

def index_note(path: Path, content: str) -> None:
    """Embed and upsert a vault note. Safe to call on create or update."""
    try:
        chunk  = content[:_MAX_CHUNK]
        vector = _embed(chunk)
        with _index_lock:
            _collection().upsert(
                ids=[_note_id(path)],
                embeddings=[vector],
                documents=[chunk],
                metadatas=[{
                    "path":     str(path),
                    "filename": path.name,
                    "stem":     path.stem,
                }],
            )
        logger.debug("Vault indexed: {}", path.name)
    except Exception as exc:
        logger.warning("Vault index write failed for {}: {}", path.name, exc)


def remove_note(path: Path) -> None:
    """Remove a note from the index when the file is deleted."""
    try:
        _collection().delete(ids=[_note_id(path)])
    except Exception as exc:
        logger.debug("Vault index remove failed for {}: {}", path.name, exc)


def search(query: str, n_results: int = 4) -> list[dict]:
    """Semantic search over indexed vault notes.

    Returns a list of dicts: {filename, path, snippet, score}
    sorted best-first. Empty list if nothing relevant found.
    """
    try:
        vector = _embed(query)
        with _index_lock:
            col   = _collection()
            total = col.count()
            if total == 0:
                return []
            n       = min(n_results, total)
            results = col.query(
                query_embeddings=[vector],
                n_results=n,
                include=["documents", "metadatas", "distances"],
            )

        hits = []
        for i, doc in enumerate(results["documents"][0]):
            score = 1.0 - results["distances"][0][i]   # cosine dist → similarity
            if score < _MIN_SCORE:
                continue
            meta = results["metadatas"][0][i]
            hits.append({
                "filename": meta.get("filename", "?"),
                "path":     meta.get("path", ""),
                "snippet":  _best_snippet(doc),
                "score":    round(score, 3),
            })
        return hits

    except Exception as exc:
        logger.warning("Vault semantic search failed: {}", exc)
        return []


async def warm_embed() -> None:
    """Pre-load nomic-embed-text into Ollama VRAM at startup.

    Makes a single dummy embed so the model is resident before the first real
    NNN query or store call arrives. Run this once as a background asyncio task
    right after the agent starts — it never blocks the caller.
    """
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: _embed("warmup"))
        logger.info("NNN/vault embedder warm ✓ (nomic-embed-text loaded)")
    except Exception as exc:
        logger.debug("Embedder warmup skipped (non-fatal): {}", exc)


def reindex_all(workspace: Path) -> int:
    """Scan the vault and index every eligible .md file.

    Call once on first run, or after manually adding notes outside Serenity.
    Returns the number of notes indexed.
    """
    count = 0
    for md_path in workspace.rglob("*.md"):
        try:
            rel = md_path.relative_to(workspace)
            if rel.parts and rel.parts[0] in _SKIP_DIRS:
                continue
        except ValueError:
            pass
        if md_path.name in _SKIP_FILES:
            continue
        try:
            content = md_path.read_text(encoding="utf-8", errors="replace")
            index_note(md_path, content)
            count += 1
        except OSError:
            pass
    logger.info("Vault reindex complete: {} notes indexed", count)
    return count
