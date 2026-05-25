"""Heartbeat service - periodic agent wake-up to check for tasks."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger

if TYPE_CHECKING:
    from serenity.providers.base import LLMProvider

# Type alias: optional callable that returns True when the heartbeat should skip
_ShouldSkipFn = Callable[[], bool] | None

_HEARTBEAT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "heartbeat",
            "description": "Report heartbeat decision after reviewing tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["skip", "run"],
                        "description": "skip = nothing to do, run = has active tasks",
                    },
                    "tasks": {
                        "type": "string",
                        "description": "Natural-language summary of active tasks (required for run)",
                    },
                },
                "required": ["action"],
            },
        },
    }
]


class HeartbeatService:
    """
    Periodic heartbeat service that wakes the agent to check for tasks.

    Phase 1 (decision): reads HEARTBEAT.md and asks the LLM — via a virtual
    tool call — whether there are active tasks.  This avoids free-text parsing
    and the unreliable HEARTBEAT_OK token.

    Phase 2 (execution): only triggered when Phase 1 returns ``run``.  The
    ``on_execute`` callback runs the task through the full agent loop and
    returns the result to deliver.
    """

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        on_execute: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        on_notify: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        interval_s: int = 30 * 60,
        enabled: bool = True,
        timezone: str | None = None,
        should_skip_fn: _ShouldSkipFn = None,
    ):
        self.workspace = workspace
        self.provider = provider
        self.model = model
        self.on_execute = on_execute
        self.on_notify = on_notify
        self.interval_s = interval_s
        self.enabled = enabled
        self.timezone = timezone
        self.should_skip_fn = should_skip_fn
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def heartbeat_file(self) -> Path:
        # Check Agent/ subfolder first (matches context builder bootstrap order),
        # then fall back to workspace root.
        agent_path = self.workspace / "Agent" / "HEARTBEAT.md"
        if agent_path.exists():
            return agent_path
        return self.workspace / "HEARTBEAT.md"

    def _read_heartbeat_file(self) -> str | None:
        if self.heartbeat_file.exists():
            try:
                return self.heartbeat_file.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    # Phase 1 decision call. Default 180s — local 4B models need the headroom.
    # Override with SERENITY_HEARTBEAT_DECIDE_TIMEOUT env var.
    _DECIDE_TIMEOUT_S: float = float(os.environ.get("SERENITY_HEARTBEAT_DECIDE_TIMEOUT", "180"))
    # Phase 2 evaluate call. Default 180s for the same reason.
    _EVAL_TIMEOUT_S:   float = float(os.environ.get("SERENITY_HEARTBEAT_EVAL_TIMEOUT",   "180"))

    async def _decide(self, content: str) -> tuple[str, str]:
        """Phase 1: ask LLM to decide skip/run via virtual tool call.

        Returns (action, tasks) where action is 'skip' or 'run'.
        """
        from serenity.utils.helpers import current_time_str

        try:
            response = await asyncio.wait_for(
                self.provider.chat_with_retry(
                    messages=[
                        {"role": "system", "content": "You are a heartbeat agent. Call the heartbeat tool to report your decision."},
                        {"role": "user", "content": (
                            f"Current Time: {current_time_str(self.timezone)}\n\n"
                            "Review the following HEARTBEAT.md and decide whether there are active tasks.\n\n"
                            f"{content}"
                        )},
                    ],
                    tools=_HEARTBEAT_TOOL,
                    model=self.model,
                    reasoning_effort="none",  # heartbeat is a binary skip/run decision, no thinking needed
                ),
                timeout=self._DECIDE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Heartbeat _decide timed out after {}s — skipping this tick",
                int(self._DECIDE_TIMEOUT_S),
            )
            return "skip", ""

        if not response.should_execute_tools:
            if response.has_tool_calls:
                logger.warning(
                    "Ignoring heartbeat tool calls under finish_reason='{}'",
                    response.finish_reason,
                )
            return "skip", ""

        args = response.tool_calls[0].arguments
        return args.get("action", "skip"), args.get("tasks", "")

    async def start(self) -> None:
        """Start the heartbeat service."""
        if not self.enabled:
            logger.info("Heartbeat disabled")
            return
        if self._running:
            logger.warning("Heartbeat already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Heartbeat started (every {}s)", self.interval_s)

    def stop(self) -> None:
        """Stop the heartbeat service."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run_loop(self) -> None:
        """Main heartbeat loop."""
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat error: {}", e)

    async def _tick(self) -> None:
        """Execute a single heartbeat tick."""
        from serenity.utils.evaluator import evaluate_response

        # Check user activity BEFORE Phase 1 (the LLM decide call).
        # Previously this check lived in on_execute (Phase 2), which meant a
        # 2-minute LLM call already ran — and competed with the user's session —
        # before we even looked at whether the user was active. Moving it here
        # means we skip immediately, waste no LLM calls, and never contend.
        if self.should_skip_fn is not None and self.should_skip_fn():
            logger.debug(
                "Heartbeat: user session active — skipping this tick entirely "
                "(skip check now runs before Phase 1, not after)"
            )
            return

        content = self._read_heartbeat_file()
        if not content:
            logger.debug("Heartbeat: HEARTBEAT.md missing or empty")
            return

        logger.info("Heartbeat: checking for tasks...")

        try:
            action, tasks = await self._decide(content)

            if action != "run":
                logger.info("Heartbeat: OK (nothing to report)")
                return

            logger.info("Heartbeat: tasks found, executing...")
            if self.on_execute:
                response = await self.on_execute(tasks)

                if response:
                    try:
                        should_notify = await asyncio.wait_for(
                            evaluate_response(response, tasks, self.provider, self.model),
                            timeout=self._EVAL_TIMEOUT_S,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Heartbeat evaluate_response timed out ({}s) — defaulting to notify",
                            int(self._EVAL_TIMEOUT_S),
                        )
                        should_notify = True
                    if should_notify and self.on_notify:
                        logger.info("Heartbeat: completed, delivering response")
                        await self.on_notify(response)
                    else:
                        logger.info("Heartbeat: silenced by post-run evaluation")
        except Exception:
            logger.exception("Heartbeat execution failed")

    async def trigger_now(self) -> str | None:
        """Manually trigger a heartbeat."""
        content = self._read_heartbeat_file()
        if not content:
            return None
        action, tasks = await self._decide(content)
        if action != "run" or not self.on_execute:
            return None
        return await self.on_execute(tasks)
