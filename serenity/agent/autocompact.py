"""Auto compact: proactive compression of idle sessions to reduce token cost and latency."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger
from serenity.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from serenity.agent.memory import Consolidator


class AutoCompact:
    _RECENT_SUFFIX_MESSAGES = 8

    def __init__(self, sessions: SessionManager, consolidator: Consolidator,
                 session_ttl_minutes: int = 0,
                 session_ttl_overrides: dict[str, int] | None = None):
        self.sessions = sessions
        self.consolidator = consolidator
        self._ttl = session_ttl_minutes
        # Per-channel overrides keyed by session-key prefix (e.g. "voice", "telegram").
        # Value is TTL in minutes; -1 means never compact.
        self._ttl_overrides: dict[str, int] = session_ttl_overrides or {}
        self._archiving: set[str] = set()
        self._summaries: dict[str, tuple[str, datetime]] = {}

    def _ttl_for(self, key: str) -> int:
        """Return the effective TTL (minutes) for a given session key.

        Checks overrides first (longest prefix match), then falls back to
        the global default.  A value of -1 means 'never compact'.
        """
        # Match on channel prefix — "voice:wake" matches key "voice"
        for prefix, ttl in self._ttl_overrides.items():
            if key == prefix or key.startswith(prefix + ":") or key.startswith(prefix + "_"):
                return ttl
        return self._ttl

    def _is_expired(self, ts: datetime | str | None,
                    now: datetime | None = None,
                    key: str = "") -> bool:
        effective_ttl = self._ttl_for(key) if key else self._ttl
        if effective_ttl < 0:
            return False  # -1 = never compact
        if effective_ttl == 0 or not ts:
            return False
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return ((now or datetime.now()) - ts).total_seconds() >= effective_ttl * 60

    @staticmethod
    def _format_summary(text: str, last_active: datetime) -> str:
        idle_min = int((datetime.now() - last_active).total_seconds() / 60)
        return f"Inactive for {idle_min} minutes.\nPrevious conversation summary: {text}"

    def _split_unconsolidated(
        self, session: Session,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split live session tail into archiveable prefix and retained recent suffix."""
        tail = list(session.messages[session.last_consolidated:])
        if not tail:
            return [], []

        probe = Session(
            key=session.key,
            messages=tail.copy(),
            created_at=session.created_at,
            updated_at=session.updated_at,
            metadata={},
            last_consolidated=0,
        )
        probe.retain_recent_legal_suffix(self._RECENT_SUFFIX_MESSAGES)
        kept = probe.messages
        cut = len(tail) - len(kept)
        return tail[:cut], kept

    def check_expired(self, schedule_background: Callable[[Coroutine], None],
                      active_session_keys: Collection[str] = ()) -> None:
        """Schedule archival for idle sessions, skipping those with in-flight agent tasks."""
        now = datetime.now()
        for info in self.sessions.list_sessions():
            key = info.get("key", "")
            if not key or key in self._archiving:
                continue
            if key in active_session_keys:
                continue
            if self._is_expired(info.get("updated_at"), now, key=key):
                self._archiving.add(key)
                schedule_background(self._archive(key))

    @staticmethod
    def _is_worth_summarising(messages: list[dict[str, Any]]) -> bool:
        """Return True if messages are worth LLM-summarising.

        Casual small-talk (all short, no tool calls, no questions, no tasks)
        adds noise to memory and wastes a full LLM round-trip. Skip it.
        """
        _SUBSTANTIAL_CHARS = 80   # min chars in a single turn to count as meaningful
        _MIN_SUBSTANTIAL   = 2    # need at least this many substantial turns

        substantial = 0
        for msg in messages:
            role = msg.get("role", "")
            # Any tool call or tool result → definitely worth keeping
            if role == "tool":
                return True
            content = msg.get("content") or ""
            # content can be a list of blocks (OpenAI format)
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_use":
                            return True
                        text = block.get("text") or block.get("content") or ""
                        if len(str(text)) >= _SUBSTANTIAL_CHARS:
                            substantial += 1
            else:
                if len(str(content)) >= _SUBSTANTIAL_CHARS:
                    substantial += 1

        return substantial >= _MIN_SUBSTANTIAL

    async def _archive(self, key: str) -> None:
        try:
            self.sessions.invalidate(key)
            session = self.sessions.get_or_create(key)
            archive_msgs, kept_msgs = self._split_unconsolidated(session)
            if not archive_msgs and not kept_msgs:
                session.updated_at = datetime.now()
                self.sessions.save(session)
                return

            last_active = session.updated_at
            summary = ""
            skipped_llm = False
            if archive_msgs:
                if self._is_worth_summarising(archive_msgs):
                    summary = await self.consolidator.archive(archive_msgs) or ""
                else:
                    skipped_llm = True
                    logger.debug(
                        "Auto-compact: skipping LLM summarisation for {} — casual/trivial session",
                        key,
                    )
            if summary and summary != "(nothing)":
                self._summaries[key] = (summary, last_active)
                session.metadata["_last_summary"] = {"text": summary, "last_active": last_active.isoformat()}
            session.messages = kept_msgs
            session.last_consolidated = 0
            session.updated_at = datetime.now()
            self.sessions.save(session)
            if archive_msgs:
                logger.info(
                    "Auto-compact: archived {} (archived={}, kept={}, summary={}, skipped_llm={})",
                    key,
                    len(archive_msgs),
                    len(kept_msgs),
                    bool(summary),
                    skipped_llm,
                )
        except Exception:
            logger.exception("Auto-compact: failed for {}", key)
        finally:
            self._archiving.discard(key)

    def prepare_session(self, session: Session, key: str) -> tuple[Session, str | None]:
        if key in self._archiving or self._is_expired(session.updated_at, key=key):
            logger.info("Auto-compact: reloading session {} (archiving={})", key, key in self._archiving)
            session = self.sessions.get_or_create(key)
        # Hot path: summary from in-memory dict (process hasn't restarted).
        # Also clean metadata copy so stale _last_summary never leaks to disk.
        entry = self._summaries.pop(key, None)
        if entry:
            session.metadata.pop("_last_summary", None)
            return session, self._format_summary(entry[0], entry[1])
        if "_last_summary" in session.metadata:
            meta = session.metadata.pop("_last_summary")
            self.sessions.save(session)
            return session, self._format_summary(meta["text"], datetime.fromisoformat(meta["last_active"]))
        # Inject running_summary if present.
        # Written by deep_summarise() on idle (10 min) or on gateway shutdown.
        # Covers everything older than the active 40-message sliding window.
        running = session.metadata.get("running_summary", "").strip()

        # Inject chunk notes — zero-LLM breadcrumbs for messages that have
        # fallen outside the active window mid-conversation. Generated by
        # TextRank + NER, cleared after a proper deep_summarise runs.
        chunk_notes: list[str] = session.metadata.get("chunk_notes", [])
        chunk_notes_text = "\n\n".join(chunk_notes).strip() if chunk_notes else ""

        if running and chunk_notes_text:
            combined = f"Earlier in this conversation:\n{running}\n\nRecent overflow (not yet summarised):\n{chunk_notes_text}"
            return session, combined
        if running:
            return session, f"Earlier in this conversation:\n{running}"
        if chunk_notes_text:
            return session, f"Recent conversation (not yet summarised):\n{chunk_notes_text}"
        return session, None
