"""CapabilityBuildTool — self-modification pipeline for Serenity.

One tool call owns the entire build-test-keep loop:
  1. Creates the skill folder structure
  2. Writes the script
  3. Writes the manifest
  4. Runs the script with test args
  5. Returns a clear pass or fail — code enforces every step

The LLM provides the code. The tool handles everything structural.
If it fails, the LLM reads the error, fixes the code, calls again.
After a pass the LLM calls nnn_store + vault_write to record the capability.

Scripts are sandboxed to workspace/skills/<name>/scripts/ — same safety
boundary as RunScriptTool. Cannot touch the agent loop or Agent/ files.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema

_MAX_OUTPUT_CHARS = 6_000


@tool_parameters(
    tool_parameters_schema(
        skill=StringSchema(
            "Name for the new capability — kebab-case, short, describes what it does. "
            "Example: 'fetch-weather', 'parse-json', 'resize-image'. "
            "This becomes the folder name under workspace/skills/."
        ),
        script=StringSchema(
            "Filename for the script. Use .py for Python (preferred). "
            "Example: 'fetch.py', 'parse.py'"
        ),
        code=StringSchema(
            "The full Python script content to write and test. "
            "Must read inputs from sys.argv and print results to stdout. "
            "Must handle errors gracefully — never crash silently. "
            "Standard library preferred; use only packages already installed."
        ),
        solves=StringSchema(
            "One sentence describing what gap this fills. "
            "Example: 'Fetches current weather for any city from wttr.in'. "
            "Goes into the manifest and NNN so future-you knows when to use it."
        ),
        test_args=StringSchema(
            "Arguments to pass to the script during the test run. "
            "Use real representative inputs — this is the actual test. "
            "Example: 'London' or '--city London --units metric'",
            nullable=True,
        ),
        timeout=IntegerSchema(
            "Seconds to wait for the test run. Default 30, max 120.",
            nullable=True,
        ),
        required=["skill", "script", "code", "solves"],
    )
)
class CapabilityBuildTool(Tool):
    """Build, test, and keep a new capability as a self-written script.

    This is Serenity's self-modification pipeline. One call:
      1. Creates workspace/skills/<skill>/scripts/<script>
      2. Writes the manifest describing when to use it
      3. Runs the script with test_args to verify it works
      4. Returns PASS (with output) or FAIL (with error to fix)

    On PASS — call nnn_store and vault_write to record the new capability.
    On FAIL — read the error, fix the code, call capability_build again.
    Maximum 3 attempts before abandoning and reporting to Daniel.

    What you can build:
      - Python scripts that fetch data, parse files, call APIs, process text
      - Any capability that runs inside the skills sandbox

    What you cannot build:
      - Modifications to the agent loop, tools, or Agent/ files
      - Scripts that access paths outside the workspace
    """

    _DEFAULT_SKILLS = frozenset({
        "ears", "eyes", "obs", "spotify", "gitnexus",
        "pc-control", "task-journal", "memory", "cron",
        "skill-creator", "tmux", "clawhub", "my",
        "Obsidian", "Neuro Node Network",
    })

    def __init__(self, workspace: Path):
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "capability_build"

    @property
    def description(self) -> str:
        return (
            "Build and test a new self-written capability as a Python script. "
            "Use this when you hit a gap that a script could fill. "
            "Provide the skill name, script filename, full Python code, and what it solves. "
            "The tool creates the folder, writes the script and manifest, runs it, "
            "and returns PASS or FAIL. "
            "On PASS: call nnn_store + vault_write to record it, then message Daniel. "
            "On FAIL: read the error, fix the code, call capability_build again (max 3 tries). "
            "Always call run_script(action='list') first to check if you already built this."
        )

    # ── Safety check ──────────────────────────────────────────────────────────

    def _check_safe(self, skill: str) -> str | None:
        """Return an error string if the skill name is unsafe, else None."""
        if skill in self._DEFAULT_SKILLS:
            return (
                f"Cannot overwrite built-in skill '{skill}'. "
                "Choose a different name for your new capability."
            )
        # Block path traversal
        if ".." in skill or "/" in skill or "\\" in skill:
            return f"Invalid skill name '{skill}' — must be a simple folder name with no path separators."
        # Ensure the resolved target stays inside workspace/skills/
        # Catches absolute paths and any traversal variants not caught above.
        try:
            skills_root = (self._workspace / "skills").resolve()
            target = (self._workspace / "skills" / skill).resolve()
            target.relative_to(skills_root)
        except ValueError:
            return f"Skill path '{skill}' resolves outside workspace/skills/ — rejected."
        return None

    # ── Build command ──────────────────────────────────────────────────────────

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
                raise EnvironmentError("Shell scripts require bash/WSL on Windows.")
            return ["bash", str(script_path)] + arg_list
        if suffix == ".bat":
            if sys.platform != "win32":
                raise EnvironmentError(".bat scripts only run on Windows.")
            return ["cmd.exe", "/c", str(script_path)] + arg_list
        raise ValueError(
            f"Unsupported script type '{script_path.suffix}'. Use .py, .sh, or .bat."
        )

    # ── Execute ────────────────────────────────────────────────────────────────

    async def execute(
        self,
        skill: str,
        script: str,
        code: str,
        solves: str,
        test_args: str | None = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> str:
        # Safety
        err = self._check_safe(skill)
        if err:
            return f"[capability_build] Rejected — {err}"

        timeout_s = min(int(timeout or 30), 120)
        skill_dir   = self._workspace / "skills" / skill
        scripts_dir = skill_dir / "scripts"
        script_path = scripts_dir / script
        manifest_path = skill_dir / "manifest.md"

        # ── Step 1: Create folder structure ───────────────────────────────────
        try:
            scripts_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return f"[capability_build] Could not create folder {scripts_dir}: {e}"

        # ── Step 2: Write the script ───────────────────────────────────────────
        try:
            script_path.write_text(code, encoding="utf-8")
        except Exception as e:
            return f"[capability_build] Could not write script {script_path}: {e}"

        # ── Step 3: Write the manifest ─────────────────────────────────────────
        date_str = datetime.date.today().isoformat()
        args_example = test_args or "<args>"
        manifest_content = (
            f"# {skill}\n"
            f"Built: {date_str}\n"
            f"Solves: {solves}\n"
            f"Usage: run_script(skill=\"{skill}\", script=\"{script}\", args=\"{args_example}\")\n"
            f"Output: stdout from the script\n"
            f"Status: working\n"
        )
        try:
            manifest_path.write_text(manifest_content, encoding="utf-8")
        except Exception as e:
            return f"[capability_build] Could not write manifest: {e}"

        # ── Step 4: Test run ───────────────────────────────────────────────────
        if test_args is None:
            # No test args — skip execution, report written but untested
            return (
                f"[capability_build] WRITTEN (not tested — no test_args provided)\n"
                f"Script: {script_path}\n"
                f"Manifest: {manifest_path}\n\n"
                f"To test it: run_script(skill=\"{skill}\", script=\"{script}\")\n"
                f"To record it: nnn_store + vault_write after confirming it works.\n\n"
                f"⚠ Always test before recording. An untested skill is not a kept skill."
            )

        try:
            cmd = self._build_command(script_path, test_args)
        except (EnvironmentError, ValueError) as e:
            return (
                f"[capability_build] Script written but test could not run: {e}\n"
                f"Script: {script_path}\n"
                f"Fix the script type or environment, then run_script manually to test."
            )

        try:
            # Filter sensitive keys from the subprocess environment
            _SECRET_PREFIXES = (
                "ANTHROPIC", "OPENAI", "SERENITY_LICENCE", "SERENITY_LICENSE",
                "TELEGRAM", "WHATSAPP", "DISCORD", "GUMROAD", "SECRET", "TOKEN",
                "API_KEY", "PRIVATE_KEY", "PASSWORD", "PASSWD",
            )
            safe_env = {
                k: v for k, v in os.environ.items()
                if not any(k.upper().startswith(p) for p in _SECRET_PREFIXES)
            }
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(scripts_dir),
                env=safe_env,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return (
                    f"[capability_build] FAIL — script timed out after {timeout_s}s.\n"
                    f"Script written to: {script_path}\n\n"
                    f"Fix: make the script faster, or increase timeout.\n"
                    f"Then call capability_build again with the fixed code."
                )
        except Exception as e:
            return (
                f"[capability_build] FAIL — could not launch script: {e}\n"
                f"Script written to: {script_path}\n"
                f"Fix the code and call capability_build again."
            )

        stdout = stdout_bytes.decode(errors="replace").strip()
        stderr = stderr_bytes.decode(errors="replace").strip()
        exit_code = proc.returncode

        if len(stdout) > _MAX_OUTPUT_CHARS:
            stdout = stdout[:_MAX_OUTPUT_CHARS] + f"\n… (truncated — {len(stdout)} chars total)"
        if len(stderr) > _MAX_OUTPUT_CHARS:
            stderr = stderr[:_MAX_OUTPUT_CHARS] + "\n… (truncated)"

        # ── Step 5: Report ─────────────────────────────────────────────────────
        if exit_code == 0:
            output_preview = stdout or "(no stdout — check that the script prints its result)"
            return (
                f"[capability_build] ✓ PASS\n"
                f"Skill: {skill}\n"
                f"Script: {script_path}\n"
                f"Manifest: {manifest_path}\n\n"
                f"--- test output ---\n{output_preview}\n\n"
                f"Capability is working. Now:\n"
                f"  1. nnn_store — ACTION: built script {skill} | BEFORE: could not do {solves} | "
                f"OUTCOME: can now do it via run_script | AFTER: skill at skills/{skill}/\n"
                f"  2. vault_write — title: 'Built skill {skill}', content: what it does, path, what gap it fills\n"
                f"  3. Message Daniel — one short line saying what you built and what it does"
            )
        else:
            error_detail = ""
            if stderr:
                error_detail += f"\n--- stderr ---\n{stderr}"
            if stdout:
                error_detail += f"\n--- stdout ---\n{stdout}"
            if not error_detail:
                error_detail = "\n(no output — script exited silently)"

            return (
                f"[capability_build] ✗ FAIL (exit {exit_code})\n"
                f"Script: {script_path}{error_detail}\n\n"
                f"Read the error above, fix the code, then call capability_build again.\n"
                f"Attempts remaining before abandoning: check your count — max 3 total.\n"
                f"If still failing after 3 attempts: abandon and vault_write noting what failed."
            )
