# Copyright © 2026 Daniel T Niamke. All rights reserved.
"""Always-on senses daemon — PC wake-word listener.

WakeWordListener
    Scans microphone in 2-second chunks using Whisper small (single model).
    Energy VAD skips silent chunks entirely.
    On wake word match → switches to high-res capture mode (0.5 s micro-chunks)
    so silence detection is sample-accurate, not ±2 s coarse.
    Silence cutoff (default 1.2 s) ends the utterance.
    Same model transcribes the complete utterance — no second model load.
    Hallucination filter drops results < min_words or < min_chars, and
    blocks a fixed blocklist of phrases Whisper emits on quiet rooms.
    Passing utterance → InboundMessage(channel="voice") on the message bus.

NOTE — primary voice input is Telegram voice notes, NOT this daemon.
  Send a voice note in Telegram and it is transcribed by faster-whisper
  and delivered to the agent automatically.  This daemon is for always-on
  PC microphone listening via a wake word ("Serenity, ...").
  Enable with:  senses.audio.enabled = true  in ~/.serenity/config.json

Start via:  start(bus, loop)
Stop via:   stop()
"""

from __future__ import annotations

import asyncio
import difflib
import re
import threading
import time
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from serenity.bus.events import InboundMessage
    from serenity.bus.queue import MessageBus

# ── Singleton state ───────────────────────────────────────────────────────────
_wake_thread: threading.Thread | None = None
_stop_event: threading.Event = threading.Event()
_running = False


# ── Pink log helper ───────────────────────────────────────────────────────────

def _log_mic(text: str) -> None:
    """Emit a pink log line so voice/mic input is instantly visible in the terminal."""
    _safe = text.replace("<", r"\<").replace(">", r"\>")
    logger.opt(colors=True).info(
        f"<fg #FF69B4><bold>🎤  MIC  ▶  </bold>{_safe}</fg #FF69B4>"
    )


# ── Public API ────────────────────────────────────────────────────────────────

def start(bus: "MessageBus", event_loop: asyncio.AbstractEventLoop) -> None:
    """Start the wake-word listener if enabled in config. Idempotent."""
    global _running, _stop_event, _wake_thread

    if _running:
        return

    try:
        from serenity.config.loader import load_config
        cfg = load_config()
    except Exception as e:
        logger.warning("SensesDaemon: could not load config — {}", e)
        return

    # Create a fresh Event each time — re-using the old one after stop()+start()
    # risks two threads sharing the same event and racing on .clear()/.set().
    _stop_event = threading.Event()
    _running = True

    if cfg.senses.audio.enabled:
        _wake_thread = threading.Thread(
            target=_wake_word_loop,
            args=(cfg.senses.audio, bus, event_loop),
            name="serenity-wake-word",
            daemon=True,
        )
        _wake_thread.start()
        logger.info(
            "SensesDaemon: PC wake-word listener started (word='{}')",
            cfg.senses.audio.wake_word,
        )
    else:
        logger.opt(colors=True).info(
            "<cyan>SensesDaemon: PC microphone is OFF</cyan> — "
            "using <bold>Telegram voice notes</bold> as primary voice input "
            "(send a voice note in Telegram to talk to Serenity). "
            "To enable always-on PC wake-word: "
            "run <cyan>serenity onboard</cyan> → [E] Senses & Voice."
        )


def stop() -> None:
    """Signal the daemon to stop. Returns immediately (thread is a daemon thread)."""
    global _running
    _stop_event.set()
    _running = False
    logger.info("SensesDaemon: stop requested")


# ── Whisper hallucination blocklist ──────────────────────────────────────────
# Phrases Whisper commonly emits when the room is quiet or there's background
# noise but no real speech.  All lowercase, punctuation stripped.
_HALLUCINATIONS: frozenset[str] = frozenset({
    "thank you", "thanks", "thanks for watching", "thank you for watching",
    "like and subscribe", "subscribe", "please subscribe",
    "you", "i", "hmm", "hm", "uh", "um", "ah", "oh", "ok", "okay",
    "bye", "goodbye", "see you", "see you later", "see you next time",
    ".", "..", "...", "you you", "the", "a", "and", "in", "is",
    "i i", "um um", "uh uh",
})


def _is_hallucination(text: str, min_words: int, min_chars: int) -> bool:
    """Return True if *text* looks like a Whisper hallucination.

    Three gates:
    1. Minimum character count (catches "I", ".", lone punctuation)
    2. Minimum real-word count (single-word results are almost always hallucinations)
    3. Known-bad phrase blocklist
    """
    stripped = text.strip().rstrip(".,!? ")
    if len(stripped) < min_chars:
        return True
    words = [w.strip(".,!?\"'") for w in stripped.lower().split() if w.strip(".,!?\"'")]
    if len(words) < min_words:
        return True
    phrase = " ".join(words)
    if phrase in _HALLUCINATIONS:
        return True
    return False


# ── Wake word listener ────────────────────────────────────────────────────────

def _wake_word_loop(
    audio_cfg: Any,
    bus: "MessageBus",
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """Main loop: record → VAD → Whisper scan → wake → capture → transcribe → inject.

    A single Whisper model (default: small) handles both scan and transcription.
    Using tiny for scanning proved unreliable — small on cpu/int8 is fast enough
    (~0.3–0.8 s per 2 s chunk) and much more accurate for wake word detection.

    Scan phase    — 2-second chunks, energy VAD gate, wake word fuzzy match.
    Capture phase — 0.5-second micro-chunks for sample-accurate silence detection.
    Transcription — same model on the full accumulated audio.
    """
    wake_word     = (audio_cfg.wake_word or "Serenity").lower()
    whisper_model = audio_cfg.whisper_model          # "small" (default)
    silence_cut   = float(audio_cfg.silence_cutoff_s)    # default 1.2 s
    max_capture   = float(audio_cfg.max_capture_s)        # default 30 s
    vad_threshold = float(audio_cfg.vad_energy_threshold) # default 0.01
    min_words     = int(getattr(audio_cfg, "min_transcript_words", 2))
    min_chars     = int(getattr(audio_cfg, "min_transcript_chars", 4))

    SAMPLE_RATE    = 16_000  # Whisper expects 16 kHz
    SCAN_S         = 2.0     # seconds per wake-word scan chunk
    CAPTURE_S      = 0.5     # seconds per post-wake micro-chunk (sample-accurate silence)
    SCAN_FRAMES    = int(SAMPLE_RATE * SCAN_S)
    CAPTURE_FRAMES = int(SAMPLE_RATE * CAPTURE_S)

    # Load the single Whisper model at startup — runs on CPU/int8 to keep VRAM
    # free for the LLM.  small on CPU: ~0.3–0.8 s per 2 s chunk, accurate enough
    # for reliable wake word detection without a separate tiny model.
    _whisper = _load_whisper(whisper_model, "cpu", "int8")
    if _whisper is None:
        logger.error(
            "SensesDaemon: Whisper {} failed to load — wake word disabled", whisper_model
        )
        return

    try:
        import sounddevice as sd
        import numpy as np
    except ImportError:
        logger.error("SensesDaemon: sounddevice / numpy not installed — wake word disabled")
        return

    # ── Resolve default input device ──────────────────────────────────────────
    _input_device: int | None = None
    try:
        raw_dev = sd.default.device[0]
        if raw_dev is not None and int(raw_dev) >= 0:
            _input_device = int(raw_dev)
            _dev_info = sd.query_devices(_input_device)
            logger.info(
                "SensesDaemon: using mic '{}' (device {})",
                _dev_info["name"], _input_device,
            )
        else:
            logger.info("SensesDaemon: using system-default microphone (OS pick)")
    except Exception as e:
        logger.info("SensesDaemon: mic device lookup failed ({}), using OS default", e)

    logger.info("SensesDaemon: listening for wake word '{}'", wake_word)

    def _record(frames: int) -> "np.ndarray":
        """Record *frames* samples and return a (N,) float32 array."""
        buf = sd.rec(frames, samplerate=SAMPLE_RATE, channels=1,
                     dtype="float32", device=_input_device)
        sd.wait()
        return buf[:, 0]

    while not _stop_event.is_set():
        try:
            # ── Scan phase: 2-second chunks, energy gate, wake word check ─────
            audio = _record(SCAN_FRAMES)

            rms = float(np.sqrt(np.mean(audio ** 2)))
            if rms < vad_threshold:
                continue  # silence — skip immediately, no Whisper call

            transcript = _transcribe_array(audio, SAMPLE_RATE, _whisper)
            if not transcript:
                continue

            if not _contains_wake_word(transcript, wake_word):
                continue

            logger.info("SensesDaemon: wake word '{}' heard in «{}»",
                        wake_word, transcript[:80])

            # ── Capture phase: 0.5 s micro-chunks until silence ───────────────
            accumulated: list["np.ndarray"] = [audio]  # include the wake chunk
            silence_s  = 0.0                            # consecutive silent seconds
            captured_s = SCAN_S                         # already have the scan chunk

            max_total_chunks = int((max_capture - SCAN_S) / CAPTURE_S)

            for _ in range(max_total_chunks):
                if _stop_event.is_set():
                    break

                seg = _record(CAPTURE_FRAMES)
                accumulated.append(seg)
                captured_s += CAPTURE_S

                seg_rms = float(np.sqrt(np.mean(seg ** 2)))
                if seg_rms < vad_threshold:
                    silence_s += CAPTURE_S
                    if silence_s >= silence_cut:
                        break   # enough silence — utterance is complete
                else:
                    silence_s = 0.0  # voice detected — reset silence counter

            # ── Transcribe full utterance ────────────────────────────────────
            full_audio = np.concatenate(accumulated)
            utterance  = _transcribe_array(full_audio, SAMPLE_RATE, _whisper)

            if not utterance or not utterance.strip():
                continue

            # ── Double-gate: re-verify wake word in the FULL transcription ───
            # The scan check above only saw a 2-second window. Background music,
            # TV, or a phonetically similar word can slip through. Verifying
            # again on the complete utterance catches false triggers before they
            # ever reach the LLM.
            if not _contains_wake_word(utterance, wake_word):
                logger.debug(
                    "SensesDaemon: wake word absent from full utterance — "
                    "dropped (background noise / music?) «{}»",
                    utterance[:80],
                )
                continue

            # ── Strip wake word prefix before sending to agent ───────────────
            # Agent doesn't need to hear its own name at the start of every msg.
            clean = _strip_wake_prefix(utterance.strip(), wake_word)

            if not clean or _is_hallucination(clean, min_words, min_chars):
                logger.debug(
                    "SensesDaemon: dropped hallucination after strip «{}»", clean[:60]
                )
                continue

            _log_mic(clean[:120])
            _inject_to_bus(clean, bus, event_loop)

        except Exception as e:
            if not _stop_event.is_set():
                logger.warning("SensesDaemon wake loop error: {}", e)
            time.sleep(1.0)


def _contains_wake_word(transcript: str, wake_word: str) -> bool:
    """Fuzzy match — handles slight Whisper mis-transcriptions of the wake word.

    Checks:
    1. Exact substring (fast path)
    2. Per-word fuzzy ratio ≥ 0.75  ("serenety", "sereniti", "serenidy")
    3. Sliding bigram / trigram join — catches "sere nity" split across two tokens
    """
    text = transcript.lower().strip()

    # 1. Exact substring
    if wake_word in text:
        return True

    words = text.split()

    # 2. Per-word fuzzy match
    for word in words:
        ratio = difflib.SequenceMatcher(None, wake_word, word).ratio()
        if ratio >= 0.75:
            return True

    # 3. Sliding n-gram join (handles Whisper splitting "Serenity" → "sere" + "nity")
    for n in (2, 3):
        for i in range(len(words) - n + 1):
            joined = "".join(words[i : i + n])
            ratio = difflib.SequenceMatcher(None, wake_word, joined).ratio()
            if ratio >= 0.80:
                return True

    return False


def _strip_wake_prefix(utterance: str, wake_word: str) -> str:
    """Remove the wake word (and common fillers before it) from the utterance start.

    Handles:
      "Serenity, what time is it?"     → "what time is it?"
      "Hey Serenity can you..."        → "can you..."
      "Ok Serenity — set a timer"      → "set a timer"
      "serenity"  (just the name)      → "" (caught by hallucination filter after)

    Falls back to the original utterance if nothing matches, so it's always safe.
    """
    # Optional filler prefix + wake word + optional punctuation / whitespace
    pattern = (
        rf"(?i)^(hey\s+|ok\s+|okay\s+|yo\s+)?"
        rf"{re.escape(wake_word)}"
        rf"[\s,.\-!?]*"
    )
    stripped = re.sub(pattern, "", utterance).strip()
    return stripped if stripped else utterance


def _inject_to_bus(
    text: str,
    bus: "MessageBus",
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """Thread-safe: post the transcribed utterance into the async message bus."""
    try:
        from serenity.bus.events import InboundMessage

        msg = InboundMessage(
            channel="voice",
            chat_id="wake",
            sender_id="microphone",
            content=text,
            metadata={"source": "wake_word"},
        )
        asyncio.run_coroutine_threadsafe(bus.publish_inbound(msg), event_loop)
    except Exception as e:
        logger.error("SensesDaemon: failed to inject utterance — {}", e)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _load_whisper(model_size: str, device: str, compute_type: str):
    """Load a Faster Whisper model. Returns None on failure."""
    # GPU first, CPU fallback
    devices_to_try = (
        [(device, compute_type), ("cpu", "int8")]
        if device == "cuda"
        else [("cpu", "int8")]
    )
    for dev, ct in devices_to_try:
        try:
            from faster_whisper import WhisperModel  # type: ignore
            model = WhisperModel(model_size, device=dev, compute_type=ct)
            logger.info(
                "SensesDaemon: Whisper {} loaded on {}/{}", model_size, dev, ct
            )
            return model
        except Exception as e:
            logger.debug(
                "SensesDaemon: Whisper {} on {} failed: {}", model_size, dev, e
            )
    return None


def _transcribe_array(audio: "np.ndarray", sample_rate: int, model) -> str | None:  # type: ignore
    """Transcribe a numpy float32 audio array.

    Returns:
        str   — transcript (may be empty string if audio was silent)
        None  — CUDA OOM; caller should retry on CPU
    """
    try:
        segments, _ = model.transcribe(
            audio,
            language="en",
            beam_size=1,              # fastest decode
            best_of=1,
            temperature=0.0,
            vad_filter=True,          # built-in VAD — skips silent regions
            no_speech_threshold=0.65, # 0.65 = more aggressive silence rejection
                                      # (was 0.6; helps with noisy rooms)
        )
        result = " ".join(s.text.strip() for s in segments).strip()
    except Exception as e:
        err = str(e).lower()
        if "out of memory" in err or "cuda" in err:
            logger.warning("SensesDaemon transcribe CUDA OOM: {}", e)
            return None
        logger.debug("SensesDaemon transcribe error: {}", e)
        return ""
    finally:
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
    return result
