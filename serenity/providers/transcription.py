"""Voice transcription providers — cloud (Groq/OpenAI) and local (Faster Whisper)."""

import os
from pathlib import Path

import httpx
from loguru import logger


class OpenAITranscriptionProvider:
    """Voice transcription provider using OpenAI's Whisper API."""

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.api_url = (
            api_base
            or os.environ.get("OPENAI_TRANSCRIPTION_BASE_URL")
            or "https://api.openai.com/v1/audio/transcriptions"
        )

    async def transcribe(self, file_path: str | Path) -> str:
        if not self.api_key:
            logger.warning("OpenAI API key not configured for transcription")
            return ""
        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""
        try:
            async with httpx.AsyncClient() as client:
                with open(path, "rb") as f:
                    files = {"file": (path.name, f), "model": (None, "whisper-1")}
                    headers = {"Authorization": f"Bearer {self.api_key}"}
                    response = await client.post(
                        self.api_url, headers=headers, files=files, timeout=60.0,
                    )
                    response.raise_for_status()
                    return response.json().get("text", "")
        except Exception as e:
            logger.error("OpenAI transcription error: {}", e)
            return ""


class GroqTranscriptionProvider:
    """
    Voice transcription provider using Groq's Whisper API.

    Groq offers extremely fast transcription with a generous free tier.
    """

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.api_url = api_base or os.environ.get("GROQ_BASE_URL") or "https://api.groq.com/openai/v1/audio/transcriptions"

    async def transcribe(self, file_path: str | Path) -> str:
        """
        Transcribe an audio file using Groq.

        Args:
            file_path: Path to the audio file.

        Returns:
            Transcribed text.
        """
        if not self.api_key:
            logger.warning("Groq API key not configured for transcription")
            return ""

        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""

        try:
            async with httpx.AsyncClient() as client:
                with open(path, "rb") as f:
                    files = {
                        "file": (path.name, f),
                        "model": (None, "whisper-large-v3"),
                    }
                    headers = {
                        "Authorization": f"Bearer {self.api_key}",
                    }

                    response = await client.post(
                        self.api_url,
                        headers=headers,
                        files=files,
                        timeout=60.0
                    )

                    response.raise_for_status()
                    data = response.json()
                    return data.get("text", "")

        except Exception as e:
            logger.error("Groq transcription error: {}", e)
            return ""


# ── Module-level singleton so the model loads once and is reused ─────────────
_faster_whisper_instance: "FasterWhisperTranscriptionProvider | None" = None


class FasterWhisperTranscriptionProvider:
    """Local transcription using Faster Whisper (ctranslate2 backend).

    Runs entirely offline. Install with:
        pip install faster-whisper

    Models download automatically on first use to ~/.cache/huggingface/hub.
    "small" (~500 MB) is a good balance of speed and accuracy.
    "medium" (~1.5 GB) is noticeably better for non-native speakers.
    "large-v3" (~3 GB) is best quality but slower on CPU.
    """

    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._model = None  # lazy load on first transcription

    def _load(self):
        """Load the Whisper model (once). Raises RuntimeError if not installed."""
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed.\n"
                "Run:  pip install faster-whisper\n"
                "Then restart Serenity."
            ) from exc
        logger.info(
            "Loading Faster Whisper '{}' on {} ({})",
            self.model_size, self.device, self.compute_type,
        )
        self._model = WhisperModel(
            self.model_size,
            device=self.device,
            compute_type=self.compute_type,
        )
        logger.info("Faster Whisper model loaded.")
        return self._model

    def _transcribe_sync(self, path: Path) -> str:
        """Blocking transcription — run in a thread to avoid blocking the event loop."""
        model = self._load()
        segments, info = model.transcribe(str(path), beam_size=5)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        logger.debug(
            "Faster Whisper: detected language '{}' ({:.0%} confidence), transcribed {} chars",
            info.language, info.language_probability, len(text),
        )
        return text

    async def transcribe(self, file_path: str | Path) -> str:
        """Transcribe an audio file asynchronously. Returns empty string on failure."""
        import asyncio

        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._transcribe_sync, path)
        except RuntimeError as e:
            logger.error("Faster Whisper not available: {}", e)
            return ""
        except Exception as e:
            logger.error("Faster Whisper transcription error: {}", e)
            return ""


def get_faster_whisper(
    model_size: str = "small",
    device: str = "cpu",
    compute_type: str = "int8",
) -> "FasterWhisperTranscriptionProvider":
    """Return the module-level FasterWhisper singleton, creating it if needed."""
    global _faster_whisper_instance
    if _faster_whisper_instance is None or _faster_whisper_instance.model_size != model_size:
        _faster_whisper_instance = FasterWhisperTranscriptionProvider(
            model_size=model_size, device=device, compute_type=compute_type
        )
    return _faster_whisper_instance
