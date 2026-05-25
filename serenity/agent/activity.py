"""Activity logger — per-session JSONL audit trail.

Every conversation session gets its own JSONL file under
~/.serenity/activity/{session_key}.jsonl.

Each line is a JSON event:
  {"ts": "...", "event": "turn_start",  "msg": "..."}
  {"ts": "...", "event": "tool",        "name": "vault_write", "args": {...}}
  {"ts": "...", "event": "observe",     "text": "..."}
  {"ts": "...", "event": "turn_end",    "tools": [...], "resp_len": 412}

The log is the raw material for:
  1. NNN distillation — model reads tail and distils causal principle
  2. Self-reflection — periodic agent pass synthesises what was learnt
  3. Session replay — audit / debugging

get_logger(session_key) returns a singleton ActivityLogger per session_key,
safe to call from anywhere in the process.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Directory ────────────────────────────────────────────────────────────────
_ACTIVITY_DIR = Path.home() / ".serenity" / "activity"

# ── Per-session singletons ────────────────────────────────────────────────────
_loggers: dict[str, "ActivityLogger"] = {}


def get_logger(session_key: str) -> "ActivityLogger":
    """Return (or lazily create) the ActivityLogger for *session_key*."""
    if session_key not in _loggers:
        _loggers[session_key] = ActivityLogger(session_key)
    return _loggers[session_key]


# ── Core class ────────────────────────────────────────────────────────────────

class ActivityLogger:
    """Append-only JSONL log for one conversation session.

    All writes are synchronous (via a normal file open) and non-blocking
    from the caller's perspective — the OS buffers the write.  Failures
    are silently swallowed so the log never interrupts the agent loop.
    """

    # Keep args/text bounded so the log stays scannable.
    _MAX_ARG_CHARS  = 200
    _MAX_MSG_CHARS  = 400
    _MAX_OBS_CHARS  = 800
    _MAX_RESP_CHARS = 300

    def __init__(self, session_key: str) -> None:
        _ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
        # Sanitise session_key → safe filename
        safe = (
            session_key
            .replace(":", "_")
            .replace("/", "_")
            .replace("\\", "_")
            .replace(" ", "_")
        )[:80]
        self._path = _ACTIVITY_DIR / f"{safe}.jsonl"

    # ── Write helpers ─────────────────────────────────────────────────────────

    def _write(self, event: dict[str, Any]) -> None:
        event["ts"] = datetime.now().isoformat(timespec="seconds")
        line = json.dumps(event, ensure_ascii=False) + "\n"
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass  # non-fatal — log is best-effort

    # ── Public API ────────────────────────────────────────────────────────────

    def turn_start(self, message: str) -> None:
        """Called at the start of each user turn."""
        self._write({"event": "turn_start", "msg": message[: self._MAX_MSG_CHARS]})

    def tool_calls(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        """Log all tool calls for one iteration.

        *calls* is a list of (tool_name, arguments_dict) pairs extracted
        from the LLM's tool_calls before execution.
        """
        for name, args in calls:
            safe_args: dict[str, Any] = {}
            for k, v in args.items():
                s = str(v)
                safe_args[k] = s[: self._MAX_ARG_CHARS] if len(s) > self._MAX_ARG_CHARS else v
            self._write({"event": "tool", "name": name, "args": safe_args})

    def observe(self, text: str) -> None:
        """Record a mid-session observation written by the agent."""
        self._write({"event": "observe", "text": text[: self._MAX_OBS_CHARS]})

    def turn_end(self, tools_used: list[str], response: str) -> None:
        """Called after the agent loop finishes a turn."""
        self._write({
            "event":    "turn_end",
            "tools":    tools_used,
            "response": response[: self._MAX_RESP_CHARS],
        })

    # ── Read helpers ──────────────────────────────────────────────────────────

    def tail(self, n: int = 40) -> list[dict[str, Any]]:
        """Return the last *n* log events as parsed dicts (oldest first)."""
        try:
            raw = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        events: list[dict[str, Any]] = []
        for line in raw[-n:]:
            try:
                events.append(json.loads(line))
            except Exception:
                pass
        return events

    def tail_text(self, n: int = 40) -> str:
        """Return the last *n* log events as a compact human-readable string."""
        events = self.tail(n)
        if not events:
            return ""
        parts: list[str] = []
        for e in events:
            ts   = e.get("ts", "")
            kind = e.get("event", "?")
            if kind == "turn_start":
                parts.append(f"[{ts}] USER: {e.get('msg', '')}")
            elif kind == "tool":
                args = e.get("args", {})
                brief = ", ".join(f"{k}={repr(v)[:60]}" for k, v in list(args.items())[:3])
                parts.append(f"[{ts}] TOOL: {e.get('name', '?')}({brief})")
            elif kind == "observe":
                parts.append(f"[{ts}] OBSERVE: {e.get('text', '')}")
            elif kind == "turn_end":
                tools = ", ".join(e.get("tools", [])) or "none"
                parts.append(f"[{ts}] DONE — tools: {tools}")
            else:
                parts.append(f"[{ts}] {kind}")
        return "\n".join(parts)

    @property
    def path(self) -> Path:
        return self._path
