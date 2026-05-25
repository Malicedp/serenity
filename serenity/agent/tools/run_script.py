"""RunScriptTool — execute a script bundled inside a skill.

Serenity uses this to run scripts she has written and stored inside a skill's
scripts/ folder. This closes the loop between creating a skill and actually
using it — she writes the script once, then calls it any time via this tool.

Supported script types
  .py   — runs with the current Python interpreter
  .sh   — runs with bash (Linux/macOS) or WSL/Git Bash (Windows)
  .bat  — runs with cmd.exe (Windows only)
  any   — if the file is executable it is invoked directly

Safety
  Scripts must live inside a skill's scripts/ directory under either the
  workspace skills folder or the builtin skills folder. Paths outside those
  roots are rejected.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema

_MAX_OUTPUT_CHARS = 8_000


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "What to do: 'run' (default) to execute a script, 'list' to see all "
            "self-built scripts and their descriptions.",
            nullable=True,
        ),
        skill=StringSchema(
            "Name of the skill that owns the script — matches the folder name under "
            "workspace/skills/ or the builtin skills directory. Example: 'weather'. "
            "Required when action='run'.",
            nullable=True,
        ),
        script=StringSchema(
            "Filename of the script inside the skill's scripts/ directory. "
            "Example: 'fetch.py' or 'setup.sh'. Required when action='run'.",
            nullable=True,
        ),
        args=StringSchema(
            "Optional arguments passed to the script as a single string. "
            "Example: '--city London' or 'arg1 arg2'",
            nullable=True,
        ),
        timeout=IntegerSchema(
            "Seconds to wait before killing the script. Default 60, max 300.",
            nullable=True,
        ),
        required=[],
    )
)
class RunScriptTool(Tool):
    """Run a script from a skill's scripts/ folder, or list all self-built scripts.

    Use this to execute Python, shell, or batch scripts that you have written
    and stored inside a skill. The script runs in a subprocess and its full
    stdout/stderr is returned so you can read the results and act on them.

    Typical workflow:
      1. Write your script into workspace/skills/<name>/scripts/ using write_file
      2. Write a manifest.md in workspace/skills/<name>/ describing what it does
      3. Call run_script(action="run", skill=<name>, script=<file>) to execute it
      4. Loop — pass new arguments each call to drive interactive workflows

    To discover capabilities you have already built:
      run_script(action="list") — returns all self-built skills with descriptions
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None):
        self._workspace = workspace
        self._builtin_skills_dir = builtin_skills_dir

    @property
    def name(self) -> str:
        return "run_script"

    @property
    def description(self) -> str:
        return (
            "Execute a script from a skill's scripts/ folder, or list all self-built scripts. "
            "action='list' — returns every skill you have built with its description from manifest.md. "
            "Use this at the start of any task to check if you already have a capability for it. "
            "action='run' (default) — executes the named script and returns full stdout/stderr. "
            "Supports Python (.py), shell (.sh), and batch (.bat). "
            "Scripts must live inside workspace/skills/<skill>/scripts/ — "
            "use write_file to create them first."
        )

    # ── Path resolution ────────────────────────────────────────────────────────

    def _find_script(self, skill: str, script: str) -> Path:
        """Locate script — workspace skills take priority over builtins."""
        candidates = [self._workspace / "skills" / skill / "scripts" / script]
        if self._builtin_skills_dir:
            candidates.append(self._builtin_skills_dir / skill / "scripts" / script)

        for path in candidates:
            if path.exists():
                return path

        searched = "\n  ".join(str(p) for p in candidates)
        raise FileNotFoundError(
            f"Script '{script}' not found in skill '{skill}'.\n"
            f"Searched:\n  {searched}\n\n"
            f"Use write_file to create it first, then call run_script again."
        )

    # ── Command builder ────────────────────────────────────────────────────────

    def _build_command(self, script_path: Path, args: str | None) -> list[str]:
        import shlex
        suffix = script_path.suffix.lower()
        arg_list = shlex.split(args) if args else []

        if suffix == ".py":
            return [sys.executable, str(script_path)] + arg_list

        if suffix == ".sh":
            if sys.platform == "win32":
                for binary in ("wsl", "bash"):
                    found = shutil.which(binary)
                    if found:
                        return [found, str(script_path)] + arg_list
                raise EnvironmentError(
                    "Shell script requires bash. Install WSL or Git Bash on Windows."
                )
            return ["bash", str(script_path)] + arg_list

        if suffix == ".bat":
            if sys.platform != "win32":
                raise EnvironmentError(".bat scripts only run on Windows.")
            return ["cmd.exe", "/c", str(script_path)] + arg_list

        if os.access(script_path, os.X_OK):
            return [str(script_path)] + arg_list

        raise ValueError(
            f"Don't know how to run '{script_path.name}'. "
            "Supported: .py  .sh  .bat  or any executable file."
        )

    # ── Execute ────────────────────────────────────────────────────────────────

    def _list_built_skills(self) -> str:
        """Scan workspace/skills/ and return all self-built skills with manifests."""
        skills_root = self._workspace / "skills"
        if not skills_root.exists():
            return "No self-built skills found. workspace/skills/ does not exist yet."

        _DEFAULT_SKILLS = frozenset({
            "ears", "eyes", "obs", "spotify", "gitnexus",
            "pc-control", "task-journal", "memory", "cron",
            "skill-creator", "tmux", "clawhub", "my",
            "Obsidian", "Neuro Node Network",
        })

        entries: list[str] = []
        try:
            skill_dirs = sorted(skills_root.iterdir())
        except PermissionError as e:
            return f"Could not read skills directory: {e}"
        for skill_dir in skill_dirs:
            if not skill_dir.is_dir():
                continue
            if skill_dir.name in _DEFAULT_SKILLS:
                continue  # skip built-ins, only show self-built

            scripts_dir = skill_dir / "scripts"
            scripts = list(scripts_dir.glob("*")) if scripts_dir.exists() else []
            script_names = [s.name for s in scripts if s.is_file()]

            # Read manifest for description
            manifest_path = skill_dir / "manifest.md"
            description = "(no manifest)"
            if manifest_path.exists():
                try:
                    raw = manifest_path.read_text(encoding="utf-8")
                    # Extract Solves: line as the description
                    for line in raw.splitlines():
                        if line.startswith("Solves:"):
                            description = line[len("Solves:"):].strip()
                            break
                    else:
                        # Fall back to first non-heading line
                        for line in raw.splitlines():
                            stripped = line.strip()
                            if stripped and not stripped.startswith("#"):
                                description = stripped[:120]
                                break
                except Exception:
                    pass

            script_list = ", ".join(script_names) if script_names else "(no scripts)"
            entries.append(
                f"  skill: {skill_dir.name}\n"
                f"  does: {description}\n"
                f"  scripts: {script_list}\n"
                f"  call: run_script(skill=\"{skill_dir.name}\", script=\"{script_names[0] if script_names else '?'}\")"
            )

        if not entries:
            return (
                "No self-built skills yet.\n"
                "When you hit a gap, build one:\n"
                "  1. make_dir workspace/skills/<name>/scripts/\n"
                "  2. write_file the script\n"
                "  3. write_file manifest.md\n"
                "  4. run_script to test it"
            )

        return "Self-built skills:\n\n" + "\n\n".join(entries)

    async def execute(
        self,
        action: str | None = None,
        skill: str | None = None,
        script: str | None = None,
        args: str | None = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> str:
        # List mode — show all self-built capabilities
        if (action or "run").lower() == "list":
            return self._list_built_skills()

        # Run mode — skill and script are required
        if not skill:
            return "[run_script] 'skill' is required when action='run'. Call run_script(action='list') to see available skills."
        if not script:
            return "[run_script] 'script' is required when action='run'. Call run_script(action='list') to see available scripts."

        timeout_s = min(int(timeout or 60), 300)

        try:
            script_path = self._find_script(skill, script)
        except FileNotFoundError as e:
            return str(e)

        try:
            cmd = self._build_command(script_path, args)
        except (EnvironmentError, ValueError) as e:
            return f"[run_script] {e}"

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(script_path.parent),
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return (
                    f"[run_script] '{script}' timed out after {timeout_s}s and was killed.\n"
                    "Pass a higher timeout value if the script needs more time."
                )

        except Exception as e:
            return f"[run_script] Failed to launch '{script}': {e}"

        stdout = stdout_bytes.decode(errors="replace").strip()
        stderr = stderr_bytes.decode(errors="replace").strip()
        exit_code = proc.returncode

        parts: list[str] = [
            f"[run_script] skill={skill}  script={script}  exit={exit_code}"
        ]

        if stdout:
            if len(stdout) > _MAX_OUTPUT_CHARS:
                stdout = stdout[:_MAX_OUTPUT_CHARS] + f"\n… (truncated — {len(stdout)} chars total)"
            parts.append(f"\n--- stdout ---\n{stdout}")

        if stderr:
            if len(stderr) > _MAX_OUTPUT_CHARS:
                stderr = stderr[:_MAX_OUTPUT_CHARS] + "\n… (truncated)"
            parts.append(f"\n--- stderr ---\n{stderr}")

        if not stdout and not stderr:
            parts.append("\n(no output)")

        if exit_code != 0:
            parts.append(f"\n[exit code {exit_code} — script reported an error]")

        return "\n".join(parts)
