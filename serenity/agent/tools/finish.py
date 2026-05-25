"""FinishTool — the model's only voice when force_tool_use is active.

When force_tool_use=True, the provider is called with tool_choice="required",
meaning the model MUST emit a tool call on every turn.  It cannot produce
free text.  To actually respond to the user it must call finish(message="...").

This eliminates hallucinated narration entirely: there is no free-text output
channel for the model to write "the bot has moved..." into.  Every turn either
produces a real action tool call (vault_write, exec, etc.) or calls
finish() to deliver a verified, grounded response.
"""

from __future__ import annotations

from typing import Any

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import StringSchema, tool_parameters_schema

# Sentinel prefix so the runner can detect a finish call in tool results
# without needing a separate code path per tool name.
FINISH_PREFIX = "\x00FINISH\x00"


@tool_parameters(
    tool_parameters_schema(
        message=StringSchema(
            "Your response to the user. "
            "Only call this after you have completed all necessary tool calls "
            "and verified their results. "
            "Base your message ONLY on what tools actually returned — "
            "do not describe actions that are not confirmed in tool results."
        ),
        content=StringSchema(
            "Alias for message — accepted for compatibility with models that use "
            "'content' as the parameter name."
        ),
        required=[],  # Both optional — finish({}) is a valid 'done, no message' signal
    )
)
class FinishTool(Tool):
    """Send a response to the user.

    When structured tool-use mode is active you MUST call this tool to
    reply — you cannot produce free text.  Call all action tools first,
    read their results, then call finish() with a response based only on
    what those results say.

    Accepts either finish(message="...") or finish(content="...") or finish()
    with no arguments (treated as 'done, nothing to report').
    """

    @property
    def name(self) -> str:
        return "finish"

    @property
    def description(self) -> str:
        return (
            "Send your response to the user. "
            "Call this LAST, after all actions are done and results verified. "
            "Your message must be grounded in actual tool results — "
            "never describe an action as complete unless a tool result confirms it."
        )

    async def execute(self, message: str = "", content: str = "", **kwargs: Any) -> str:
        # Accept either 'message' or 'content' — some models (QWEN3/QWEN3.5) use 'content'.
        # finish({}) with no args is a valid "done" signal — returns empty message.
        text = message or content or ""
        # Prefix lets the runner identify this result without importing the tool class.
        return f"{FINISH_PREFIX}{text}"
