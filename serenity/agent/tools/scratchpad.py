"""Scratchpad — Serenity's working memory for any multi-step task.

When Serenity starts a task she opens a scratchpad and writes her plan,
predictions, and observations as she works. At the end she closes it with
a summary that gets archived to the Vault and distilled into NNN.

This gives her genuine state-persistence within a task — she can read back
exactly where she was, what she predicted, and what actually happened — and
the archive means the user can read every thinking session in Vault Memories.

Three tools:
  scratchpad_write(section, content)  — append a plan / prediction / outcome / note
  scratchpad_read()                   — recall full working memory for this task
  scratchpad_close(summary, task_name) — archive to Vault, distil to NNN, clear

The active scratchpad lives at:
  Vault Memories/scratch/active.md

Archives land at:
  Vault Memories/scratch/archive/YYYY-MM-DD-HHMMSS-{task_name}.md
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import StringSchema, tool_parameters_schema

_VAULT_DIR    = Path(__file__).resolve().parent.parent.parent.parent / "Vault Memories"
_SCRATCH_DIR  = _VAULT_DIR / "scratch"
_ARCHIVE_DIR  = _SCRATCH_DIR / "archive"
_ACTIVE_FILE  = _SCRATCH_DIR / "active.md"
_SUMMARY_FILE = _SCRATCH_DIR / "SUMMARY.md"   # compact last-session summary, always overwritten

_VALID_SECTIONS = {"plan", "prediction", "outcome", "observation", "note", "reasoning", "position", "hypothesis", "error", "learning"}


# ── Write ─────────────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        section=StringSchema(
            "What kind of entry this is. Use one of: plan, prediction, outcome, "
            "observation, note, reasoning, position, hypothesis, error, learning. "
            "Example: 'plan' when starting a task, 'outcome' after an action, "
            "'position' to log where you are in a game or workflow."
        ),
        content=StringSchema(
            "What to write. Be specific and concrete — include exact values, "
            "coordinates, action names, results. This is your working memory."
        ),
        required=["section", "content"],
    )
)
class ScratchpadWriteTool(Tool):
    """Append a plan, prediction, outcome, or note to your active working memory scratchpad.

    Call this at the START of any multi-step task to write your plan,
    and after each action to record what happened vs what you predicted.
    Reading it back with scratchpad_read() gives you full task context
    without relying on conversation history.
    """

    @property
    def name(self) -> str:
        return "scratchpad_write"

    @property
    def description(self) -> str:
        return (
            "Append a section to your active working-memory scratchpad. "
            "Use this to record plans, predictions, outcomes, and observations "
            "as you work through a multi-step task. "
            "Read it back with scratchpad_read() to recall your full task state."
        )

    async def execute(self, section: str = "", content: str = "", **_: Any) -> str:
        section = section.strip().lower() or "note"
        content = content.strip()
        if not content:
            return "Nothing written — content was empty."

        _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%H:%M:%S")
        label = section.upper()

        # First write: add a header
        if not _ACTIVE_FILE.exists():
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            header = f"# Serenity Working Memory\nStarted: {date_str}\n\n---\n"
            _ACTIVE_FILE.write_text(header, encoding="utf-8")

        entry = f"\n### [{timestamp}] {label}\n{content}\n"
        with open(_ACTIVE_FILE, "a", encoding="utf-8") as f:
            f.write(entry)

        # Count sections so far
        try:
            total = _ACTIVE_FILE.read_text(encoding="utf-8").count("### [")
        except Exception:
            total = 1

        return f"Scratchpad updated: [{label}] written. ({total} entries total)"


# ── Read ──────────────────────────────────────────────────────────────────────

@tool_parameters(tool_parameters_schema(required=[]))
class ScratchpadReadTool(Tool):
    """Read your full active working-memory scratchpad.

    Call this whenever you need to recall your plan, check what you already
    tried, or resume a task after a break. Returns everything you have written
    since the scratchpad was opened.
    """

    @property
    def name(self) -> str:
        return "scratchpad_read"

    @property
    def description(self) -> str:
        return (
            "Read your active working-memory scratchpad. "
            "Reads the compact SUMMARY first (last session's distilled key points). "
            "If you need the full step-by-step log, pass full=true. "
            "Essential for picking up a multi-step task without losing state."
        )

    async def execute(self, full: bool = False, **_: Any) -> str:
        parts = []

        # Always show summary first if it exists — cheap, compact, cross-session
        if _SUMMARY_FILE.exists():
            summary_text = _SUMMARY_FILE.read_text(encoding="utf-8").strip()
            if summary_text:
                parts.append(
                    "=== LAST SESSION SUMMARY (compact) ===\n"
                    f"{summary_text}\n"
                    "=== END SUMMARY ===\n"
                    "Call scratchpad_read(full=True) to see the full step-by-step log if stuck."
                )

        if full or not parts:
            # Full active log
            if not _ACTIVE_FILE.exists():
                if parts:
                    return "\n\n".join(parts)
                return (
                    "No active scratchpad. "
                    "Start one with scratchpad_write(section='plan', content='...')"
                )
            content = _ACTIVE_FILE.read_text(encoding="utf-8").strip()
            if content:
                lines = content.count("\n") + 1
                entries = content.count("### [")
                parts.append(
                    f"=== ACTIVE SCRATCHPAD ({entries} entries, {lines} lines) ===\n\n"
                    f"{content}\n\n"
                    "=== END SCRATCHPAD ===\n"
                    "Use scratchpad_close(summary=...) when the task is done."
                )

        return "\n\n".join(parts) if parts else "Scratchpad is empty."


# ── Close ─────────────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        summary=StringSchema(
            "A concise summary of what happened and what was learned. "
            "This gets stored to NNN in causal format and archived with the scratchpad. "
            "Include: what the task was, what approach worked, what failed and why, "
            "and the single most important thing to remember next time."
        ),
        task_name=StringSchema(
            "Short name for the archive filename. No spaces — use hyphens. "
            "Example: 'telegram-reply' or 'research-quantum'. "
            "Defaults to 'task' if omitted."
        ),
        required=["summary"],
    )
)
class ScratchpadCloseTool(Tool):
    """Archive the active scratchpad, store the summary to NNN, and clear for the next task.

    Call this when a task is complete (win, loss, or give up).
    The full scratchpad is saved to Vault Memories/scratch/archive/ so the
    user can read every thinking session. The summary is distilled to NNN so
    future attempts benefit from this one.
    """

    @property
    def name(self) -> str:
        return "scratchpad_close"

    @property
    def description(self) -> str:
        return (
            "Archive the active scratchpad to Vault and store a summary to NNN. "
            "Call this when a task finishes — win, loss, or abandon. "
            "The full thinking log is saved to Vault Memories/scratch/archive/ "
            "and the summary is distilled into long-term NNN memory."
        )

    async def execute(self, summary: str = "", task_name: str = "task", **_: Any) -> str:
        summary   = summary.strip()
        task_name = (task_name.strip().replace(" ", "-") or "task")[:40]

        if not summary:
            return "Nothing archived — summary was empty. Provide a summary of what happened."

        # Archive even if no active file (summary still goes to NNN)
        archived_path = None
        if _ACTIVE_FILE.exists():
            _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            archive_path = _ARCHIVE_DIR / f"{date_str}-{task_name}.md"

            content = _ACTIVE_FILE.read_text(encoding="utf-8")
            archived_content = (
                f"{content}\n\n---\n\n"
                f"## Summary\n{summary}\n\n"
                f"*Archived: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n"
            )
            archive_path.write_text(archived_content, encoding="utf-8")
            _ACTIVE_FILE.unlink()
            archived_path = archive_path.name

        # Always write/overwrite SUMMARY.md — compact single-file cross-session recall
        # Next session reads this first (~200 tokens) instead of hunting through archives
        _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        _SUMMARY_FILE.write_text(
            f"# Last Session Summary\n"
            f"Task: {task_name} | Archived: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"{summary}\n\n"
            f"*(Full log in scratch/archive/{archived_path or 'N/A'} — read with scratchpad_read(full=True) if stuck)*\n",
            encoding="utf-8",
        )

        # Distil to NNN
        nnn_result = ""
        try:
            from serenity.agent.tools.nnn import _get_nnn_fns
            import asyncio as _asyncio
            _, encode_fn, _ = _get_nnn_fns()
            if encode_fn:
                await _asyncio.wait_for(
                    _asyncio.get_running_loop().run_in_executor(
                        None, encode_fn, summary, {"source": f"scratchpad:{task_name}"}
                    ),
                    timeout=60,
                )
                nnn_result = " Summary stored to NNN."
        except Exception as e:
            nnn_result = f" (NNN store skipped: {e})"

        if archived_path:
            return (
                f"Scratchpad archived to scratch/archive/{archived_path}.{nnn_result} "
                f"Active scratchpad cleared."
            )
        return f"Summary stored to NNN (no active scratchpad file found).{nnn_result}"
