"""Memory system: pure file I/O store, lightweight Consolidator, and Dream processor."""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from serenity.utils.prompt_templates import render_template
from serenity.utils.helpers import ensure_dir, estimate_message_tokens, estimate_prompt_tokens_chain, strip_think

from serenity.agent.runner import AgentRunSpec, AgentRunner
from serenity.agent.tools.registry import ToolRegistry
from serenity.utils.gitstore import GitStore

if TYPE_CHECKING:
    from serenity.providers.base import LLMProvider
    from serenity.session.manager import Session, SessionManager


# ---------------------------------------------------------------------------
# MemoryStore — pure file I/O layer
# ---------------------------------------------------------------------------

class MemoryStore:
    """Pure file I/O for memory files: MEMORY.md, history.jsonl, SOUL.md, USER.md."""

    _DEFAULT_MAX_HISTORY = 1000
    _LEGACY_ENTRY_START_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[^\]]*)\]\s*")
    _LEGACY_TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*")
    _LEGACY_RAW_MESSAGE_RE = re.compile(
        r"^\[\d{4}-\d{2}-\d{2}[^\]]*\]\s+[A-Z][A-Z0-9_]*(?:\s+\[tools:\s*[^\]]+\])?:"
    )

    def __init__(self, workspace: Path, max_history_entries: int = _DEFAULT_MAX_HISTORY):
        self.workspace = workspace
        self.max_history_entries = max_history_entries
        self.memory_dir = ensure_dir(workspace / "memory")
        self.history_file = self.memory_dir / "history.jsonl"
        self.legacy_history_file = self.memory_dir / "HISTORY.md"
        self._cursor_file = self.memory_dir / ".cursor"
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"

        # All bootstrap files live in Agent/ (canonical) with vault-root fallback
        # so existing workspaces keep working without migration.
        agent_dir = workspace / "Agent"
        self.memory_file  = self._resolve(agent_dir, workspace, "MEMORY.md")
        self.soul_file    = self._resolve(agent_dir, workspace, "SOUL.md")
        self.user_file    = self._resolve(agent_dir, workspace, "USER.md")

        self._git = GitStore(workspace, tracked_files=[
            "Agent/SOUL.md", "Agent/USER.md", "Agent/MEMORY.md",
            # legacy paths kept so git doesn't lose history on old workspaces
            "SOUL.md", "USER.md", "memory/MEMORY.md",
        ])
        self._maybe_migrate_legacy_history()

    @staticmethod
    def _resolve(agent_dir: Path, root: Path, filename: str) -> Path:
        """Return Agent/<filename> if it exists, otherwise root/<filename>.

        Writes always go to the Agent/ path (created on first write).
        This keeps new vaults tidy while old vaults keep working.
        """
        agent_path = agent_dir / filename
        if agent_path.exists():
            return agent_path
        root_path = root / filename
        if root_path.exists():
            return root_path
        # Neither exists yet — default to Agent/ so first write lands there
        agent_dir.mkdir(parents=True, exist_ok=True)
        return agent_path

    @property
    def git(self) -> GitStore:
        return self._git

    # -- generic helpers -----------------------------------------------------

    @staticmethod
    def read_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _maybe_migrate_legacy_history(self) -> None:
        """One-time upgrade from legacy HISTORY.md to history.jsonl.

        The migration is best-effort and prioritizes preserving as much content
        as possible over perfect parsing.
        """
        if not self.legacy_history_file.exists():
            return
        if self.history_file.exists() and self.history_file.stat().st_size > 0:
            return

        try:
            legacy_text = self.legacy_history_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            logger.exception("Failed to read legacy HISTORY.md for migration")
            return

        entries = self._parse_legacy_history(legacy_text)
        try:
            if entries:
                self._write_entries(entries)
                last_cursor = entries[-1]["cursor"]
                self._cursor_file.write_text(str(last_cursor), encoding="utf-8")
                # Default to "already processed" so upgrades do not replay the
                # user's entire historical archive into Dream on first start.
                self._dream_cursor_file.write_text(str(last_cursor), encoding="utf-8")

            backup_path = self._next_legacy_backup_path()
            self.legacy_history_file.replace(backup_path)
            logger.info(
                "Migrated legacy HISTORY.md to history.jsonl ({} entries)",
                len(entries),
            )
        except Exception:
            logger.exception("Failed to migrate legacy HISTORY.md")

    def _parse_legacy_history(self, text: str) -> list[dict[str, Any]]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []

        fallback_timestamp = self._legacy_fallback_timestamp()
        entries: list[dict[str, Any]] = []
        chunks = self._split_legacy_history_chunks(normalized)

        for cursor, chunk in enumerate(chunks, start=1):
            timestamp = fallback_timestamp
            content = chunk
            match = self._LEGACY_TIMESTAMP_RE.match(chunk)
            if match:
                timestamp = match.group(1)
                remainder = chunk[match.end():].lstrip()
                if remainder:
                    content = remainder

            entries.append({
                "cursor": cursor,
                "timestamp": timestamp,
                "content": content,
            })
        return entries

    def _split_legacy_history_chunks(self, text: str) -> list[str]:
        lines = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        saw_blank_separator = False

        for line in lines:
            if saw_blank_separator and line.strip() and current:
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            if self._should_start_new_legacy_chunk(line, current):
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            current.append(line)
            saw_blank_separator = not line.strip()

        if current:
            chunks.append("\n".join(current).strip())
        return [chunk for chunk in chunks if chunk]

    def _should_start_new_legacy_chunk(self, line: str, current: list[str]) -> bool:
        if not current:
            return False
        if not self._LEGACY_ENTRY_START_RE.match(line):
            return False
        if self._is_raw_legacy_chunk(current) and self._LEGACY_RAW_MESSAGE_RE.match(line):
            return False
        return True

    def _is_raw_legacy_chunk(self, lines: list[str]) -> bool:
        first_nonempty = next((line for line in lines if line.strip()), "")
        match = self._LEGACY_TIMESTAMP_RE.match(first_nonempty)
        if not match:
            return False
        return first_nonempty[match.end():].lstrip().startswith("[RAW]")

    def _legacy_fallback_timestamp(self) -> str:
        try:
            return datetime.fromtimestamp(
                self.legacy_history_file.stat().st_mtime,
            ).strftime("%Y-%m-%d %H:%M")
        except OSError:
            return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _next_legacy_backup_path(self) -> Path:
        candidate = self.memory_dir / "HISTORY.md.bak"
        suffix = 2
        while candidate.exists():
            candidate = self.memory_dir / f"HISTORY.md.bak.{suffix}"
            suffix += 1
        return candidate

    # -- MEMORY.md (long-term facts) -----------------------------------------

    def read_memory(self) -> str:
        return self.read_file(self.memory_file)

    def write_memory(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    # -- SOUL.md -------------------------------------------------------------

    def read_soul(self) -> str:
        return self.read_file(self.soul_file)

    def write_soul(self, content: str) -> None:
        self.soul_file.write_text(content, encoding="utf-8")

    # -- USER.md -------------------------------------------------------------

    def read_user(self) -> str:
        return self.read_file(self.user_file)

    def write_user(self, content: str) -> None:
        self.user_file.write_text(content, encoding="utf-8")

    # -- context injection (used by context.py) ------------------------------

    def get_memory_context(self) -> str:
        long_term = self.read_memory()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    # -- history.jsonl — append-only, JSONL format ---------------------------

    def append_history(self, entry: str) -> int:
        """Append *entry* to history.jsonl and return its auto-incrementing cursor."""
        cursor = self._next_cursor()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        record = {"cursor": cursor, "timestamp": ts, "content": strip_think(entry.rstrip()) or entry.rstrip()}
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._cursor_file.write_text(str(cursor), encoding="utf-8")
        return cursor

    def _next_cursor(self) -> int:
        """Read the current cursor counter and return next value."""
        if self._cursor_file.exists():
            try:
                return int(self._cursor_file.read_text(encoding="utf-8").strip()) + 1
            except (ValueError, OSError):
                pass
        # Fallback: read last line's cursor from the JSONL file.
        last = self._read_last_entry()
        if last and last.get("cursor"):
            return last["cursor"] + 1
        return 1

    def read_unprocessed_history(self, since_cursor: int) -> list[dict[str, Any]]:
        """Return history entries with cursor > *since_cursor*."""
        return [e for e in self._read_entries() if e.get("cursor", 0) > since_cursor]

    def compact_history(self) -> None:
        """Drop oldest entries if the file exceeds *max_history_entries*."""
        if self.max_history_entries <= 0:
            return
        entries = self._read_entries()
        if len(entries) <= self.max_history_entries:
            return
        kept = entries[-self.max_history_entries:]
        self._write_entries(kept)

    # -- JSONL helpers -------------------------------------------------------

    def _read_entries(self) -> list[dict[str, Any]]:
        """Read all entries from history.jsonl."""
        entries: list[dict[str, Any]] = []
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except FileNotFoundError:
            pass
        return entries

    def _read_last_entry(self) -> dict[str, Any] | None:
        """Read the last entry from the JSONL file efficiently.

        Tries a fast tail-read first (4 KB). Falls back to a full sequential
        scan if the tail read cannot find a valid JSON line — this handles the
        edge case where the last entry is larger than the read window.
        """
        try:
            with open(self.history_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None

                # Fast path: read the last 4 KB and try to parse the last line
                read_size = min(size, 4096)
                f.seek(size - read_size)
                tail = f.read().decode("utf-8", errors="replace")
                lines = [ln for ln in tail.split("\n") if ln.strip()]
                if lines:
                    try:
                        return json.loads(lines[-1])
                    except json.JSONDecodeError:
                        pass  # last entry is truncated — fall through to full scan

                # Slow path: full sequential scan (large entry > 4 KB)
                f.seek(0)
                last: dict[str, Any] | None = None
                for raw_line in f:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        last = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                return last

        except FileNotFoundError:
            return None
        except Exception:
            return None

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        """Overwrite history.jsonl with the given entries."""
        with open(self.history_file, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # -- dream cursor --------------------------------------------------------

    def get_last_dream_cursor(self) -> int:
        if self._dream_cursor_file.exists():
            try:
                return int(self._dream_cursor_file.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pass
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        self._dream_cursor_file.write_text(str(cursor), encoding="utf-8")

    # -- message formatting utility ------------------------------------------

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    def raw_archive(self, messages: list[dict]) -> None:
        """Fallback: dump raw messages to history.jsonl without LLM summarization."""
        self.append_history(
            f"[RAW] {len(messages)} messages\n"
            f"{self._format_messages(messages)}"
        )
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages", len(messages)
        )



# ---------------------------------------------------------------------------
# Consolidator — lightweight token-budget triggered consolidation
# ---------------------------------------------------------------------------


class Consolidator:
    """Lightweight consolidation: summarizes evicted messages into history.jsonl."""

    _MAX_CONSOLIDATION_ROUNDS = 5
    _MAX_CHUNK_MESSAGES = 60  # hard cap per consolidation round

    _SAFETY_BUFFER = 1024  # extra headroom for tokenizer estimation drift
    # Cap on session.metadata["running_summary"] to prevent unbounded growth
    # in very long sessions. 6000 chars ≈ 1500 tokens — enough for ~3 micro-
    # summaries worth of context. Oldest content is dropped when exceeded.
    _MAX_RUNNING_SUMMARY_CHARS = 6000
    # NNN context + vault hits + dynamics/style blocks get injected AFTER trim_to_budget
    # runs but BEFORE the LLM call, so trim must leave room for them.
    # Worst case: NNN 600 tokens + vault 200 tokens + dynamics 300 tokens + user msg 500 tokens.
    _INJECTION_OVERHEAD = 2048

    # Active context window — how many recent messages the LLM actually sees.
    # Must match the max_messages= argument passed to session.get_history() in loop.py.
    # Everything outside this window is covered by running_summary (idle/shutdown summary).
    _ACTIVE_WINDOW = 40     # last 40 messages (~20 turns) fed to the LLM each turn

    # Micro-summary is no longer called every turn — see deep_summarise() and
    # the shutdown/idle hooks in loop.py and commands.py.
    _MICRO_THRESHOLD = 20   # kept for compatibility (idle/shutdown trigger check)
    _MICRO_CHUNK    = 10    # kept for compatibility

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        max_completion_tokens: int = 4096,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = max_completion_tokens
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: dict[str, asyncio.Lock] = {}

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    # How many recent messages to keep when the context is over budget.
    # Old messages are already captured in running_summary — no need to keep them.
    _KEEP_ON_TRIM = 6

    def trim_to_budget(self, session: Session) -> bool:
        """Fast inline trim — no LLM, no I/O beyond saving the session pointer.

        When the session is over the token budget, advances last_consolidated
        to keep only the most recent _KEEP_ON_TRIM messages. The older messages
        are already covered by running_summary / chunk_notes so nothing is lost.
        Returns True if trimming was needed, False if already within budget.

        Use this on the critical path (before the LLM call) so the turn is
        never blocked by an LLM summarisation.
        """
        if not session.messages or self.context_window_tokens <= 0:
            return False
        budget = self.context_window_tokens - self.max_completion_tokens - self._SAFETY_BUFFER - self._INJECTION_OVERHEAD
        try:
            estimated, _ = self.estimate_session_prompt_tokens(session)
        except Exception:
            return False
        if estimated <= budget:
            return False

        # Jump straight to keeping only the last _KEEP_ON_TRIM messages.
        # Trimming 2 messages at a time when the session is 12k tokens over budget
        # means 20+ rounds of trim across 20+ turns — the old content dominates
        # the entire context until it's finally gone. A hard cut is cleaner:
        # the gist of older turns is already in running_summary.
        unconsolidated = session.messages[session.last_consolidated:]
        if len(unconsolidated) <= self._KEEP_ON_TRIM:
            # Already at minimum — nothing safe to drop
            return False

        keep_from = len(session.messages) - self._KEEP_ON_TRIM
        # Snap to a user-turn boundary so we don't start mid-assistant-turn
        for idx in range(keep_from, len(session.messages)):
            if session.messages[idx].get("role") == "user":
                keep_from = idx
                break

        if keep_from <= session.last_consolidated:
            return False

        chunk = session.messages[session.last_consolidated:keep_from]
        if not chunk:
            return False

        logger.info(
            "Fast trim for {}: {}/{} tokens — dropping {} msgs to keep last {}",
            session.key, estimated, self.context_window_tokens, len(chunk), self._KEEP_ON_TRIM,
        )
        self.store.raw_archive(chunk)
        session.last_consolidated = keep_from
        self.sessions.save(session)
        return True

    async def micro_summarise(self, session: Session) -> bool:
        """Layer 1 — rolling micro-summaries. Fires as a background task after every turn.

        When the unconsolidated message count crosses _MICRO_THRESHOLD, summarises
        the oldest _MICRO_CHUNK messages via LLM and replaces them with a compact
        note. The summary is written to workspace/memory/session_summaries/<slug>/
        and stored in session.metadata["running_summary"] so it's injected into
        every subsequent turn as context — the agent always knows what was said
        earlier in the same session even after the raw messages are gone.

        Never blocks a turn — always runs in the background.
        """
        unconsolidated = len(session.messages) - session.last_consolidated
        if unconsolidated < self._MICRO_THRESHOLD:
            return False

        lock = self.get_lock(session.key)
        async with lock:
            # Re-check inside lock — another task may have already summarised
            unconsolidated = len(session.messages) - session.last_consolidated
            if unconsolidated < self._MICRO_THRESHOLD:
                return False

            end_idx = min(session.last_consolidated + self._MICRO_CHUNK, len(session.messages))
            chunk = session.messages[session.last_consolidated:end_idx]
            if not chunk:
                return False

            logger.info(
                "Micro-summary for {}: summarising {} msgs ({} unconsolidated)",
                session.key, len(chunk), unconsolidated,
            )

            # Hard time budget — micro-summary must not hog the local GPU.
            # If the LLM is busy with a real user request, the archive call
            # would queue behind it and add 5+ min of latency before the next
            # user turn can start. 90 s is generous for a 10-message summary;
            # if it still exceeds that, skip and let the next turn retry.
            _MICRO_BUDGET_S = float(os.environ.get("SERENITY_MICRO_SUMMARY_TIMEOUT", "90"))
            try:
                summary = await asyncio.wait_for(self.archive(chunk), timeout=_MICRO_BUDGET_S)
            except asyncio.TimeoutError:
                logger.warning(
                    "Micro-summary for {} skipped — LLM did not respond within {}s "
                    "(GPU likely busy with a user request; will retry next turn).",
                    session.key, int(_MICRO_BUDGET_S),
                )
                return False
            if not summary:
                return False

            # Write .md file to workspace/memory/session_summaries/<slug>/
            slug = session.key.replace(":", "-").replace("/", "-")[:40]
            summaries_dir = self.store.workspace / "memory" / "session_summaries" / slug
            summaries_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
            summary_file = summaries_dir / f"{ts}.md"
            summary_file.write_text(
                f"---\nsession: {session.key}\ndate: {ts}\n---\n\n{summary}\n",
                encoding="utf-8",
            )

            # Advance pointer and update running summary in metadata
            session.last_consolidated = end_idx
            existing = session.metadata.get("running_summary", "")
            combined = f"{existing}\n\n{summary}".strip() if existing else summary
            # Cap to prevent unbounded growth in marathon sessions.
            # Keep the tail — newest summaries are most relevant.
            if len(combined) > self._MAX_RUNNING_SUMMARY_CHARS:
                combined = combined[-self._MAX_RUNNING_SUMMARY_CHARS:]
                # Trim to a clean paragraph boundary so we don't start mid-sentence
                first_para = combined.find("\n\n")
                if 0 < first_para < self._MAX_RUNNING_SUMMARY_CHARS // 4:
                    combined = combined[first_para:].lstrip()
            session.metadata["running_summary"] = combined
            self.sessions.save(session)
            logger.info("Micro-summary written: {}", summary_file.name)
            return True

    async def deep_summarise(self, session: Session) -> bool:
        """Layer 2 — idle deep consolidation. Fires when the user goes quiet.

        Unlike micro_summarise (which processes 10 messages at a time in rolling
        chunks during the conversation), this takes ALL unconsolidated messages at
        once and produces a single coherent narrative summary. The result replaces
        the patchwork of micro-summaries with one clean record of what happened
        this session.

        Designed to be called from _fire_reflection (10-min idle trigger) so it
        only runs when the user is not actively messaging.
        """
        unconsolidated = len(session.messages) - session.last_consolidated
        if unconsolidated < 2:
            # Nothing meaningful to summarise
            return False

        lock = self.get_lock(session.key)
        async with lock:
            # Re-check inside lock
            unconsolidated = len(session.messages) - session.last_consolidated
            if unconsolidated < 2:
                return False

            chunk = session.messages[session.last_consolidated:]
            if not chunk:
                return False

            logger.info(
                "Deep summarise for {}: consolidating {} unconsolidated msgs (user idle)",
                session.key, unconsolidated,
            )

            # Deep summaries get more time than micro-summaries — user is away
            # so there's no urgency, but we still cap to avoid blocking forever.
            _DEEP_BUDGET_S = float(os.environ.get("SERENITY_DEEP_SUMMARY_TIMEOUT", "300"))
            try:
                summary = await asyncio.wait_for(
                    self.archive(chunk), timeout=_DEEP_BUDGET_S
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Deep summarise for {} timed out after {}s — skipping.",
                    session.key, int(_DEEP_BUDGET_S),
                )
                return False

            if not summary:
                return False

            # Write a dated summary file
            slug = session.key.replace(":", "-").replace("/", "-")[:40]
            summaries_dir = self.store.workspace / "memory" / "session_summaries" / slug
            summaries_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
            summary_file = summaries_dir / f"{ts}_deep.md"
            summary_file.write_text(
                f"---\nsession: {session.key}\ndate: {ts}\ntype: deep\n---\n\n{summary}\n",
                encoding="utf-8",
            )

            # Replace running_summary entirely with this clean narrative.
            # Clear chunk_notes — they are superseded by the proper summary.
            session.metadata["running_summary"] = summary[: self._MAX_RUNNING_SUMMARY_CHARS]
            session.metadata.pop("chunk_notes", None)
            session.metadata.pop("chunk_notes_ptr", None)
            session.last_consolidated = len(session.messages)
            self.sessions.save(session)
            logger.info(
                "Deep summary complete for {} — {} msgs consolidated, written to {}",
                session.key, unconsolidated, summary_file.name,
            )
            return True

    # ── _MAX_CHUNK_NOTES: cap so metadata stays small ──────────────────────────
    _MAX_CHUNK_NOTES = 10   # covers up to 100 messages beyond the active window
    _CHUNK_NOTE_SIZE = 10   # generate a note per 10-message chunk

    def generate_chunk_notes_if_needed(self, session: Session) -> None:
        """Generate zero-LLM breadcrumb notes for messages that have fallen outside
        the active context window.

        Called as a background task after each turn. Checks how many messages are
        now sitting outside the _ACTIVE_WINDOW and generates a TextRank + NER note
        for each 10-message chunk that doesn't have a note yet.

        Notes are stored in session.metadata["chunk_notes"] (list of strings, capped
        at _MAX_CHUNK_NOTES). They are cleared when deep_summarise() runs because
        the proper summary supersedes them.
        """
        from serenity.agent.chunk_notes import generate_chunk_note

        total = len(session.messages)
        # Messages visible to LLM = last _ACTIVE_WINDOW of unconsolidated
        unconsolidated_start = session.last_consolidated
        unconsolidated = session.messages[unconsolidated_start:]
        if len(unconsolidated) <= self._ACTIVE_WINDOW:
            return  # everything fits in the active window, nothing to note

        # Messages outside the window = unconsolidated[:-_ACTIVE_WINDOW]
        overflow_end = total - self._ACTIVE_WINDOW  # absolute index
        noted_up_to = session.metadata.get("chunk_notes_ptr", unconsolidated_start)

        if noted_up_to >= overflow_end:
            return  # already noted everything outside the window

        existing_notes: list[str] = session.metadata.get("chunk_notes", [])

        # Generate notes for un-noted overflow chunks
        ptr = noted_up_to
        while ptr < overflow_end:
            chunk_end = min(ptr + self._CHUNK_NOTE_SIZE, overflow_end)
            chunk = session.messages[ptr:chunk_end]
            if chunk:
                try:
                    note = generate_chunk_note(chunk, ptr, chunk_end)
                    if note:
                        existing_notes.append(note)
                except Exception as e:
                    logger.debug("Chunk note generation failed for {}: {}", session.key, e)
            ptr = chunk_end

        # Cap to avoid metadata bloat — keep newest notes
        if len(existing_notes) > self._MAX_CHUNK_NOTES:
            existing_notes = existing_notes[-self._MAX_CHUNK_NOTES:]

        session.metadata["chunk_notes"] = existing_notes
        session.metadata["chunk_notes_ptr"] = ptr
        self.sessions.save(session)
        logger.debug(
            "Chunk notes for {}: {} notes covering msgs up to {}",
            session.key, len(existing_notes), ptr,
        )

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    def _cap_consolidation_boundary(
        self,
        session: Session,
        end_idx: int,
    ) -> int | None:
        """Clamp the chunk size without breaking the user-turn boundary.

        Critical invariant: never let the chunk end between a tool_use call and
        its tool_result.  If we archive the tool_use but leave the tool_result
        outside the chunk, the model's next turn has an orphaned result it can't
        match — causing "I didn't receive any tool results" errors.
        """
        start = session.last_consolidated
        if end_idx - start <= self._MAX_CHUNK_MESSAGES:
            candidate = end_idx
        else:
            capped_end = start + self._MAX_CHUNK_MESSAGES
            candidate = None
            for idx in range(capped_end, start, -1):
                if session.messages[idx].get("role") == "user":
                    candidate = idx
                    break

        if candidate is None:
            return None

        # Walk candidate back if it would split a tool_use / tool_result pair.
        # A tool_use lives in an assistant message; the result follows immediately
        # as role="tool" or a user message with type="tool_result" content.
        msgs = session.messages
        while candidate > start:
            prev = msgs[candidate - 1] if candidate > 0 else None
            if prev is None:
                break
            # If the message just before the boundary is an assistant tool_use,
            # the tool_result sits AT candidate — pulling it into the chunk would
            # be fine, but we must not leave the tool_use without its result.
            # Walk back one more user-turn boundary.
            if prev.get("role") == "assistant":
                is_tool_use = False
                # OpenAI format: tool calls are in the top-level tool_calls field
                if prev.get("tool_calls"):
                    is_tool_use = True
                else:
                    # Anthropic format: tool_use blocks inside content
                    content = prev.get("content", "")
                    if isinstance(content, list):
                        is_tool_use = any(
                            isinstance(b, dict) and b.get("type") == "tool_use"
                            for b in content
                        )
                    elif isinstance(content, str) and '"type": "tool_use"' in content:
                        is_tool_use = True
                if is_tool_use:
                    # Find the previous user-turn before this tool_use
                    new_candidate = None
                    for idx in range(candidate - 2, start - 1, -1):
                        if msgs[idx].get("role") == "user":
                            new_candidate = idx
                            break
                    if new_candidate is None:
                        return None   # can't find a safe boundary — skip consolidation
                    candidate = new_candidate
                    continue
            break

        if candidate <= start:
            return None
        return candidate

    def estimate_session_prompt_tokens(self, session: Session) -> tuple[int, str]:
        """Estimate current prompt size for the normal session history view.

        Uses the same _ACTIVE_WINDOW as the real LLM call so trim_to_budget
        only fires when the actual prompt is over budget — not when old messages
        outside the window push the theoretical total over the limit.
        """
        history = session.get_history(max_messages=self._ACTIVE_WINDOW)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    async def archive(self, messages: list[dict]) -> str | None:
        """Summarize messages via LLM and append to history.jsonl.

        Returns the summary text on success, None if nothing to archive.
        """
        if not messages:
            return None
        try:
            formatted = MemoryStore._format_messages(messages)
            response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/consolidator_archive.md",
                            strip=True,
                        ),
                    },
                    {"role": "user", "content": formatted},
                ],
                tools=None,
                tool_choice=None,
                reasoning_effort="none",  # consolidation is structured summarisation, no thinking needed
            )
            if response.finish_reason == "error":
                raise RuntimeError(f"LLM returned error: {response.content}")
            summary = response.content or "[no summary]"
            self.store.append_history(summary)
            return summary
        except Exception:
            # LLM summary failed — raw-dump the messages so they're still archived,
            # then return a truthy value so the caller advances last_consolidated.
            # Without this, the same chunk gets re-targeted on every subsequent turn,
            # creating an infinite consolidation retry loop that blocks the agent.
            logger.warning("Consolidation LLM call failed, raw-dumping to history")
            self.store.raw_archive(messages)
            return "[raw dump]"

    async def maybe_consolidate_by_tokens(self, session: Session) -> None:
        """Loop: archive old messages until prompt fits within safe budget.

        The budget reserves space for completion tokens and a safety buffer
        so the LLM request never exceeds the context window.
        """
        if not session.messages or self.context_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            budget = self.context_window_tokens - self.max_completion_tokens - self._SAFETY_BUFFER - self._INJECTION_OVERHEAD
            # Target 80% of budget — trim enough to have headroom, but don't
            # aggressively halve. budget // 2 was causing spurious consolidation
            # rounds at 21k/40k tokens (well within budget) because the loop
            # kept trying to reach ~16k. Now one round is usually enough.
            target = int(budget * 0.80)
            try:
                estimated, source = self.estimate_session_prompt_tokens(session)
            except Exception:
                logger.exception("Token estimation failed for {}", session.key)
                estimated, source = 0, "error"
            if estimated <= 0:
                return
            unconsolidated_count = len(session.messages) - session.last_consolidated
            if estimated < budget:
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}, msgs={}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    unconsolidated_count,
                )
                return
            # Log that we're about to consolidate so it's easy to spot
            logger.info(
                "Token consolidation triggered {}: {}/{} via {}, msgs={}, budget={}",
                session.key,
                estimated,
                self.context_window_tokens,
                source,
                unconsolidated_count,
                budget,
            )

            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    return

                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    return

                end_idx = boundary[0]
                end_idx = self._cap_consolidation_boundary(session, end_idx)
                if end_idx is None:
                    logger.debug(
                        "Token consolidation: no capped boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    return

                chunk = session.messages[session.last_consolidated:end_idx]
                if not chunk:
                    return

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )
                # Hard timeout — archive() queues an LLM call on Ollama.
                # Without a cap it blocks the GPU indefinitely when the user
                # is actively messaging, causing both to timeout.
                _CONSOLIDATION_TIMEOUT_S = float(
                    os.environ.get("SERENITY_CONSOLIDATION_TIMEOUT", "90")
                )
                try:
                    result = await asyncio.wait_for(
                        self.archive(chunk), timeout=_CONSOLIDATION_TIMEOUT_S
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Token consolidation round {} archive timed out ({}s) for {} — "
                        "skipping remaining rounds (GPU likely busy with user request)",
                        round_num, int(_CONSOLIDATION_TIMEOUT_S), session.key,
                    )
                    return
                if not result:
                    return
                session.last_consolidated = end_idx
                self.sessions.save(session)

                try:
                    estimated, source = self.estimate_session_prompt_tokens(session)
                except Exception:
                    logger.exception("Token estimation failed for {}", session.key)
                    estimated, source = 0, "error"
                if estimated <= 0:
                    return


# ---------------------------------------------------------------------------
# Dream — history.jsonl janitor (no LLM, no file edits)
# ---------------------------------------------------------------------------


class Dream:
    """Lightweight history.jsonl janitor.

    Advances the dream cursor and compacts history.jsonl so it never grows
    unbounded. Does NOT touch SOUL.md, USER.md, MEMORY.md, or any file in
    Agent/ — those are user-owned and must not be auto-modified.

    Memory continuity is handled by the layered memory system:
      - session.messages + running_summary  — within a session
      - vault (full notes) + NNN (causal principles)  — cross-session, retrieved per turn
      - MEMORY.md  — user-curated always-visible facts, never auto-written

    Dream's only job: keep history.jsonl clean so it does not accumulate
    indefinitely between vault/NNN writes.
    """

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider | None = None,
        model: str | None = None,
        max_batch_size: int = 100,
        **_kwargs: Any,
    ):
        self.store = store
        # provider / model retained for signature compatibility — not used
        self.provider = provider
        self.model = model
        self.max_batch_size = max_batch_size

    async def run(self) -> bool:
        """Advance the dream cursor and compact history.jsonl.

        Returns True if there were entries to process, False if already up to date.
        Never calls the LLM. Never modifies any file in Agent/.
        """
        last_cursor = self.store.get_last_dream_cursor()
        entries = self.store.read_unprocessed_history(since_cursor=last_cursor)
        if not entries:
            logger.debug("Dream: nothing to process (cursor={})", last_cursor)
            return False

        batch = entries[: self.max_batch_size]
        new_cursor = batch[-1]["cursor"]

        self.store.set_last_dream_cursor(new_cursor)
        self.store.compact_history()

        logger.info(
            "Dream: advanced cursor {}→{} ({} entries), history compacted",
            last_cursor, new_cursor, len(batch),
        )
        return True
