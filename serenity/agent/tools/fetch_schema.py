"""fetch_schema — meta-tool that activates an extended tool for the current turn.

When Serenity decides she needs a non-core tool (e.g. open_app, vault_write),
she calls this tool first.  The registry makes the full schema available and
the runner's next iteration includes it in the live tools list so she can
generate a valid call with the correct parameter names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from serenity.agent.tools.base import Tool, tool_parameters

if TYPE_CHECKING:
    from serenity.agent.tools.registry import ToolRegistry


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "Exact name of the extended tool to activate.",
            }
        },
        "required": ["tool_name"],
    }
)
class FetchSchemaTool(Tool):
    """Activate an extended tool so you can call it this turn.

    Use this when you decide you need a capability that is not currently in your
    active tool list (e.g. open_app, vault_write, exec, spotify_play).  After
    calling fetch_schema the tool will be available immediately — call it on
    the very next step without any further setup.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return "fetch_schema"

    @property
    def description(self) -> str:
        return (
            "Activate an extended tool so you can call it this turn. "
            "Use this when you decide you need a capability that is not currently in your "
            "active tool list (e.g. open_app, vault_write, exec, spotify_play).  After "
            "calling fetch_schema the tool will be available immediately — call it on "
            "the very next step without any further setup."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, tool_name: str, **_: Any) -> str:  # type: ignore[override]
        if self._registry.activate_tool(tool_name):
            schema = self._registry._tools[tool_name].to_schema()
            fn = schema.get("function", schema)
            params = fn.get("parameters", {})
            props = params.get("properties", {})
            required = params.get("required", [])
            param_summary = ", ".join(
                f"{k}{'*' if k in required else ''}" for k in props
            )
            return (
                f"Tool '{tool_name}' is now active. "
                f"Parameters: {param_summary or '(none)'}. "
                f"Call it directly on your next step."
            )

        # Not found — list what's available as extended tools
        extended = sorted(
            n for n in self._registry._tools
            if n not in self._registry._core_tools
        )
        return (
            f"Tool '{tool_name}' not found. "
            f"Available extended tools: {', '.join(extended) or '(none)'}."
        )
