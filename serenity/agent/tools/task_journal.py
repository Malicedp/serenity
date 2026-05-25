"""Task journal — persistent task state stored in the Obsidian vault.

Serenity writes her plan, step completions, decisions, logs, and captured
tool output here so she can resume any multi-step task across restarts.

Vault layout
------------
  tasks/task-<id>.md          — live task file (plan, log, decisions, captures)
  tasks/_active.txt           — pointer to the current in-progress task filename
  tasks/summaries/<id>.md     — auto-written when a task completes (searchable)
  Decisions Index.md          — running cross-task decisions log in vault root

Tools
-----
  task_start(goal, steps)               — begin a new task, write the plan
  task_step(step, status, notes, output)— tick off a step, optionally capture output
  task_decide(decision, reason)         — log a decision (also appended to Decisions Index)
  task_complete(summary, lessons)       — mark done, write searchable summary note
  task_status()                         — read back the full active task state
  task_capture(title, content, type)    — store code/exec output mid-task
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema

# File extensions that are images / binaries — never stored as content
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".svg"}
_MAX_CAPTURE_CHARS = 8_000  # cap stored output to keep notes readable


# ── helpers ───────────────────────────────────────────────────────────────────

def _tasks_dir(workspace: Path) -> Path:
    d = workspace / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _summaries_dir(workspace: Path) -> Path:
    d = _tasks_dir(workspace) / "summaries"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _active_pointer(workspace: Path) -> Path:
    return _tasks_dir(workspace) / "_active.txt"


def _get_active_path(workspace: Path) -> Path | None:
    ptr = _active_pointer(workspace)
    if not ptr.exists():
        return None
    name = ptr.read_text(encoding="utf-8").strip()
    if not name:
        return None
    p = _tasks_dir(workspace) / name
    return p if p.exists() else None


def _set_active(workspace: Path, filename: str) -> None:
    _active_pointer(workspace).write_text(filename, encoding="utf-8")


def _clear_active(workspace: Path) -> None:
    ptr = _active_pointer(workspace)
    if ptr.exists():
        ptr.unlink()


def _check_and_expire(workspace: Path) -> str | None:
    """If the active task is past its due date, delete it and return an expiry message.

    Returns a human-readable string if the task was expired, None if still valid or no task.
    """
    path = _get_active_path(workspace)
    if path is None:
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    due_str = _extract_frontmatter_field(content, "due")
    if not due_str:
        return None  # no deadline set — never expires

    try:
        # Strip timezone suffix for compatibility with Python < 3.11
        # Tasks are always stored as naive local datetimes — TZ suffix only
        # appears if someone manually edited the frontmatter.
        due_str_clean = due_str.split("+")[0].split("Z")[0].strip()
        due_dt = datetime.fromisoformat(due_str_clean)
    except ValueError:
        return None

    if datetime.now() < due_dt:
        return None  # still within deadline

    # Expired — archive it
    goal = _extract_frontmatter_field(content, "goal")
    task_id = _extract_frontmatter_field(content, "task_id")

    # Mark as expired in the file before archiving
    expired_content = re.sub(r"^status: in_progress", "status: expired", content, flags=re.MULTILINE)
    expired_content = expired_content.rstrip() + f"\n\n## Expired\n{_today()} {_now()} — Task expired (deadline: {due_str})\n"
    path.write_text(expired_content, encoding="utf-8")
    _clear_active(workspace)

    return (
        f"Task '{goal}' (id: {task_id}) expired at {due_str} and has been removed. "
        "Tell the user their task has expired."
    )


def _now() -> str:
    return datetime.now().strftime("%H:%M")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_active_task(workspace: Path) -> str | None:
    """Return the full markdown content of the active task, or None.

    Automatically expires the task if it is past its due date.
    """
    expired_msg = _check_and_expire(workspace)
    if expired_msg:
        return f"[TASK EXPIRED] {expired_msg}"
    path = _get_active_path(workspace)
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _append_to_decisions_index(workspace: Path, goal: str, task_id: str, decision: str, reason: str) -> None:
    """Append a decision to the cross-task Decisions Index in the vault root."""
    index_path = workspace / "Decisions Index.md"
    entry = f"- {_today()} {_now()} | **{decision}** — {reason} *(task: {goal[:50]}, id: {task_id})*\n"
    try:
        if not index_path.exists():
            index_path.write_text(
                "# Decisions Index\n\nCross-task log of every decision Serenity has made.\n\n",
                encoding="utf-8",
            )
        with index_path.open("a", encoding="utf-8") as f:
            f.write(entry)
    except OSError:
        pass


def _extract_frontmatter_field(content: str, field: str) -> str:
    """Extract a field value from YAML frontmatter."""
    m = re.search(rf"^{field}:\s*(.+)$", content, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _extract_decisions(content: str) -> str:
    """Pull the ## Decisions block out of a task file."""
    m = re.search(r"## Decisions\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    return m.group(1).strip() if m else ""


# ── task_start ────────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        goal=StringSchema("Clear one-sentence description of what needs to be achieved."),
        steps=StringSchema(
            "Numbered plan as a newline-separated list, e.g.:\n"
            "1. Create the file structure\n2. Write the code\n3. Test it\n4. Deploy"
        ),
        due_hours=IntegerSchema(
            "Hours from now until this task expires and is auto-deleted if not completed. "
            "Default is 48 hours. Set lower for urgent tasks, higher for long research tasks.",
            nullable=True,
        ),
        required=["goal", "steps"],
    )
)
class TaskStartTool(Tool):
    """Begin a new multi-step task and write the plan to the vault."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "task_start"

    @property
    def description(self) -> str:
        return (
            "Start a new multi-step task. Write the goal and plan to the vault so progress "
            "survives restarts. Call this FIRST before doing any work on a task that needs "
            "more than 2-3 steps. Set due_hours based on how long the task should take — "
            "tasks that are not completed by the deadline are automatically deleted. "
            "Returns the task ID and deadline."
        )

    async def execute(self, goal: str, steps: str, due_hours: int | None = None, **kwargs: Any) -> str:
        task_id = uuid.uuid4().hex[:8]
        filename = f"task-{task_id}.md"

        hours = max(1, int(due_hours or 48))
        due_dt = datetime.now() + timedelta(hours=hours)
        due_str = due_dt.isoformat(timespec="seconds")

        step_lines = [s.strip() for s in steps.strip().splitlines() if s.strip()]
        _strip_num = lambda line: re.sub(r'^\d+[.)]\s*', '', line)
        checklist = "\n".join(f"- [ ] {_strip_num(line)}" for line in step_lines)

        content = (
            f"---\n"
            f"task_id: {task_id}\n"
            f"goal: {goal}\n"
            f"status: in_progress\n"
            f"created: {_ts()}\n"
            f"due: {due_str}\n"
            f"---\n\n"
            f"# Goal\n{goal}\n\n"
            f"## Plan\n{checklist}\n\n"
            f"## Log\n- {_today()} {_now()} — Task started (deadline: {due_dt.strftime('%Y-%m-%d %H:%M')})\n\n"
            f"## Decisions\n\n"
            f"## Captures\n"
        )

        path = _tasks_dir(self._workspace) / filename
        path.write_text(content, encoding="utf-8")
        _set_active(self._workspace, filename)

        return (
            f"Task {task_id} started. Deadline: {due_dt.strftime('%Y-%m-%d %H:%M')} "
            f"({hours}h from now). Plan written to tasks/{filename}."
        )


# ── task_step ─────────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        step=StringSchema("The step description (must match or closely match one line from the plan)."),
        status=StringSchema('Either "done" or "failed".'),
        notes=StringSchema("Brief note on what happened, what was created, or why it failed.", nullable=True),
        output=StringSchema(
            "Optional: paste relevant exec output, file paths created, or short code snippet. "
            "Do NOT paste image paths or binary content. Capped at 8000 chars.",
            nullable=True,
        ),
        required=["step", "status"],
    )
)
class TaskStepTool(Tool):
    """Mark a task step as done or failed, log it, and optionally store output."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "task_step"

    @property
    def description(self) -> str:
        return (
            "Mark a step in the active task as done or failed. "
            "Call this after EVERY completed step. "
            "Use 'output' to store relevant exec output, file paths, or code snippets — "
            "never image paths or binary data. status must be 'done' or 'failed'."
        )

    async def execute(
        self,
        step: str,
        status: str,
        notes: str | None = None,
        output: str | None = None,
        **kwargs: Any,
    ) -> str:
        path = _get_active_path(self._workspace)
        if path is None:
            return "No active task. Call task_start() first."

        content = path.read_text(encoding="utf-8")
        status = status.lower().strip()
        tick = "x" if status == "done" else "!"

        # Tick the checklist item
        needle = step.lower()
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if re.match(r"- \[ \]", line) and needle[:30] in line.lower():
                lines[i] = re.sub(r"- \[ \]", f"- [{tick}]", line, count=1)
                break

        # Build log entry
        log_entry = f"- {_today()} {_now()} — {'✓' if status == 'done' else '✗'} {step}"
        if notes:
            log_entry += f" — {notes}"

        # Append log entry before next ## section
        new_lines = []
        in_log = False
        inserted = False
        for line in lines:
            new_lines.append(line)
            if line.strip() == "## Log":
                in_log = True
            elif in_log and not inserted and line.startswith("## ") and line.strip() != "## Log":
                new_lines.insert(-1, log_entry)
                inserted = True
                in_log = False
        if not inserted:
            new_lines.append(log_entry)

        # Append output to ## Captures if provided
        if output:
            safe_output = _sanitise_output(output)
            if safe_output:
                capture_block = (
                    f"\n### {step[:60]} ({_today()} {_now()})\n"
                    f"```\n{safe_output}\n```\n"
                )
                full = "\n".join(new_lines)
                if "## Captures" in full:
                    full = full.rstrip() + "\n" + capture_block
                else:
                    full = full.rstrip() + "\n\n## Captures\n" + capture_block
                path.write_text(full, encoding="utf-8")
                return f"Step marked {status} with output captured."

        path.write_text("\n".join(new_lines), encoding="utf-8")
        return f"Step marked {status}."


# ── task_decide ───────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        decision=StringSchema("What you decided to do."),
        reason=StringSchema("Why you made this choice."),
        required=["decision", "reason"],
    )
)
class TaskDecideTool(Tool):
    """Log a decision made during the task — also appended to the cross-task Decisions Index."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "task_decide"

    @property
    def description(self) -> str:
        return (
            "Log a decision made during the active task. "
            "Call this when you make a non-obvious choice — library picked, "
            "approach chosen, something skipped and why. "
            "Saved in the task file AND the vault-wide Decisions Index."
        )

    async def execute(self, decision: str, reason: str, **kwargs: Any) -> str:
        path = _get_active_path(self._workspace)
        if path is None:
            return "No active task. Call task_start() first."

        content = path.read_text(encoding="utf-8")
        goal = _extract_frontmatter_field(content, "goal")
        task_id = _extract_frontmatter_field(content, "task_id")

        entry = f"- {_today()} {_now()} — **{decision}** — {reason}"

        if "## Decisions" in content:
            content = content.rstrip() + f"\n{entry}\n"
        else:
            content = content.rstrip() + f"\n\n## Decisions\n{entry}\n"

        path.write_text(content, encoding="utf-8")

        # Also append to vault-wide Decisions Index
        _append_to_decisions_index(self._workspace, goal, task_id, decision, reason)

        return "Decision logged in task file and Decisions Index."


# ── task_complete ─────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        summary=StringSchema("One paragraph summary of what was accomplished."),
        lessons=StringSchema(
            "What worked, what didn't, and what to do differently next time. "
            "Be specific — this is what Serenity will read before starting similar tasks.",
            nullable=True,
        ),
        required=["summary"],
    )
)
class TaskCompleteTool(Tool):
    """Mark the active task complete and write a searchable summary note to the vault."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "task_complete"

    @property
    def description(self) -> str:
        return (
            "Mark the active task as complete. Call this when the goal is fully achieved. "
            "Writes a searchable summary note to tasks/summaries/ so future similar tasks "
            "can learn from this one. Include lessons — what worked and what didn't."
        )

    async def execute(self, summary: str, lessons: str | None = None, **kwargs: Any) -> str:
        path = _get_active_path(self._workspace)
        if path is None:
            return "No active task to complete."

        content = path.read_text(encoding="utf-8")
        goal = _extract_frontmatter_field(content, "goal")
        task_id = _extract_frontmatter_field(content, "task_id")
        decisions = _extract_decisions(content)

        # Update the task file
        content = re.sub(r"^status: in_progress", "status: complete", content, flags=re.MULTILINE)
        completion_block = (
            f"\n\n## Completed\n{_today()} {_now()}\n\n"
            f"{summary.strip()}\n"
        )
        if lessons:
            completion_block += f"\n### Lessons\n{lessons.strip()}\n"
        completion_block += f"\n- {_today()} {_now()} — ✅ Task completed\n"
        content = content.rstrip() + completion_block
        path.write_text(content, encoding="utf-8")
        _clear_active(self._workspace)

        # Write searchable summary note to tasks/summaries/
        slug = re.sub(r"[^\w\s-]", "", goal)[:40].strip().replace(" ", "-").lower()
        summary_filename = f"{_today()}-{slug}-{task_id}.md"
        summary_content = (
            f"---\n"
            f"task_id: {task_id}\n"
            f"goal: {goal}\n"
            f"status: complete\n"
            f"date: {_today()}\n"
            f"tags: [task-summary, completed]\n"
            f"---\n\n"
            f"# {goal}\n\n"
            f"## What was done\n{summary.strip()}\n\n"
        )
        if lessons:
            summary_content += f"## Lessons learned\n{lessons.strip()}\n\n"
        if decisions:
            summary_content += f"## Key decisions\n{decisions}\n\n"
        summary_content += f"*Completed {_today()} {_now()} — full log: tasks/{path.name}*\n"

        summary_path = _summaries_dir(self._workspace) / summary_filename
        summary_path.write_text(summary_content, encoding="utf-8")

        return (
            f"Task complete. Summary saved to tasks/summaries/{summary_filename}. "
            f"Full log: tasks/{path.name}"
        )


# ── task_capture ──────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        title=StringSchema("Short title describing what this is, e.g. 'Bot main.py', 'npm install output'."),
        content=StringSchema(
            "The content to store — code, exec output, file paths, terminal output. "
            "Do NOT paste image file paths or binary data."
        ),
        capture_type=StringSchema(
            'Type of content: "code", "output", "paths", or "other".',
            nullable=True,
        ),
        required=["title", "content"],
    )
)
class TaskCaptureTool(Tool):
    """Store code, exec output, or file paths produced during the active task.

    Use this mid-task to save anything worth keeping — the code that was written,
    the output of a shell command, the paths of files that were created.
    Screenshots and images are blocked. Content is capped at 8000 chars.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "task_capture"

    @property
    def description(self) -> str:
        return (
            "Capture code, exec output, or file paths produced during the active task. "
            "Stored in the task file under ## Captures. "
            "Do NOT use for screenshots or image paths — text only. "
            "Types: 'code', 'output', 'paths', 'other'."
        )

    async def execute(
        self,
        title: str,
        content: str,
        capture_type: str | None = None,
        **kwargs: Any,
    ) -> str:
        path = _get_active_path(self._workspace)
        if path is None:
            return "No active task. Call task_start() first."

        safe = _sanitise_output(content)
        if not safe:
            return "Content was empty or contained only image/binary references — nothing stored."

        ctype = (capture_type or "other").lower()
        lang = {"code": "python", "output": "", "paths": "", "other": ""}.get(ctype, "")
        fence_open = f"```{lang}" if lang else "```"

        capture_block = (
            f"\n### {title} ({_today()} {_now()})\n"
            f"{fence_open}\n{safe}\n```\n"
        )

        task_content = path.read_text(encoding="utf-8")
        if "## Captures" in task_content:
            task_content = task_content.rstrip() + "\n" + capture_block
        else:
            task_content = task_content.rstrip() + "\n\n## Captures\n" + capture_block

        path.write_text(task_content, encoding="utf-8")
        return f"Captured '{title}' ({len(safe)} chars) in task file."


# ── task_status ───────────────────────────────────────────────────────────────

@tool_parameters(tool_parameters_schema())
class TaskStatusTool(Tool):
    """Read back the full state of the active task."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "task_status"

    @property
    def description(self) -> str:
        return (
            "Read back the active task — goal, plan, log, decisions, and captures. "
            "Call this at the start of any session where a task might be in progress."
        )

    async def execute(self, **kwargs: Any) -> str:
        content = read_active_task(self._workspace)
        if content is None:
            return "No active task."
        return content


# ── task_cancel ───────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        reason=StringSchema(
            "Why the task is being cancelled, e.g. 'user asked to stop', 'goal changed'.",
            nullable=True,
        ),
        required=[],
    )
)
class TaskCancelTool(Tool):
    """Cancel and remove the active task immediately.

    Call this when the user says any of:
      "stop the task", "cancel the task", "drop the task", "forget the task",
      "stop what you're doing", "abort", "never mind the task", "scrap it"
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "task_cancel"

    @property
    def description(self) -> str:
        return (
            "Cancel and remove the active task. Call this when the user tells you to stop, "
            "cancel, drop, abort, or forget the current task. "
            "Trigger phrases: 'stop the task', 'cancel the task', 'drop it', 'forget the task', "
            "'stop what you're doing', 'abort', 'never mind', 'scrap it'."
        )

    async def execute(self, reason: str | None = None, **kwargs: Any) -> str:
        path = _get_active_path(self._workspace)
        if path is None:
            return "No active task to cancel."

        content = path.read_text(encoding="utf-8")
        goal = _extract_frontmatter_field(content, "goal")
        task_id = _extract_frontmatter_field(content, "task_id")

        # Mark as cancelled in the file
        cancelled_content = re.sub(r"^status: in_progress", "status: cancelled", content, flags=re.MULTILINE)
        note = reason or "user requested cancellation"
        cancelled_content = cancelled_content.rstrip() + f"\n\n## Cancelled\n{_today()} {_now()} — {note}\n"
        path.write_text(cancelled_content, encoding="utf-8")
        _clear_active(self._workspace)

        return f"Task '{goal}' (id: {task_id}) cancelled and removed. Reason: {note}."


# ── output sanitiser ──────────────────────────────────────────────────────────

def _sanitise_output(text: str) -> str:
    """Strip image/binary file references and cap length."""
    if not text:
        return ""
    # Remove lines that are just image/binary paths
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            ext = Path(stripped).suffix.lower()
            if ext in _IMAGE_EXTENSIONS:
                lines.append(f"[image omitted: {Path(stripped).name}]")
                continue
        lines.append(line)
    result = "\n".join(lines).strip()
    if len(result) > _MAX_CAPTURE_CHARS:
        result = result[:_MAX_CAPTURE_CHARS] + f"\n... [truncated at {_MAX_CAPTURE_CHARS} chars]"
    return result
