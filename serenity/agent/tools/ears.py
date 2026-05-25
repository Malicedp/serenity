"""Ears tools — audio sensing for Serenity.

Stack:
  sounddevice  — microphone capture
  Faster Whisper — speech-to-text (local, offline)
  CLAP          — audio semantic embedding
  AudioRAG      — flat store with JSON metadata sidecar

Tools:
  ears_open(source, duration_s)  — capture audio, transcribe, store to RAG
  ears_close()                   — stop any active continuous listening
  ears_recall(query, mode)       — query the AudioRAG store

Enable in config:  senses.audio.enabled = true
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from typing import Any

from loguru import logger

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import (
    IntegerSchema,
    NumberSchema as FloatSchema,
    StringSchema,
    tool_parameters_schema,
)

# Module-level state for continuous listening
_active_task: asyncio.Task | None = None
_active_stop = asyncio.Event()


def _is_enabled() -> bool:
    try:
        from serenity.config.loader import load_config
        cfg = load_config()
        return cfg.senses.audio.enabled
    except Exception:
        return False


def _whisper_config() -> tuple[str, str, str]:
    """Return (model_size, device, compute_type) from config."""
    try:
        from serenity.config.loader import load_config
        s = load_config().senses.audio
        return s.whisper_model, s.whisper_device, s.whisper_compute_type
    except Exception:
        return "medium", "cpu", "int8"


# ── ears_open ────────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        source=StringSchema(
            'Audio source: "mic" (default) or an absolute path to an audio file.',
            nullable=True,
        ),
        duration_s=FloatSchema(
            description=(
                "How many seconds to capture (default 10). "
                "Pass 0 for continuous mode — Serenity keeps listening until ears_close() is called."
            ),
            nullable=True,
        ),
        tags=StringSchema(
            "Comma-separated tags to attach to this recording, e.g. 'ambient,speech'.",
            nullable=True,
        ),
        required=[],
    )
)
class EarsOpenTool(Tool):
    """Open Serenity's ears — capture audio from microphone or a file,
    transcribe it with Faster Whisper, embed it with CLAP, and store it
    to the AudioRAG store for later recall.
    """

    @property
    def name(self) -> str:
        return "ears_open"

    @property
    def description(self) -> str:
        return (
            "Start listening — capture audio from mic or file, transcribe with Faster Whisper, "
            "embed with CLAP, store to AudioRAG. "
            "Returns the transcript and what was heard. "
            'source: "mic" or absolute file path. '
            "duration_s: seconds to capture (default 10); 0 = continuous until ears_close(). "
            "Requires: senses.audio.enabled = true in config."
        )

    async def execute(
        self,
        source: str | None = None,
        duration_s: float | None = None,
        tags: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not _is_enabled():
            return (
                "Ears are not enabled. Set senses.audio.enabled = true in ~/.serenity/config.json "
                "or re-run the setup wizard."
            )

        src = (source or "mic").strip()
        dur = float(duration_s) if duration_s is not None else 10.0
        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]

        if dur == 0:
            return await self._start_continuous(src, tag_list)
        return await self._capture_once(src, dur, tag_list)

    async def _capture_once(self, source: str, duration_s: float, tags: list[str]) -> str:
        loop = asyncio.get_running_loop()
        session_id = f"ears_{int(time.time())}"

        # Capture or load audio
        if source == "mic":
            audio_path = await loop.run_in_executor(None, _record_mic, duration_s)
        else:
            audio_path = Path(source)
            if not audio_path.exists():
                return f"Error: audio file not found — {source}"
            duration_s = _audio_duration(audio_path)

        # Transcribe
        transcript = await _transcribe(audio_path)

        # CLAP embed (optional — graceful fail)
        clap_emb: list[float] | None = None
        try:
            from serenity.senses.clap_embedder import embed_audio
            clap_emb = await loop.run_in_executor(None, embed_audio, audio_path)
        except Exception as e:
            logger.debug("CLAP embedding skipped: {}", e)

        # Store to AudioRAG
        from serenity.senses import audio_rag
        entry_id = audio_rag.store(
            audio_path=audio_path,
            transcript=transcript,
            session_id=session_id,
            source=source,
            duration_s=duration_s,
            tags=tags,
            clap_embedding=clap_emb,
        )

        result = (
            f"👂 Ears — captured {duration_s:.1f}s from {source}\n"
            f"Transcript: {transcript or '(nothing detected)'}\n"
            f"Stored: AudioRAG #{entry_id[:8]}"
        )
        if clap_emb:
            result += " (CLAP embedded)"
        return result

    async def _start_continuous(self, source: str, tags: list[str]) -> str:
        global _active_task, _active_stop
        if _active_task and not _active_task.done():
            return "Ears are already open. Call ears_close() first."

        _active_stop.clear()
        _active_task = asyncio.create_task(
            _continuous_loop(source, tags, _active_stop)
        )
        return f"👂 Ears open — continuous listening from {source}. Call ears_close() to stop."


async def _continuous_loop(source: str, tags: list[str], stop_event: asyncio.Event) -> None:
    """Background task: capture 10s chunks until stopped."""
    chunk_s = 10.0
    session_id = f"ears_continuous_{int(time.time())}"

    while not stop_event.is_set():
        try:
            loop = asyncio.get_running_loop()
            audio_path = await loop.run_in_executor(None, _record_mic, chunk_s)
            transcript = await _transcribe(audio_path)

            clap_emb: list[float] | None = None
            try:
                from serenity.senses.clap_embedder import embed_audio
                clap_emb = await loop.run_in_executor(None, embed_audio, audio_path)
            except Exception:
                pass

            from serenity.senses import audio_rag
            audio_rag.store(
                audio_path=audio_path,
                transcript=transcript,
                session_id=session_id,
                source=source,
                duration_s=chunk_s,
                tags=tags,
                clap_embedding=clap_emb,
            )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Ears continuous capture error: {}", e)


# ── ears_close ───────────────────────────────────────────────────────────────

@tool_parameters(tool_parameters_schema(required=[]))
class EarsCloseTool(Tool):
    """Stop Serenity's continuous listening session."""

    @property
    def name(self) -> str:
        return "ears_close"

    @property
    def description(self) -> str:
        return (
            "Stop the active continuous listening session started by ears_open(duration_s=0). "
            "No-op if ears are not open."
        )

    async def execute(self, **kwargs: Any) -> str:
        global _active_task, _active_stop
        if _active_task is None or _active_task.done():
            return "Ears are not open — nothing to close."
        _active_stop.set()
        _active_task.cancel()
        try:
            await asyncio.wait_for(_active_task, timeout=3.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        _active_task = None
        return "👂 Ears closed — continuous listening stopped."


# ── ears_recall ──────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        query=StringSchema("What to search for — a keyword, phrase, or concept."),
        mode=StringSchema(
            'Search mode: "keyword" (default) or "semantic" (requires CLAP).',
            nullable=True,
        ),
        limit=IntegerSchema(5, description="Maximum results to return.", minimum=1, maximum=20),
        required=["query"],
    )
)
class EarsRecallTool(Tool):
    """Query Serenity's AudioRAG store — what did she hear?"""

    @property
    def name(self) -> str:
        return "ears_recall"

    @property
    def description(self) -> str:
        return (
            "Search AudioRAG — Serenity's store of everything she has heard. "
            'mode: "keyword" searches transcripts; "semantic" uses CLAP embeddings. '
            "Returns matching transcripts with timestamps."
        )

    async def execute(
        self,
        query: str,
        mode: str | None = None,
        limit: int = 5,
        **kwargs: Any,
    ) -> str:
        from serenity.senses import audio_rag

        search_mode = (mode or "keyword").lower()

        if search_mode == "semantic":
            try:
                from serenity.senses.clap_embedder import embed_text
                loop = asyncio.get_running_loop()
                emb = await loop.run_in_executor(None, embed_text, query)
                results = audio_rag.query_semantic(emb, limit=limit)
            except Exception as e:
                return f"Semantic recall failed (CLAP not available): {e}\nTry mode='keyword'."
        else:
            results = audio_rag.query_keyword(query, limit=limit)

        if not results:
            return f"AudioRAG: nothing found for '{query}'."

        lines = [f"AudioRAG — {len(results)} result(s) for '{query}':"]
        for r in results:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.get("timestamp", 0)))
            transcript = r.get("transcript") or "(no transcript)"
            src = r.get("source", "?")
            lines.append(f"  [{ts}] [{src}] {transcript[:120]}")
        return "\n".join(lines)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _record_mic(duration_s: float) -> Path:
    """Blocking: record from mic for duration_s seconds. Returns temp WAV path."""
    try:
        import sounddevice as sd  # type: ignore
        import scipy.io.wavfile as wav  # type: ignore
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "Microphone capture requires 'sounddevice' and 'scipy'.\n"
            "Run:  pip install sounddevice scipy"
        ) from exc

    sr = 16000  # Whisper expects 16 kHz
    logger.debug("Recording {} seconds from mic…", duration_s)
    audio = sd.rec(int(duration_s * sr), samplerate=sr, channels=1, dtype="int16")
    sd.wait()

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav.write(tmp.name, sr, audio)
    return Path(tmp.name)


async def _transcribe(audio_path: Path) -> str:
    """Transcribe audio using Faster Whisper (via config) or return empty string."""
    try:
        from serenity.providers.transcription import get_faster_whisper
        model_size, device, compute_type = _whisper_config()
        provider = get_faster_whisper(model_size, device, compute_type)
        return await provider.transcribe(audio_path)
    except Exception as e:
        logger.warning("Transcription failed: {}", e)
        return ""


def _audio_duration(path: Path) -> float:
    """Return duration in seconds of a WAV file."""
    try:
        import scipy.io.wavfile as wav  # type: ignore
        sr, data = wav.read(str(path))
        return len(data) / sr
    except Exception:
        return 0.0
