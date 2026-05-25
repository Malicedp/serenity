"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from loguru import logger

from serenity.bus.events import OutboundMessage
from serenity.bus.queue import MessageBus
from serenity.channels.base import BaseChannel
from serenity.config.schema import Config
from serenity.utils.restart import consume_restart_notice_from_env, format_restart_completed_message

# Retry delays for message sending (exponential backoff: 1s, 2s, 4s)
_SEND_RETRY_DELAYS = (1, 2, 4)

# ── TTS text cleaner ──────────────────────────────────────────────────────────
_MD_STRIP = re.compile(
    r"\*{1,3}([^*]+)\*{1,3}"   # **bold** / *italic* / ***both***
    r"|`{1,3}[^`]*`{1,3}"      # `code` / ```block```
    r"|#{1,6}\s+"               # # headings
    r"|\[([^\]]+)\]\([^)]+\)"  # [link text](url) → link text
    r"|\n{2,}"                  # double newlines → single space
)

# Strips <think>…</think> and unclosed opening tags (plain text reasoning bleed-through).
_THINK_BLOCK  = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_THINK_OPEN   = re.compile(r"<think>[\s\S]*$",          re.IGNORECASE)
_THOUGHT_BLOCK = re.compile(r"<thought>[\s\S]*?</thought>", re.IGNORECASE)
_THOUGHT_OPEN  = re.compile(r"<thought>[\s\S]*$",           re.IGNORECASE)

def _clean_for_tts(text: str) -> str:
    """Prepare LLM output for speech synthesis.

    1. Strip <think>/<thought> blocks (Qwen3, Gemma, etc.)
    2. Strip markdown symbols (**, *, #, backticks)
    3. Collapse whitespace
    """
    # 1. Think/thought blocks
    text = _THINK_BLOCK.sub("", text)
    text = _THINK_OPEN.sub("", text)
    text = _THOUGHT_BLOCK.sub("", text)
    text = _THOUGHT_OPEN.sub("", text)

    # 2. Markdown
    def _sub(m: re.Match) -> str:
        return (m.group(1) or m.group(2) or " ").strip()
    text = _MD_STRIP.sub(_sub, text)

    # 3. Whitespace
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text


# ── Audio playback (sounddevice with os fallback) ─────────────────────────────

def _play_audio_file(path: Path) -> None:
    """Play an audio file through the default output device.

    Primary: sounddevice + soundfile (already in senses deps).
    Fallback: system default player (opens asynchronously — good enough for TTS).
    """
    try:
        import sounddevice as sd   # type: ignore
        import soundfile as sf     # type: ignore
        data, rate = sf.read(str(path), always_2d=False)
        sd.play(data, rate)
        sd.wait()
        return
    except ImportError:
        pass
    except Exception as e:
        logger.warning("TTS playback (sounddevice) failed: {}", e)

    # Fallback: hand off to OS player (non-blocking for wav/mp3)
    try:
        import subprocess, os as _os
        if sys.platform == "win32":
            _os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["afplay", str(path)])
        else:
            subprocess.Popen(["aplay", str(path)])
    except Exception as e:
        logger.warning("TTS playback (OS fallback) failed: {}", e)


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.

    Responsibilities:
    - Initialize enabled channels (Telegram, WhatsApp, etc.)
    - Start/stop channels
    - Route outbound messages
    """

    def __init__(self, config: Config, bus: MessageBus):
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._tts_provider = None
        # Allow only one TTS synthesis at a time — GPU can't handle two LLM+TTS loads
        self._tts_lock = asyncio.Lock()

        self._init_channels()
        self._init_tts()

    def _init_tts(self) -> None:
        """Build TTS provider for PC speaker output (wake-word voice replies)."""
        try:
            cfg = self.config.voice
            if not cfg.tts_enabled:
                return
            from serenity.providers.tts import build_tts_provider
            self._tts_provider = build_tts_provider(
                provider=cfg.tts_provider,
                api_key=cfg.tts_api_key,
                model=cfg.tts_model or cfg.tts_local_model,
                voice=cfg.tts_voice or cfg.tts_local_voice,
                api_base=cfg.tts_api_base,
                speed=cfg.tts_speed,
                response_format=cfg.tts_format,
                instruct=cfg.tts_instruct,
                region=cfg.tts_region,
                voice_id=cfg.tts_elevenlabs_voice_id,
                stability=cfg.tts_elevenlabs_stability,
                similarity_boost=cfg.tts_elevenlabs_similarity,
                style=cfg.tts_elevenlabs_style,
                local_device=cfg.tts_local_device,
                piper_model_path=cfg.tts_piper_model,
            )
            logger.info("TTS provider ready: {}", cfg.tts_provider)
        except Exception as e:
            logger.warning("TTS init failed (voice replies disabled): {}", e)

    async def _speak(self, text: str) -> None:
        """Synthesize *text* and play it through the PC speaker.

        Skips silently if TTS is disabled or the lock is already held
        (prevents a slow TTS synthesis from piling up behind the current one).
        """
        if not self._tts_provider or not text.strip():
            return

        if self._tts_lock.locked():
            logger.debug("TTS: skipping response — synthesiser already busy")
            return

        clean = _clean_for_tts(text)
        if not clean:
            return

        ext = getattr(self._tts_provider, "output_extension", ".wav")
        tmp = Path(tempfile.gettempdir()) / f"serenity_tts_{int(time.time())}{ext}"
        try:
            async with self._tts_lock:
                ok = await self._tts_provider.synthesize(clean, tmp)
                if ok and tmp.exists():
                    await asyncio.get_event_loop().run_in_executor(
                        None, _play_audio_file, tmp
                    )
        except Exception as e:
            logger.warning("TTS speak error: {}", e)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def _init_channels(self) -> None:
        """Initialize channels discovered via pkgutil scan + entry_points plugins."""
        from serenity.channels.registry import discover_all

        transcription_provider = self.config.channels.transcription_provider
        transcription_key = self._resolve_transcription_key(transcription_provider)
        transcription_base = self._resolve_transcription_base(transcription_provider)

        for name, cls in discover_all().items():
            section = getattr(self.config.channels, name, None)
            if section is None:
                continue
            enabled = (
                section.get("enabled", False)
                if isinstance(section, dict)
                else getattr(section, "enabled", False)
            )
            if not enabled:
                continue
            try:
                channel = cls(section, self.bus)
                channel.transcription_provider = transcription_provider
                channel.transcription_api_key = transcription_key
                channel.transcription_api_base = transcription_base
                self.channels[name] = channel
                logger.info("{} channel enabled", cls.display_name)
            except Exception as e:
                logger.warning("{} channel not available: {}", name, e)

        self._validate_allow_from()

    def _resolve_transcription_key(self, provider: str) -> str:
        """Pick the API key for the configured transcription provider."""
        try:
            if provider == "openai":
                return self.config.providers.openai.api_key
            return self.config.providers.groq.api_key
        except AttributeError:
            return ""

    def _resolve_transcription_base(self, provider: str) -> str:
        """Pick the API base URL for the configured transcription provider."""
        try:
            if provider == "openai":
                return self.config.providers.openai.api_base or ""
            return self.config.providers.groq.api_base or ""
        except AttributeError:
            return ""

    def _validate_allow_from(self) -> None:
        for name, ch in self.channels.items():
            cfg = ch.config
            if isinstance(cfg, dict):
                if "allow_from" in cfg:
                    allow = cfg.get("allow_from")
                else:
                    allow = cfg.get("allowFrom")
            else:
                allow = getattr(cfg, "allow_from", None)
            if allow == []:
                raise SystemExit(
                    f'Error: "{name}" has empty allowFrom (denies all). '
                    f'Set ["*"] to allow everyone, or add specific user IDs.'
                )

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        """Start a channel and log any exceptions."""
        try:
            await channel.start()
        except Exception as e:
            logger.error("Failed to start channel {}: {}", name, e)

    async def start_all(self) -> None:
        """Start all channels and the outbound dispatcher."""
        if not self.channels:
            logger.warning("No channels enabled")
            return

        # Start outbound dispatcher
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())

        # Start channels
        tasks = []
        for name, channel in self.channels.items():
            logger.info("Starting {} channel...", name)
            tasks.append(asyncio.create_task(self._start_channel(name, channel)))

        self._notify_restart_done_if_needed()

        # Wait for all to complete (they should run forever)
        await asyncio.gather(*tasks, return_exceptions=True)

    def _notify_restart_done_if_needed(self) -> None:
        """Send restart completion message when runtime env markers are present."""
        notice = consume_restart_notice_from_env()
        if not notice:
            return
        target = self.channels.get(notice.channel)
        if not target:
            return
        asyncio.create_task(self._send_with_retry(
            target,
            OutboundMessage(
                channel=notice.channel,
                chat_id=notice.chat_id,
                content=format_restart_completed_message(notice.started_at_raw),
            ),
        ))

    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        logger.info("Stopping all channels...")

        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass

        # Stop all channels
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info("Stopped {} channel", name)
            except Exception as e:
                logger.error("Error stopping {}: {}", name, e)

    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.info("Outbound dispatcher started")

        # Buffer for messages that couldn't be processed during delta coalescing
        # (since asyncio.Queue doesn't support push_front)
        pending: list[OutboundMessage] = []

        while True:
            try:
                # First check pending buffer before waiting on queue
                if pending:
                    msg = pending.pop(0)
                else:
                    msg = await asyncio.wait_for(
                        self.bus.consume_outbound(),
                        timeout=1.0
                    )

                if msg.metadata.get("_progress"):
                    if msg.metadata.get("_tool_hint") and not self.config.channels.send_tool_hints:
                        continue
                    if not msg.metadata.get("_tool_hint") and not self.config.channels.send_progress:
                        continue

                if msg.metadata.get("_retry_wait"):
                    continue

                # Coalesce consecutive _stream_delta messages for the same (channel, chat_id)
                # to reduce API calls and improve streaming latency
                if msg.metadata.get("_stream_delta") and not msg.metadata.get("_stream_end"):
                    msg, extra_pending = self._coalesce_stream_deltas(msg)
                    pending.extend(extra_pending)

                channel = self.channels.get(msg.channel)
                if channel:
                    await self._send_with_retry(channel, msg)
                elif msg.channel == "voice":
                    # Route wake-word responses to the PC speaker via TTS.
                    # Rules:
                    #   _stream_delta without _stream_end → mid-stream partial, skip
                    #   _streamed → duplicate signal sent after streaming completes, skip
                    #   everything else (plain msg or final _stream_end chunk) → speak
                    is_mid_stream = (
                        msg.metadata.get("_stream_delta")
                        and not msg.metadata.get("_stream_end")
                    )
                    is_duplicate = bool(msg.metadata.get("_streamed"))
                    if not is_mid_stream and not is_duplicate and msg.content:
                        asyncio.create_task(self._speak(msg.content))
                else:
                    logger.warning("Unknown channel: {}", msg.channel)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    @staticmethod
    async def _send_once(channel: BaseChannel, msg: OutboundMessage) -> None:
        """Send one outbound message without retry policy."""
        if msg.metadata.get("_stream_delta") or msg.metadata.get("_stream_end"):
            await channel.send_delta(msg.chat_id, msg.content, msg.metadata)
        elif not msg.metadata.get("_streamed"):
            await channel.send(msg)

    def _coalesce_stream_deltas(
        self, first_msg: OutboundMessage
    ) -> tuple[OutboundMessage, list[OutboundMessage]]:
        """Merge consecutive _stream_delta messages for the same (channel, chat_id).

        This reduces the number of API calls when the queue has accumulated multiple
        deltas, which happens when LLM generates faster than the channel can process.

        Returns:
            tuple of (merged_message, list_of_non_matching_messages)
        """
        target_key = (first_msg.channel, first_msg.chat_id)
        combined_content = first_msg.content
        final_metadata = dict(first_msg.metadata or {})
        non_matching: list[OutboundMessage] = []

        # Only merge consecutive deltas. As soon as we hit any other message,
        # stop and hand that boundary back to the dispatcher via `pending`.
        while True:
            try:
                next_msg = self.bus.outbound.get_nowait()
            except asyncio.QueueEmpty:
                break

            # Check if this message belongs to the same stream
            same_target = (next_msg.channel, next_msg.chat_id) == target_key
            is_delta = next_msg.metadata and next_msg.metadata.get("_stream_delta")
            is_end = next_msg.metadata and next_msg.metadata.get("_stream_end")

            if same_target and is_delta and not final_metadata.get("_stream_end"):
                # Accumulate content
                combined_content += next_msg.content
                # If we see _stream_end, remember it and stop coalescing this stream
                if is_end:
                    final_metadata["_stream_end"] = True
                    # Stream ended - stop coalescing this stream
                    break
            else:
                # First non-matching message defines the coalescing boundary.
                non_matching.append(next_msg)
                break

        merged = OutboundMessage(
            channel=first_msg.channel,
            chat_id=first_msg.chat_id,
            content=combined_content,
            metadata=final_metadata,
        )
        return merged, non_matching

    async def _send_with_retry(self, channel: BaseChannel, msg: OutboundMessage) -> None:
        """Send a message with retry on failure using exponential backoff.

        Note: CancelledError is re-raised to allow graceful shutdown.
        """
        max_attempts = max(self.config.channels.send_max_retries, 1)

        for attempt in range(max_attempts):
            try:
                await self._send_once(channel, msg)
                return  # Send succeeded
            except asyncio.CancelledError:
                raise  # Propagate cancellation for graceful shutdown
            except Exception as e:
                if attempt == max_attempts - 1:
                    logger.error(
                        "Failed to send to {} after {} attempts: {} - {}",
                        msg.channel, max_attempts, type(e).__name__, e
                    )
                    return
                delay = _SEND_RETRY_DELAYS[min(attempt, len(_SEND_RETRY_DELAYS) - 1)]
                logger.warning(
                    "Send to {} failed (attempt {}/{}): {}, retrying in {}s",
                    msg.channel, attempt + 1, max_attempts, type(e).__name__, delay
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise  # Propagate cancellation during sleep

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        return {
            name: {
                "enabled": True,
                "running": channel.is_running
            }
            for name, channel in self.channels.items()
        }

    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())
