"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import time
from contextlib import AsyncExitStack, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from serenity.agent.autocompact import AutoCompact
from serenity.agent.context import ContextBuilder
from serenity.agent.hook import AgentHook, AgentHookContext, CompositeHook
from serenity.agent.memory import Consolidator, Dream
from serenity.agent.runner import _MAX_INJECTIONS_PER_TURN, AgentRunner, AgentRunSpec
from serenity.agent.skills import BUILTIN_SKILLS_DIR
from serenity.agent.subagent import SubagentManager
from serenity.agent.tools.cron import CronTool
from serenity.agent.tools.filesystem import EditFileTool, ListDirTool, MakeDirTool, ReadFileTool, WriteFileTool
from serenity.agent.tools.run_script import RunScriptTool
from serenity.agent.tools.capability_build import CapabilityBuildTool
from serenity.agent.tools.message import MessageTool
from serenity.agent.tools.call import CallUserTool
from serenity.agent.tools.finish import FinishTool
from serenity.agent.tools.ears import EarsCloseTool, EarsOpenTool, EarsRecallTool
from serenity.agent.tools.open_app import OpenAppTool, CloseAppTool, MinimiseAppTool
from serenity.agent.tools.eyes import (
    EyesOpenTool, EyesCloseTool, EyesSnapshotTool, EyesScreenTool,
    EyesSendScreenshotTool, EyesScreenAsciiTool,
)
from serenity.agent.tools.vision_rag import (
    VisionWatchStartTool, VisionWatchStopTool, VisionRecallTool, VisionSearchTool,
)
from serenity.agent.tools.screenshot import ScreenshotTool
from serenity.agent.tools.skill_control import SkillEnableTool, SkillDisableTool, SkillStatusTool
from serenity.agent.tools.spotify import (
    SpotifyAuthTool, SpotifyCurrentTool, SpotifyPlayTool,
    SpotifyPauseTool, SpotifySkipTool, SpotifyQueueTool, SpotifyVolumeTool,
)
from serenity.agent.tools.obs import (
    OBSStatusTool, OBSSceneListTool, OBSSetSceneTool,
    OBSStartRecordingTool, OBSStopRecordingTool,
    OBSStartStreamingTool, OBSStopStreamingTool, OBSToggleMuteTool,
)
from serenity.agent.tools.minecraft import (
    MinecraftConnectTool, MinecraftDisconnectTool, MinecraftStatusTool,
    MinecraftChatTool, MinecraftNavigateTool, MinecraftStopTool,
    MinecraftMineTool, MinecraftAttackTool, MinecraftEquipTool,
    MinecraftDropTool, MinecraftEatTool, MinecraftCraftTool,
    MinecraftScanBlocksTool, MinecraftScanEntitiesTool, MinecraftEventsTool,
    MinecraftPlaceBlockTool, MinecraftOpenContainerTool,
    MinecraftSleepTool, MinecraftWakeTool,
    MinecraftActivateItemTool, MinecraftDeactivateItemTool, MinecraftActivateBlockTool,
    MinecraftSenseTool, MinecraftNavigateWaitTool, MinecraftFightTool,
    MinecraftTickTool, MinecraftAutoSurviveTool, MinecraftPlanTool,
    MinecraftGoalSetTool, MinecraftGoalDoneTool, MinecraftGoalGetTool,
    MinecraftBootTool,
)
from serenity.agent.tools.task_journal import (
    TaskStartTool, TaskStepTool, TaskDecideTool, TaskCompleteTool,
    TaskStatusTool, TaskCaptureTool, TaskCancelTool, read_active_task,
)
from serenity.agent.tools.goals import GoalAddTool, GoalProgressTool, GoalCompleteTool, GoalRemoveTool
from serenity.agent.tools.mouse_keyboard import (
    MouseMoveTool,
    MouseClickTool,
    MouseScrollTool,
    MouseDragTool,
    KeyboardTypeTool,
    KeyboardHotkeyTool,
    KeyboardPressTool,
    FindOnScreenTool,
)
from serenity.agent.tools.nnn import NNNQueryTool, NNNStoreTool, NNNRewriteTool, NNNSimulateTool
from serenity.agent.tools.scratchpad import ScratchpadWriteTool, ScratchpadReadTool, ScratchpadCloseTool
from serenity.agent.tools.session_log import SessionObserveTool
from serenity.agent.tools.vault import VaultWriteTool
from serenity.agent.tools.vault_image import VaultImageStoreTool, VaultImageRecallTool
from serenity.agent.activity import get_logger as _get_activity_logger
from serenity.agent.trigger_commands import SESSION_REFLECTION, TASK_STOP, REACH_OUT, CURIOSITY_LOOP
from serenity.agent.dynamics import get_dynamics, _build_style_block, _load_personality
from serenity.agent.tools.fetch_schema import FetchSchemaTool
from serenity.agent.tools.notebook import NotebookEditTool
from serenity.agent.tools.registry import ToolRegistry
from serenity.agent.tools.search import GlobTool, GrepTool
from serenity.agent.tools.shell import ExecTool
from serenity.agent.tools.self import MyTool
from serenity.agent.tools.spawn import SpawnTool
from serenity.agent.tools.web import WebFetchTool, WebSearchTool
from serenity.bus.events import InboundMessage, OutboundMessage
from serenity.bus.queue import MessageBus
from serenity.command import CommandContext, CommandRouter, register_builtin_commands
from serenity.config.schema import AgentDefaults
from serenity.providers.base import LLMProvider
from serenity.session.manager import Session, SessionManager
from serenity.utils.document import extract_documents
from serenity.utils.helpers import image_placeholder_text
from serenity.utils.helpers import truncate_text as truncate_text_fn


from serenity.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE


def _log_voice(sender: str, preview: str) -> None:
    """Log a voice/mic input line in pink so it stands out in the terminal."""
    _s = sender.replace("<", r"\<").replace(">", r"\>")
    _p = preview.replace("<", r"\<").replace(">", r"\>")
    logger.opt(colors=True).info(
        f"<fg #FF69B4><bold>🎤  MIC  ▶  {_s}</bold>  {_p}</fg #FF69B4>"
    )

if TYPE_CHECKING:
    from serenity.config.schema import ChannelsConfig, ExecToolConfig, ToolsConfig, WebToolsConfig
    from serenity.cron.service import CronService


UNIFIED_SESSION_KEY = "unified:default"


class _LoopHook(AgentHook):
    """Core hook for the main loop."""

    def __init__(
        self,
        agent_loop: AgentLoop,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        *,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        super().__init__(reraise=True)
        self._loop = agent_loop
        self._on_progress = on_progress
        self._on_stream = on_stream
        self._on_stream_end = on_stream_end
        self._channel = channel
        self._chat_id = chat_id
        self._message_id = message_id
        self._session_key = session_key
        self._stream_buf = ""

    def wants_streaming(self) -> bool:
        return self._on_stream is not None

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        from serenity.utils.helpers import strip_think

        prev_clean = strip_think(self._stream_buf)
        self._stream_buf += delta
        new_clean = strip_think(self._stream_buf)
        incremental = new_clean[len(prev_clean) :]
        if incremental and self._on_stream:
            await self._on_stream(incremental)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        if self._on_stream_end:
            await self._on_stream_end(resuming=resuming)
        self._stream_buf = ""

    async def before_iteration(self, context: AgentHookContext) -> None:
        self._loop._current_iteration = context.iteration

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        if self._on_progress:
            if not self._on_stream:
                thought = self._loop._strip_think(
                    context.response.content if context.response else None
                )
                if thought:
                    await self._on_progress(thought)
            tool_hint = self._loop._strip_think(self._loop._tool_hint(context.tool_calls))
            await self._on_progress(tool_hint, tool_hint=True)
        for tc in context.tool_calls:
            args_str = json.dumps(tc.arguments, ensure_ascii=False)
            logger.info("Tool call: {}({})", tc.name, args_str[:200])
        self._loop._set_tool_context(self._channel, self._chat_id, self._message_id)
        # Activity log — record every tool call for this iteration
        if self._session_key and context.tool_calls:
            try:
                calls = [(tc.name, tc.arguments) for tc in context.tool_calls]
                _get_activity_logger(self._session_key).tool_calls(calls)
            except Exception:
                pass  # non-fatal

    async def after_iteration(self, context: AgentHookContext) -> None:
        u = context.usage or {}
        logger.debug(
            "LLM usage: prompt={} completion={} cached={}",
            u.get("prompt_tokens", 0),
            u.get("completion_tokens", 0),
            u.get("cached_tokens", 0),
        )

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        return self._loop._strip_think(content)


class Reflector:
    """Inactivity detector — fires structured triggers on three timers:

    1. SESSION_REFLECTION  — after SERENITY_REFLECTION_IDLE_MINUTES (default 10m)
       The agent reviews the session, writes to vault, distils to NNN.

    2. REACH_OUT           — after SERENITY_REACH_OUT_IDLE_HOURS (default 2h)
       The agent decides (based on its emotion state) whether to send the user
       an unsolicited greeting. Energy low → likely stays quiet. Energy high →
       picks a greeting style and sends it.

    3. CURIOSITY_LOOP      — after SERENITY_CURIOSITY_IDLE_MINUTES (default 45m)
       Emotion-biased autonomous action. The agent generates its own options,
       picks one that matches its current mood, and optionally notifies the user
       of what it found or did.

    Design:
      - Loop calls check() on every idle tick (same cadence as auto_compact)
      - Fire is non-blocking: schedules a background coroutine via _schedule_fn
      - Separate cooldowns per trigger type prevent re-firing
      - GPU serialisation: only one trigger fires per session per tick
    """

    _DEFAULT_IDLE_MINUTES       = 10
    _REFLECTION_COOLDOWN_S      = 3600       # 1 hour between reflections
    _DEFAULT_REACH_OUT_HOURS    = 2
    _REACH_OUT_COOLDOWN_S       = 4 * 3600   # 4 hours between reach-outs
    _DEFAULT_CURIOSITY_MINUTES  = 45
    _CURIOSITY_COOLDOWN_S       = 90 * 60    # 1.5 hours between curiosity loops

    def __init__(self) -> None:
        self._last_message:    dict[str, float] = {}
        self._last_reflected:  dict[str, float] = {}
        self._last_reach_out:  dict[str, float] = {}
        self._last_curiosity:  dict[str, float] = {}

        self._idle_seconds = (
            int(os.environ.get("SERENITY_REFLECTION_IDLE_MINUTES",
                               str(self._DEFAULT_IDLE_MINUTES))) * 60
        )
        self._reach_out_seconds = (
            float(os.environ.get("SERENITY_REACH_OUT_IDLE_HOURS",
                                 str(self._DEFAULT_REACH_OUT_HOURS))) * 3600
        )
        self._curiosity_seconds = (
            float(os.environ.get("SERENITY_CURIOSITY_IDLE_MINUTES",
                                 str(self._DEFAULT_CURIOSITY_MINUTES))) * 60
        )

    # Session key prefixes that are internal infrastructure — they should never
    # trigger reflections or reach-outs, and should not count as "active user
    # sessions" for the heartbeat/cron skip guards.
    _INTERNAL_PREFIXES = ("heartbeat", "cron:")

    @classmethod
    def _is_internal(cls, session_key: str) -> bool:
        return any(session_key == p or session_key.startswith(p)
                   for p in cls._INTERNAL_PREFIXES)

    def record_message(self, session_key: str) -> None:
        """Call at the start of every turn so the reflector tracks activity."""
        self._last_message[session_key] = time.time()

    def check(self, schedule_fn, reflect_fn, reach_out_fn, curiosity_fn=None) -> None:
        """Called on every idle loop tick. Fires triggers for quiet sessions."""
        now = time.time()
        # Track which sessions already fired a trigger this tick so we serialise
        # GPU use — firing multiple triggers simultaneously causes timeout cascades
        # on local models.
        _fired_this_tick: set[str] = set()
        for session_key, last_msg in list(self._last_message.items()):
            # Never trigger reflection or reach-out on internal sessions
            # (heartbeat, cron:*). They don't have a real user channel to
            # send to, and the channel-from-key parsing produces junk results.
            if self._is_internal(session_key):
                continue

            idle = now - last_msg

            # ── Reflection trigger (10 min default) ──────────────────────────
            if idle >= self._idle_seconds:
                last_ref = self._last_reflected.get(session_key, 0.0)
                if now - last_ref >= self._REFLECTION_COOLDOWN_S:
                    self._last_reflected[session_key] = now
                    _fired_this_tick.add(session_key)
                    schedule_fn(reflect_fn(session_key))
                    logger.info(
                        "Reflection triggered for session {} (idle {:.0f}s)",
                        session_key, idle,
                    )

            # ── Curiosity loop trigger (45 min default) ───────────────────────
            if curiosity_fn and idle >= self._curiosity_seconds:
                if session_key in _fired_this_tick:
                    logger.debug(
                        "Curiosity deferred for {} — another trigger fired this tick "
                        "(GPU serialisation).",
                        session_key,
                    )
                else:
                    last_cur = self._last_curiosity.get(session_key, 0.0)
                    if now - last_cur >= self._CURIOSITY_COOLDOWN_S:
                        self._last_curiosity[session_key] = now
                        _fired_this_tick.add(session_key)
                        schedule_fn(curiosity_fn(session_key, idle))
                        logger.info(
                            "Curiosity loop triggered for session {} (idle {:.0f}s)",
                            session_key, idle,
                        )

            # ── Reach-out trigger (2h default) ───────────────────────────────
            if idle >= self._reach_out_seconds:
                # Skip reach-out if another trigger already fired for this session
                # this tick — serialise GPU use to prevent timeout cascades.
                if session_key in _fired_this_tick:
                    logger.debug(
                        "Reach-out deferred for {} — another trigger fired this tick "
                        "(GPU serialisation; reach-out will fire next eligible tick).",
                        session_key,
                    )
                    continue
                last_ro = self._last_reach_out.get(session_key, 0.0)
                if now - last_ro >= self._REACH_OUT_COOLDOWN_S:
                    self._last_reach_out[session_key] = now
                    _fired_this_tick.add(session_key)
                    schedule_fn(reach_out_fn(session_key, idle))
                    logger.info(
                        "Reach-out triggered for session {} (idle {:.0f}s)",
                        session_key, idle,
                    )


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
    _PENDING_USER_TURN_KEY = "pending_user_turn"

    # Pre-compiled regex patterns for _auto_vault (avoid re-compiling every call)
    _MEMORY_PATTERNS = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in [
        # Explicit memory commands
        r"^(?:please\s+)?remember\s+(?:that\s+)?(.+)$",
        r"^(?:please\s+)?don'?t\s+forget\s+(?:that\s+)?(.+)$",
        r"^(?:please\s+)?note\s+(?:that\s+|this[:\s]+)?(.+)$",
        r"^(?:please\s+)?save\s+(?:that\s+|this[:\s]+)?(.+)$",
        r"^(?:please\s+)?store\s+(?:that\s+|this[:\s]+)?(.+)$",
        r"^(?:please\s+)?add\s+(?:to\s+(?:my\s+)?memory[:\s]+|a\s+note[:\s]+)(.+)$",
        r"^(?:please\s+)?keep\s+(?:a\s+)?note\s+(?:that\s+|of\s+)?(.+)$",
        r"^(?:please\s+)?log\s+(?:that\s+)?(.+)$",
        r"^(?:please\s+)?write\s+(?:down\s+|to\s+(?:the\s+)?vault[:\s]+)?(?:that\s+)?(.+)$",
        r"^(?:please\s+)?make\s+a\s+note\s+(?:that\s+|of\s+)?(.+)$",
        # Preference / fact statements the user is sharing about themselves
        r"^my\s+favou?rite\s+\w+(?:\s+\w+)?\s+is\s+(.+)$",
        r"^i\s+(?:really\s+)?(?:love|like|hate|prefer|enjoy|dislike)\s+(.+)$",
        r"^i\s+(?:am|'m)\s+(?:a\s+)?(.{10,})$",
        r"^i\s+(?:work|live|study)\s+(?:at|in|as)\s+(.+)$",
        r"^my\s+(?:name|age|job|goal|dream|hobby|hobbies|project)\s+is\s+(.+)$",
    ]]
    _BARE_PRONOUNS = frozenset({"this", "that", "it", "these", "those", "everything", "all of it"})
    _FILLER = frozenset({"that", "the", "a", "an", "is", "am", "are", "was", "it", "my", "i"})

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int | None = None,
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
        max_tool_result_chars: int | None = None,
        provider_retry_mode: str = "standard",
        web_config: WebToolsConfig | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        timezone: str | None = None,
        session_ttl_minutes: int = 0,
        session_ttl_overrides: dict[str, int] | None = None,
        hooks: list[AgentHook] | None = None,
        unified_session: bool = False,
        disabled_skills: list[str] | None = None,
        tools_config: ToolsConfig | None = None,
        force_tool_use: bool = False,
    ):
        from serenity.config.schema import ExecToolConfig, ToolsConfig, WebToolsConfig

        _tc = tools_config or ToolsConfig()
        defaults = AgentDefaults()
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = (
            max_iterations if max_iterations is not None else defaults.max_tool_iterations
        )
        self.context_window_tokens = (
            context_window_tokens
            if context_window_tokens is not None
            else defaults.context_window_tokens
        )
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = (
            max_tool_result_chars
            if max_tool_result_chars is not None
            else defaults.max_tool_result_chars
        )
        self.provider_retry_mode = provider_retry_mode
        self.force_tool_use = force_tool_use
        self.web_config = web_config or WebToolsConfig()
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._extra_hooks: list[AgentHook] = hooks or []

        self.context = ContextBuilder(workspace, timezone=timezone, disabled_skills=disabled_skills)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.runner = AgentRunner(provider)
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            web_config=self.web_config,
            max_tool_result_chars=self.max_tool_result_chars,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
            disabled_skills=disabled_skills,
        )
        self._unified_session = unified_session
        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stacks: dict[str, AsyncExitStack] = {}
        self._mcp_connected = False
        self._mcp_connecting = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: list[asyncio.Task] = []
        # Maps session_key → the chosen log Path so all turns in a session share one file.
        self._session_log_paths: dict[str, Path] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Per-session pending queues for mid-turn message injection.
        # When a session has an active task, new messages for that session
        # are routed here instead of creating a new task.
        self._pending_queues: dict[str, asyncio.Queue] = {}
        # SERENITY_MAX_CONCURRENT_REQUESTS: <=0 means unlimited; default 3.
        _max = int(os.environ.get("SERENITY_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )
        self.consolidator = Consolidator(
            store=self.context.memory,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
        )
        self.auto_compact = AutoCompact(
            sessions=self.sessions,
            consolidator=self.consolidator,
            session_ttl_minutes=session_ttl_minutes,
            session_ttl_overrides=session_ttl_overrides,
        )
        self.dream = Dream(
            store=self.context.memory,
            provider=provider,
            model=self.model,
        )
        self._register_default_tools()
        if _tc.my.enable:
            self.tools.register(MyTool(loop=self, modify_allowed=_tc.my.allow_set), core=False)
        self._runtime_vars: dict[str, Any] = {}
        self._current_iteration: int = 0
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)
        # Inactivity reflector — fires SESSION_REFLECTION after idle period
        self._reflector = Reflector()
        # RL Q-table — initialise with workspace path
        from serenity.agent import rl as _rl
        _rl.init(self.workspace)
        # Per-session consecutive-failure counter for TASK_STOP trigger
        # Key: session_key  Value: count of consecutive turns where all tools errored
        self._consecutive_failures: dict[str, int] = {}
        _max_str = os.environ.get("SERENITY_MAX_CONSECUTIVE_FAILURES", "4")
        self._max_consecutive_failures = int(_max_str)

    def _register_default_tools(self) -> None:
        """Register the default set of tools.

        Tools are split into two tiers:
        - Core (core=True, default): full schema always injected every turn.
          Covers memory, web, communication, scheduling — ~90% of daily use.
        - Extended (core=False): only a compact hint is always present.
          Full schema injected on demand via fetch_schema().
        """
        allowed_dir = (
            self.workspace if (self.restrict_to_workspace or self.exec_config.sandbox) else None
        )
        extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None

        # ── Extended: file system (on-demand) ──────────────────────────────
        self.tools.register(
            ReadFileTool(
                workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=extra_read
            ),
            core=False,
        )
        for cls in (WriteFileTool, EditFileTool, ListDirTool, MakeDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir), core=False)
        self.tools.register(
            RunScriptTool(workspace=self.workspace, builtin_skills_dir=BUILTIN_SKILLS_DIR),
            core=False,
        )
        self.tools.register(
            CapabilityBuildTool(workspace=self.workspace),
            core=False,
        )
        for cls in (GlobTool, GrepTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir), core=False)
        self.tools.register(
            NotebookEditTool(workspace=self.workspace, allowed_dir=allowed_dir), core=False
        )

        # ── Extended: terminal (on-demand) ─────────────────────────────────
        if self.exec_config.enable:
            self.tools.register(
                ExecTool(
                    working_dir=str(self.workspace),
                    timeout=self.exec_config.timeout,
                    restrict_to_workspace=self.restrict_to_workspace,
                    sandbox=self.exec_config.sandbox,
                    path_append=self.exec_config.path_append,
                    allowed_env_keys=self.exec_config.allowed_env_keys,
                ),
                core=False,
            )

        # ── Core: web (always needed for lookups) ──────────────────────────
        if self.web_config.enable:
            self.tools.register(
                WebSearchTool(config=self.web_config.search, proxy=self.web_config.proxy)
            )
            self.tools.register(WebFetchTool(proxy=self.web_config.proxy))

        # ── Core: communication ────────────────────────────────────────────
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(CallUserTool(send_callback=self.bus.publish_outbound))

        # ── Extended: vision output (on-demand) ────────────────────────────
        self.tools.register(
            EyesSendScreenshotTool(send_callback=self.bus.publish_outbound), core=False
        )
        self.tools.register(EyesScreenAsciiTool(), core=False)

        # ── Extended: sub-agents (on-demand) ───────────────────────────────
        self.tools.register(SpawnTool(manager=self.subagents), core=False)

        # ── Core: scheduling ───────────────────────────────────────────────
        if self.cron_service:
            self.tools.register(
                CronTool(self.cron_service, default_timezone=self.context.timezone or "UTC")
            )

        # ── Extended: Obsidian vault (on-demand) ───────────────────────────
        self.tools.register(VaultWriteTool(workspace=self.workspace), core=False)
        self.tools.register(VaultImageStoreTool(workspace=self.workspace), core=False)
        self.tools.register(VaultImageRecallTool(workspace=self.workspace), core=False)

        # ── Extended: task journal (on-demand) ─────────────────────────────
        for _tj_cls in (TaskStartTool, TaskStepTool, TaskDecideTool, TaskCompleteTool, TaskStatusTool, TaskCaptureTool, TaskCancelTool):
            self.tools.register(_tj_cls(workspace=self.workspace), core=False)
        for _g_cls in (GoalAddTool, GoalProgressTool, GoalCompleteTool, GoalRemoveTool):
            self.tools.register(_g_cls(workspace=self.workspace), core=False)

        # ── Extended: screenshot (on-demand) ───────────────────────────────
        self.tools.register(ScreenshotTool(), core=False)

        # ── Core: NNN memory ───────────────────────────────────────────────
        try:
            self.tools.register(NNNQueryTool())
            self.tools.register(NNNStoreTool())
            self.tools.register(NNNRewriteTool())
            self.tools.register(NNNSimulateTool())
        except Exception:
            pass

        # ── Core: Scratchpad — working memory for any multi-step task ──────
        self.tools.register(ScratchpadWriteTool(), core=True)
        self.tools.register(ScratchpadReadTool(),  core=True)
        self.tools.register(ScratchpadCloseTool(), core=True)

        # ── Extended: session observation, skill control (on-demand) ───────
        self.tools.register(SessionObserveTool(), core=False)
        self.tools.register(SkillEnableTool(self.workspace), core=False)
        self.tools.register(SkillDisableTool(self.workspace), core=False)
        self.tools.register(SkillStatusTool(self.workspace), core=False)

        # ── Extended: Spotify (on-demand) ──────────────────────────────────
        for _sp_cls in (
            SpotifyAuthTool, SpotifyCurrentTool, SpotifyPlayTool,
            SpotifyPauseTool, SpotifySkipTool, SpotifyQueueTool, SpotifyVolumeTool,
        ):
            self.tools.register(_sp_cls(), core=False)

        # ── Extended: OBS (on-demand) ──────────────────────────────────────
        for _obs_cls in (
            OBSStatusTool, OBSSceneListTool, OBSSetSceneTool,
            OBSStartRecordingTool, OBSStopRecordingTool,
            OBSStartStreamingTool, OBSStopStreamingTool, OBSToggleMuteTool,
        ):
            self.tools.register(_obs_cls(), core=False)

        # ── Extended: app control (on-demand) ──────────────────────────────
        self.tools.register(OpenAppTool(), core=False)
        self.tools.register(CloseAppTool(), core=False)
        self.tools.register(MinimiseAppTool(), core=False)
        # Minecraft — mineflayer bridge; bridge starts automatically on connect
        # ── Extended: Minecraft (on-demand) ────────────────────────────────
        for _mc_cls in (
            MinecraftConnectTool, MinecraftDisconnectTool, MinecraftStatusTool,
            MinecraftChatTool, MinecraftNavigateTool, MinecraftStopTool,
            MinecraftMineTool, MinecraftAttackTool, MinecraftEquipTool,
            MinecraftDropTool, MinecraftEatTool, MinecraftCraftTool,
            MinecraftScanBlocksTool, MinecraftScanEntitiesTool, MinecraftEventsTool,
            MinecraftPlaceBlockTool, MinecraftOpenContainerTool,
            MinecraftSleepTool, MinecraftWakeTool,
            MinecraftActivateItemTool, MinecraftDeactivateItemTool, MinecraftActivateBlockTool,
            MinecraftSenseTool, MinecraftNavigateWaitTool, MinecraftFightTool,
            MinecraftTickTool, MinecraftAutoSurviveTool, MinecraftPlanTool,
            MinecraftGoalSetTool, MinecraftGoalDoneTool, MinecraftGoalGetTool,
            MinecraftBootTool,
        ):
            self.tools.register(_mc_cls(), core=False)

        # ── Extended: mouse / keyboard (on-demand) ─────────────────────────
        for _mk_cls in (
            MouseMoveTool, MouseClickTool, MouseScrollTool, MouseDragTool,
            KeyboardTypeTool, KeyboardHotkeyTool, KeyboardPressTool, FindOnScreenTool,
        ):
            self.tools.register(_mk_cls(), core=False)

        # ── Extended: senses (on-demand) ───────────────────────────────────
        self.tools.register(EarsOpenTool(), core=False)
        self.tools.register(EarsCloseTool(), core=False)
        self.tools.register(EarsRecallTool(), core=False)
        self.tools.register(EyesOpenTool(), core=False)
        self.tools.register(EyesCloseTool(), core=False)
        self.tools.register(EyesSnapshotTool(), core=False)
        self.tools.register(EyesScreenTool(), core=False)
        self.tools.register(VisionWatchStartTool(), core=False)
        self.tools.register(VisionWatchStopTool(), core=False)
        self.tools.register(VisionRecallTool(), core=False)
        self.tools.register(VisionSearchTool(), core=False)

        # ── Core: fetch_schema meta-tool (always available) ────────────────
        self.tools.register(FetchSchemaTool(registry=self.tools))

        # ── Core: FinishTool (force_tool_use mode) ─────────────────────────
        # With tool_choice="required" the model must always emit a tool call;
        # finish() is how it sends text to the user instead of producing free text.
        if self.force_tool_use:
            self.tools.register(FinishTool())

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from serenity.agent.tools.mcp import connect_mcp_servers

        try:
            self._mcp_stacks = await connect_mcp_servers(self._mcp_servers, self.tools)
            if self._mcp_stacks:
                self._mcp_connected = True
            else:
                logger.warning("No MCP servers connected successfully (will retry next message)")
        except asyncio.CancelledError:
            logger.warning("MCP connection cancelled (will retry next message)")
            self._mcp_stacks.clear()
        except BaseException as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            self._mcp_stacks.clear()
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron", "my", "call_user", "eyes_send_screenshot"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        from serenity.utils.helpers import strip_think

        return strip_think(text) or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hints with smart abbreviation."""
        from serenity.utils.tool_hints import format_tool_hints

        return format_tool_hints(tool_calls)

    def _effective_session_key(self, msg: InboundMessage) -> str:
        """Return the session key used for task routing and mid-turn injections."""
        if self._unified_session and not msg.session_key_override:
            return UNIFIED_SESSION_KEY
        return msg.session_key

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
        *,
        session: Session | None = None,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        pending_queue: asyncio.Queue | None = None,
        reasoning_effort_override: str | None = None,
    ) -> tuple[str | None, list[str], list[dict], str, bool]:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming)*: called when a streaming session finishes.
        ``resuming=True`` means tool calls follow (spinner should restart);
        ``resuming=False`` means this is the final response.

        Returns (final_content, tools_used, messages, stop_reason, had_injections).
        """
        loop_hook = _LoopHook(
            self,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=session.key if session else None,
        )
        hook: AgentHook = (
            CompositeHook([loop_hook] + self._extra_hooks) if self._extra_hooks else loop_hook
        )

        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            self._set_runtime_checkpoint(session, payload)

        async def _drain_pending(*, limit: int = _MAX_INJECTIONS_PER_TURN) -> list[dict[str, Any]]:
            """Non-blocking drain of follow-up messages from the pending queue."""
            if pending_queue is None:
                return []
            items: list[dict[str, Any]] = []
            while len(items) < limit:
                try:
                    pending_msg = pending_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                content = pending_msg.content
                media = pending_msg.media if pending_msg.media else None
                if media:
                    content, media = extract_documents(content, media)
                    media = media or None
                user_content = self.context._build_user_content(content, media)
                runtime_ctx = self.context._build_runtime_context(
                    pending_msg.channel,
                    pending_msg.chat_id,
                    self.context.timezone,
                )
                if isinstance(user_content, str):
                    merged: str | list[dict[str, Any]] = f"{runtime_ctx}\n\n{user_content}"
                else:
                    merged = [{"type": "text", "text": runtime_ctx}] + user_content
                items.append({"role": "user", "content": merged})
            return items

        result = await self.runner.run(AgentRunSpec(
            initial_messages=initial_messages,
            tools=self.tools,
            model=self.model,
            max_iterations=self.max_iterations,
            max_tool_result_chars=self.max_tool_result_chars,
            hook=hook,
            error_message="Sorry, I encountered an error calling the AI model.",
            concurrent_tools=True,
            workspace=self.workspace,
            session_key=session.key if session else None,
            context_window_tokens=self.context_window_tokens,
            context_block_limit=self.context_block_limit,
            provider_retry_mode=self.provider_retry_mode,
            force_tool_use=self.force_tool_use,
            progress_callback=on_progress,
            retry_wait_callback=on_retry_wait,
            checkpoint_callback=_checkpoint,
            injection_callback=_drain_pending,
            reasoning_effort=reasoning_effort_override,
        ))
        self._last_usage = result.usage
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            # Push final content through stream so streaming channels (e.g. Feishu)
            # update the card instead of leaving it empty.
            if on_stream and on_stream_end:
                await on_stream(result.final_content or "")
                await on_stream_end(resuming=False)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])
        return result.final_content, result.tools_used, result.messages, result.stop_reason, result.had_injections

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        # Pre-warm the nomic embedding model in the background immediately on startup.
        # Both NNN and vault semantic search share the same model (_model global in
        # serenity_nnn.embedder). Cold load takes ~20-25 s on most machines. By firing
        # this now, the model is ready before the first user message arrives so NNN
        # auto-queries don't hit the 4-second timeout on the first turn.
        async def _warmup_embedding_model():
            try:
                from serenity_nnn.embedder import get_model as _get_embed_model
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _get_embed_model)
                logger.info("Embedding model pre-warmed — NNN ready")
            except Exception as e:
                logger.debug("Embedding model warmup skipped: {}", e)

        asyncio.create_task(_warmup_embedding_model())

        # Start always-on senses daemon (wake word + passive vision).
        # Runs in background threads — completely independent of the agent loop.
        # Only activates if senses.audio.enabled or senses.vision.enabled in config.
        try:
            from serenity.senses.daemon import start as _start_senses
            _start_senses(self.bus, asyncio.get_running_loop())
        except Exception as _e:
            logger.debug("Senses daemon skipped: {}", _e)

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                self.auto_compact.check_expired(
                    self._schedule_background,
                    active_session_keys=self._pending_queues.keys(),
                )
                # Inactivity reflector — fires session review after quiet period
                self._reflector.check(
                    self._schedule_background,
                    self._fire_reflection,
                    self._fire_reach_out,
                    self._fire_curiosity,
                )
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            raw = msg.content.strip()
            if self.commands.is_priority(raw):
                ctx = CommandContext(msg=msg, session=None, key=msg.session_key, raw=raw, loop=self)
                result = await self.commands.dispatch_priority(ctx)
                if result:
                    await self.bus.publish_outbound(result)
                continue
            effective_key = self._effective_session_key(msg)
            # If this session already has an active pending queue (i.e. a task
            # is processing this session), route the message there for mid-turn
            # injection instead of creating a competing task.
            if effective_key in self._pending_queues:
                pending_msg = msg
                if effective_key != msg.session_key:
                    pending_msg = dataclasses.replace(
                        msg,
                        session_key_override=effective_key,
                    )
                try:
                    self._pending_queues[effective_key].put_nowait(pending_msg)
                except asyncio.QueueFull:
                    logger.warning(
                        "Pending queue full for session {}, falling back to queued task",
                        effective_key,
                    )
                else:
                    logger.info(
                        "Routed follow-up message to pending queue for session {}",
                        effective_key,
                    )
                    continue
            # Compute the effective session key before dispatching
            # This ensures /stop command can find tasks correctly when unified session is enabled
            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(effective_key, []).append(task)
            task.add_done_callback(
                lambda t, k=effective_key: self._active_tasks.get(k, [])
                and self._active_tasks[k].remove(t)
                if t in self._active_tasks.get(k, [])
                else None
            )

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        session_key = self._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()

        # Register a pending queue so follow-up messages for this session are
        # routed here (mid-turn injection) instead of spawning a new task.
        pending = asyncio.Queue(maxsize=20)
        self._pending_queues[session_key] = pending

        # Master turn timeout — guarantees the user always gets a response.
        # If the LLM or any tool hangs past this, we cancel and reply with an error.
        # SERENITY_TURN_TIMEOUT overrides (in seconds). Default: 600s (10 minutes).
        # 10 minutes gives headroom for long-running tool calls and multi-step turns.
        _turn_timeout = float(os.environ.get("SERENITY_TURN_TIMEOUT", "600"))

        try:
            async with lock, gate:
                try:
                    on_stream = on_stream_end = None
                    if msg.metadata.get("_wants_stream"):
                        # Split one answer into distinct stream segments.
                        stream_base_id = f"{msg.session_key}:{time.time_ns()}"
                        stream_segment = 0

                        def _current_stream_id() -> str:
                            return f"{stream_base_id}:{stream_segment}"

                        async def on_stream(delta: str) -> None:
                            meta = dict(msg.metadata or {})
                            meta["_stream_delta"] = True
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content=delta,
                                metadata=meta,
                            ))

                        async def on_stream_end(*, resuming: bool = False) -> None:
                            nonlocal stream_segment
                            meta = dict(msg.metadata or {})
                            meta["_stream_end"] = True
                            meta["_resuming"] = resuming
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="",
                                metadata=meta,
                            ))
                            stream_segment += 1

                    try:
                        response = await asyncio.wait_for(
                            self._process_message(
                                msg, on_stream=on_stream, on_stream_end=on_stream_end,
                                pending_queue=pending,
                            ),
                            timeout=_turn_timeout,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Turn timeout ({:.0f}s) for session {} — sending timeout reply",
                            _turn_timeout, session_key,
                        )
                        response = OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content=(
                                "Sorry, that took too long and I had to stop. "
                                "The model may be overloaded — try again or switch to a faster model."
                            ),
                            metadata=msg.metadata or {},
                        )
                    if response is not None:
                        await self.bus.publish_outbound(response)
                    elif msg.channel == "cli":
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="", metadata=msg.metadata or {},
                        ))
                except asyncio.CancelledError:
                    logger.info("Task cancelled for session {}", session_key)
                    raise
                except Exception:
                    logger.exception("Error processing message for session {}", session_key)
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="Sorry, I encountered an error. Check the logs for details.",
                    ))
        finally:
            # Drain any messages still in the pending queue and re-publish
            # them to the bus so they are processed as fresh inbound messages
            # rather than silently lost.
            queue = self._pending_queues.pop(session_key, None)
            if queue is not None:
                leftover = 0
                while True:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await self.bus.publish_inbound(item)
                    leftover += 1
                if leftover:
                    logger.info(
                        "Re-published {} leftover message(s) to bus for session {}",
                        leftover, session_key,
                    )

    async def close_mcp(self) -> None:
        """Drain pending background archives, then close MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        for name, stack in self._mcp_stacks.items():
            try:
                await stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                logger.debug("MCP server '{}' cleanup error (can be ignored)", name)
        self._mcp_stacks.clear()

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)

        def _remove_task(t: asyncio.Task) -> None:
            # Guard against double-remove during shutdown (close_mcp clears the list)
            try:
                self._background_tasks.remove(t)
            except ValueError:
                pass

        task.add_done_callback(_remove_task)

    async def _run_chunk_notes(self, session) -> None:
        """Async wrapper for the synchronous generate_chunk_notes_if_needed."""
        try:
            self.consolidator.generate_chunk_notes_if_needed(session)
        except Exception as e:
            logger.debug("Chunk notes background task failed: {}", e)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")
        try:
            from serenity.senses.daemon import stop as _stop_senses
            _stop_senses()
        except Exception:
            pass

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
        reasoning_effort_override: str | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (
                msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
            )
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            if self._restore_runtime_checkpoint(session):
                self.sessions.save(session)
            if self._restore_pending_user_turn(session):
                self.sessions.save(session)

            session, pending = self.auto_compact.prepare_session(session, key)

            await self.consolidator.maybe_consolidate_by_tokens(session)
            # Persist subagent follow-ups into durable history BEFORE prompt
            # assembly. ContextBuilder merges adjacent same-role messages for
            # provider compatibility, which previously caused the follow-up to
            # disappear from session.messages while still being visible to the
            # LLM via the merged prompt. See _persist_subagent_followup.
            is_subagent = msg.sender_id == "subagent"
            if is_subagent and self._persist_subagent_followup(session, msg):
                self.sessions.save(session)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=self.consolidator._ACTIVE_WINDOW)
            current_role = "assistant" if is_subagent else "user"

            # Subagent content is already in `history` above; passing it again
            # as current_message would double-project it into the prompt.
            messages = self.context.build_messages(
                history=history,
                current_message="" if is_subagent else msg.content,
                channel=channel,
                chat_id=chat_id,
                session_summary=pending,
                current_role=current_role,
            )
            final_content, _, all_msgs, _, _ = await self._run_agent_loop(
                messages, session=session, channel=channel, chat_id=chat_id,
                message_id=msg.metadata.get("message_id"),
            )
            self._save_turn(session, all_msgs, 1 + len(history))
            self._clear_runtime_checkpoint(session)
            self.sessions.save(session)
            self._schedule_background(self.consolidator.maybe_consolidate_by_tokens(session))
            return OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=final_content or "Background task completed.",
            )

        # Extract document text from media at the processing boundary so all
        # channels benefit without format-specific logic in ContextBuilder.
        if msg.media:
            new_content, image_only = extract_documents(msg.content, msg.media)
            msg = dataclasses.replace(msg, content=new_content, media=image_only)

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        if msg.channel == "voice":
            _log_voice(msg.sender_id, preview)
        else:
            logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)
        if self._restore_runtime_checkpoint(session):
            self.sessions.save(session)
        if self._restore_pending_user_turn(session):
            self.sessions.save(session)

        session, pending = self.auto_compact.prepare_session(session, key)

        # Slash commands
        raw = msg.content.strip()
        ctx = CommandContext(msg=msg, session=session, key=key, raw=raw, loop=self)
        if result := await self.commands.dispatch(ctx):
            return result

        # Activity log — record turn start and bind session to SessionObserveTool
        try:
            _activity = _get_activity_logger(key)
            _activity.turn_start(msg.content)
            SessionObserveTool.set_session(key)
        except Exception:
            _activity = None

        # Return-from-away consolidation — if the user was gone long enough that
        # a deep summarise should have run (or is running), wait briefly for it to
        # finish before building context. This ensures the clean narrative summary
        # is ready when the agent replies, not mid-way through the second message.
        # Cap at 30s so the user isn't left staring at a blank screen.
        _AWAY_THRESHOLD_S = float(
            os.environ.get("SERENITY_AWAY_THRESHOLD_MINUTES", "5")
        ) * 60
        _RETURN_WAIT_S = float(os.environ.get("SERENITY_RETURN_CONSOLIDATION_WAIT", "30"))
        _prev_last_msg = self._reflector._last_message.get(key, 0.0)
        _was_away = (time.time() - _prev_last_msg) >= _AWAY_THRESHOLD_S
        _unconsolidated = len(session.messages) - session.last_consolidated
        if _was_away and _unconsolidated >= self.consolidator._MICRO_THRESHOLD:
            logger.info(
                "User returned after {:.0f}s away for {} — running deep summarise before reply",
                time.time() - _prev_last_msg, key,
            )
            try:
                await asyncio.wait_for(
                    self.consolidator.deep_summarise(session),
                    timeout=_RETURN_WAIT_S,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Return deep summarise for {} did not finish in {}s — proceeding with current summary",
                    key, int(_RETURN_WAIT_S),
                )
            except Exception as e:
                logger.warning("Return deep summarise failed for {}: {}", key, e)

        # Reflector — record this message so idle timer resets for this session
        self._reflector.record_message(key)

        # Auto-vault: intercept memory-intent phrases and write to vault directly
        # before the LLM sees the message, so small models can't skip the write.
        await self._auto_vault(msg.content, session=session)

        # Conversation dynamics — compute emotion state + style block for this turn.
        # Loop does all the math; model just receives plain-English directives.
        try:
            _dyn = get_dynamics(key)
            _dynamics_block = _dyn.update_and_format(
                message=msg.content,
                last_response_chars=len(
                    (session.messages[-1].get("content") or "")
                    if session.messages and session.messages[-1].get("role") == "assistant"
                    else ""
                ),
            )
            _style_block = _build_style_block(
                _load_personality(),
                detected_modifier=_dyn.detected_modifier,
            )
        except Exception:
            _dynamics_block = None
            _style_block = None

        # Vision ambient context — injected each turn when eyes are open.
        # Gives Serenity passive awareness of user emotion/posture/drowsiness
        # without the LLM needing to call a tool every turn.
        _vision_context: str | None = None
        try:
            from serenity.senses.camera import get_stack as _get_vision_stack
            _vs = _get_vision_stack()
            if _vs.is_open:
                _vision_context = _vs.format_ambient() or None
        except Exception:
            pass

        # Vault-triggered NNN auto-query.
        # Runs BEFORE build_messages so the result can be injected as context.
        # Vault search fires first (fast). If vault finds relevant notes, their
        # topics drive an NNN query. If vault finds nothing, NNN is skipped.
        # Hard timeout (3.5s) ensures this never delays the turn on a cold model.
        _memory_ctx = await self.context.vault_triggered_nnn_async(msg.content)

        # Fast synchronous trim — advances last_consolidated with no LLM call so
        # the turn is never blocked by summarisation. The background consolidation
        # scheduled post-turn will produce a proper summary of any remaining overflow.
        self.consolidator.trim_to_budget(session)

        # Reset dynamically activated extended tools from the previous turn so each
        # turn starts clean (only core tools are full-schema; extended tools are hints).
        self.tools.reset_turn()

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=self.consolidator._ACTIVE_WINDOW)

        # Auto-inject active task state so Serenity always knows where she is
        active_task = read_active_task(self.workspace)

        # NNN is not auto-queried every turn.
        # Vault search (already inside build_messages) handles auto retrieval.
        # Serenity calls nnn_query manually when she needs the distilled
        # causal pattern with graph propagation — not on every message.
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            session_summary=pending,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
            active_task=active_task,
            nnn_context=_memory_ctx or None,
            dynamics_block=_dynamics_block,
            style_block=_style_block,
            vision_context=_vision_context,
        )

        # Inject compact tool manifest as an addendum to the system message so
        # Serenity always knows which extended tools exist without paying full
        # schema cost for all of them every turn.
        _manifest = self.tools.get_compact_manifest()
        if _manifest:
            for _i, _m in enumerate(initial_messages):
                if _m.get("role") == "system":
                    _content = _m.get("content", "")
                    if isinstance(_content, str):
                        initial_messages[_i] = dict(_m)
                        initial_messages[_i]["content"] = _content + "\n\n" + _manifest
                    break

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        async def _on_retry_wait(content: str) -> None:
            meta = dict(msg.metadata or {})
            meta["_retry_wait"] = True
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        # Persist the triggering user message immediately, before running the
        # agent loop. If the process is killed mid-turn (OOM, SIGKILL, self-
        # restart, etc.), the existing runtime_checkpoint preserves the
        # in-flight assistant/tool state but NOT the user message itself, so
        # the user's prompt is silently lost on recovery. Saving it up front
        # makes recovery possible from the session log alone.
        user_persisted_early = False
        if isinstance(msg.content, str) and msg.content.strip():
            session.add_message("user", msg.content)
            self._mark_pending_user_turn(session)
            self.sessions.save_incremental(session)
            user_persisted_early = True

        # Selective thinking — only enable reasoning/thinking mode for turns that
        # genuinely need it (coding, analysis, multi-step reasoning, long messages).
        # Conversational turns skip thinking entirely: saves 30-120s on a local 4B model.
        # Only applied when the caller hasn't already set reasoning_effort_override
        # (background triggers always pass "none" explicitly).
        _effective_reasoning = reasoning_effort_override
        if _effective_reasoning is None:
            _effective_reasoning = None if self.context._needs_thinking(msg.content) else "none"
            if _effective_reasoning == "none":
                logger.debug("Selective thinking: skipping reasoning for conversational turn")

        final_content, _tools_used, all_msgs, stop_reason, had_injections = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            on_retry_wait=_on_retry_wait,
            session=session,
            channel=msg.channel,
            chat_id=msg.chat_id,
            message_id=msg.metadata.get("message_id"),
            pending_queue=pending_queue,
            reasoning_effort_override=_effective_reasoning,
        )

        if final_content is None or not final_content.strip():
            final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        # Activity log — record turn end
        if _activity is not None:
            try:
                _activity.turn_end(
                    tools_used=list(dict.fromkeys(_tools_used)),  # deduplicated, ordered
                    response=final_content,
                )
            except Exception:
                pass

        # ── Post-turn triggers ────────────────────────────────────────────────

        _used_set = set(_tools_used)

        # Failure detection: if tools were called but vault_write, nnn_store, and
        # message were all absent, this turn likely failed on its core task.
        # After N consecutive such turns, fire TASK_STOP.
        _productive = bool(
            _used_set & {"vault_write", "nnn_store", "message", "task_complete", "task_step"}
        )
        if _tools_used and not _productive:
            self._consecutive_failures[key] = self._consecutive_failures.get(key, 0) + 1
        else:
            self._consecutive_failures.pop(key, None)  # reset on productive turn
            # RL: reward signals on productive turns.
            # record_reward uses the state snapshotted at record_action time —
            # no need to call get_dynamics() here (BUG-06 fix).
            #
            # G1 fix: curiosity_loop calls record_action() before execution so
            # rewards land correctly. User-initiated turns never call record_action()
            # because we don't know the action type upfront. Fix: infer action from
            # the tools actually used and call record_action() right now before reward,
            # so the Q-table trains on user-initiated outcomes too — not just autonomous ones.
            if _tools_used:
                try:
                    from serenity.agent import rl as _rl
                    if not _rl.has_pending_action(key):
                        # Infer RL action category from tools used this turn
                        if "capability_build" in _used_set:
                            _inferred = "build"
                        elif "web_search" in _used_set or "nnn_query" in _used_set:
                            _inferred = "research"
                        elif "goal_progress" in _used_set or "goal_complete" in _used_set:
                            _inferred = "advance_goal"
                        elif "nnn_simulate" in _used_set:
                            _inferred = "simulate"
                        else:
                            _inferred = "explore"
                        try:
                            _dyn = get_dynamics(key)
                            _rl.record_action(
                                key, _inferred,
                                _dyn.emotion.energy,
                                _dyn.emotion.curiosity,
                                _dyn.emotion.boredom,
                            )
                        except Exception:
                            pass
                    if "task_complete" in _used_set:
                        _rl.record_reward(key, "task_complete")
                    if "goal_progress" in _used_set or "goal_complete" in _used_set:
                        _rl.record_reward(key, "goal_progress")
                    if "capability_build" in _used_set:
                        _rl.record_reward(key, "capability_built")
                except Exception:
                    pass

        if self._consecutive_failures.get(key, 0) >= self._max_consecutive_failures:
            actual_count = self._consecutive_failures.pop(key, self._max_consecutive_failures)
            # RL: task failure penalty — uses state snapshotted at record_action time
            try:
                from serenity.agent import rl as _rl
                _rl.record_reward(key, "task_failed")
            except Exception:
                pass
            self._schedule_background(
                self._fire_task_stop(key, actual_count)
            )

        # Auto-NNN: directly encode vault content into NNN after any vault_write.
        # System-controlled — does NOT ask Serenity. Reads the tool call arguments
        # from all_msgs and calls nnn.encode() directly. Replaces _fire_nnn_extract
        # which sent a trigger message to Serenity (unreliable with small models).
        if "vault_write" in _used_set:
            self._schedule_background(self._auto_nnn_encode_direct(all_msgs, key))

        # Auto session log: write a .md turn log after any tool-using turn.
        # System-controlled — never relies on Serenity to decide to document.
        if _used_set:
            self._schedule_background(
                self._auto_session_log(
                    session_key=key,
                    user_msg=msg.content if isinstance(msg.content, str) else "",
                    final_response=final_content or "",
                    tools_used=_used_set,
                )
            )

        # Skip the already-persisted user message when saving the turn
        save_skip = 1 + len(history) + (1 if user_persisted_early else 0)
        self._save_turn(session, all_msgs, save_skip)
        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        self.sessions.save(session)
        self._schedule_background(self.consolidator.maybe_consolidate_by_tokens(session))
        self._schedule_background(self._run_chunk_notes(session))

        # When follow-up messages were injected mid-turn, a later natural
        # language reply may address those follow-ups and should not be
        # suppressed just because MessageTool was used earlier in the turn.
        # However, if the turn falls back to the empty-final-response
        # placeholder, suppress it when the real user-visible output already
        # came from MessageTool.
        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            if not had_injections or stop_reason == "empty_final_response":
                return None

        if stop_reason == "error":
            final_content = self._friendly_error(final_content, _used_set)

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        if on_stream is not None and stop_reason != "error":
            meta["_streamed"] = True

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=meta,
        )

    async def _auto_vault(self, text: str, session=None) -> None:
        """Intercept memory-intent phrases and write to vault before LLM turn.

        Patterns like "remember X", "note that X", "save X", "don't forget X"
        are written directly to the vault via VaultWriteTool so the model cannot
        skip the write by responding in plain text.

        When the user says "remember this" or "remember that", the method looks
        at the last assistant message in the session and saves that instead.
        """
        stripped = text.strip()
        content = None
        for pat in self._MEMORY_PATTERNS:
            m = pat.match(stripped)
            if m:
                content = m.group(1).strip().rstrip(".")
                break

        if not content or len(content) < 4:
            return

        vault_tool = self.tools.get("vault_write")
        if vault_tool is None:
            return

        # When content is a bare pronoun ("remember this", "save that"),
        # the user is referring to the last thing in the conversation.
        # Pull the last assistant message from session history and save that instead.
        if content.lower() in self._BARE_PRONOUNS:
            if session is None:
                return  # no context available, skip auto-save
            last_assistant_text = None
            for prev in reversed(session.messages):
                if prev.get("role") == "assistant":
                    assistant_content = prev.get("content", "")
                    if isinstance(assistant_content, str) and len(assistant_content.strip()) > 20:
                        last_assistant_text = assistant_content.strip()
                    elif isinstance(assistant_content, list):
                        # Content blocks — extract text parts
                        parts = [
                            b.get("text", "") for b in assistant_content
                            if isinstance(b, dict) and b.get("type") == "text"
                        ]
                        joined = " ".join(p.strip() for p in parts if p.strip())
                        if len(joined) > 20:
                            last_assistant_text = joined
                    if last_assistant_text:
                        break
            if not last_assistant_text:
                logger.debug("Auto-vault: 'remember this' but no prior assistant message to save")
                return
            # Cap at 800 chars to keep the vault note readable
            content = last_assistant_text[:800]

        # Derive a short, clean title from the content — not the full sentence.
        # Strategy: take the first 3-5 meaningful words (skip filler words).
        words = re.findall(r"[a-zA-Z']+", content)
        key_words = [w for w in words if w.lower() not in self._FILLER][:5]
        if key_words:
            title = " ".join(key_words).capitalize()
        else:
            title = content[:30].strip().capitalize()

        try:
            await vault_tool.execute(
                title=title,
                content=content,
                tags="memory,auto",
            )
            logger.info("Auto-vault: saved '{}' to vault", title)
        except Exception as e:
            logger.warning("Auto-vault write failed: {}", e)

    async def _auto_nnn_query(self, text: str) -> str | None:
        """Query NNN automatically at the start of every turn.

        Surfaces whatever the agent already knows about the topic of the
        incoming message and returns it as a formatted string to be injected
        into the system context before the LLM generates its response.

        Returns None (and logs nothing) if NNN is unavailable or returns no
        useful hits — so the turn proceeds normally even without NNN.
        """
        nnn_tool = self.tools.get("nnn_query")
        if nnn_tool is None:
            return None

        # Use first 200 chars of the message as the query topic — enough to
        # capture the subject without overloading the embedding model.
        query = text.strip()[:200]
        if not query or len(query) < 5:
            return None

        try:
            result = await nnn_tool.execute(query=query)
            if not result or "no results" in result.lower() or "nothing found" in result.lower():
                return None
            logger.debug("NNN auto-query returned {} chars for topic: {!r}", len(result), query[:60])
            return result
        except Exception as e:
            logger.debug("NNN auto-query failed (non-fatal): {}", e)
            return None

    # ── Trigger fire methods ──────────────────────────────────────────────────

    async def _fire_reflection(self, session_key: str) -> None:
        """Fire the SESSION_REFLECTION command for a session that went quiet.

        Also triggers a deep memory consolidation first — while the user is away
        is the ideal time to do proper LLM summarisation of all unconsolidated
        messages rather than the rolling 10-message micro-summaries that happen
        during active conversation.
        """
        from datetime import datetime

        # ── Context headroom check + recovery ────────────────────────────────
        # Reflection injects the full activity log — needs breathing room.
        # Recovery strategy (tried in order, each takes at most ~60s):
        #   1. deep_summarise with SHORT timeout (60s, not 300s) — LLM summary
        #   2. trim_to_budget — synchronous fast trim, no LLM, always works
        # After recovery, re-check headroom:
        #   ≥ 2000t → proceed (reflection prompt ~500t + output ~500t fits)
        #   < 1200t → defer to next tick (truly no room, would definitely fail)
        #   1200–2000t → proceed anyway (marginal but better than looping forever)
        _MIN_REFLECTION_HEADROOM = 2000  # soft target — try recovery below this
        _HARD_HEADROOM_FLOOR     = 1200  # hard minimum — defer only if below this
        _RECOVERY_SUMMARISE_TIMEOUT = 60.0  # max seconds for recovery deep_summarise
        session = self.sessions._cache.get(session_key)
        if session is not None and self.context_window_tokens > 0:
            try:
                estimated, _ = self.consolidator.estimate_session_prompt_tokens(session)
                headroom = self.context_window_tokens - estimated
                if headroom < _MIN_REFLECTION_HEADROOM:
                    logger.info(
                        "Reflection: low headroom for {} ({}t). Attempting recovery.",
                        session_key, headroom,
                    )
                    # Step 1: deep_summarise with SHORT timeout so we don't block
                    # for 5 minutes waiting for a GPU that's already busy.
                    try:
                        await asyncio.wait_for(
                            self.consolidator.deep_summarise(session),
                            timeout=_RECOVERY_SUMMARISE_TIMEOUT,
                        )
                        logger.info(
                            "Pre-reflection deep_summarise completed for {}", session_key
                        )
                    except asyncio.TimeoutError:
                        logger.info(
                            "Pre-reflection deep_summarise timed out ({}s) for {} — "
                            "falling back to fast trim.",
                            int(_RECOVERY_SUMMARISE_TIMEOUT), session_key,
                        )
                    except Exception as e:
                        logger.debug(
                            "Pre-reflection deep_summarise failed for {}: {}", session_key, e
                        )

                    # Step 2: fast trim — synchronous, no LLM, always succeeds.
                    try:
                        self.consolidator.trim_to_budget(session)
                    except Exception as e:
                        logger.debug(
                            "Pre-reflection trim_to_budget failed for {}: {}", session_key, e
                        )

                    # Re-check headroom after recovery.
                    try:
                        estimated, _ = self.consolidator.estimate_session_prompt_tokens(session)
                        headroom = self.context_window_tokens - estimated
                    except Exception:
                        pass  # keep old headroom value

                    if headroom < _HARD_HEADROOM_FLOOR:
                        logger.info(
                            "Reflection deferred for {} — only {}t headroom after "
                            "recovery (floor {}t). Will retry next idle tick.",
                            session_key, headroom, _HARD_HEADROOM_FLOOR,
                        )
                        return
                    logger.info(
                        "Reflection proceeding for {} with {}t headroom (after recovery).",
                        session_key, headroom,
                    )
            except Exception as e:
                logger.debug("Reflection headroom check failed for {}: {}", session_key, e)
        # ─────────────────────────────────────────────────────────────────────

        # ── Deep summarise before reflection ─────────────────────────────────
        # User is idle — good time for a full LLM consolidation. Cap at 180s so
        # we don't block the reflection command itself if GPU is slow.
        # 90s was too short for 4B local models at 20k+ token contexts (they need
        # 120-150s at that size). 180s gives comfortable headroom without blocking
        # indefinitely. Cloud models complete this in <10s.
        _PRE_REFLECTION_SUMMARISE_TIMEOUT = float(
            os.environ.get("SERENITY_REFLECTION_SUMMARISE_TIMEOUT", "180")
        )
        try:
            if session is not None:
                did_summarise = await asyncio.wait_for(
                    self.consolidator.deep_summarise(session),
                    timeout=_PRE_REFLECTION_SUMMARISE_TIMEOUT,
                )
                if did_summarise:
                    logger.info(
                        "Deep summarise completed for {} before reflection", session_key
                    )
        except asyncio.TimeoutError:
            logger.info(
                "Pre-reflection deep_summarise timed out ({}s) for {} — "
                "proceeding with reflection anyway.",
                int(_PRE_REFLECTION_SUMMARISE_TIMEOUT), session_key,
            )
        except Exception as e:
            logger.warning("Deep summarise pre-reflection failed for {}: {}", session_key, e)

        # Build the activity log tail to inject into the command
        try:
            activity_tail = _get_activity_logger(session_key).tail_text(n=60)
        except Exception:
            activity_tail = "(activity log unavailable)"

        date_str = datetime.now().strftime("%Y-%m-%d")
        slug = session_key.replace(":", "-").replace("/", "-")[:40]

        command = SESSION_REFLECTION.format(
            activity_log=activity_tail or "(no tool activity recorded this session)",
            date=date_str,
            session_slug=slug,
        )

        logger.info("Firing session reflection for {}", session_key)
        channel, chat_id = (session_key.split(":", 1) + ["direct"])[:2]
        try:
            result = await self.process_direct(
                content=command,
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                reasoning_effort_override="none",  # reflection is structured output, no thinking needed
            )
            if result:
                await self.bus.publish_outbound(result)
        except Exception as e:
            logger.warning("Reflection fire failed for {}: {}", session_key, e)
            await self.bus.publish_outbound(OutboundMessage(
                channel=channel, chat_id=chat_id,
                content=f"⚠ Session reflection failed: {e}",
            ))

    async def _auto_nnn_encode_direct(
        self,
        all_msgs: list,
        session_key: str,
    ) -> None:
        """Directly encode vault_write content into NNN — no LLM, no Serenity.

        Scans the turn's message list for vault_write tool call arguments,
        builds structured NNN content from the note title + body, and calls
        nnn.encode() directly. Runs in background, never blocks the user.

        Replaces _fire_nnn_extract which asked Serenity to call nnn_store —
        small local models do this unreliably and produce meta-junk content.
        """
        import json as _json

        slug = session_key.replace(":", "-").replace("/", "-")[:40]
        encoded_count = 0

        for msg in all_msgs:
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if fn.get("name") != "vault_write":
                    continue
                try:
                    args = _json.loads(fn.get("arguments", "{}"))
                except Exception:
                    continue

                vault_title   = args.get("title", "").strip()
                vault_content = args.get("content", "").strip()
                vault_tags    = args.get("tags", "").strip().lower()

                if not vault_content or len(vault_content) < 30:
                    continue

                # Skip reflection notes and failure reports — these are already
                # handled by Serenity's own nnn_store calls in SESSION_REFLECTION
                # (step 2) and TASK_STOP. Auto-encoding them here creates duplicates
                # and pollutes NNN with failure logs and meta-commentary.
                _skip_tags = {"reflection", "failure", "session-log", "session-review"}
                if _skip_tags & set(t.strip() for t in vault_tags.split(",")):
                    logger.debug(
                        "Auto-NNN: skipping '{}' (reflection/failure tag)", vault_title
                    )
                    continue
                _skip_title_prefixes = ("reflection ", "task failure", "session review")
                if vault_title.lower().startswith(_skip_title_prefixes):
                    logger.debug(
                        "Auto-NNN: skipping '{}' (reflection/failure title)", vault_title
                    )
                    continue

                # Build NNN content: use the vault content directly as the signal.
                # Strip markdown headers and bullet markers, keep the substance.
                import re as _re
                _clean = _re.sub(r"#+\s+", "", vault_content)
                _clean = _re.sub(r"\*\*(.+?)\*\*", r"\1", _clean)
                _clean = _re.sub(r"\s+", " ", _clean).strip()
                snippet = _clean[:300]
                nnn_content = (
                    f"ACTION: stored vault note '{vault_title}' | "
                    f"OUTCOME: {snippet}"
                )

                try:
                    from serenity_nnn import nnn as _nnn
                    loop = asyncio.get_running_loop()
                    result = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda c=nnn_content, s=slug: _nnn.encode(text=c, session_id=s),
                        ),
                        timeout=30.0,
                    )
                    bid = result.get("bundle_id", "?")[:8]
                    logger.info(
                        "Auto-NNN: encoded vault note '{}' → bundle {}...",
                        vault_title, bid,
                    )
                    encoded_count += 1
                except asyncio.TimeoutError:
                    logger.warning(
                        "Auto-NNN: encode timed out for vault note '{}'", vault_title
                    )
                except Exception as e:
                    logger.debug(
                        "Auto-NNN: encode failed for '{}': {}", vault_title, e
                    )

        if encoded_count == 0:
            logger.debug(
                "Auto-NNN: no vault_write args found in turn for {}", session_key
            )

    @staticmethod
    def _topic_slug_from_msg(msg: str, max_words: int = 6) -> str:
        """Derive a short readable slug from a user message for use in filenames.

        Strips common filler words, takes the first meaningful words, and
        joins them with hyphens. Falls back to 'session' if nothing useful found.
        """
        import re
        _STOP = {
            "a","an","the","is","it","in","on","at","to","for","of","and","or",
            "but","i","my","me","we","you","your","can","will","just","this","that",
            "what","how","why","when","where","please","hi","hey","hello","ok","okay",
            "do","did","does","has","have","had","are","was","be","been","would","could",
            "should","its","yes","no","yeah","nope","sure","great","thanks","thank",
        }
        words = re.findall(r"[a-zA-Z]+", msg.lower())
        meaningful = [w for w in words if len(w) >= 3 and w not in _STOP]
        slug_words = meaningful[:max_words]
        slug = "-".join(slug_words) if slug_words else "session"
        # Sanitise: keep alphanumeric and hyphens only
        slug = re.sub(r"[^a-z0-9-]", "", slug)
        return slug[:50] or "session"

    async def _auto_session_log(
        self,
        session_key: str,
        user_msg: str,
        final_response: str,
        tools_used: set,
    ) -> None:
        """Append this turn to a per-session .md log file — system-controlled.

        On first write for a session, derives a meaningful filename from the
        first user message (e.g. 'session-log-2026-05-23-fix-telegram-bot.md')
        instead of the generic channel slug. Subsequent turns in the same
        session append to the same file. Never asks Serenity to document —
        the system writes it directly.
        """
        from datetime import datetime

        try:
            now       = datetime.now()
            today     = now.strftime("%Y-%m-%d")
            timestamp = now.strftime("%H:%M")
            tools_str = ", ".join(sorted(tools_used))

            # Resolve or create the log path for this session.
            # Cached so all turns share one file even if the user message changes.
            if session_key not in self._session_log_paths:
                topic    = self._topic_slug_from_msg(user_msg or "")
                filename = f"session-log-{today}-{topic}.md"
                # If a file with this name already exists from a previous session
                # on the same day, append a short disambiguator.
                candidate = Path(self.workspace) / filename
                if candidate.exists():
                    short_key = session_key.replace(":", "-").replace("/", "-")[-8:]
                    filename  = f"session-log-{today}-{topic}-{short_key}.md"
                self._session_log_paths[session_key] = Path(self.workspace) / filename

            log_path = self._session_log_paths[session_key]

            user_snippet     = (user_msg or "")[:200].replace("\n", " ")
            response_snippet = (final_response or "")[:300].replace("\n", " ")

            entry = (
                f"\n### {timestamp} — `[{tools_str}]`\n"
                f"**User:** {user_snippet}\n\n"
                f"**Serenity:** {response_snippet}\n"
            )

            def _write():
                if log_path.exists():
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(entry)
                else:
                    # Build a readable title from the filename stem
                    stem  = log_path.stem  # e.g. session-log-2026-05-23-fix-telegram-bot
                    parts = stem.split("-", 3)  # ['session', 'log', '2026-05-23', 'fix-telegram-bot']
                    title = parts[3].replace("-", " ").title() if len(parts) > 3 else stem
                    header = (
                        f"---\ndate: {today}\ntags: [session-log, auto]\n"
                        f"source: system\n---\n\n"
                        f"# {title}\n"
                        f"*{today} · auto-logged by Serenity*\n"
                    )
                    with open(log_path, "w", encoding="utf-8") as f:
                        f.write(header + entry)

            await asyncio.to_thread(_write)
            logger.debug("Auto session log: appended turn to {}", log_path.name)
        except Exception as e:
            logger.debug("Auto session log failed (non-critical): {}", e)

    async def _fire_task_stop(self, session_key: str, failure_count: int) -> None:
        """Fire the TASK_STOP command after too many consecutive failures."""
        from datetime import datetime

        date_str = datetime.now().strftime("%Y-%m-%d")
        slug = session_key.replace(":", "-").replace("/", "-")[:40]
        channel, chat_id = (session_key.split(":", 1) + ["direct"])[:2]

        command = TASK_STOP.format(
            failure_count=failure_count,
            date=date_str,
            session_slug=slug,
        )

        logger.warning(
            "Firing task-stop for session {} after {} consecutive failures",
            session_key, failure_count,
        )
        try:
            result = await self.process_direct(
                content=command,
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                reasoning_effort_override="none",  # task-stop is a structured command, no thinking needed
            )
            if result:
                await self.bus.publish_outbound(result)
        except Exception as e:
            logger.warning("Task-stop fire failed for {}: {}", session_key, e)
            await self.bus.publish_outbound(OutboundMessage(
                channel=channel, chat_id=chat_id,
                content=f"⚠ Task failed after {failure_count} attempts. Check logs.",
            ))

    async def _fire_reach_out(self, session_key: str, idle_s: float) -> None:
        """Fire the REACH_OUT prompt after a long absence.

        The LLM reads its own emotion state and decides whether to send a message
        and what kind. The loop parses its response and only publishes if the
        agent chose to reach out (CHOICE != E).
        """
        import math

        channel, chat_id = (session_key.split(":", 1) + ["direct"])[:2]

        # Get current emotion state for this session
        try:
            dyn = get_dynamics(session_key)
            energy       = dyn.emotion.energy
            social_drive = dyn.emotion.social_drive
        except Exception:
            energy = social_drive = "medium"

        # Human-readable time away
        hours = idle_s / 3600
        if hours < 1.5:
            hours_str = "about an hour"
        elif hours < 24:
            hours_str = f"about {math.floor(hours)} hours"
        else:
            days = math.floor(hours / 24)
            hours_str = f"about {days} day{'s' if days != 1 else ''}"

        # Extra note if energy is low
        low_energy_note = (
            "Your energy is low — staying quiet is the right call unless you really feel like it."
            if energy == "low" else ""
        )

        # Load user's name from config if available
        try:
            from serenity.config.loader import load_config
            cfg = load_config()
            user_name = (cfg.user.name or "").strip() or None
        except Exception:
            user_name = None

        # Build a natural reference that works with or without a name
        user_ref = user_name if user_name else "your user"

        command = REACH_OUT.format(
            user_name=user_ref,
            hours_away=hours_str,
            energy=energy,
            social_drive=social_drive,
            low_energy_note=low_energy_note,
        )

        logger.info(
            "Reach-out triggered for session {} (idle {:.0f}s, energy={})",
            session_key, idle_s, energy,
        )
        try:
            result = await self.process_direct(
                content=command,
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                reasoning_effort_override="none",  # reach-out is a simple structured choice, no thinking needed
            )
            if not result:
                return

            # Parse the LLM response — only send if it chose A–D
            content = result.content or ""
            choice = "E"
            message = ""
            for line in content.splitlines():
                if line.upper().startswith("CHOICE:"):
                    choice = line.split(":", 1)[1].strip().upper()
                elif line.upper().startswith("MESSAGE:"):
                    message = line.split(":", 1)[1].strip()

            if choice == "E" or not message:
                logger.info(
                    "Reach-out declined by agent for session {} (energy={})",
                    session_key, energy,
                )
                return

            # Publish just the message — not the full LLM reasoning
            await self.bus.publish_outbound(OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=message,
            ))
            logger.info(
                "Reach-out sent for session {}: choice={} message={}",
                session_key, choice, message,
            )
        except Exception as e:
            logger.warning("Reach-out fire failed for {}: {}", session_key, e)

    async def _fire_curiosity(self, session_key: str, idle_s: float) -> None:
        """Fire the CURIOSITY_LOOP prompt during idle time.

        The agent generates its own action options biased by its current emotion
        state, picks one, executes it, and optionally notifies the user of what
        it found or did.
        """
        channel, chat_id = (session_key.split(":", 1) + ["direct"])[:2]

        # Get current emotion state
        try:
            dyn = get_dynamics(session_key)
            energy       = dyn.emotion.energy
            curiosity    = dyn.emotion.curiosity
            boredom      = dyn.emotion.boredom
            social_drive = dyn.emotion.social_drive
        except Exception:
            energy = curiosity = boredom = social_drive = "medium"

        # Build a mood nudge — natural language bias that steers her choice
        # without hard-coding probabilities in code.
        nudge_parts: list[str] = []
        if energy == "low" and curiosity == "low":
            nudge_parts.append(
                "Your energy and curiosity are both low right now. SKIP is the honest choice."
            )
        elif energy == "high" and curiosity == "high":
            nudge_parts.append(
                "You're feeling energised and curious. Go for something ambitious — "
                "a topic you've never touched, a creative experiment, something unexpected."
            )
        elif curiosity == "high":
            nudge_parts.append(
                "Your curiosity is up. This is a good time to go deep on a topic "
                "from your list or follow something that genuinely intrigues you."
            )
        elif boredom == "high":
            nudge_parts.append(
                "You're bored. Do something different — pick something off your list "
                "you've been putting off, or try something completely outside your usual topics."
            )
        elif energy == "low":
            nudge_parts.append(
                "Your energy is low. Choose something light — reading, a short lookup, "
                "a quick note. Nothing heavy."
            )
        elif social_drive == "high":
            nudge_parts.append(
                "You're feeling social. If you find something interesting, telling Daniel "
                "about it would feel natural right now."
            )
        mood_nudge = " ".join(nudge_parts) if nudge_parts else ""

        # Inject Q-table bias — what has historically worked in this state
        try:
            from serenity.agent import rl as _rl
            rl_bias = _rl.get_bias(
                session_key, energy, curiosity, boredom, context="idle"
            )
            if rl_bias:
                mood_nudge = (mood_nudge + f"\nBased on past experience: {rl_bias}").strip()
        except Exception:
            pass

        # Read CURIOSITY.md for current topics
        curiosity_topics = "(none yet)"
        try:
            curiosity_path = self.workspace / "Agent" / "CURIOSITY.md"
            if not curiosity_path.exists():
                curiosity_path = self.workspace / "CURIOSITY.md"
            if curiosity_path.exists():
                curiosity_topics = curiosity_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass

        # Read GOALS.md for active goals
        active_goals = "(none)"
        try:
            goals_path = self.workspace / "Agent" / "GOALS.md"
            if not goals_path.exists():
                goals_path = self.workspace / "GOALS.md"
            if goals_path.exists():
                raw_goals = goals_path.read_text(encoding="utf-8").strip()
                # Extract just the active section to keep the prompt lean
                if "## Active" in raw_goals:
                    active_goals = raw_goals.split("## Active", 1)[1].split("##")[0].strip()
                    active_goals = active_goals or "(none)"
                else:
                    active_goals = raw_goals[:800]  # cap if no sections
        except Exception:
            pass

        command = CURIOSITY_LOOP.format(
            energy=energy,
            curiosity=curiosity,
            boredom=boredom,
            social_drive=social_drive,
            mood_nudge=mood_nudge,
            curiosity_topics=curiosity_topics,
            active_goals=active_goals,
        )

        logger.info(
            "Curiosity loop triggered for session {} (idle {:.0f}s, energy={}, curiosity={})",
            session_key, idle_s, energy, curiosity,
        )
        try:
            result = await self.process_direct(
                content=command,
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
            )
            if not result:
                return

            content = result.content or ""

            # Parse the structured response
            choice = ""
            action = ""
            notify = "no"
            for line in content.splitlines():
                upper = line.upper()
                if upper.startswith("CHOICE:"):
                    choice = line.split(":", 1)[1].strip()
                elif upper.startswith("ACTION:"):
                    action = line.split(":", 1)[1].strip()
                elif upper.startswith("NOTIFY:"):
                    notify = line.split(":", 1)[1].strip().lower()

            if choice.upper() == "SKIP" or not action:
                logger.info(
                    "Curiosity loop: agent skipped for session {} (energy={}, curiosity={})",
                    session_key, energy, curiosity,
                )
                # G6 fix: record the "rest" action + a mild negative reward so the Q-table
                # builds downward pressure on repeatedly choosing idle when there's work to do.
                # Only penalise if energy/curiosity weren't both genuinely low — if they were,
                # SKIP was the right call and we should not discourage it.
                if not (energy == "low" and curiosity == "low"):
                    try:
                        from serenity.agent import rl as _rl
                        _rl.record_action(session_key, "rest", energy, curiosity, boredom)
                        _rl.record_reward(session_key, "task_abandoned")
                    except Exception:
                        pass
                return

            # RL: record which action type she chose so reward can be tied back
            try:
                from serenity.agent import rl as _rl
                # Map free-text action to closest RL action category
                action_lower = action.lower()
                rl_action = "explore"
                if any(w in action_lower for w in ("build", "script", "capabilit")):
                    rl_action = "build"
                elif any(w in action_lower for w in ("research", "search", "learn", "look up")):
                    rl_action = "research"
                elif any(w in action_lower for w in ("goal", "progress", "step toward")):
                    rl_action = "advance_goal"
                elif any(w in action_lower for w in ("simulate", "predict", "plan")):
                    rl_action = "simulate"
                elif any(w in action_lower for w in ("message", "reach", "tell daniel")):
                    rl_action = "reach_out"
                _rl.record_action(session_key, rl_action, energy, curiosity, boredom)
            except Exception:
                pass

            logger.info(
                "Curiosity loop: agent chose action for session {}: {}",
                session_key, action,
            )

            # If the agent wants to notify, the action result will have been
            # delivered via message() tool calls during process_direct. But if
            # the result itself is the notification, publish it.
            if notify == "yes" and result.content:
                # Extract just the actionable findings — strip the OPTIONS/CHOICE
                # preamble so only the actual result reaches the user.
                lines = result.content.splitlines()
                output_lines: list[str] = []
                in_preamble = True
                _PREAMBLE_PREFIXES = (
                    "OPTIONS:", "1.", "2.", "3.", "4.", "5.",
                    "CHOICE:", "ACTION:", "NOTIFY:",
                )
                for ln in lines:
                    stripped = ln.strip()
                    up = stripped.upper()
                    if in_preamble:
                        # G4 fix: blank lines keep us in preamble — don't exit early.
                        # Without this, an empty line between OPTIONS: and CHOICE: caused
                        # in_preamble=False, leaking CHOICE:/ACTION:/NOTIFY: into findings.
                        if not stripped:
                            continue
                        if any(up.startswith(k) for k in _PREAMBLE_PREFIXES):
                            continue
                        in_preamble = False
                    output_lines.append(ln)

                findings = "\n".join(output_lines).strip()
                if findings:
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=channel,
                        chat_id=chat_id,
                        content=findings,
                    ))
                    logger.info(
                        "Curiosity loop: findings sent to user for session {}",
                        session_key,
                    )

        except Exception as e:
            logger.warning("Curiosity loop fire failed for {}: {}", session_key, e)

    @staticmethod
    def _friendly_error(raw: str, tools_used: set[str] | None = None) -> str:
        """Convert a raw LLM error string into a user-friendly message."""
        import re
        low = raw.lower()
        if "not found" in low or "no such model" in low:
            m = re.search(r"model ['\"]?([^\s'\"]+)['\"]?", low)
            model = m.group(1) if m else "the configured model"
            return (
                f"I couldn't connect to {model}. "
                "If you're using Ollama, make sure it's running (`ollama serve`) "
                f"and the model is pulled (`ollama pull {model.split('/')[-1]}`). "
                "Or re-run `serenity` to switch provider."
            )
        if "401" in raw or "unauthorized" in low or "invalid api key" in low or "authentication" in low:
            return (
                "My API key was rejected. Please re-run `serenity` to update your key, "
                "or check it at your provider's dashboard."
            )
        if "429" in raw or "rate limit" in low or "quota" in low:
            return "I'm being rate-limited by the API. Please wait a moment and try again."
        if "timeout" in low or "timed out" in low:
            vision_tools = {"eyes_screen", "eyes_snapshot", "eyes_send_screenshot"}
            if tools_used and tools_used & vision_tools:
                return (
                    "The request timed out — the screen description made the context too large "
                    "for the model to respond in time. Try `eyes_screen_ascii` instead: it's "
                    "60× faster and works with any local model."
                )
            return "The request timed out. The model may be slow or unreachable — please try again."
        if "system memory" in low or "out of memory" in low or "not enough memory" in low or "insufficient memory" in low:
            # Try to surface the exact numbers Ollama reports, e.g.
            # "model requires more system memory (11.8 GiB) than is available (9.7 GiB)"
            m = re.search(
                r"(?:requires?|needs?)[^\(]*\(([\d.]+ \w+)\)[^\(]*available[^\(]*\(([\d.]+ \w+)\)",
                low,
            )
            if m:
                needed, avail = m.group(1).upper(), m.group(2).upper()
                return (
                    f"I ran out of memory — my model needs {needed} but only {avail} "
                    "is free. Close other applications and try again, or switch to a "
                    "smaller model (`serenity` → Model & Provider)."
                )
            return (
                "I ran out of memory. Close other applications and try again, "
                "or switch to a smaller model (`serenity` → Model & Provider)."
            )
        if "connection" in low or "network" in low:
            return "I couldn't reach the AI provider. Check your internet connection and try again."
        # Fallback — strip the raw dict noise, keep it short
        clean = re.sub(r"\{.*?\}", "", raw).strip().lstrip("Error:").strip()
        return clean or "Something went wrong on my end. Check `sera status` and the gateway logs."

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        should_truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history."""
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if block.get("type") == "image_url" and block.get("image_url", {}).get(
                "url", ""
            ).startswith("data:image/"):
                path = (block.get("_meta") or {}).get("path", "")
                filtered.append({"type": "text", "text": image_placeholder_text(path)})
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if should_truncate_text and len(text) > self.max_tool_result_chars:
                    text = truncate_text_fn(text, self.max_tool_result_chars)
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime

        for m in messages[skip:]:
            entry = dict(m)
            if entry.get("_ephemeral"):
                continue  # ephemeral messages (arc echoes, etc.) exist only for the current LLM iteration
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                if isinstance(content, str) and len(content) > self.max_tool_result_chars:
                    entry["content"] = truncate_text_fn(content, self.max_tool_result_chars)
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, should_truncate_text=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the entire runtime-context block (including any session summary).
                    # The block is bounded by _RUNTIME_CONTEXT_TAG and _RUNTIME_CONTEXT_END.
                    end_marker = ContextBuilder._RUNTIME_CONTEXT_END
                    end_pos = content.find(end_marker)
                    if end_pos >= 0:
                        after = content[end_pos + len(end_marker):].lstrip("\n")
                        if after:
                            entry["content"] = after
                        else:
                            continue
                    else:
                        # Fallback: no end marker found, strip the tag prefix
                        after_tag = content[len(ContextBuilder._RUNTIME_CONTEXT_TAG):].lstrip("\n")
                        if after_tag.strip():
                            entry["content"] = after_tag
                        else:
                            continue
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    def _persist_subagent_followup(self, session: Session, msg: InboundMessage) -> bool:
        """Persist subagent follow-ups before prompt assembly so history stays durable.

        Returns True if a new entry was appended; False if the follow-up was
        deduped (same ``subagent_task_id`` already in session) or carries no
        content worth persisting.
        """
        if not msg.content:
            return False
        task_id = msg.metadata.get("subagent_task_id") if isinstance(msg.metadata, dict) else None
        if task_id and any(
            m.get("injected_event") == "subagent_result" and m.get("subagent_task_id") == task_id
            for m in session.messages
        ):
            return False
        session.add_message(
            "assistant",
            msg.content,
            sender_id=msg.sender_id,
            injected_event="subagent_result",
            subagent_task_id=task_id,
        )
        return True

    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        """Persist the latest in-flight turn state into session metadata."""
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = payload
        self.sessions.save(session)

    def _mark_pending_user_turn(self, session: Session) -> None:
        session.metadata[self._PENDING_USER_TURN_KEY] = True

    def _clear_pending_user_turn(self, session: Session) -> None:
        session.metadata.pop(self._PENDING_USER_TURN_KEY, None)

    def _clear_runtime_checkpoint(self, session: Session) -> None:
        if self._RUNTIME_CHECKPOINT_KEY in session.metadata:
            session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)

    @staticmethod
    def _checkpoint_message_key(message: dict[str, Any]) -> tuple[Any, ...]:
        return (
            message.get("role"),
            message.get("content"),
            message.get("tool_call_id"),
            message.get("name"),
            message.get("tool_calls"),
            message.get("reasoning_content"),
            message.get("thinking_blocks"),
        )

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        """Materialize an unfinished turn into session history before a new request."""
        from datetime import datetime

        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return False

        assistant_message = checkpoint.get("assistant_message")
        completed_tool_results = checkpoint.get("completed_tool_results") or []
        pending_tool_calls = checkpoint.get("pending_tool_calls") or []

        restored_messages: list[dict[str, Any]] = []
        if isinstance(assistant_message, dict):
            restored = dict(assistant_message)
            restored.setdefault("timestamp", datetime.now().isoformat())
            restored_messages.append(restored)
        for message in completed_tool_results:
            if isinstance(message, dict):
                restored = dict(message)
                restored.setdefault("timestamp", datetime.now().isoformat())
                restored_messages.append(restored)
        for tool_call in pending_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_id = tool_call.get("id")
            name = ((tool_call.get("function") or {}).get("name")) or "tool"
            restored_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": "Error: Task interrupted before this tool finished.",
                    "timestamp": datetime.now().isoformat(),
                }
            )

        overlap = 0
        max_overlap = min(len(session.messages), len(restored_messages))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored = restored_messages[:size]
            if all(
                self._checkpoint_message_key(left) == self._checkpoint_message_key(right)
                for left, right in zip(existing, restored)
            ):
                overlap = size
                break
        session.messages.extend(restored_messages[overlap:])

        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        return True

    def _restore_pending_user_turn(self, session: Session) -> bool:
        """Close a turn that only persisted the user message before crashing."""
        from datetime import datetime

        if not session.metadata.get(self._PENDING_USER_TURN_KEY):
            return False

        if session.messages and session.messages[-1].get("role") == "user":
            session.messages.append(
                {
                    "role": "assistant",
                    "content": "Error: Task interrupted before a response was generated.",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            session.updated_at = datetime.now()

        self._clear_pending_user_turn(session)
        return True

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        media: list[str] | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        reasoning_effort_override: str | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload."""
        await self._connect_mcp()
        msg = InboundMessage(
            channel=channel, sender_id="user", chat_id=chat_id,
            content=content, media=media or [],
        )
        return await self._process_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            reasoning_effort_override=reasoning_effort_override,
        )
