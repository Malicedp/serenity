"""vault_write — write a note directly to the Obsidian vault."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import StringSchema, tool_parameters_schema


def _title_words(title: str) -> set[str]:
    """Return significant words from a title for overlap comparison."""
    _STOP = {"a", "an", "the", "and", "or", "of", "in", "on", "at", "to",
             "for", "with", "after", "before", "about", "from", "by", "is",
             "was", "this", "that", "my", "i", "session", "note", "log"}
    return {w.lower() for w in re.findall(r"\w+", title) if w.lower() not in _STOP and len(w) > 2}


def _find_similar_note(workspace: Path, title: str) -> Path | None:
    """Return path of an existing note whose title is ≥60% word-overlapping with title.

    Checks the frontmatter/heading of each .md file in the workspace root.
    Returns the first match found, or None.
    """
    new_words = _title_words(title)
    if not new_words:
        return None

    for md in workspace.glob("*.md"):
        try:
            first_lines = md.read_text(encoding="utf-8", errors="replace")[:400]
        except OSError:
            continue
        # Pull the H1 heading or the frontmatter title if present
        existing_title = ""
        for line in first_lines.splitlines():
            line = line.strip()
            if line.startswith("# "):
                existing_title = line[2:].strip()
                break
        if not existing_title:
            existing_title = md.stem  # fall back to filename

        existing_words = _title_words(existing_title)
        if not existing_words:
            continue

        overlap = len(new_words & existing_words) / max(len(new_words), len(existing_words))
        if overlap >= 0.60:
            return md

    return None


def _slugify(title: str) -> str:
    """Convert a title to a short filename-safe slug.

    The slug is derived from the title only — no date prefix.
    Dates live inside the file (frontmatter + heading), not in the filename.
    Max 40 characters so filenames stay readable in Obsidian's sidebar.
    """
    slug = title.strip()
    slug = re.sub(r"[^\w\s-]", "", slug)          # strip punctuation
    slug = re.sub(r"[\s_]+", " ", slug).strip()   # normalise whitespace
    slug = slug[:40].strip()                       # hard cap, clean edge
    return slug or "note"


@tool_parameters(
    tool_parameters_schema(
        title=StringSchema(
            "Short, clear title for the note — used as the heading and filename. "
            "Keep it brief: 'Echo VR', 'Favourite colour', 'Goals for 2026'. "
            "This becomes the filename exactly, so avoid special characters."
        ),
        content=StringSchema(
            "Body of the note in Markdown. Write clearly as if the user will read it back in six months.\n"
            "For LEARNED notes (anything going to NNN after this): use this exact structure:\n"
            "## What I learned\n<explanation>\n\n"
            "## How I learned it\n<source, experience, or reasoning>\n\n"
            "## What it means\n<implication or strategy>\n\n"
            "## nnn_query\n<short topic string — this acts as the NNN search prompt when this note is read back>\n\n"
            "For personal memories about the user: state the fact/preference/feeling directly."
        ),
        tags=StringSchema(
            "Comma-separated tags, e.g. 'preference,colour' or 'learned,nnn'. "
            "Use: memory, preference, feeling, goal, project, idea, learned, nnn.",
            nullable=True,
        ),
        subfolder=StringSchema(
            "Optional subfolder inside the vault. "
            "Only use 'Agent' for Serenity's own system files (SOUL.md, skills, memory index). "
            "For ALL other notes — user facts, session summaries, memories, learning — "
            "leave this blank. Blank = vault root, which is correct for everything except Agent files.",
            nullable=True,
        ),
        required=["title", "content"],
    )
)
class VaultWriteTool(Tool):
    """Write a note directly to the Obsidian vault workspace."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "vault_write"

    @property
    def description(self) -> str:
        return (
            "Write a note to the Obsidian vault. "
            "Use this whenever the user asks to remember, save, or note something — "
            "and whenever you call nnn_store (write the human-readable version here first). "
            "The tool handles filename, frontmatter, and date automatically. "
            "Supply a SHORT title like 'Favourite colour' or 'Echo VR' — the title becomes "
            "the filename so keep it concise. "
            "IMPORTANT: NEVER pass subfolder for user notes or memories. "
            "All notes — personal facts, session summaries, learning notes, everything — "
            "go to the vault ROOT (leave subfolder blank). "
            "Only exception: subfolder='Agent' for Serenity's own system files (SOUL, skills). "
            "User notes in subfolders are invisible to grep and will be lost."
        )

    async def execute(
        self,
        title: str,
        content: str,
        tags: str | None = None,
        subfolder: str | None = None,
        **kwargs: Any,
    ) -> str:
        today = date.today().isoformat()
        slug = _slugify(title)
        filename = f"{slug}.md"

        # Resolve destination folder, creating it if needed.
        # Guard against path traversal (e.g. subfolder="../../etc").
        if subfolder and subfolder.strip():
            dest_dir = (self._workspace / subfolder.strip()).resolve()
            if not str(dest_dir).startswith(str(self._workspace.resolve())):
                return f"Error: subfolder '{subfolder}' escapes the vault workspace — operation rejected."
        else:
            dest_dir = self._workspace

        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return f"Could not create subfolder '{subfolder}': {e}"

        path = dest_dir / filename

        tag_list = [t.strip() for t in tags.split(",")] if tags else ["memory"]
        tag_yaml = ", ".join(tag_list)

        # ── Deduplication: append to similar existing note rather than create new file ──
        # Only check in vault root (not subfolders like Agent/) to avoid false matches.
        _is_reflection = any(t in {"reflection", "session-review", "failure"} for t in tag_list)
        if not subfolder and not _is_reflection:
            similar = _find_similar_note(self._workspace, title)
            if similar and similar != path:
                # Append new content to the existing note under a dated section
                try:
                    existing = similar.read_text(encoding="utf-8")
                    updated = (
                        existing.rstrip()
                        + f"\n\n---\n\n## Update — {today}\n\n{content.strip()}\n"
                    )
                    similar.write_text(updated, encoding="utf-8")
                    # Re-index the updated note
                    try:
                        from serenity.agent.vault_index import index_note
                        index_note(similar, updated)
                    except Exception:
                        pass
                    return (
                        f"✅ UPDATED existing note `{similar.name}` (similar to '{title}') "
                        f"— appended {len(content)} chars rather than creating a duplicate."
                    )
                except Exception:
                    pass  # fall through to normal write if append fails
        # ─────────────────────────────────────────────────────────────────────

        note = (
            f"---\n"
            f"date: {today}\n"
            f"tags: [{tag_yaml}]\n"
            f"source: conversation\n"
            f"---\n\n"
            f"# {title}\n"
            f"*{today}*\n\n"
            f"{content.strip()}\n"
        )

        try:
            path.write_text(note, encoding="utf-8")
            rel = Path(subfolder.strip()) / filename if subfolder and subfolder.strip() else Path(filename)
            # Read back immediately to verify the write succeeded
            written = path.read_text(encoding="utf-8")

            # Index the note semantically so vault search finds it without grep
            try:
                from serenity.agent.vault_index import index_note
                index_note(path, written)
            except Exception:
                pass  # indexing failure must never break the write

            return (
                f"✅ SAVED: `{rel}` — {len(written)} chars written and verified.\n"
                f"Full path: {path}"
            )
        except Exception as e:
            return f"❌ FAILED: Could not write to vault: {e}"
