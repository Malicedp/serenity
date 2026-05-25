"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import os
import platform
import re
import time
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Any

from serenity.agent.memory import MemoryStore
from serenity.agent.skills import SkillsLoader
from serenity.utils.helpers import build_assistant_message, current_time_str, detect_image_mime
from serenity.utils.prompt_templates import render_template


class _SystemPromptCache:
    """Cache for build_system_prompt() output with mtime invalidation."""
    __slots__ = ("_prompt", "_mtimes", "_extra_key")

    def __init__(self):
        self._prompt: str | None = None
        self._mtimes: dict[str, float] = {}  # path -> mtime
        self._extra_key: tuple = ()  # (skill_names, channel, vault_query args)

    def _track_file(self, path: Path) -> str:
        """Read a file, track its mtime, return contents."""
        try:
            text = path.read_text(encoding="utf-8")
            self._mtimes[str(path)] = path.stat().st_mtime
            return text
        except (OSError, FileNotFoundError):
            return ""

    def get(self, extra_key: tuple) -> str | None:
        """Return cached prompt if all tracked files unchanged."""
        if self._prompt is None or self._extra_key != extra_key:
            return None
        for path_str, old_mtime in self._mtimes.items():
            try:
                if Path(path_str).stat().st_mtime != old_mtime:
                    return None
            except OSError:
                return None
        return self._prompt

    def set(self, prompt: str, extra_key: tuple) -> None:
        self._prompt = prompt
        self._extra_key = extra_key

    def begin_tracking(self) -> None:
        """Clear tracked mtimes for a fresh build."""
        self._mtimes.clear()


class _VaultFileCache:
    """Cache rglob("*.md") results with directory mtime + TTL invalidation."""
    __slots__ = ("_files", "_dir_mtime", "_workspace", "_last_refresh")

    # Refresh at most every 10 seconds to catch subdirectory changes
    _TTL_SECONDS = 10.0

    def __init__(self, workspace: Path):
        self._workspace = workspace
        self._files: list[Path] | None = None
        self._dir_mtime: float = 0.0
        self._last_refresh: float = 0.0

    def get_md_files(self) -> list[Path]:
        """Return cached file list, refreshing if workspace dir changed or TTL expired."""
        now = time.monotonic()
        try:
            current_mtime = self._workspace.stat().st_mtime
        except OSError:
            return []
        # Return cached if mtime unchanged AND TTL not expired
        if (
            self._files is not None
            and current_mtime == self._dir_mtime
            and (now - self._last_refresh) < self._TTL_SECONDS
        ):
            return self._files
        self._files = list(self._workspace.rglob("*.md"))
        self._dir_mtime = current_mtime
        self._last_refresh = now
        return self._files


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    # Ordered list of bootstrap files — defines load priority.
    # Only files also present in _CONTEXT_FILE_MAP are actually loaded;
    # the map governs WHICH contexts each file appears in.
    BOOTSTRAP_FILES = [
        "SOUL_CORE.md", "SOUL_MEMORY.md", "SOUL_TASKS.md", "SOUL_SCHEDULE.md",
        "USER.md", "AGENTS.md", "TOOLS.md", "HEARTBEAT.md",
        "DYNAMICS.md", "SCRATCHPAD.md", "Character.md", "Preferences.md",
        "CURIOSITY.md", "GOALS.md",
    ]

    # Maps each Agent/ file to the set of context tags that trigger its inclusion.
    # Files NOT listed here are never loaded (e.g. old SOUL.md monolith).
    # Tags: always | chat | task | autonomous | scheduling | game
    _CONTEXT_FILE_MAP: dict[str, set[str]] = {
        "SOUL_CORE.md":     {"always"},
        "SOUL_MEMORY.md":   {"chat", "task", "autonomous"},
        "SOUL_TASKS.md":    {"autonomous", "task"},
        "SOUL_SCHEDULE.md": {"autonomous", "scheduling"},
        "DYNAMICS.md":      {"chat"},
        "SCRATCHPAD.md":    {"task", "autonomous"},
        "HEARTBEAT.md":     {"autonomous"},
        "AGENTS.md":        {"autonomous", "scheduling"},
        "TOOLS.md":         {"chat", "task"},
        "Character.md":     {"chat"},
        "USER.md":          {"chat"},
        "Preferences.md":   {"chat"},
        "CURIOSITY.md":     {"autonomous"},
        "GOALS.md":         {"autonomous", "task"},
    }

    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    _MAX_RECENT_HISTORY = 50
    _RUNTIME_CONTEXT_END = "[/Runtime Context]"

    def __init__(self, workspace: Path, timezone: str | None = None, disabled_skills: list[str] | None = None):
        self.workspace = workspace
        self.timezone = timezone
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace, disabled_skills=set(disabled_skills) if disabled_skills else None)
        self._prompt_cache = _SystemPromptCache()
        self._vault_files = _VaultFileCache(workspace)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        vault_query: str | None = None,
    ) -> str:
        """Build the system prompt from identity, Agent/ bootstrap files, and skills."""
        cache_key = (tuple(skill_names or []), channel, bool(vault_query))
        cached = self._prompt_cache.get(cache_key)
        if cached is not None:
            return cached
        self._prompt_cache.begin_tracking()

        parts = [self._get_identity(channel=channel)]

        # 1. Agent/ — context-filtered bootstrap files
        bootstrap = self._load_bootstrap_files(channel=channel)
        if bootstrap:
            parts.append(bootstrap)

        # Track MEMORY.md mtime for cache invalidation even though MemoryStore reads it
        memory_path = self.memory.memory_file
        if memory_path.exists():
            self._prompt_cache._track_file(memory_path)  # just for mtime tracking
        memory = self.memory.get_memory_context()
        if memory and not self._is_template_content(self.memory.read_memory(), "memory/MEMORY.md"):
            parts.append(f"# Memory\n\n{memory}")

        # 2. Coding-task detection — inject task-journal (any coding hit) and
        # gitnexus + project-log recall (only when the message is heavily code-focused,
        # i.e. > _GITNEXUS_MATCH_THRESHOLD distinct coding keyword matches).
        # Personal vault recall is handled at the user-turn level via nnn_context
        # (vault_triggered_nnn_async) so it does NOT go in the system prompt here.
        # Keeping the system prompt stable across turns lets Ollama reuse its KV cache.
        coding_skills: list[str] = []
        if vault_query and self._is_coding_task(vault_query):
            available_names = {e["name"] for e in self.skills.list_skills(filter_unavailable=False)}

            # task-journal: inject on any coding detection (lightweight context)
            if "task-journal" in available_names:
                coding_skills.append("task-journal")

            # gitnexus + project-log recall: only when coding signal is strong (> threshold)
            coding_match_count = self._count_coding_matches(vault_query)
            if coding_match_count > self._GITNEXUS_MATCH_THRESHOLD:
                if "gitnexus" in available_names:
                    coding_skills.append("gitnexus")

                code_vault_hits = self._search_vault_code(vault_query)
                if code_vault_hits:
                    parts.append(
                        f"# Project log recall ({vault_query[:60]!r}…)\n\n{code_vault_hits}"
                    )

        always_skills = self.skills.get_always_skills()
        dynamic_skills = self.skills.get_dynamic_skills()
        # Combine: always + dynamic + coding (deduplicated, always first)
        all_active = list(dict.fromkeys(
            always_skills
            + [s for s in dynamic_skills if s not in always_skills]
            + [s for s in coding_skills if s not in always_skills]
        ))
        if all_active:
            active_content = self.skills.load_skills_for_context(
                all_active, max_chars_per_skill=self._SKILL_MAX_CHARS
            )
            if active_content:
                parts.append(f"# Active Skills\n\n{active_content}")

        skills_summary = self.skills.build_skills_summary(exclude=set(all_active))
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        # NOTE: Recent History (history.jsonl) is intentionally NOT injected here.
        # In long sessions the raw-archive feedback loop causes history.jsonl to accumulate
        # thousands of tokens of raw message dumps which then re-enter the system prompt,
        # pushing the prompt past the context window even before any session messages.
        # Continuity is handled by:
        #   • session.messages       — live conversation in the current session
        #   • running_summary        — micro-summaries of older parts of this session
        #   • NNN / vault context    — cross-session semantic memory (injected at user turn)
        #   • Dream                  — reads history.jsonl every 2h and updates MEMORY.md
        # If you need to re-enable this for debugging, set SERENITY_INJECT_HISTORY=1.
        if os.environ.get("SERENITY_INJECT_HISTORY"):
            entries = self.memory.read_unprocessed_history(since_cursor=self.memory.get_last_dream_cursor())
            if entries:
                capped = entries[-min(self._MAX_RECENT_HISTORY, 10):]
                parts.append("# Recent Activity\n\n" + "\n".join(
                    f"- [{e['timestamp']}] {e['content'][:120]}" for e in capped
                ))

        result = "\n\n---\n\n".join(parts)
        self._prompt_cache.set(result, cache_key)
        return result

    def _get_identity(self, channel: str | None = None) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
        )

    @staticmethod
    def _build_runtime_context(
        channel: str | None, chat_id: str | None, timezone: str | None = None,
        session_summary: str | None = None,
        dynamics_block: str | None = None,
        style_block: str | None = None,
        vision_context: str | None = None,
    ) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        lines = [f"Current Time: {current_time_str(timezone)}"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if session_summary:
            lines += ["", "[Resumed Session]", session_summary]
        ctx = ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines) + "\n" + ContextBuilder._RUNTIME_CONTEXT_END
        # Append dynamics + style + vision blocks outside the runtime tag so they are
        # not stripped by _save_turn (which strips the runtime context block).
        extras = []
        if dynamics_block:
            extras.append(dynamics_block)
        if style_block:
            extras.append(style_block)
        if vision_context:
            extras.append(vision_context)
        if extras:
            ctx = "\n\n".join([ctx] + extras)
        return ctx

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    # Files in Agent/ that are managed by the system — never injected as context
    _AGENT_SKIP_FILES = frozenset({"MEMORY.md", "history.jsonl"})

    @staticmethod
    def _detect_context(channel: str | None) -> set[str]:
        """Derive active context tags from the channel string.

        Returns a set of tags used to filter _CONTEXT_FILE_MAP entries.
        "always" is always included.  Other tags depend on the channel:
          game       — any game session (minimal context, saves tokens)
          autonomous — heartbeat / cron-triggered turns
          scheduling — scheduling-related channels
          chat+task  — default for all normal user-facing channels
        """
        contexts: set[str] = {"always"}
        ch = (channel or "").lower()

        # Game session — load bare minimum (SOUL_CORE only).
        _is_game = any(x in ch for x in ("game",))
        if _is_game:
            contexts.add("game")
            return contexts

        # Autonomous / heartbeat turns
        if "heartbeat" in ch or ch.startswith("cron"):
            contexts.add("autonomous")
            contexts.add("scheduling")
            return contexts

        # Default: interactive chat + task
        contexts.add("chat")
        contexts.add("task")
        if any(x in ch for x in ("cron", "schedule", "remind")):
            contexts.add("scheduling")
        return contexts

    def _load_bootstrap_files(self, channel: str | None = None) -> str:
        """Load Agent/ .md files filtered by active context.

        Only files listed in _CONTEXT_FILE_MAP whose context tags overlap
        the active context set (derived from channel) are loaded.
        BOOTSTRAP_FILES defines load order; any remaining mapped files found
        in Agent/ are appended alphabetically.
        Files in _AGENT_SKIP_FILES and files absent from _CONTEXT_FILE_MAP
        (e.g. the old monolithic SOUL.md) are always excluded.
        """
        active_contexts = self._detect_context(channel)
        parts: list[str] = []
        agent_dir = self.workspace / "Agent"
        loaded: set[str] = set()

        # 1. Priority files in defined order
        for filename in self.BOOTSTRAP_FILES:
            if filename in self._AGENT_SKIP_FILES:
                continue
            file_contexts = self._CONTEXT_FILE_MAP.get(filename)
            if file_contexts is None or not (file_contexts & active_contexts):
                continue  # not mapped or context mismatch — skip
            agent_path = agent_dir / filename
            root_path  = self.workspace / filename
            file_path  = agent_path if agent_path.exists() else (root_path if root_path.exists() else None)
            if file_path:
                content = self._prompt_cache._track_file(file_path)
                if content:
                    parts.append(f"## {filename}\n\n{content}")
                    loaded.add(filename)

        # 2. Any remaining mapped files in Agent/ not yet loaded (alphabetical)
        if agent_dir.exists():
            for md_path in sorted(agent_dir.glob("*.md")):
                if md_path.name in loaded or md_path.name in self._AGENT_SKIP_FILES:
                    continue
                file_contexts = self._CONTEXT_FILE_MAP.get(md_path.name)
                if file_contexts is None or not (file_contexts & active_contexts):
                    continue  # not in map, or wrong context — skip
                content = self._prompt_cache._track_file(md_path).strip()
                if content:
                    parts.append(f"## {md_path.stem}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _is_template_content(content: str, template_path: str) -> bool:
        """Check if *content* is identical to the bundled template (user hasn't customized it)."""
        try:
            tpl = pkg_files("serenity") / "templates" / template_path
            if tpl.is_file():
                return content.strip() == tpl.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        return False

    # Hard cap on total vault output — prevents a large vault from flooding context
    _VAULT_MAX_CHARS = 800

    # Max characters injected per skill — prevents large SKILL.md files from
    # flooding context on every turn. Model can read the full file if needed.
    _SKILL_MAX_CHARS = 1500

    def _search_vault_semantic(self, query: str) -> str:
        """Semantic vault search using the ChromaDB embedding index.

        Returns formatted string of hits, or empty string if index is empty
        or nothing scores above the similarity floor.
        Falls back to grep (_search_vault) when this returns empty.
        """
        try:
            from serenity.agent.vault_index import search as vault_search
            hits = vault_search(query, n_results=3)
        except Exception:
            return ""
        if not hits:
            return ""
        lines = ["[Vault memory — semantic search]"]
        total_chars = 0
        for h in hits:
            line = f"• {h['filename']} (relevance {h['score']}): {h['snippet']}"
            total_chars += len(line)
            if total_chars > self._VAULT_MAX_CHARS:
                break
            lines.append(line)
        return "\n".join(lines)

    # Stop words to skip when extracting search keywords from the user message
    _STOP_WORDS = frozenset({
        "a", "an", "the", "is", "it", "in", "on", "at", "to", "for", "of",
        "and", "or", "but", "i", "my", "me", "we", "you", "your", "what",
        "was", "are", "be", "do", "did", "does", "has", "have", "had",
        "can", "will", "just", "that", "this", "with", "from", "how",
        "tell", "show", "say", "said", "about", "remember", "know",
    })

    # Patterns that signal the message needs personal memory context
    _VAULT_TRIGGER_PATTERNS = re.compile(
        r"\b("
        r"remember|recall|remind|forgot|forget|note|saved|stored|written down"
        r"|my (name|age|job|goal|goals|project|projects|hobby|hobbies|preference|favourite|favorite|password|address|birthday)"
        r"|what (do i|did i|am i|are my|is my|was my|have i|were my)"
        r"|do i (like|hate|prefer|have|use|own|know|want|need)"
        r"|i (told|mentioned|said|asked|showed|shared)"
        r"|last time|previously|before|earlier|used to|history|past"
        r"|who is|who are|what is .{0,20} called|where (do i|did i|am i)"
        r")\b",
        re.IGNORECASE,
    )

    @classmethod
    def _needs_vault_search(cls, query: str) -> bool:
        """Return True only when the message has signals that personal memory is needed."""
        return bool(cls._VAULT_TRIGGER_PATTERNS.search(query))

    # Threshold below which a message is treated as casual chit-chat (no memory needed)
    _CASUAL_WORD_LIMIT = 8

    # Words that always force a memory lookup even in short messages
    _MEMORY_FORCE_WORDS = re.compile(
        r"\b(remember|recall|remind|forgot|forget|note|project|serenity|task|"
        r"goal|plan|work|build|fix|error|bug|code|why|what|how|when|where|who)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _is_casual_message(cls, text: str) -> bool:
        """Return True when the message is casual chit-chat that needs no memory injection.

        Criteria (all must hold):
          • fewer than _CASUAL_WORD_LIMIT words
          • no question mark
          • no memory-force keyword
        Keeps injection off for greetings, acks, and one-liners like 'go on', 'okay', 'thanks'.
        """
        words = text.split()
        if len(words) >= cls._CASUAL_WORD_LIMIT:
            return False
        if "?" in text:
            return False
        if cls._MEMORY_FORCE_WORDS.search(text):
            return False
        return True

    # Patterns that signal the turn needs deep reasoning (thinking mode on).
    # Anything NOT matching is treated as conversational — thinking skipped.
    _COMPLEX_TURN_PATTERNS = re.compile(
        r"\b("
        r"code|coding|bug|fix|debug|error|exception|crash|traceback|stack trace"
        r"|implement|build|create|design|architect|refactor|review|optimize|optimise"
        r"|analyze|analyse|explain (how|why|what)|compare|evaluate|summarize|summarise"
        r"|write (a|me|the|some)|help me (build|create|write|code|fix|debug|implement|make)"
        r"|step by step|how (do|does|did|can|should|would) (i|you|it|this|that|we)"
        r"|why (does|did|is|are|won'?t|can'?t|doesn'?t|didn'?t)"
        r"|what('?s| is) the (difference|best|right|correct|proper|issue|problem|cause)"
        r"|difference between|pros and cons|trade.?off|versus|vs\b"
        r"|calculate|compute|solve|formula|algorithm"
        r"|plan|strategy|approach|architecture|structure"
        r"|research|look (up|into)|find (out|a way)"
        r"|remember (that|this|my|the)|recall|remind me"
        r")\b",
        re.IGNORECASE,
    )

    @classmethod
    def _needs_thinking(cls, text: str) -> bool:
        """Return True when the turn warrants enabling thinking/reasoning mode.

        Conversational turns (chat, reactions, short questions) skip thinking to
        avoid the 30-120s reasoning overhead on a local 4B model.
        Complex turns (coding, analysis, multi-step planning) keep thinking on.

        Rules (thinking ON if ANY holds):
          • Message is long (>= 30 words) — implies depth
          • Message looks like a coding/technical task
          • Message contains complex-turn keywords (explain, analyze, plan, etc.)
        """
        words = text.split()
        if len(words) >= 30:
            return True
        if cls._is_coding_task(text):
            return True
        if cls._COMPLEX_TURN_PATTERNS.search(text):
            return True
        return False

    # Patterns that signal a coding / software-engineering task
    _CODING_TASK_PATTERNS = re.compile(
        r"\b("
        r"code|coding|program|programming|script|scripts|function|functions"
        r"|bug|fix|debug|error|exception|crash|traceback|stack trace"
        r"|implement|build|write|create|refactor|review|test|tests|testing"
        r"|class|method|module|library|package|import|dependency|dependencies"
        r"|git|commit|push|pull|branch|merge|repo|repository"
        r"|api|endpoint|server|database|query|schema|migration"
        r"|file|files|folder|directory|path|config|configuration"
        r"|deploy|deployment|docker|container|ci|cd|pipeline"
        r"|html|css|javascript|python|typescript|rust|go|java|c\+\+|kotlin|swift"
        r"|install|pip|npm|yarn|cargo|maven|gradle"
        r")\b",
        re.IGNORECASE,
    )

    # Vault sub-paths to search for project logs when coding (in addition to root)
    _CODE_VAULT_SKIP_SUBDIRS = {"memory", "sessions", "cron", "state", "skills", "User", "Daniel"}

    # Minimum number of distinct coding-keyword matches required before gitnexus
    # and project-log recall are injected.  task-journal still injects on any hit.
    # Override with env var SERENITY_GITNEXUS_THRESHOLD (default 5).
    _GITNEXUS_MATCH_THRESHOLD: int = int(os.environ.get("SERENITY_GITNEXUS_THRESHOLD", "5"))

    @classmethod
    def _is_coding_task(cls, query: str) -> bool:
        """Return True when the message looks like a coding / dev task."""
        return bool(cls._CODING_TASK_PATTERNS.search(query))

    @classmethod
    def _count_coding_matches(cls, query: str) -> int:
        """Count how many distinct coding-keyword matches appear in *query*.

        Uses findall on the same regex as _is_coding_task so the threshold
        scales with how heavily code-focused the message actually is.
        A casual "fix the bug" scores 2; a detailed multi-tool request
        scores 6+ and earns gitnexus + project-log recall injection.
        """
        return len(cls._CODING_TASK_PATTERNS.findall(query))

    def _search_vault_code(self, query: str, max_results: int = 4) -> str:
        """Vault search scoped to project logs and past work — only used for coding tasks.

        Unlike the personal-memory search this one does NOT skip the Agent/ or
        project sub-folders; it looks for .md files that look like dev logs,
        project notes, or changelogs.
        """
        words = re.findall(r"[a-zA-Z']+", query.lower())
        keywords = [w for w in words if len(w) >= 4 and w not in self._STOP_WORDS]
        if not keywords:
            return ""

        md_files = self._vault_files.get_md_files()
        if not md_files:
            return ""

        hits: list[tuple[str, str]] = []

        for md_path in md_files:
            try:
                rel = md_path.relative_to(self.workspace)
                # Skip pure system dirs that have no project notes
                if rel.parts and rel.parts[0] in self._CODE_VAULT_SKIP_SUBDIRS:
                    continue
            except ValueError:
                pass
            # Skip agent bootstrap / identity files
            if md_path.name in ("SOUL.md", "SOUL_CORE.md", "SOUL_MEMORY.md", "SOUL_TASKS.md",
                                 "SOUL_SCHEDULE.md", "AGENTS.md", "HEARTBEAT.md", "TOOLS.md",
                                 "USER.md", "MEMORY.md", "SKILLS.md", "BOOTSTRAP.md",
                                 "IDENTITY.md", "SKILL.md", "DYNAMICS.md", "SCRATCHPAD.md",
                                 "GOALS.md", "CURIOSITY.md"):
                continue
            try:
                text = md_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            text_lower = text.lower()
            matched_lines: list[str] = []
            for line in text.splitlines():
                if any(kw in line.lower() for kw in keywords):
                    stripped = line.strip()
                    if stripped and not stripped.startswith("---") and not stripped.startswith("#"):
                        matched_lines.append(stripped)
                    if len(matched_lines) >= 3:
                        break

            if matched_lines:
                try:
                    display_rel = md_path.relative_to(self.workspace)
                    display_name = str(display_rel) if len(display_rel.parts) > 1 else md_path.name
                except ValueError:
                    display_name = md_path.name
                snippet = " | ".join(matched_lines[:2])
                hits.append((display_name, snippet))
            if len(hits) >= max_results:
                break

        if not hits:
            return ""

        lines = ["[Vault project logs — retrieved for coding context]"]
        for filename, snippet in hits:
            lines.append(f"• {filename}: {snippet}")
        return "\n".join(lines)

    def _search_vault(self, query: str, max_results: int = 3) -> str:
        """Grep the Obsidian vault for keywords from the query.

        Returns a formatted string of matching snippets, or empty string if
        nothing relevant is found.
        """
        # Only search when the message actually needs personal memory context
        if not self._needs_vault_search(query):
            return ""

        # Extract meaningful keywords (≥4 chars, not stop words)
        words = re.findall(r"[a-zA-Z']+", query.lower())
        keywords = [w for w in words if len(w) >= 4 and w not in self._STOP_WORDS]
        if not keywords:
            return ""

        md_files = self._vault_files.get_md_files()
        if not md_files:
            return ""

        # Subfolders that are system/template — only the vault root has user notes
        _SKIP_SUBDIRS = {"Agent", "memory", "sessions", "cron", "state", "skills",
                         "User", "Daniel"}  # User/Daniel are legacy template folders

        hits: list[tuple[str, str]] = []  # (filename, snippet)

        for md_path in md_files:
            # Skip system/template subfolders — user notes live only in vault root
            try:
                rel = md_path.relative_to(self.workspace)
                if rel.parts and rel.parts[0] in _SKIP_SUBDIRS:
                    continue
            except ValueError:
                pass
            # Skip bootstrap/system filenames
            if md_path.name in ("SOUL.md", "SOUL_CORE.md", "SOUL_MEMORY.md", "SOUL_TASKS.md",
                                 "SOUL_SCHEDULE.md", "AGENTS.md", "HEARTBEAT.md", "TOOLS.md",
                                 "USER.md", "MEMORY.md", "SKILLS.md", "BOOTSTRAP.md",
                                 "IDENTITY.md", "Experience.md", "DYNAMICS.md", "SCRATCHPAD.md",
                                 "GOALS.md", "CURIOSITY.md", "Character.md", "Preferences.md"):
                continue
            try:
                text = md_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            text_lower = text.lower()
            matched_lines: list[str] = []
            for line in text.splitlines():
                if any(kw in line.lower() for kw in keywords):
                    stripped = line.strip()
                    if stripped and not stripped.startswith("---") and not stripped.startswith("#"):
                        matched_lines.append(stripped)
                    if len(matched_lines) >= 3:
                        break

            if matched_lines:
                # Show subfolder/filename for notes outside the vault root
                try:
                    display_rel = md_path.relative_to(self.workspace)
                    display_name = str(display_rel) if len(display_rel.parts) > 1 else md_path.name
                except ValueError:
                    display_name = md_path.name
                snippet = " | ".join(matched_lines[:2])
                hits.append((display_name, snippet))
            if len(hits) >= max_results:
                break

        if not hits:
            return ""

        lines = ["[Vault memory — retrieved from Obsidian notes]"]
        for filename, snippet in hits:
            lines.append(f"• {filename}: {snippet}")
        return "\n".join(lines)

    def _extract_vault_hints(self, content: str) -> list[str]:
        """Pull search keywords from an NNN bundle's content string.

        Prioritises the AFTER clause (what was learned), then OUTCOME, then
        falls back to all meaningful words in the content.
        """
        for clause in ("AFTER:", "OUTCOME:", "ACTION:"):
            if clause in content:
                segment = content.split(clause, 1)[1].split("|")[0].strip()
                words = re.findall(r"[a-zA-Z']+", segment.lower())
                hits = [w for w in words if len(w) >= 4 and w not in self._STOP_WORDS]
                if hits:
                    return list(dict.fromkeys(hits[:5]))
        words = re.findall(r"[a-zA-Z']+", content.lower())
        return list(dict.fromkeys(
            w for w in words if len(w) >= 5 and w not in self._STOP_WORDS
        ))[:5]

    def _query_nnn_and_vault(self, user_message: str) -> str:
        """Query NNN with the current user message, then grep the vault for
        vault_hint keywords extracted from activated bundles.

        Returns a formatted memory block combining:
          - NNN abstractions (principles, causal rules)
          - Vault facts (exact numbers, detailed notes)

        Returns empty string if neither source has anything relevant.
        """
        # Skip memory lookup entirely for short casual messages (greetings, acks, one-liners).
        # Saves ~200-600ms of embedding time and prevents noisy injections.
        if self._is_casual_message(user_message):
            return ""

        nnn_results = []
        try:
            from serenity_nnn import query as nnn_query_fn
            nnn_results = nnn_query_fn(text=user_message, token_budget=600)
        except Exception:
            pass

        all_hint_keywords: list[str] = []
        nnn_parts: list[str] = []

        for r in nnn_results:
            hint_kws = self._extract_vault_hints(r.content)
            all_hint_keywords.extend(hint_kws)
            hint_str = f"\n   vault_hint: {', '.join(hint_kws)}" if hint_kws else ""
            nnn_parts.append(
                f"[{r.type.upper()} | relevance: {r.activation_score:.2f}]\n"
                f"{r.content}{hint_str}"
            )

        # Vault search — semantic first, grep fallback if index empty
        vault_query = " ".join(dict.fromkeys(all_hint_keywords)) if all_hint_keywords else user_message
        vault_hits = self._search_vault_semantic(vault_query) or self._search_vault(vault_query)

        if not nnn_parts and not vault_hits:
            return ""

        blocks: list[str] = []
        if nnn_parts:
            blocks.append("=== Long-term Memory (NNN) ===\n" + "\n\n".join(nnn_parts))
        if vault_hits:
            blocks.append("=== Vault Facts (Obsidian) ===\n" + vault_hits)

        return "\n\n".join(blocks)

    async def query_memory_async(self, user_message: str, timeout: float = 5.0) -> str:
        """Run NNN + vault memory query in a thread with a hard timeout.

        Safe to await from async context — never blocks the event loop.
        Returns empty string on timeout or any error so the turn always continues.
        """
        import asyncio
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._query_nnn_and_vault, user_message),
                timeout=timeout,
            )
        except Exception:
            return ""

    def _search_vault_broad(self, keywords: list[str], max_results: int = 4) -> str:
        """Keyword vault search WITHOUT the memory-pattern restriction.

        Used by vault_triggered_nnn so NNN auto-query fires on any turn that
        produces vault hits — not just turns that match personal-memory patterns.
        """
        if not keywords:
            return ""
        md_files = self._vault_files.get_md_files()
        if not md_files:
            return ""
        _SKIP = {"Agent", "memory", "sessions", "cron", "state", "skills"}
        _SKIP_NAMES = frozenset({
            "SOUL.md", "SOUL_CORE.md", "SOUL_MEMORY.md", "SOUL_TASKS.md",
            "SOUL_SCHEDULE.md", "AGENTS.md", "HEARTBEAT.md", "TOOLS.md",
            "USER.md", "MEMORY.md", "SKILLS.md", "BOOTSTRAP.md",
            "DYNAMICS.md", "SCRATCHPAD.md", "GOALS.md", "CURIOSITY.md",
            "Character.md", "Preferences.md",
        })
        hits: list[tuple[str, str]] = []
        for md_path in md_files:
            try:
                rel = md_path.relative_to(self.workspace)
                if rel.parts and rel.parts[0] in _SKIP:
                    continue
            except ValueError:
                pass
            if md_path.name in _SKIP_NAMES:
                continue
            try:
                text = md_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            matched: list[str] = []
            for line in text.splitlines():
                if any(kw in line.lower() for kw in keywords):
                    s = line.strip()
                    if s and not s.startswith("---") and not s.startswith("#"):
                        matched.append(s)
                    if len(matched) >= 3:
                        break
            if matched:
                try:
                    name = str(md_path.relative_to(self.workspace))
                except ValueError:
                    name = md_path.name
                hits.append((name, " | ".join(matched[:2])))
            if len(hits) >= max_results:
                break
        if not hits:
            return ""
        lines = ["[Vault — broad search]"]
        for name, snip in hits:
            lines.append(f"• {name}: {snip}")
        return "\n".join(lines)

    def _vault_then_nnn(self, message: str) -> str:
        """Vault-first memory query (synchronous — run in thread).

        1. Semantic vault search (ChromaDB, no pattern restriction)
        2. If nothing, broad keyword grep
        3. If vault found something → extract topics → NNN query
        4. Return vault hits + NNN principles together

        NNN is skipped entirely if vault finds nothing — keeps cold turns fast.
        """
        # Skip memory lookup entirely for short casual messages.
        if self._is_casual_message(message):
            return ""

        # Step 1: semantic vault search (always runs, no pattern gate)
        vault_str = self._search_vault_semantic(message)

        # Step 2: keyword fallback
        if not vault_str:
            words = re.findall(r"[a-zA-Z']+", message.lower())
            kws = [w for w in words if len(w) >= 5 and w not in self._STOP_WORDS]
            if kws:
                vault_str = self._search_vault_broad(kws)

        if not vault_str:
            return ""  # vault found nothing → NNN skipped

        # Step 3: extract topics from vault hits
        words_from_vault = re.findall(r"[a-zA-Z']+", vault_str.lower())
        topics = list(dict.fromkeys(
            w for w in words_from_vault
            if len(w) >= 5 and w not in self._STOP_WORDS
        ))[:6]

        if not topics:
            return vault_str  # vault hit but no extractable topics

        # Step 4: NNN query on vault-derived topics
        topic_query = " ".join(topics)
        nnn_block = ""
        try:
            from serenity_nnn import query as nnn_query_fn
            results = nnn_query_fn(text=topic_query, token_budget=400)
            if results:
                nnn_parts = [
                    f"[{r.type.upper()} | score: {r.activation_score:.2f}]\n{r.content}"
                    for r in results
                ]
                nnn_block = "NNN principles (from vault topics):\n" + "\n\n".join(nnn_parts)
        except Exception:
            pass

        parts = [vault_str]
        if nnn_block:
            parts.append(nnn_block)
        return "\n\n".join(parts)

    async def vault_triggered_nnn_async(self, message: str, timeout: float = float(os.environ.get("SERENITY_NNN_QUERY_TIMEOUT", "1.0"))) -> str:
        """Vault-first NNN query — the main auto-memory call for each turn.

        Vault search runs first (cheap). If vault finds relevant notes, their
        topics drive the NNN query (semantic deepening). If vault finds nothing,
        NNN is skipped entirely — keeps turns fast when there is no memory hit.

        Returns a formatted memory block (vault hits + NNN principles), or ""
        if nothing relevant was found.
        """
        import asyncio
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._vault_then_nnn, message),
                timeout=timeout,
            )
        except Exception:
            return ""

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        session_summary: str | None = None,
        nnn_context: str | None = None,
        active_task: str | None = None,
        dynamics_block: str | None = None,
        style_block: str | None = None,
        vision_context: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        # Vault grep — fast keyword search, no embedding model needed.
        # Injects relevant notes into the system prompt so Serenity sees them
        # before reasoning. Notes with ## nnn_query prompt her to follow up with NNN.
        system_prompt = self.build_system_prompt(
            skill_names, channel=channel, vault_query=current_message or None
        )

        runtime_ctx = self._build_runtime_context(
            channel, chat_id, self.timezone,
            session_summary=session_summary,
            dynamics_block=dynamics_block,
            style_block=style_block,
            vision_context=vision_context,
        )

        # Prepend active task state so Serenity always knows which step she's on
        if active_task:
            runtime_ctx = (
                "[Active Task — resume from here]\n"
                f"{active_task.strip()}\n"
                "[/Active Task]\n\n"
                + runtime_ctx
            )

        # Vault + NNN memory — injected before the runtime context so it's
        # visible to the model at the top of the user turn, not buried.
        if nnn_context:
            runtime_ctx = (
                "[Memory — Vault + NNN]\n"
                f"{nnn_context}\n"
                "[/Memory]\n\n"
                + runtime_ctx
            )

        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        if isinstance(user_content, str):
            merged = f"{runtime_ctx}\n\n{user_content}"
        else:
            merged = [{"type": "text", "text": runtime_ctx}] + user_content
        messages = [
            {"role": "system", "content": system_prompt},
            *history,
        ]
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            messages[-1] = last
            return messages
        messages.append({"role": current_role, "content": merged})
        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def add_tool_result(
        self, messages: list[dict[str, Any]],
        tool_call_id: str, tool_name: str, result: Any,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result})
        return messages

    def add_assistant_message(
        self, messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        messages.append(build_assistant_message(
            content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks,
        ))
        return messages
