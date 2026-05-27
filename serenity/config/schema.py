"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

from serenity.cron.types import CronSchedule


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

class ChannelsConfig(Base):
    """Configuration for chat channels.

    Built-in and plugin channel configs are stored as extra fields (dicts).
    Each channel parses its own config in __init__.
    Per-channel "streaming": true enables streaming output (requires send_delta impl).
    """

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("…"))
    send_max_retries: int = Field(default=3, ge=0, le=10)  # Max delivery attempts (initial send included)
    transcription_provider: str = "groq"  # Voice transcription backend: "groq" or "openai"


class DreamConfig(Base):
    """Dream memory consolidation configuration."""

    _HOUR_MS = 3_600_000

    interval_h: int = Field(default=2, ge=1)  # Every 2 hours by default
    cron: str | None = Field(default=None, exclude=True)  # Legacy compatibility override
    model_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("modelOverride", "model", "model_override"),
    )  # Optional Dream-specific model override
    max_batch_size: int = Field(default=20, ge=1)  # Max history entries per run
    # Bumped from 10 to 15 in #3212 (exp002: +30% dedup, no accuracy loss; >15 plateaus).
    max_iterations: int = Field(default=15, ge=1)  # Max tool calls per Phase 2
    # Per-line git-blame age annotation in Phase 1 prompt (see #3212). Default
    # on — set to False to feed MEMORY.md raw if a specific LLM reacts poorly
    # to the `← Nd` suffix or you want deterministic, git-independent prompts.
    annotate_line_ages: bool = True

    def build_schedule(self, timezone: str) -> CronSchedule:
        """Build the runtime schedule, preferring the legacy cron override if present."""
        if self.cron:
            return CronSchedule(kind="cron", expr=self.cron, tz=timezone)
        return CronSchedule(kind="every", every_ms=self.interval_h * self._HOUR_MS)

    def describe_schedule(self) -> str:
        """Return a human-readable summary for logs and startup output."""
        if self.cron:
            return f"cron {self.cron} (legacy)"
        hours = self.interval_h
        return f"every {hours}h"


class UserConfig(Base):
    """Basic info about the person running Serenity.

    Collected during wizard setup. Used to personalise prompts and triggers —
    e.g. the reach-out trigger addresses the user by name if set.
    All fields are optional — Serenity works fine without them.
    """
    name: str = ""          # First name or preferred name
    timezone: str = ""      # e.g. "Europe/London" — future use for scheduling


class PersonalityConfig(Base):
    """Agent personality — set during wizard, biases emotion state + style."""

    # Trait words chosen in wizard — bias which emotions start higher
    # e.g. ["curious", "funny", "direct"]
    traits: list[str] = Field(default_factory=list)

    # Style vector — controls how the agent sounds (0.0–1.0)
    formality: float = Field(default=0.4, ge=0.0, le=1.0)
    humor: float = Field(default=0.3, ge=0.0, le=1.0)
    verbosity: str = "medium"   # low | medium | high
    directness: float = Field(default=0.7, ge=0.0, le=1.0)

    # Tone modifier — linguistic flavour layer
    # neutral | casual | formal | aave | uk-slang
    tone_modifier: str = "neutral"


class AgentDefaults(Base):
    """Default agent configuration."""

    workspace: str = "~/.serenity/workspace"
    model: str = "qwen3:8b"
    provider: str = (
        "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
    )
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    context_block_limit: int | None = None
    temperature: float = 0.1
    max_tool_iterations: int = 200
    max_tool_result_chars: int = 16_000
    provider_retry_mode: Literal["standard", "persistent"] = "standard"
    force_tool_use: bool = False  # Set True to require tool_choice="required" + finish tool for all responses
    reasoning_effort: str | None = "adaptive"  # low / medium / high / adaptive / none — enables thinking mode. "adaptive" is on for models that support it, ignored by those that don't.
    timezone: str = "UTC"  # IANA timezone, e.g. "Asia/Shanghai", "America/New_York"
    unified_session: bool = False  # Share one session across all channels (single-user multi-device)
    disabled_skills: list[str] = Field(default_factory=list)  # Skill names to exclude from loading (e.g. ["summarize", "skill-creator"])
    session_ttl_minutes: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("idleCompactAfterMinutes", "sessionTtlMinutes"),
        serialization_alias="idleCompactAfterMinutes",
    )  # Auto-compact idle threshold in minutes (0 = disabled)
    session_ttl_overrides: dict[str, int] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("sessionTtlOverrides", "session_ttl_overrides"),
        serialization_alias="sessionTtlOverrides",
    )  # Per-channel TTL overrides, keyed by session-key prefix.
    # e.g. {"voice": 30, "telegram": 480, "heartbeat": 60}
    # A value of -1 means "never compact" (keeps indefinitely).
    dream: DreamConfig = Field(default_factory=DreamConfig)
    personality: PersonalityConfig = Field(default_factory=PersonalityConfig)


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str | None = None
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)


class ProvidersConfig(Base):
    """Configuration for LLM providers."""

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)  # Azure OpenAI (model = deployment name)
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama local models
    lm_studio: ProviderConfig = Field(default_factory=ProviderConfig)  # LM Studio local models
    ovms: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenVINO Model Server (OVMS)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax_anthropic: ProviderConfig = Field(default_factory=ProviderConfig)  # MiniMax Anthropic endpoint (thinking)
    mistral: ProviderConfig = Field(default_factory=ProviderConfig)
    stepfun: ProviderConfig = Field(default_factory=ProviderConfig)  # Step Fun (阶跃星辰)
    xiaomi_mimo: ProviderConfig = Field(default_factory=ProviderConfig)  # Xiaomi MIMO (小米)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow (硅基流动)
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine (火山引擎)
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine Coding Plan
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus (VolcEngine international)
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus Coding Plan
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # OpenAI Codex (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # Github Copilot (OAuth)
    qianfan: ProviderConfig = Field(default_factory=ProviderConfig)  # Qianfan (百度千帆)


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes
    keep_recent_messages: int = 8


class ApiConfig(Base):
    """OpenAI-compatible API server configuration."""

    host: str = "127.0.0.1"  # Safer default: local-only bind.
    port: int = 8900
    timeout: float = 120.0  # Per-request timeout in seconds.


class VoiceConfig(Base):
    """Local voice stack — Faster Whisper STT + multi-provider TTS.

    TTS provider keys (set tts_provider to switch):
      LOCAL (no key, offline):
        "qwen3-local-0.6b"  Qwen3-TTS 0.6B — fast, CPU-friendly
        "qwen3-local-1.7b"  Qwen3-TTS 1.7B — best quality, voice clone ✦
        "qwen3-local"       alias for qwen3-local-0.6b
        "kokoro"            Kokoro-82M — lightweight, fast
        "coqui"             Coqui XTTS-v2 — multilingual, voice clone ✦
        "piper"             Piper TTS — ultra-fast subprocess
        "bark"              Bark — expressive ([laughs] etc.)
      CLOUD FREE:
        "edge-tts"          Microsoft Edge TTS — 200+ voices, no key
      CLOUD PAID:
        "elevenlabs"        ElevenLabs — ultra-realistic, voice clone ✦
        "openai"            OpenAI TTS (tts-1 / tts-1-hd)
        "google"            Google Cloud TTS (Neural2 / Chirp3)
        "amazon"            Amazon Polly (neural voices)
        "cartesia"          Cartesia — real-time, voice clone ✦
        "playht"            PlayHT — high quality, voice clone ✦
        "deepgram"          Deepgram Aura TTS
        "qwen3"             Qwen3 via DashScope cloud (free tier)
      DISABLED:
        "disabled"          No TTS

    ✦ Voice clone: drop any audio file (5–30 s WAV/MP3/FLAC) into
      ~/.serenity/voice_clone/  and these engines will clone it automatically.
    """

    # ── STT — Faster Whisper ──────────────────────────────────────────────────
    whisper_model: str = "small"          # tiny / small / medium / large-v3
    whisper_device: str = "cpu"           # "cpu" or "cuda"
    whisper_compute_type: str = "int8"    # "int8" (cpu) or "float16" (cuda)

    # ── TTS — Provider selection ───────────────────────────────────────────────
    tts_enabled: bool = False
    tts_provider: str = "qwen3-local-0.6b"  # see docstring above

    # ── Shared API key / output ────────────────────────────────────────────────
    tts_api_key: str = ""               # cloud provider API key (reads env as fallback)
    tts_model: str = ""                 # override model; blank = provider default
    tts_voice: str = ""                 # override voice; blank = provider default
    tts_api_base: str = ""              # OpenAI-compatible base URL
    tts_speed: float = 1.0
    tts_format: str = "opus"            # output format for OpenAI provider
    tts_instruct: str = ""              # natural-language prosody hint (Qwen3)
    tts_region: str = "international"   # DashScope region

    # ── ElevenLabs specifics ──────────────────────────────────────────────────
    tts_elevenlabs_api_key: str = ""    # also reads ELEVENLABS_API_KEY env
    tts_elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"  # Rachel
    tts_elevenlabs_model: str = "eleven_flash_v2_5"
    tts_elevenlabs_stability: float = 0.5
    tts_elevenlabs_similarity: float = 0.75
    tts_elevenlabs_style: float = 0.0

    # ── Qwen3 Local specifics ─────────────────────────────────────────────────
    tts_local_model: str = ""           # blank = auto based on clone file presence
    tts_local_voice: str = "Cherry"     # Cherry/Vivian/Ryan/Sohee/Alloy/Echo/Fable/Onyx/Nova
    tts_local_device: str = ""          # blank = auto (CUDA → CPU)

    # ── Piper specifics ───────────────────────────────────────────────────────
    tts_piper_model: str = ""           # path to .onnx model file


class AudioSensesConfig(Base):
    """Audio sensing configuration — Faster Whisper STT + wake word listener."""

    enabled: bool = False
    whisper_model: str = "small"         # tiny / small / medium / large-v3
    whisper_device: str = "cpu"          # cpu or cuda
    whisper_compute_type: str = "int8"   # int8 (cpu) or float16 (cuda)

    # Wake word — always-on listener activates when this word/name is heard.
    # Fuzzy-matched so "serenity", "Serenity", "serinity" all trigger.
    wake_word: str = "Serenity"

    # wake_whisper_model is no longer used — a single whisper_model handles both
    # scanning and full transcription.  Field kept for backwards-compat with old
    # config files (pydantic ignores unknown fields by default).
    # wake_whisper_model: str = "tiny"  ← removed

    # ── Capture timing ────────────────────────────────────────────────────────
    # Seconds of silence before the utterance is considered complete.
    # Lower = snappier response but may cut off slow speakers.
    silence_cutoff_s: float = 1.2

    # Max seconds to record after wake word before force-sending (prevents runaway).
    max_capture_s: float = 30.0

    # ── Voice activity detection ──────────────────────────────────────────────
    # RMS energy threshold — below this is treated as silence (0.0–1.0).
    # Raise if background noise keeps triggering; lower if mic is quiet.
    vad_energy_threshold: float = 0.01

    # ── Hallucination filter ──────────────────────────────────────────────────
    # Whisper hallucinates short phrases on silence ("Thank you.", "You.", "Hmm.").
    # Transcriptions with fewer words OR characters than these thresholds are dropped.
    min_transcript_words: int = 2    # single-word results are almost always hallucinations
    min_transcript_chars: int = 4    # very short strings (punctuation, "I", etc.) are dropped


class VisionSensesConfig(Base):
    """Vision sensing configuration — opencv-python (frame grab) + MiniCPM-V 4.6 via Ollama."""

    enabled: bool = False
    camera_enabled: bool = False         # allow camera capture in addition to screen
    camera_index: int = 0                # OpenCV camera device index


class SensesConfig(Base):
    """Serenity senses — audio (Ears) and vision (Eyes). Off by default."""

    audio: AudioSensesConfig = Field(default_factory=AudioSensesConfig)
    vision: VisionSensesConfig = Field(default_factory=VisionSensesConfig)


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "127.0.0.1"  # Safer default: local-only bind.
    port: int = 18790
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)


class WebSearchConfig(Base):
    """Web search tool configuration."""

    provider: str = "duckduckgo"  # brave, tavily, duckduckgo, searxng, jina, kagi
    api_key: str = ""
    base_url: str = ""  # SearXNG base URL
    max_results: int = 5
    timeout: int = 30  # Wall-clock timeout (seconds) for search operations


class WebToolsConfig(Base):
    """Web tools configuration."""

    enable: bool = True
    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(Base):
    """Shell exec tool configuration."""

    enable: bool = True
    timeout: int = 60
    path_append: str = ""
    sandbox: str = ""  # sandbox backend: "" (none) or "bwrap"
    allowed_env_keys: list[str] = Field(default_factory=list)  # Env var names to pass through to subprocess (e.g. ["GOPATH", "JAVA_HOME"])

class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"])  # Only register these tools; accepts raw MCP names or wrapped mcp_<server>_<tool> names; ["*"] = all tools; [] = no tools

class MyToolConfig(Base):
    """Self-inspection tool configuration."""

    enable: bool = True  # register the `my` tool (agent runtime state inspection)
    allow_set: bool = False  # let `my` modify loop state (read-only if False)


class ToolsConfig(Base):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    my: MyToolConfig = Field(default_factory=MyToolConfig)
    restrict_to_workspace: bool = False  # restrict all tool access to workspace directory
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    ssrf_whitelist: list[str] = Field(default_factory=list)  # CIDR ranges to exempt from SSRF blocking (e.g. ["100.64.0.0/10"] for Tailscale)


class Config(BaseSettings):
    """Root configuration for serenity."""

    user: UserConfig = Field(default_factory=UserConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    senses: SensesConfig = Field(default_factory=SensesConfig)

    # Licence — set during wizard, validated on every gateway start.
    licence_key: str = ""             # Lemon Squeezy licence key (UUID format)
    licence_tier: str = ""            # e.g. "personal", "solo", "small_business", "growth", "enterprise"
    licence_instance_id: str = ""     # Lemon Squeezy instance ID returned on first activation
    licence_last_validated: str = ""  # ISO-8601 UTC timestamp of last successful server check
    trial_start: str = ""             # ISO-8601 UTC — session initialisation anchor (internal)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def _match_provider(
        self, model: str | None = None
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name)."""
        from serenity.providers.registry import PROVIDERS, find_by_name

        forced = self.agents.defaults.provider
        if forced != "auto":
            spec = find_by_name(forced)
            if spec:
                p = getattr(self.providers, spec.name, None)
                return (p, spec.name) if p else (None, None)
            return None, None

        model_lower = (model or self.agents.defaults.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # Explicit provider prefix wins — prevents `github-copilot/...codex` matching openai_codex.
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Fallback: configured local providers can route models without
        # provider-specific keywords (for example plain "llama3.2" on Ollama).
        # Prefer providers whose detect_by_base_keyword matches the configured api_base
        # (e.g. Ollama's "11434" in "http://localhost:11434") over plain registry order.
        local_fallback: tuple[ProviderConfig, str] | None = None
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = getattr(self.providers, spec.name, None)
            if not (p and p.api_base):
                continue
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                return p, spec.name
            if local_fallback is None:
                local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

        # Fallback: gateways first, then others (follows registry order)
        # OAuth providers are NOT valid fallbacks — they require explicit model selection
        for spec in PROVIDERS:
            if spec.is_oauth:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p, spec.name
        return None, None

    def get_provider(self, model: str | None = None) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        p, _ = self._match_provider(model)
        return p

    def get_provider_name(self, model: str | None = None) -> str | None:
        """Get the registry name of the matched provider (e.g. "deepseek", "openrouter")."""
        _, name = self._match_provider(model)
        return name

    def get_api_key(self, model: str | None = None) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model)
        return p.api_key if p else None

    def get_api_base(self, model: str | None = None) -> str | None:
        """Get API base URL for the given model. Applies default URLs for gateway/local providers."""
        from serenity.providers.registry import find_by_name

        p, name = self._match_provider(model)
        if p and p.api_base:
            return p.api_base
        # Only gateways get a default api_base here. Standard providers
        # resolve their base URL from the registry in the provider constructor.
        if name:
            spec = find_by_name(name)
            if spec and (spec.is_gateway or spec.is_local) and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config = ConfigDict(env_prefix="SERENITY_", env_nested_delimiter="__", extra="ignore")
