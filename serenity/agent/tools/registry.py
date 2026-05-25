"""Tool registry for dynamic tool management."""

import re
from typing import Any

from serenity.agent.tools.base import Tool


def _extract_hint(description: str) -> str:
    """Auto-extract first sentence from a tool description as a compact hint."""
    text = description.strip()
    # Stop at first sentence-ending punctuation followed by whitespace or end
    match = re.search(r"[.!?](?:\s|$)", text)
    if match and match.start() > 0 and match.start() <= 120:
        return text[: match.start()].strip()
    # Fall back to first newline
    nl = text.find("\n")
    if 0 < nl <= 120:
        return text[:nl].strip()
    # Last resort: truncate to 100 chars
    return text[:100].strip()


class ToolRegistry:
    """
    Registry for agent tools.

    Supports a two-tier model:
    - Core tools (core=True): full schemas always injected into every LLM call.
    - Extended tools (core=False): only a compact one-liner hint is always present;
      full schema is injected on demand when activate_tool() is called (e.g. via
      the fetch_schema meta-tool).

    Call reset_turn() at the start of each user turn to clear dynamic activations.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._core_tools: set[str] = set()          # always full schema
        self._active_tools: set[str] = set()         # activated this turn by fetch_schema
        self._cached_definitions: list[dict[str, Any]] | None = None

    def register(self, tool: Tool, *, core: bool = True) -> None:
        """Register a tool. core=True → always full schema; core=False → hint only until activated."""
        self._tools[tool.name] = tool
        if core:
            self._core_tools.add(tool.name)
        else:
            self._core_tools.discard(tool.name)
        self._cached_definitions = None

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
        self._core_tools.discard(name)
        self._active_tools.discard(name)
        self._cached_definitions = None

    def activate_tool(self, name: str) -> bool:
        """Make a non-core tool's full schema available for the current turn.

        Returns True if the tool was found, False otherwise.
        Called by FetchSchemaTool during execution so the runner's next
        iteration picks up the new schema automatically.
        """
        if name in self._tools:
            self._active_tools.add(name)
            self._cached_definitions = None
            return True
        return False

    def reset_turn(self) -> None:
        """Clear dynamically activated tools. Call at the start of each user turn."""
        if self._active_tools:
            self._active_tools.clear()
            self._cached_definitions = None

    def get_compact_manifest(self) -> str:
        """Return a compact manifest of all non-core, non-active tools.

        Each line is ``- tool_name: first-sentence-of-description``.
        Returns an empty string if every tool is already core/active.
        """
        lines: list[str] = []
        for name in sorted(self._tools):
            if name not in self._core_tools and name not in self._active_tools:
                hint = _extract_hint(self._tools[name].description)
                lines.append(f"- {name}: {hint}")
        if not lines:
            return ""
        return (
            "Extended tools available on demand — call fetch_schema(tool_name) to activate:\n"
            + "\n".join(lines)
        )

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        """Extract a normalized tool name from either OpenAI or flat schemas."""
        fn = schema.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str):
                return name
        name = schema.get("name")
        return name if isinstance(name, str) else ""

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions for the current turn.

        Only returns schemas for core tools and tools activated this turn via
        activate_tool().  Extended (non-core, non-active) tools appear only as
        one-liners in get_compact_manifest() to save context tokens.

        Built-in tools are sorted first as a stable prefix, then MCP tools are
        sorted and appended.  The result is cached until the next
        register/unregister/activate call.
        """
        if self._cached_definitions is not None:
            return self._cached_definitions

        visible = self._core_tools | self._active_tools
        definitions = [
            tool.to_schema()
            for name, tool in self._tools.items()
            if name in visible
        ]
        builtins: list[dict[str, Any]] = []
        mcp_tools: list[dict[str, Any]] = []
        for schema in definitions:
            name = self._schema_name(schema)
            if name.startswith("mcp_"):
                mcp_tools.append(schema)
            else:
                builtins.append(schema)

        builtins.sort(key=self._schema_name)
        mcp_tools.sort(key=self._schema_name)
        self._cached_definitions = builtins + mcp_tools
        return self._cached_definitions

    def prepare_call(
        self,
        name: str,
        params: dict[str, Any],
    ) -> tuple[Tool | None, dict[str, Any], str | None]:
        """Resolve, cast, and validate one tool call."""
        # Guard against invalid parameter types (e.g., list instead of dict)
        if not isinstance(params, dict) and name in ('write_file', 'read_file'):
            return None, params, (
                f"Error: Tool '{name}' parameters must be a JSON object, got {type(params).__name__}. "
                "Use named parameters: tool_name(param1=\"value1\", param2=\"value2\")"
            )

        tool = self._tools.get(name)
        if not tool:
            visible = sorted(self._core_tools | self._active_tools)
            return None, params, (
                f"Error: Tool '{name}' not found or not yet activated. "
                f"Active tools: {', '.join(visible)}. "
                f"Call fetch_schema('{name}') first if it is an extended tool."
            )

        cast_params = tool.cast_params(params)
        errors = tool.validate_params(cast_params)
        if errors:
            return tool, cast_params, (
                f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors)
            )
        return tool, cast_params, None

    async def execute(self, name: str, params: dict[str, Any]) -> Any:
        """Execute a tool by name with given parameters."""
        _HINT = "\n\n[Analyze the error above and try a different approach.]"
        tool, params, error = self.prepare_call(name, params)
        if error:
            return error + _HINT

        try:
            assert tool is not None  # guarded by prepare_call()
            result = await tool.execute(**params)
            if isinstance(result, str) and result.startswith("Error"):
                return result + _HINT
            return result
        except Exception as e:
            return f"Error executing {name}: {str(e)}" + _HINT

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
