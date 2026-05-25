"""Session observation tool — lets Serenity annotate the activity log mid-task.

The activity log (activity.py) auto-captures tool calls at the loop level.
This tool gives the model a way to add its own observations, realisations,
and reflections directly into the same JSONL stream — so the distilled NNN
write at the end has richer material to work from.

Usage pattern (from SOUL.md):
  - Call session_observe after you notice something non-obvious.
  - Call it when you realise why something failed.
  - Call it when you spot a user preference or pattern.
  - The observation feeds the NNN distillation at session end.
"""

from __future__ import annotations

from typing import Any

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import StringSchema, tool_parameters_schema


@tool_parameters(
    tool_parameters_schema(
        text=StringSchema(
            "Your observation, realisation, or reflection. "
            "Be concise and specific — this becomes raw material for NNN distillation. "
            "Good: 'User prefers dark UIs — mentioned it twice and rejected light theme.' "
            "Bad: 'I did the task.' "
            "Write what you LEARNED, not what you DID."
        ),
        required=["text"],
    )
)
class SessionObserveTool(Tool):
    """Add a mid-task observation to the current session activity log."""

    # Module-level registry of current session key → filled by loop at turn start
    _current_session_key: str | None = None

    @classmethod
    def set_session(cls, session_key: str) -> None:
        """Called by the loop at the start of each turn to bind the session."""
        cls._current_session_key = session_key

    @property
    def name(self) -> str:
        return "session_observe"

    @property
    def description(self) -> str:
        return (
            "Record an observation or realisation into the current session log. "
            "Call this when you notice something worth remembering — a user preference, "
            "a pattern, the reason a tool failed, a decision you made and why. "
            "These observations feed the NNN distillation at session end. "
            "Do NOT use for routine status updates — only for genuine insights."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, text: str, **kwargs: Any) -> str:
        if not self._current_session_key:
            return "Session observe: no active session key — observation not recorded."
        try:
            from serenity.agent.activity import get_logger
            get_logger(self._current_session_key).observe(text)
            return "Observation recorded."
        except Exception as e:
            return f"Could not record observation: {e}"
