"""Text-to-speech providers for Serenity voice responses.

Supported engines (all respect the voice_clone drop zone automatically):

  LOCAL (free, offline)
  ─────────────────────
  qwen3-local   Qwen3-TTS-0.6B / 1.7B — GPU or CPU, ~97 ms TTFB on GPU
                  Clone support: YES — drop audio into voice_clone/
  kokoro        Kokoro-82M — CPU-friendly, fast, OpenAI-compatible
  coqui         Coqui XTTS-v2 — multilingual, zero-shot clone
                  Clone support: YES — speaker_wav parameter
  piper         Piper TTS — ultra-fast, small, subprocess-based
  bark          Bark (suno/bark) — expressive, supports [laughs] etc.

  CLOUD FREE
  ──────────
  edge-tts      Microsoft Edge TTS (200+ voices, free, no key)

  CLOUD PAID
  ──────────
  elevenlabs    ElevenLabs — ultra-realistic, ~75 ms Flash v2.5
                  Clone support: YES — Instant Voice Clone API
  openai        OpenAI TTS (tts-1 / tts-1-hd)
  google        Google Cloud Text-to-Speech
  amazon        Amazon Polly
  cartesia      Cartesia — real-time, low-latency
                  Clone support: YES — voices.clone() API
  playht        PlayHT — high quality, instant clone
                  Clone support: YES — clone_instant() API
  deepgram      Deepgram Aura TTS

Voice Clone Drop Zone
─────────────────────
Drop any audio file (WAV/MP3/FLAC/OGG/M4A, ideally 5–30 s) into:
  ~/.serenity/voice_clone/         ← runtime (checked first)
  <repo>/sense/voice_clone/        ← dev fallback

Any provider that supports cloning will automatically detect the file
and switch to clone mode. Remove the file → reverts to preset voice.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

from loguru import logger

# ── Voice clone locator ───────────────────────────────────────────────────────
try:
    from serenity.senses.voice_clone import get_clone_audio, file_hash, resample_to_24k
except ImportError:
    # Graceful fallback if senses package unavailable
    def get_clone_audio():  # type: ignore[misc]
        return None
    def file_hash(p):  # type: ignore[misc]
        return ""
    def resample_to_24k(src, dst=None):  # type: ignore[misc]
        return src

# ── Per-engine install commands (shown on ImportError) ───────────────────────
_ENGINE_INSTALL: dict[str, str] = {
    "qwen_tts":                       "pip install -U qwen-tts",
    "kokoro":                         "pip install kokoro soundfile",
    "TTS":                            'pip install TTS  (and: pip install "pandas<2")',
    "piper":                          "pip install piper-tts  OR grab the binary from https://github.com/rhasspy/piper/releases",
    "bark":                           "pip install suno-bark scipy",
    "edge_tts":                       "pip install edge-tts",
    "google.cloud.texttospeech":      "pip install google-cloud-texttospeech",
    "boto3":                          "pip install boto3",
    "cartesia":                       "pip install cartesia",
    "pyht":                           "pip install pyht",
    "deepgram":                       "pip install deepgram-sdk",
}

# ── Module-level singleton ────────────────────────────────────────────────────
_tts_instance = None

# Response format → file extension
_FORMAT_EXT: dict[str, str] = {
    "opus": ".ogg",
    "mp3":  ".mp3",
    "aac":  ".aac",
    "flac": ".flac",
    "wav":  ".wav",
    "pcm":  ".pcm",
}

_DASHSCOPE_INT_URL = (
    "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/"
    "multimodal-generation/generation"
)
_DASHSCOPE_CN_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
    "multimodal-generation/generation"
)


# ─────────────────────────────────────────────────────────────────────────────
# LOCAL PROVIDERS
# ─────────────────────────────────────────────────────────────────────────────

# ── Qwen3 Local TTS ───────────────────────────────────────────────────────────

class Qwen3LocalTTSProvider:
    """Qwen3 TTS running fully locally via the `qwen-tts` package.

    Requires:  pip install -U qwen-tts
    Models downloaded from Hugging Face on first use (Apache 2.0, free):

    Normal mode (preset voice):
      Model: Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice
      Voices: Cherry, Vivian, Ryan, Sohee, Alloy, Echo, Fable, Onyx, Nova

    Clone mode (drop audio into voice_clone/ automatically):
      Model: Qwen/Qwen3-TTS-12Hz-1.7B-Base
      Serenity detects the file, switches model, and clones that voice.
      Remove the file → reverts to preset voice.

    GPU (CUDA) strongly recommended. CPU works but is slow (~10–30× slower).
    """

    _CLONE_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}
    _MODEL_CUSTOM = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
    _MODEL_BASE   = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
    _MODEL_0_6B   = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"

    def __init__(
        self,
        model_name: str = "",      # "" = auto (CustomVoice / Base by clone state)
        voice: str = "Cherry",     # preset voice when not cloning
        device: str = "",          # "" = auto CUDA → GPU first, CPU fallback
        instruct: str = "",        # e.g. "speak warmly and at a relaxed pace"
    ):
        self._preset_voice   = voice
        self._device         = device or ("cuda" if self._cuda_ok() else "cpu")
        self.instruct        = instruct
        self._override_model = model_name
        self._model_cache: dict[str, object] = {}

    @staticmethod
    def _cuda_ok() -> bool:
        try:
            import torch
            return torch.cuda.is_available()
        except ImportError:
            return False

    def _get_model(self, model_name: str):
        if model_name in self._model_cache:
            return self._model_cache[model_name]
        try:
            from qwen_tts import Qwen3TTSModel  # type: ignore
        except ImportError:
            raise RuntimeError(
                f"qwen-tts not installed.\n  Run: {_ENGINE_INSTALL['qwen_tts']}"
            )
        logger.info("Qwen3 Local TTS: loading {} on {}…", model_name, self._device)
        device_map = self._device if self._device in ("cpu", "cuda") else "auto"
        model = Qwen3TTSModel.from_pretrained(model_name, device_map=device_map)
        self._model_cache[model_name] = model
        logger.info("Qwen3 Local TTS: {} ready.", model_name)
        return model

    @property
    def output_extension(self) -> str:
        return ".wav"

    async def synthesize(self, text: str, output_path: Path) -> bool:
        if not text or not text.strip():
            return False

        clone_file = get_clone_audio()

        def _run() -> bool:
            try:
                import soundfile as sf  # type: ignore
            except ImportError:
                logger.error("soundfile not installed. Run: pip install soundfile")
                return False

            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)

                if clone_file is not None:
                    # Clone mode — any Base model supports this
                    ref = resample_to_24k(clone_file)
                    model_name = self._override_model or self._MODEL_BASE
                    model = self._get_model(model_name)
                    logger.info("Qwen3 TTS: clone mode — ref '{}'", ref.name)
                    # x_vector_only_mode=True skips the need for ref_text
                    wavs, rate = model.generate_voice_clone(
                        text=text[:4096],
                        language="en",
                        ref_audio=str(ref),
                        x_vector_only_mode=True,
                    )
                else:
                    # Preset voice mode — requires the CustomVoice model
                    model_name = self._override_model or self._MODEL_CUSTOM
                    if "Base" in model_name and "CustomVoice" not in model_name:
                        logger.error(
                            "Qwen3 TTS: Base models need a voice clone file. "
                            "Drop a WAV/MP3 into ~/.serenity/voice_clone/ "
                            "or switch ttsProvider to 'qwen3-local-1.7b' for preset voices."
                        )
                        return False
                    model = self._get_model(model_name)
                    logger.info("Qwen3 TTS: preset voice '{}'", self._preset_voice)
                    wavs, rate = model.generate_custom_voice(
                        text=text[:4096],
                        speaker=self._preset_voice,
                        language="en",
                        instruct=self.instruct or None,
                    )

                if not wavs:
                    logger.error("Qwen3 Local TTS: model returned no audio")
                    return False

                sf.write(str(output_path), wavs[0], rate)
                mode = f"clone({clone_file.name})" if clone_file else f"voice({self._preset_voice})"
                logger.info(
                    "Qwen3 Local TTS [{}]: {} chars → {} ({} bytes)",
                    mode, len(text), output_path.name, output_path.stat().st_size,
                )
                return True
            except Exception as e:
                logger.error("Qwen3 Local TTS failed: {}", e)
                return False

        return await asyncio.get_running_loop().run_in_executor(None, _run)


# ── Kokoro TTS ────────────────────────────────────────────────────────────────

class KokoroTTSProvider:
    """Kokoro-82M — lightweight, fast, CPU-friendly local TTS.

    Two modes:
      Direct  — uses `kokoro` Python package directly
      Server  — calls any OpenAI-compatible /v1/audio/speech endpoint
                (e.g. kokoro-fastapi or kokoro-onnx server)

    Voices (American EN): af_heart, af_bella, af_nicole, am_adam, am_michael
    Voices (British EN):  bf_emma, bf_isabella, bm_george, bm_lewis
    """

    def __init__(
        self,
        voice: str = "af_heart",
        lang: str = "a",          # "a" = American EN, "b" = British EN
        speed: float = 1.0,
        api_base: str = "",       # set to use server mode
        api_key: str = "",
    ):
        self.voice    = voice
        self.lang     = lang
        self.speed    = speed
        self.api_base = api_base.strip()
        self.api_key  = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._pipeline = None

    @property
    def output_extension(self) -> str:
        return ".wav"

    async def synthesize(self, text: str, output_path: Path) -> bool:
        if not text or not text.strip():
            return False

        if self.api_base:
            return await self._synthesize_server(text, output_path)
        return await self._synthesize_direct(text, output_path)

    async def _synthesize_server(self, text: str, output_path: Path) -> bool:
        import httpx
        payload = {
            "model": "kokoro",
            "input": text[:4096],
            "voice": self.voice,
            "speed": self.speed,
            "response_format": "wav",
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = self.api_base.rstrip("/") + "/audio/speech"
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    resp.raise_for_status()
                    with open(output_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(8192):
                            f.write(chunk)
            logger.info("Kokoro TTS (server): {} chars → {}", len(text), output_path.name)
            return True
        except Exception as e:
            logger.error("Kokoro TTS (server) failed: {}", e)
            return False

    async def _synthesize_direct(self, text: str, output_path: Path) -> bool:
        def _run() -> bool:
            try:
                import soundfile as sf
                from kokoro import KPipeline  # type: ignore
                if self._pipeline is None:
                    self.__class__._pipeline = KPipeline(lang_code=self.lang)
                pipeline = self._pipeline
                output_path.parent.mkdir(parents=True, exist_ok=True)
                import numpy as np
                chunks = []
                for _, _, audio in pipeline(text, voice=self.voice, speed=self.speed):
                    if audio is not None:
                        chunks.append(audio)
                if chunks:
                    data = np.concatenate(chunks)
                    sf.write(str(output_path), data, 24000)
                    logger.info("Kokoro TTS (direct): {} chars → {}", len(text), output_path.name)
                    return True
                return False
            except ImportError:
                logger.error("kokoro not installed.  Run: {}", _ENGINE_INSTALL["kokoro"])
                return False
            except Exception as e:
                logger.error("Kokoro TTS failed: {}", e)
                return False

        return await asyncio.get_running_loop().run_in_executor(None, _run)


# ── Coqui XTTS-v2 ─────────────────────────────────────────────────────────────

class CoquiTTSProvider:
    """Coqui XTTS-v2 — zero-shot multilingual TTS with voice cloning.

    Requires:  pip install TTS
    Model:     tts_models/multilingual/multi-dataset/xtts_v2 (~2 GB)

    Clone support: YES — drops any audio from voice_clone/ as speaker_wav.
    Fallback:      built-in speaker list when no clone file is present.

    GPU recommended. CPU works, ~3–8× slower.
    """

    _MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"
    _DEFAULT_SPEAKER = "Claribel Dervla"  # any XTTS built-in speaker

    def __init__(
        self,
        speaker: str = "",          # preset speaker (ignored in clone mode)
        language: str = "en",
        device: str = "",           # "" = auto (GPU → CPU)
    ):
        self.speaker  = speaker or self._DEFAULT_SPEAKER
        self.language = language
        self._device  = device or ("cuda" if self._cuda_ok() else "cpu")
        self._tts     = None

    @staticmethod
    def _cuda_ok() -> bool:
        try:
            import torch
            return torch.cuda.is_available()
        except ImportError:
            return False

    def _get_tts(self):
        if self._tts is not None:
            return self._tts
        try:
            from TTS.api import TTS  # type: ignore
        except ImportError:
            raise RuntimeError(
                f"Coqui TTS not installed.\n  Run: {_ENGINE_INSTALL['TTS']}"
            )
        logger.info("Coqui XTTS-v2: loading model on {}…", self._device)
        tts = TTS(self._MODEL)
        if self._device == "cuda":
            tts = tts.to("cuda")
        self._tts = tts
        logger.info("Coqui XTTS-v2: ready.")
        return tts

    @property
    def output_extension(self) -> str:
        return ".wav"

    async def synthesize(self, text: str, output_path: Path) -> bool:
        if not text or not text.strip():
            return False

        clone_file = get_clone_audio()

        def _run() -> bool:
            try:
                tts = self._get_tts()
                output_path.parent.mkdir(parents=True, exist_ok=True)

                if clone_file is not None:
                    ref = resample_to_24k(clone_file)
                    logger.info("Coqui XTTS-v2: clone mode — ref '{}'", ref.name)
                    tts.tts_to_file(
                        text=text[:4096],
                        file_path=str(output_path),
                        speaker_wav=str(ref),
                        language=self.language,
                    )
                else:
                    logger.info("Coqui XTTS-v2: speaker '{}'", self.speaker)
                    tts.tts_to_file(
                        text=text[:4096],
                        file_path=str(output_path),
                        speaker=self.speaker,
                        language=self.language,
                    )

                logger.info("Coqui XTTS-v2: {} chars → {} ({} bytes)",
                    len(text), output_path.name, output_path.stat().st_size)
                return True
            except Exception as e:
                logger.error("Coqui XTTS-v2 failed: {}", e)
                return False

        return await asyncio.get_running_loop().run_in_executor(None, _run)


# ── Piper TTS ─────────────────────────────────────────────────────────────────

class PiperTTSProvider:
    """Piper TTS — ultra-fast, runs on CPU, subprocess-based.

    Requires:  piper-tts binary on PATH  OR  pip install piper-tts (Python wrapper)
    Model:     download from https://huggingface.co/rhasspy/piper-voices
               e.g. en_US-lessac-medium.onnx  +  en_US-lessac-medium.onnx.json
    """

    def __init__(
        self,
        model_path: str = "",      # path to .onnx model file
        speaker_id: int = 0,
        length_scale: float = 1.0,
        noise_scale: float = 0.667,
        noise_w: float = 0.8,
    ):
        self.model_path   = model_path
        self.speaker_id   = speaker_id
        self.length_scale = length_scale
        self.noise_scale  = noise_scale
        self.noise_w      = noise_w

    @property
    def output_extension(self) -> str:
        return ".wav"

    async def synthesize(self, text: str, output_path: Path) -> bool:
        if not text or not text.strip():
            return False
        if not self.model_path:
            logger.error("Piper TTS: no model_path configured")
            return False

        def _run() -> bool:
            import subprocess
            output_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                cmd = [
                    "piper",
                    "--model", self.model_path,
                    "--output_file", str(output_path),
                    "--length_scale", str(self.length_scale),
                    "--noise_scale", str(self.noise_scale),
                    "--noise_w", str(self.noise_w),
                ]
                if self.speaker_id:
                    cmd += ["--speaker", str(self.speaker_id)]
                result = subprocess.run(
                    cmd,
                    input=text.encode(),
                    capture_output=True,
                    timeout=60,
                )
                if result.returncode == 0 and output_path.exists():
                    logger.info("Piper TTS: {} chars → {}", len(text), output_path.name)
                    return True
                logger.error("Piper TTS error: {}", result.stderr.decode())
                return False
            except FileNotFoundError:
                # Fallback to piper Python package
                try:
                    from piper import PiperVoice  # type: ignore
                    import wave
                    voice = PiperVoice.load(self.model_path)
                    with wave.open(str(output_path), "w") as wav_file:
                        voice.synthesize(text, wav_file)
                    logger.info("Piper TTS (lib): {} chars → {}", len(text), output_path.name)
                    return True
                except ImportError:
                    logger.error(
                        "Piper not found. Install: pip install piper-tts "
                        "or put piper binary on PATH"
                    )
                    return False
            except Exception as e:
                logger.error("Piper TTS failed: {}", e)
                return False

        return await asyncio.get_running_loop().run_in_executor(None, _run)


# ── Bark TTS ──────────────────────────────────────────────────────────────────

class BarkTTSProvider:
    """Bark by suno-ai — expressive TTS with [laughs], [sighs], etc.

    Requires:  pip install bark  (or suno-bark)
    Models:    ~1–5 GB, downloaded on first use from HuggingFace.

    Speaker prompts: v2/en_speaker_6, v2/en_speaker_9, etc.
    Wrap text with non-speech sounds: "Hello! [laughs] That's funny."
    """

    _DEFAULT_SPEAKER = "v2/en_speaker_6"

    def __init__(
        self,
        speaker: str = "",
        use_small_models: bool = True,   # smaller, faster, less RAM
        device: str = "",
    ):
        self.speaker  = speaker or self._DEFAULT_SPEAKER
        self.small    = use_small_models
        self._device  = device or ("cuda" if self._cuda_ok() else "cpu")
        self._loaded  = False

    @staticmethod
    def _cuda_ok() -> bool:
        try:
            import torch
            return torch.cuda.is_available()
        except ImportError:
            return False

    @property
    def output_extension(self) -> str:
        return ".wav"

    async def synthesize(self, text: str, output_path: Path) -> bool:
        if not text or not text.strip():
            return False

        def _run() -> bool:
            try:
                from scipy.io.wavfile import write as wav_write
                import os as _os
                if self.small:
                    _os.environ.setdefault("SUNO_USE_SMALL_MODELS", "1")
                if self._device == "cpu":
                    _os.environ.setdefault("SUNO_OFFLOAD_CPU", "1")

                from bark import generate_audio, preload_models  # type: ignore
                if not self._loaded:
                    preload_models()
                    self.__class__._loaded = True

                audio = generate_audio(text[:1024], history_prompt=self.speaker)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                wav_write(str(output_path), 24000, audio)
                logger.info("Bark TTS: {} chars → {}", len(text), output_path.name)
                return True
            except ImportError:
                logger.error("bark not installed.  Run: {}", _ENGINE_INSTALL["bark"])
                return False
            except Exception as e:
                logger.error("Bark TTS failed: {}", e)
                return False

        return await asyncio.get_running_loop().run_in_executor(None, _run)


# ─────────────────────────────────────────────────────────────────────────────
# CLOUD FREE
# ─────────────────────────────────────────────────────────────────────────────

# ── edge-tts ──────────────────────────────────────────────────────────────────

class EdgeTTSProvider:
    """Microsoft Edge TTS — 200+ voices, free, no API key required.

    Requires:  pip install edge-tts
    Voices:    en-US-AriaNeural, en-US-GuyNeural, en-GB-SoniaNeural, etc.
    Full list: edge-tts --list-voices
    """

    def __init__(
        self,
        voice: str = "en-US-AriaNeural",
        rate: str = "+0%",    # e.g. "+10%", "-5%"
        pitch: str = "+0Hz",  # e.g. "+50Hz", "-20Hz"
        volume: str = "+0%",
    ):
        self.voice  = voice
        self.rate   = rate
        self.pitch  = pitch
        self.volume = volume

    @property
    def output_extension(self) -> str:
        return ".mp3"

    async def synthesize(self, text: str, output_path: Path) -> bool:
        if not text or not text.strip():
            return False
        try:
            import edge_tts  # type: ignore
            output_path.parent.mkdir(parents=True, exist_ok=True)
            communicate = edge_tts.Communicate(
                text[:5000],
                self.voice,
                rate=self.rate,
                pitch=self.pitch,
                volume=self.volume,
            )
            await communicate.save(str(output_path))
            logger.info("edge-tts: {} chars → {}", len(text), output_path.name)
            return True
        except ImportError:
            logger.error("edge-tts not installed.  Run: {}", _ENGINE_INSTALL["edge_tts"])
            return False
        except Exception as e:
            logger.error("edge-tts failed: {}", e)
            return False


# ─────────────────────────────────────────────────────────────────────────────
# CLOUD PAID
# ─────────────────────────────────────────────────────────────────────────────

# ── ElevenLabs ────────────────────────────────────────────────────────────────

class ElevenLabsTTSProvider:
    """ElevenLabs cloud TTS — ultra-realistic, ~75 ms first chunk with Flash v2.5.

    Clone support: YES — Instant Voice Clone API.
    Drop audio into voice_clone/ → temporary clone voice_id is created and
    cached per session (re-upload only on file change).

    Models (fastest → highest quality):
      eleven_flash_v2_5      ~75 ms
      eleven_turbo_v2_5     ~300 ms
      eleven_multilingual_v2 ~400 ms

    API key from: https://elevenlabs.io
    """

    _BASE_URL    = "https://api.elevenlabs.io/v1"
    _DEFAULT_VID = "21m00Tcm4TlvDq8ikWAM"  # Rachel

    def __init__(
        self,
        api_key: str = "",
        voice_id: str = "",
        model: str = "eleven_flash_v2_5",
        stability: float = 0.5,
        similarity_boost: float = 0.75,
        style: float = 0.0,
        output_format: str = "mp3_44100_128",
    ):
        self.api_key    = api_key or os.environ.get("ELEVENLABS_API_KEY", "")
        self.voice_id   = voice_id or self._DEFAULT_VID
        self.model      = model
        self.stability  = stability
        self.similarity = similarity_boost
        self.style      = style
        self.output_fmt = output_format
        # Clone cache: file_hash → temporary voice_id
        self._clone_cache: dict[str, str] = {}

    @property
    def output_extension(self) -> str:
        return ".mp3"

    async def _get_clone_voice_id(self, clone_file: Path) -> str | None:
        """Upload clone audio (if not already cached) and return ephemeral voice_id."""
        import httpx
        fhash = file_hash(clone_file)
        if fhash in self._clone_cache:
            return self._clone_cache[fhash]
        try:
            logger.info("ElevenLabs: uploading clone reference '{}'…", clone_file.name)
            async with httpx.AsyncClient(timeout=120.0) as client:
                with open(clone_file, "rb") as audio_fh:
                    resp = await client.post(
                        f"{self._BASE_URL}/voices/add",
                        headers={"xi-api-key": self.api_key},
                        data={"name": f"serenity_clone_{fhash}"},
                        files={"files": (clone_file.name, audio_fh, "audio/mpeg")},
                    )
                    resp.raise_for_status()
                    vid = resp.json()["voice_id"]
            logger.info("ElevenLabs: clone voice created (id={})", vid)
            self._clone_cache[fhash] = vid
            return vid
        except Exception as e:
            logger.error("ElevenLabs IVC upload failed: {}", e)
            return None

    async def synthesize(self, text: str, output_path: Path) -> bool:
        if not self.api_key:
            logger.warning("ElevenLabs TTS: no API key (set ELEVENLABS_API_KEY)")
            return False
        if not text or not text.strip():
            return False

        import httpx

        # Determine voice ID (clone or preset)
        clone_file = get_clone_audio()
        if clone_file is not None:
            vid = await self._get_clone_voice_id(clone_file)
            vid = vid or self.voice_id  # fallback to preset if upload failed
        else:
            vid = self.voice_id

        url = f"{self._BASE_URL}/text-to-speech/{vid}/stream"
        payload = {
            "text": text[:5000],
            "model_id": self.model,
            "output_format": self.output_fmt,
            "voice_settings": {
                "stability":        self.stability,
                "similarity_boost": self.similarity,
                "style":            self.style,
                "use_speaker_boost": True,
            },
        }
        headers = {
            "xi-api-key":   self.api_key,
            "Content-Type": "application/json",
            "Accept":       "audio/mpeg",
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    resp.raise_for_status()
                    with open(output_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(4096):
                            f.write(chunk)
            mode = f"clone({clone_file.name})" if clone_file else f"preset({vid})"
            logger.info("ElevenLabs TTS [{}]: {} chars → {} ({} bytes)",
                mode, len(text), output_path.name, output_path.stat().st_size)
            return True
        except Exception as e:
            logger.error("ElevenLabs TTS request failed: {}", e)
            output_path.unlink(missing_ok=True)
            return False


# ── OpenAI TTS ────────────────────────────────────────────────────────────────

class OpenAICompatibleTTSProvider:
    """TTS using any /v1/audio/speech endpoint (OpenAI, kokoro server, etc.).

    Streams audio directly to file — no buffering in memory.
    """

    _DEFAULT_API_BASE = "https://api.openai.com/v1/audio/speech"

    def __init__(
        self,
        api_key: str = "",
        api_base: str = "",
        model: str = "tts-1",
        voice: str = "alloy",
        speed: float = 1.0,
        response_format: str = "opus",
    ):
        self.api_key         = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.api_base        = api_base.strip() or self._DEFAULT_API_BASE
        self.model           = model
        self.voice           = voice
        self.speed           = max(0.25, min(4.0, speed))
        self.response_format = response_format
        self._ext            = _FORMAT_EXT.get(response_format, ".ogg")

    @property
    def output_extension(self) -> str:
        return self._ext

    async def synthesize(self, text: str, output_path: Path) -> bool:
        import httpx
        if not self.api_key:
            logger.warning("OpenAI TTS: no API key (set OPENAI_API_KEY)")
            return False
        if not text or not text.strip():
            return False

        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": self.model,
            "input": text[:4096],
            "voice": self.voice,
            "speed": self.speed,
            "response_format": self.response_format,
        }
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST", self.api_base,
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"},
                    json=payload,
                ) as resp:
                    resp.raise_for_status()
                    with open(output_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(8192):
                            f.write(chunk)
            logger.info("OpenAI TTS: {} chars → {} ({} bytes)",
                len(text), output_path.name, output_path.stat().st_size)
            return True
        except Exception as e:
            logger.error("OpenAI TTS synthesis failed: {}", e)
            output_path.unlink(missing_ok=True)
            return False


# ── Google Cloud TTS ──────────────────────────────────────────────────────────

class GoogleTTSProvider:
    """Google Cloud Text-to-Speech.

    Requires:  pip install google-cloud-texttospeech
    Credentials: set GOOGLE_APPLICATION_CREDENTIALS env var, or pass api_key.

    Voices: en-US-Neural2-A … F, en-US-Journey-D, en-US-Chirp3-HD-*, etc.
    """

    def __init__(
        self,
        api_key: str = "",
        language_code: str = "en-US",
        voice_name: str = "en-US-Neural2-F",
        speaking_rate: float = 1.0,
        pitch: float = 0.0,
        audio_encoding: str = "MP3",   # LINEAR16, MP3, OGG_OPUS
    ):
        self.api_key       = api_key or os.environ.get("GOOGLE_TTS_API_KEY", "")
        self.language_code = language_code
        self.voice_name    = voice_name
        self.speaking_rate = speaking_rate
        self.pitch         = pitch
        self.encoding      = audio_encoding
        self._ext          = {"MP3": ".mp3", "LINEAR16": ".wav",
                              "OGG_OPUS": ".ogg"}.get(audio_encoding, ".mp3")

    @property
    def output_extension(self) -> str:
        return self._ext

    async def synthesize(self, text: str, output_path: Path) -> bool:
        if not text or not text.strip():
            return False

        def _run() -> bool:
            try:
                from google.cloud import texttospeech  # type: ignore
            except ImportError:
                logger.error("google-cloud-texttospeech not installed.  Run: {}",
                             _ENGINE_INSTALL["google.cloud.texttospeech"])
                return False
            try:
                if self.api_key:
                    client = texttospeech.TextToSpeechClient(
                        client_options={"api_key": self.api_key}
                    )
                else:
                    client = texttospeech.TextToSpeechClient()

                synthesis_input = texttospeech.SynthesisInput(text=text[:5000])
                voice = texttospeech.VoiceSelectionParams(
                    language_code=self.language_code,
                    name=self.voice_name,
                )
                audio_config = texttospeech.AudioConfig(
                    audio_encoding=getattr(texttospeech.AudioEncoding, self.encoding),
                    speaking_rate=self.speaking_rate,
                    pitch=self.pitch,
                )
                response = client.synthesize_speech(
                    input=synthesis_input, voice=voice, audio_config=audio_config
                )
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(response.audio_content)
                logger.info("Google TTS: {} chars → {}", len(text), output_path.name)
                return True
            except Exception as e:
                logger.error("Google TTS failed: {}", e)
                return False

        return await asyncio.get_running_loop().run_in_executor(None, _run)


# ── Amazon Polly ──────────────────────────────────────────────────────────────

class AmazonPollyProvider:
    """Amazon Polly TTS.

    Requires:  pip install boto3
    Auth:      AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION env vars,
               or pass them directly.

    Voices: Joanna, Matthew, Ivy, Kendra, Kimberly, Salli, Joey, Justin, etc.
    Neural voices add "Engine='neural'" automatically when voice supports it.
    """

    def __init__(
        self,
        access_key: str = "",
        secret_key: str = "",
        region: str = "us-east-1",
        voice_id: str = "Joanna",
        engine: str = "neural",       # "neural" or "standard"
        output_format: str = "mp3",   # "mp3", "ogg_vorbis", "pcm"
        sample_rate: str = "22050",
    ):
        self.access_key    = access_key or os.environ.get("AWS_ACCESS_KEY_ID", "")
        self.secret_key    = secret_key or os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        self.region        = region or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        self.voice_id      = voice_id
        self.engine        = engine
        self.output_format = output_format
        self.sample_rate   = sample_rate
        self._ext          = {"mp3": ".mp3", "ogg_vorbis": ".ogg", "pcm": ".pcm"}.get(
            output_format, ".mp3"
        )

    @property
    def output_extension(self) -> str:
        return self._ext

    async def synthesize(self, text: str, output_path: Path) -> bool:
        if not text or not text.strip():
            return False

        def _run() -> bool:
            try:
                import boto3  # type: ignore
            except ImportError:
                logger.error("boto3 not installed.  Run: {}", _ENGINE_INSTALL["boto3"])
                return False
            try:
                session_kwargs: dict = {"region_name": self.region}
                if self.access_key and self.secret_key:
                    session_kwargs["aws_access_key_id"]     = self.access_key
                    session_kwargs["aws_secret_access_key"] = self.secret_key
                polly = boto3.client("polly", **session_kwargs)

                kw: dict = {
                    "Text":         text[:3000],
                    "OutputFormat": self.output_format,
                    "VoiceId":      self.voice_id,
                    "SampleRate":   self.sample_rate,
                }
                # Neural engine: not all voices support it — try, fall back
                try:
                    response = polly.synthesize_speech(**kw, Engine=self.engine)
                except polly.exceptions.InvalidSampleRateException:
                    response = polly.synthesize_speech(**kw, Engine="standard")

                output_path.parent.mkdir(parents=True, exist_ok=True)
                audio_stream = response["AudioStream"].read()
                output_path.write_bytes(audio_stream)
                logger.info("Amazon Polly: {} chars → {}", len(text), output_path.name)
                return True
            except Exception as e:
                logger.error("Amazon Polly failed: {}", e)
                return False

        return await asyncio.get_running_loop().run_in_executor(None, _run)


# ── Cartesia TTS ──────────────────────────────────────────────────────────────

class CartesiaTTSProvider:
    """Cartesia real-time TTS with voice cloning.

    Requires:  pip install cartesia
    API key:   https://cartesia.ai

    Clone support: YES — uploads reference audio via voices.clone(),
    caches the resulting voice_id per file hash.
    """

    _DEFAULT_VOICE = "a0e99841-438c-4a64-b679-ae501e7d6091"  # Barbershop Man

    def __init__(
        self,
        api_key: str = "",
        voice_id: str = "",
        model: str = "sonic-2",
        language: str = "en",
        output_format: str = "mp3",
    ):
        self.api_key    = api_key or os.environ.get("CARTESIA_API_KEY", "")
        self.voice_id   = voice_id or self._DEFAULT_VOICE
        self.model      = model
        self.language   = language
        self.fmt        = output_format
        self._clone_cache: dict[str, str] = {}

    @property
    def output_extension(self) -> str:
        return ".mp3"

    def _get_client(self):
        try:
            from cartesia import Cartesia  # type: ignore
            return Cartesia(api_key=self.api_key)
        except ImportError:
            raise RuntimeError(
                f"cartesia not installed.\n  Run: {_ENGINE_INSTALL['cartesia']}"
            )

    async def _get_clone_voice_id(self, clone_file: Path) -> str | None:
        fhash = file_hash(clone_file)
        if fhash in self._clone_cache:
            return self._clone_cache[fhash]

        def _upload() -> str | None:
            try:
                client = self._get_client()
                logger.info("Cartesia: cloning voice from '{}'…", clone_file.name)
                with open(clone_file, "rb") as f:
                    voice = client.voices.clone(
                        name=f"serenity_clone_{fhash}",
                        clip=f,
                        mode="similarity",
                        language=self.language,
                    )
                vid = voice.id if hasattr(voice, "id") else voice["id"]
                logger.info("Cartesia: clone voice created (id={})", vid)
                return vid
            except Exception as e:
                logger.error("Cartesia voice clone failed: {}", e)
                return None

        vid = await asyncio.get_running_loop().run_in_executor(None, _upload)
        if vid:
            self._clone_cache[fhash] = vid
        return vid

    async def synthesize(self, text: str, output_path: Path) -> bool:
        if not self.api_key:
            logger.warning("Cartesia TTS: no API key (set CARTESIA_API_KEY)")
            return False
        if not text or not text.strip():
            return False

        clone_file = get_clone_audio()
        if clone_file is not None:
            vid = await self._get_clone_voice_id(clone_file)
            vid = vid or self.voice_id
        else:
            vid = self.voice_id

        def _run() -> bool:
            try:
                client = self._get_client()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                audio = client.tts.bytes(
                    model_id=self.model,
                    transcript=text[:8000],
                    voice={"id": vid},
                    language=self.language,
                    output_format={
                        "container": self.fmt,
                        "encoding": "mp3",
                        "sample_rate": 44100,
                    },
                )
                output_path.write_bytes(audio)
                mode = f"clone({clone_file.name})" if clone_file else f"preset"
                logger.info("Cartesia TTS [{}]: {} chars → {}", mode, len(text), output_path.name)
                return True
            except Exception as e:
                logger.error("Cartesia TTS failed: {}", e)
                return False

        return await asyncio.get_running_loop().run_in_executor(None, _run)


# ── PlayHT TTS ────────────────────────────────────────────────────────────────

class PlayHTTTSProvider:
    """PlayHT — high-quality TTS with instant voice cloning.

    Requires:  pip install pyht
    Credentials: PLAY_HT_USER_ID + PLAY_HT_API_KEY env vars, or pass directly.
    API docs:  https://docs.play.ht/

    Clone support: YES — instant clone via PlayHT 2.0 Turbo.
    """

    _DEFAULT_VOICE = "s3://voice-cloning-zero-shot/d9ff78ba-d016-47f6-b0ef-dd630f59414e/female-cs/manifest.json"

    def __init__(
        self,
        api_key: str = "",
        user_id: str = "",
        voice: str = "",
        model: str = "Play3.0-mini",
        quality: str = "medium",
        output_format: str = "mp3",
    ):
        self.api_key = api_key or os.environ.get("PLAY_HT_API_KEY", "")
        self.user_id = user_id or os.environ.get("PLAY_HT_USER_ID", "")
        self.voice   = voice or self._DEFAULT_VOICE
        self.model   = model
        self.quality = quality
        self.fmt     = output_format
        self._clone_cache: dict[str, str] = {}

    @property
    def output_extension(self) -> str:
        return ".mp3"

    async def _get_clone_voice(self, clone_file: Path) -> str | None:
        fhash = file_hash(clone_file)
        if fhash in self._clone_cache:
            return self._clone_cache[fhash]

        def _upload() -> str | None:
            try:
                from pyht import Client  # type: ignore
                client = Client(user_id=self.user_id, api_key=self.api_key)
                logger.info("PlayHT: cloning voice from '{}'…", clone_file.name)
                # Instant clone: uploads audio, returns cloned voice manifest URL
                result = client.clone_voice(
                    name=f"serenity_{fhash}",
                    voice_file=str(clone_file),
                )
                vid = result.id if hasattr(result, "id") else result.get("id") or result.get("voice_engine")
                logger.info("PlayHT: clone voice created (id={})", vid)
                return str(vid) if vid else None
            except ImportError:
                logger.error("pyht not installed.  Run: {}", _ENGINE_INSTALL["pyht"])
                return None
            except Exception as e:
                logger.error("PlayHT voice clone failed: {}", e)
                return None

        vid = await asyncio.get_running_loop().run_in_executor(None, _upload)
        if vid:
            self._clone_cache[fhash] = vid
        return vid

    async def synthesize(self, text: str, output_path: Path) -> bool:
        if not (self.api_key and self.user_id):
            logger.warning("PlayHT TTS: missing PLAY_HT_API_KEY / PLAY_HT_USER_ID")
            return False
        if not text or not text.strip():
            return False

        clone_file = get_clone_audio()
        if clone_file is not None:
            voice = await self._get_clone_voice(clone_file)
            voice = voice or self.voice
        else:
            voice = self.voice

        def _run() -> bool:
            try:
                from pyht import Client, TTSOptions  # type: ignore
                client = Client(user_id=self.user_id, api_key=self.api_key)
                options = TTSOptions(
                    voice=voice,
                    sample_rate=44100,
                    quality=self.quality,
                    format=self.fmt,
                )
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, "wb") as f:
                    for chunk in client.tts(text[:5000], options):
                        f.write(chunk)
                mode = f"clone({clone_file.name})" if clone_file else "preset"
                logger.info("PlayHT TTS [{}]: {} chars → {}", mode, len(text), output_path.name)
                return True
            except ImportError:
                logger.error("pyht not installed.  Run: {}", _ENGINE_INSTALL["pyht"])
                return False
            except Exception as e:
                logger.error("PlayHT TTS failed: {}", e)
                return False

        return await asyncio.get_running_loop().run_in_executor(None, _run)


# ── Deepgram TTS ─────────────────────────────────────────────────────────────

class DeepgramTTSProvider:
    """Deepgram Aura TTS — fast, clean, production-quality cloud TTS.

    Requires:  pip install deepgram-sdk
    API key:   https://deepgram.com

    Models/Voices: aura-asteria-en, aura-orion-en, aura-luna-en,
                   aura-stella-en, aura-athena-en, aura-hera-en,
                   aura-zeus-en, aura-arcas-en, aura-perseus-en, etc.
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "aura-asteria-en",
        encoding: str = "mp3",
        sample_rate: int = 24000,
    ):
        self.api_key     = api_key or os.environ.get("DEEPGRAM_API_KEY", "")
        self.model       = model
        self.encoding    = encoding
        self.sample_rate = sample_rate

    @property
    def output_extension(self) -> str:
        return ".mp3"

    async def synthesize(self, text: str, output_path: Path) -> bool:
        if not self.api_key:
            logger.warning("Deepgram TTS: no API key (set DEEPGRAM_API_KEY)")
            return False
        if not text or not text.strip():
            return False

        def _run() -> bool:
            try:
                from deepgram import DeepgramClient, SpeakOptions  # type: ignore
            except ImportError:
                logger.error("deepgram-sdk not installed.  Run: {}", _ENGINE_INSTALL["deepgram"])
                return False
            try:
                client = DeepgramClient(self.api_key)
                options = SpeakOptions(
                    model=self.model,
                    encoding=self.encoding,
                    sample_rate=self.sample_rate,
                )
                output_path.parent.mkdir(parents=True, exist_ok=True)
                client.speak.v("1").save(
                    str(output_path),
                    {"text": text[:2000]},
                    options,
                )
                logger.info("Deepgram TTS: {} chars → {}", len(text), output_path.name)
                return True
            except Exception as e:
                logger.error("Deepgram TTS failed: {}", e)
                return False

        return await asyncio.get_running_loop().run_in_executor(None, _run)


# ── DashScope Qwen3 TTS (cloud) ───────────────────────────────────────────────

class DashScopeTTSProvider:
    """Qwen3 TTS via DashScope SSE API — same model, hosted by Alibaba Cloud.

    Free tier available. Models: qwen3-tts-flash, qwen3-tts-instruct-flash.
    Voices: cherry, serena, ethan, dylan, ryan, aiden, chelsie, mia, etc.

    API key from: https://dashscope.console.aliyun.com/apiKey
    """

    _PCM_SAMPLE_RATE  = 24_000
    _PCM_CHANNELS     = 1
    _PCM_SAMPLE_WIDTH = 2

    def __init__(
        self,
        api_key: str = "",
        model: str = "qwen3-tts-flash",
        voice: str = "cherry",
        region: str = "international",
        instruct: str = "",
    ):
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        self.model   = model
        self.voice   = voice
        self.url     = _DASHSCOPE_INT_URL if region == "international" else _DASHSCOPE_CN_URL
        self.instruct = instruct

    @property
    def output_extension(self) -> str:
        return ".ogg"

    async def synthesize(self, text: str, output_path: Path) -> bool:
        if not self.api_key:
            logger.warning("DashScope TTS: no API key configured")
            return False
        if not text or not text.strip():
            return False

        import httpx

        input_block: dict = {"text": text[:4096]}
        if self.instruct:
            input_block["instruct"] = self.instruct

        payload = {
            "model": self.model,
            "input": input_block,
            "parameters": {
                "voice":       self.voice,
                "format":      "pcm",
                "sample_rate": self._PCM_SAMPLE_RATE,
            },
        }
        headers = {
            "Authorization":    f"Bearer {self.api_key}",
            "Content-Type":     "application/json",
            "X-DashScope-SSE":  "enable",
            "X-DashScope-Async": "disable",
        }

        pcm_chunks: list[bytes] = []
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", self.url, headers=headers, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        outputs = event.get("output", {})
                        audio_b64 = outputs.get("audio") or outputs.get("audio_output", "")
                        if audio_b64:
                            pcm_chunks.append(base64.b64decode(audio_b64))
        except Exception as e:
            logger.error("DashScope TTS request failed: {}", e)
            return False

        if not pcm_chunks:
            logger.warning("DashScope TTS: no audio data received")
            return False

        raw_pcm = b"".join(pcm_chunks)
        return await self._pcm_to_ogg(raw_pcm, output_path)

    async def _pcm_to_ogg(self, pcm_data: bytes, output_path: Path) -> bool:
        def _convert() -> bool:
            try:
                from pydub import AudioSegment  # type: ignore
                seg = AudioSegment(
                    data=pcm_data,
                    sample_width=self._PCM_SAMPLE_WIDTH,
                    frame_rate=self._PCM_SAMPLE_RATE,
                    channels=self._PCM_CHANNELS,
                )
                output_path.parent.mkdir(parents=True, exist_ok=True)
                seg.export(str(output_path), format="ogg", codec="libopus")
                return True
            except ImportError:
                logger.error("pydub not installed. Run: pip install pydub (requires ffmpeg)")
                return False
            except Exception as e:
                logger.error("PCM → OGG conversion failed: {}", e)
                return False

        ok = await asyncio.get_running_loop().run_in_executor(None, _convert)
        if ok:
            logger.info("DashScope TTS: wrote {} ({} bytes)",
                output_path.name, output_path.stat().st_size)
        return ok


# ─────────────────────────────────────────────────────────────────────────────
# FACTORY
# ─────────────────────────────────────────────────────────────────────────────

# All concrete provider types (for type annotation)
_AnyProvider = (
    "Qwen3LocalTTSProvider | KokoroTTSProvider | CoquiTTSProvider | "
    "PiperTTSProvider | BarkTTSProvider | EdgeTTSProvider | "
    "ElevenLabsTTSProvider | OpenAICompatibleTTSProvider | GoogleTTSProvider | "
    "AmazonPollyProvider | CartesiaTTSProvider | PlayHTTTSProvider | "
    "DeepgramTTSProvider | DashScopeTTSProvider"
)


def build_tts_provider(
    # ── Universal ───────────────────────────────────────────────────────────
    provider: str = "qwen3-local",
    api_key: str = "",
    model: str = "",
    voice: str = "",
    # ── OpenAI-compatible ────────────────────────────────────────────────────
    api_base: str = "",
    speed: float = 1.0,
    response_format: str = "opus",
    # ── DashScope cloud ──────────────────────────────────────────────────────
    region: str = "international",
    instruct: str = "",
    # ── ElevenLabs ───────────────────────────────────────────────────────────
    voice_id: str = "",
    stability: float = 0.5,
    similarity_boost: float = 0.75,
    style: float = 0.0,
    # ── Local device override ────────────────────────────────────────────────
    local_device: str = "",
    # ── AWS Polly ────────────────────────────────────────────────────────────
    aws_access_key: str = "",
    aws_secret_key: str = "",
    aws_region: str = "us-east-1",
    # ── PlayHT ───────────────────────────────────────────────────────────────
    playht_user_id: str = "",
    # ── Piper ────────────────────────────────────────────────────────────────
    piper_model_path: str = "",
):
    """Return the appropriate TTS provider based on *provider* key.

    provider key → engine:
      "qwen3-local"    / "qwen3-local-0.6b"  → Qwen3LocalTTSProvider (0.6B, CPU-friendly)
      "qwen3-local-1.7b"                     → Qwen3LocalTTSProvider (1.7B, best quality)
      "qwen3"          / "dashscope"          → DashScopeTTSProvider (cloud)
      "kokoro"                                → KokoroTTSProvider
      "coqui"                                 → CoquiTTSProvider (with clone)
      "piper"                                 → PiperTTSProvider
      "bark"                                  → BarkTTSProvider
      "edge-tts"       / "edge"               → EdgeTTSProvider
      "elevenlabs"                            → ElevenLabsTTSProvider (with clone)
      "openai"                                → OpenAICompatibleTTSProvider
      "google"                                → GoogleTTSProvider
      "amazon"         / "polly"              → AmazonPollyProvider
      "cartesia"                              → CartesiaTTSProvider (with clone)
      "playht"                                → PlayHTTTSProvider (with clone)
      "deepgram"                              → DeepgramTTSProvider
    """
    p = provider.lower().strip()

    # ── Local: Qwen3 ─────────────────────────────────────────────────────────
    if p in ("qwen3-local", "qwen3-local-0.6b"):
        return Qwen3LocalTTSProvider(
            model_name=model or "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
            voice=voice or "Cherry",
            device=local_device,
            instruct=instruct,
        )

    if p == "qwen3-local-1.7b":
        return Qwen3LocalTTSProvider(
            model_name=model or "",  # auto: CustomVoice or Base depending on clone
            voice=voice or "Cherry",
            device=local_device,
            instruct=instruct,
        )

    # ── Cloud: DashScope / Qwen3 hosted ──────────────────────────────────────
    if p in ("qwen3", "dashscope"):
        return DashScopeTTSProvider(
            api_key=api_key,
            model=model or "qwen3-tts-flash",
            voice=voice or "cherry",
            region=region,
            instruct=instruct,
        )

    # ── Local: Kokoro ─────────────────────────────────────────────────────────
    if p == "kokoro":
        return KokoroTTSProvider(
            voice=voice or "af_heart",
            api_base=api_base,
            api_key=api_key,
            speed=speed,
        )

    # ── Local: Coqui XTTS-v2 ─────────────────────────────────────────────────
    if p in ("coqui", "xtts", "xtts-v2"):
        return CoquiTTSProvider(
            speaker=voice or "",
            device=local_device,
        )

    # ── Local: Piper ──────────────────────────────────────────────────────────
    if p == "piper":
        return PiperTTSProvider(
            model_path=piper_model_path or model,
        )

    # ── Local: Bark ───────────────────────────────────────────────────────────
    if p == "bark":
        return BarkTTSProvider(
            speaker=voice or "",
            device=local_device,
        )

    # ── Cloud free: edge-tts ─────────────────────────────────────────────────
    if p in ("edge-tts", "edge"):
        return EdgeTTSProvider(
            voice=voice or "en-US-AriaNeural",
        )

    # ── Cloud paid: ElevenLabs ───────────────────────────────────────────────
    if p == "elevenlabs":
        return ElevenLabsTTSProvider(
            api_key=api_key,
            voice_id=voice_id or voice or "",
            model=model or "eleven_flash_v2_5",
            stability=stability,
            similarity_boost=similarity_boost,
            style=style,
        )

    # ── Cloud paid: OpenAI TTS ───────────────────────────────────────────────
    if p == "openai":
        return OpenAICompatibleTTSProvider(
            api_key=api_key,
            api_base=api_base,
            model=model or "tts-1",
            voice=voice or "alloy",
            speed=speed,
            response_format=response_format,
        )

    # ── Cloud paid: Google TTS ────────────────────────────────────────────────
    if p == "google":
        return GoogleTTSProvider(
            api_key=api_key,
            voice_name=voice or "en-US-Neural2-F",
        )

    # ── Cloud paid: Amazon Polly ──────────────────────────────────────────────
    if p in ("amazon", "polly"):
        return AmazonPollyProvider(
            access_key=aws_access_key or api_key,
            secret_key=aws_secret_key,
            region=aws_region,
            voice_id=voice or "Joanna",
        )

    # ── Cloud paid: Cartesia ─────────────────────────────────────────────────
    if p == "cartesia":
        return CartesiaTTSProvider(
            api_key=api_key,
            voice_id=voice_id or voice or "",
            model=model or "sonic-2",
        )

    # ── Cloud paid: PlayHT ────────────────────────────────────────────────────
    if p == "playht":
        return PlayHTTTSProvider(
            api_key=api_key,
            user_id=playht_user_id,
            voice=voice or "",
            model=model or "Play3.0-mini",
        )

    # ── Cloud paid: Deepgram ─────────────────────────────────────────────────
    if p == "deepgram":
        return DeepgramTTSProvider(
            api_key=api_key,
            model=model or "aura-asteria-en",
        )

    # ── Final fallback: OpenAI-compatible ─────────────────────────────────────
    logger.warning("Unknown TTS provider '{}' — falling back to OpenAI-compatible.", provider)
    return OpenAICompatibleTTSProvider(
        api_key=api_key,
        api_base=api_base,
        model=model or "tts-1",
        voice=voice or "alloy",
        speed=speed,
        response_format=response_format,
    )
