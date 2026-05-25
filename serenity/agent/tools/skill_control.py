"""Skill control tools — enable/disable skills at runtime.

Enabled skills are injected into every system prompt for the session and
persisted to {workspace}/state/active_skills.json so they survive restarts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from serenity.agent.skills import SkillsLoader
from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import StringSchema, tool_parameters_schema


@tool_parameters(
    tool_parameters_schema(
        name=StringSchema(
            "Skill name to enable (directory name under skills/). "
            "Examples: 'pc-control', 'gitnexus', 'ears'."
        ),
        required=["name"],
    )
)
class SkillEnableTool(Tool):
    """Enable a skill so it is injected into every system prompt from now on.

    The skill stays enabled across restarts until explicitly disabled.
    Use this when starting a session that needs a specific skill always in context.
    """

    def __init__(self, workspace: Path):
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "skill_enable"

    @property
    def description(self) -> str:
        return (
            "Enable a skill so its full instructions are injected into every system prompt. "
            "Persisted across restarts. Use at the start of a specialised session."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, name: str, **kwargs: Any) -> str:
        loader = SkillsLoader(self._workspace)
        ok = loader.enable_skill(name)
        if not ok:
            available = [e["name"] for e in loader.list_skills(filter_unavailable=False)]
            return (
                f"❌ Skill '{name}' not found.\n"
                f"Available skills: {', '.join(available) or 'none'}"
            )
        return (
            f"✅ Skill '{name}' enabled — its instructions will be injected into every "
            f"prompt from the next turn onwards. Active until skill_disable('{name}') is called."
        )


@tool_parameters(
    tool_parameters_schema(
        name=StringSchema("Skill name to disable."),
        required=["name"],
    )
)
class SkillDisableTool(Tool):
    """Disable a previously enabled skill, removing it from system prompt injection."""

    def __init__(self, workspace: Path):
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "skill_disable"

    @property
    def description(self) -> str:
        return (
            "Disable a skill that was previously enabled with skill_enable. "
            "Removes it from system prompt injection immediately."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, name: str, **kwargs: Any) -> str:
        loader = SkillsLoader(self._workspace)
        ok = loader.disable_skill(name)
        if not ok:
            return f"ℹ️ Skill '{name}' was not active (nothing to disable)."
        return f"✅ Skill '{name}' disabled — removed from prompt injection."


class SkillStatusTool(Tool):
    """List all currently active (dynamically enabled) skills."""

    def __init__(self, workspace: Path):
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "skill_status"

    @property
    def description(self) -> str:
        return (
            "List which skills are currently active (dynamically enabled via skill_enable). "
            "Does not include always-on skills — only runtime-enabled ones."
        )

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        loader = SkillsLoader(self._workspace)
        active = loader.get_dynamic_skills()
        if not active:
            return "No skills currently enabled via skill_enable."
        return "Active dynamic skills:\n" + "\n".join(f"  - {s}" for s in active)
