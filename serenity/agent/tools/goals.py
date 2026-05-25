"""Goal stack — persistent long-lived goals that survive across sessions.

Serenity maintains a goal stack independently of user conversations. Goals
represent things she is working towards over days or weeks — not immediate
tasks. She checks and updates goals during heartbeat cycles and proactively
moves them forward without being reminded.

Storage
-------
  state/goals.json       — structured source of truth
  Agent/GOALS.md         — human-readable rendering, auto-injected into every
                           system prompt so Serenity always knows her goals

Tools
-----
  goal_add(title, description, priority)   — add a new goal
  goal_progress(goal_id, note)             — record progress on a goal
  goal_complete(goal_id, summary)          — mark a goal done
  goal_remove(goal_id)                     — drop a goal entirely
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import StringSchema, tool_parameters_schema

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


# ── helpers ───────────────────────────────────────────────────────────────────

def _goals_path(workspace: Path) -> Path:
    p = workspace / "state" / "goals.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _goals_md_path(workspace: Path) -> Path:
    p = workspace / "Agent" / "GOALS.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load(workspace: Path) -> dict:
    p = _goals_path(workspace)
    if not p.exists():
        return {"active": [], "completed": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"active": [], "completed": []}


def _save(workspace: Path, data: dict) -> None:
    _goals_path(workspace).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _render_md(workspace, data)


def _render_md(workspace: Path, data: dict) -> None:
    """Render goals.json → Agent/GOALS.md so it's auto-injected into context."""
    lines = [
        "# Goal Stack\n",
        "Serenity's active long-term goals. Updated automatically — never edit manually.\n",
    ]

    active = sorted(data.get("active", []), key=lambda g: _PRIORITY_ORDER.get(g.get("priority", "medium"), 1))

    if active:
        lines.append("## Active\n")
        for g in active:
            pri = g.get("priority", "medium").upper()
            lines.append(f"### [{g['id']}] {g['title']}  `{pri}`")
            lines.append(f"*Added: {g['created']}*")
            if g.get("description"):
                lines.append(f"\n{g['description']}")
            progress = g.get("progress", [])
            if progress:
                lines.append("\n**Progress:**")
                for entry in progress[-5:]:  # last 5 entries only
                    lines.append(f"- {entry}")
            lines.append("")
    else:
        lines.append("## Active\n\n*No active goals.*\n")

    completed = data.get("completed", [])
    if completed:
        lines.append("## Completed\n")
        for g in completed[-10:]:  # last 10
            lines.append(f"- [{g['id']}] **{g['title']}** — completed {g.get('completed_date', '?')}")
        lines.append("")

    _goals_md_path(workspace).write_text("\n".join(lines), encoding="utf-8")


def _find_active(data: dict, goal_id: str) -> dict | None:
    for g in data.get("active", []):
        if g["id"] == goal_id:
            return g
    return None


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ── goal_add ──────────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        title=StringSchema("Short name for the goal. Example: 'Learn Spanish by December'"),
        description=StringSchema(
            "What needs to happen for this goal to be complete. Be specific.",
            nullable=True,
        ),
        priority=StringSchema(
            "Goal priority: 'high', 'medium', or 'low'. Default is 'medium'.",
            nullable=True,
        ),
        required=["title"],
    )
)
class GoalAddTool(Tool):
    """Add a new long-term goal to the goal stack.

    Use this when the user mentions something they want to achieve over days
    or weeks, or when Serenity identifies something worth pursuing long-term.
    Goals are injected into every session so Serenity always remembers them.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "goal_add"

    @property
    def description(self) -> str:
        return (
            "Add a long-term goal to the goal stack. Use this for things the user wants "
            "to achieve over days or weeks — not immediate tasks. Goals persist across all "
            "sessions and are automatically shown in every conversation. "
            "Trigger: user mentions a future ambition, project, or objective."
        )

    async def execute(
        self,
        title: str,
        description: str | None = None,
        priority: str | None = None,
        **kwargs: Any,
    ) -> str:
        data = _load(self._workspace)
        # 8-char hex = 16^8 = 4 billion possible IDs — collision-safe
        existing_ids = {g["id"] for g in data.get("active", []) + data.get("completed", [])}
        for _ in range(10):
            goal_id = f"G{uuid.uuid4().hex[:8].upper()}"
            if goal_id not in existing_ids:
                break
        pri = (priority or "medium").lower()
        if pri not in _PRIORITY_ORDER:
            pri = "medium"

        goal = {
            "id": goal_id,
            "title": title.strip(),
            "description": (description or "").strip(),
            "priority": pri,
            "created": _today(),
            "progress": [],
        }
        data.setdefault("active", []).append(goal)
        _save(self._workspace, data)

        return f"Goal added: [{goal_id}] {title} (priority: {pri}). Now in GOALS.md."


# ── goal_progress ─────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        goal_id=StringSchema("The goal ID, e.g. 'G3A1F'"),
        note=StringSchema("What progress was made — be specific."),
        required=["goal_id", "note"],
    )
)
class GoalProgressTool(Tool):
    """Record progress on an active goal."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "goal_progress"

    @property
    def description(self) -> str:
        return (
            "Record progress on an active goal. Call this after doing work that advances "
            "a goal — even partial progress. Keeps the goal stack up to date across sessions."
        )

    async def execute(self, goal_id: str, note: str, **kwargs: Any) -> str:
        data = _load(self._workspace)
        goal = _find_active(data, goal_id.upper())
        if not goal:
            return f"No active goal with id '{goal_id}'. Use goal_add to create it first."

        entry = f"{_now_str()} — {note.strip()}"
        goal.setdefault("progress", []).append(entry)
        _save(self._workspace, data)

        return f"Progress recorded on [{goal_id}] {goal['title']}."


# ── goal_complete ─────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        goal_id=StringSchema("The goal ID to mark as complete."),
        summary=StringSchema("One sentence on what was achieved."),
        required=["goal_id", "summary"],
    )
)
class GoalCompleteTool(Tool):
    """Mark a goal as achieved and move it to the completed list."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "goal_complete"

    @property
    def description(self) -> str:
        return (
            "Mark a goal as complete. Call this when the goal has been fully achieved. "
            "Moves it from active to completed in the goal stack and updates GOALS.md."
        )

    async def execute(self, goal_id: str, summary: str, **kwargs: Any) -> str:
        data = _load(self._workspace)
        goal = _find_active(data, goal_id.upper())
        if not goal:
            return f"No active goal with id '{goal_id}'."

        data["active"] = [g for g in data["active"] if g["id"] != goal_id.upper()]
        goal["status"] = "complete"
        goal["summary"] = summary.strip()
        goal["completed_date"] = _today()
        data.setdefault("completed", []).append(goal)
        _save(self._workspace, data)

        return f"Goal [{goal_id}] '{goal['title']}' marked complete. Summary: {summary}"


# ── goal_remove ───────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        goal_id=StringSchema("The goal ID to remove."),
        reason=StringSchema("Why this goal is being dropped.", nullable=True),
        required=["goal_id"],
    )
)
class GoalRemoveTool(Tool):
    """Remove a goal from the stack entirely."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "goal_remove"

    @property
    def description(self) -> str:
        return (
            "Remove an active goal entirely. Use when the user explicitly drops a goal "
            "or when the goal is no longer relevant. Unlike goal_complete, this does not "
            "save a summary — the goal is simply removed."
        )

    async def execute(self, goal_id: str, reason: str | None = None, **kwargs: Any) -> str:
        data = _load(self._workspace)
        goal = _find_active(data, goal_id.upper())
        if not goal:
            return f"No active goal with id '{goal_id}'."

        data["active"] = [g for g in data["active"] if g["id"] != goal_id.upper()]
        _save(self._workspace, data)

        return f"Goal [{goal_id}] '{goal['title']}' removed. Reason: {reason or 'not specified'}."
