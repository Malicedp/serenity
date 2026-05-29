#!/usr/bin/env python3
"""Serenity — setup wizard.

Aesthetic matches the OpenClaws / Serenity terminal style:
  o  Section title -------+   full-width dashed section headers
  |  Note box content     |   ASCII +--+ bordered note boxes
  +------------------...--+
  Light-blue accent throughout.
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

# ── UTF-8 safety on Windows ─────────────────────────────────────────────────
if sys.platform == "win32":
    if (sys.stdout.encoding or "").lower() != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from rich.console import Console
from rich.prompt import Confirm, Prompt

console = Console(highlight=False)

CLEAR_CMD = "cls" if sys.platform == "win32" else "clear"

# ── Palette ──────────────────────────────────────────────────────────────────
A  = "#5BC8F5"   # light blue — primary accent
AB = "#89D9F8"   # lighter blue — option numbers / bright
OK = "#2FBF71"   # green  — success ticks
WN = "#FFB020"   # amber  — warnings
MU = "#5A7A8A"   # blue-grey — muted borders / hints
WH = "bold white"

# ── Terminal width ────────────────────────────────────────────────────────────
def _cols() -> int:
    try:
        return min(os.get_terminal_size().columns, 100)
    except Exception:
        return 80


def clear() -> None:
    os.system(CLEAR_CMD)


# ── VERSION ───────────────────────────────────────────────────────────────────

def _version() -> str:
    try:
        from serenity import __version__
        return __version__
    except Exception:
        pass
    try:
        from importlib.metadata import version
        return version("serenity")
    except Exception:
        return "unknown"


# ── ASCII LOGO ────────────────────────────────────────────────────────────────

LOGO = r"""
  ███████╗███████╗██████╗ ███████╗███╗   ██╗██╗████████╗██╗   ██╗
  ██╔════╝██╔════╝██╔══██╗██╔════╝████╗  ██║██║╚══██╔══╝╚██╗ ██╔╝
  ███████╗█████╗  ██████╔╝█████╗  ██╔██╗ ██║██║   ██║    ╚████╔╝
  ╚════██║██╔══╝  ██╔══██╗██╔══╝  ██║╚██╗██║██║   ██║     ╚██╔╝
  ███████║███████╗██║  ██║███████╗██║ ╚████║██║   ██║      ██║
  ╚══════╝╚══════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝╚═╝   ╚═╝      ╚═╝
"""


def show_logo() -> None:
    clear()
    ver = _version()
    cols = _cols()

    # Version header line
    tagline = f"Serenity {ver} — by Sera-Team"
    console.print(f"[{A}]{tagline}[/{A}]")
    console.print()

    # Windows detection
    if sys.platform == "win32":
        console.print(f"[{A}]Windows detected — use Windows Terminal for best experience.[/{A}]")
        console.print(f"[{A}]Quick setup: pip install -e . then run: serenity[/{A}]")
        console.print()

    # Logo
    console.print(f"[{WH}]{LOGO}[/{WH}]")
    console.print()


# ── UI PRIMITIVES — matching the screenshot layout ────────────────────────────

def intro(title: str) -> None:
    """  | Serenity setup"""
    console.print(f"[{A}]|[/{A}]  [{WH}]{title}[/{WH}]")
    console.print()


def _section_header(title: str) -> None:
    """  o Title ----------------...---+"""
    cols = _cols()
    # "o Title " then dashes to fill, then "+"
    prefix = f"o {title} "
    dash_count = max(4, cols - len(prefix) - 1)
    dashes = "-" * dash_count
    console.print(f"\n[{A}]{prefix}{dashes}+[/{A}]")


def _box_top(cols: int) -> None:
    console.print(f"[{MU}]|[/{MU}]")


def _box_line(text: str, cols: int) -> None:
    inner = cols - 4          # 2 for "| " prefix, 2 for " |" suffix
    # pad right
    padded = f"  {text}"
    console.print(f"[{MU}]|[/{MU}]{padded}")


def _box_line_plain(cols: int) -> None:
    console.print(f"[{MU}]|[/{MU}]")


def _box_bottom(cols: int) -> None:
    dashes = "-" * (cols - 2)
    console.print(f"[{MU}]+{dashes}+[/{MU}]")
    console.print()


def note(message: str, title: str = "") -> None:
    """
    o Title --------...---+
    |                     |
    |  line 1             |
    |  line 2             |
    |                     |
    +---------------------+
    """
    cols = _cols()
    _section_header(title or "Note")
    _box_top(cols)
    lines = message.strip().split("\n")
    for line in lines:
        if line.strip():
            _box_line(f"[white]{line}[/white]", cols)
        else:
            _box_line_plain(cols)
    _box_top(cols)
    _box_bottom(cols)


def step_label(title: str) -> None:
    """  o Title --------...---+  (section header only, no box)"""
    _section_header(title)
    console.print(f"[{MU}]|[/{MU}]")


def ok_line(message: str) -> None:
    console.print(f"[{OK}]o[/{OK}]  [white]{message}[/white]")


def info_line(message: str) -> None:
    console.print(f"[{MU}]|[/{MU}]  [{MU}]{message}[/{MU}]")


def divider() -> None:
    console.print(f"[{MU}]|[/{MU}]")


# ── OPTION MENUS ─────────────────────────────────────────────────────────────

def show_options(options: list[tuple[str, str, str]]) -> None:
    """
    |  1  Label          hint text
    |  2  Label          hint text
    """
    for num, label, hint in options:
        console.print(
            f"[{MU}]|[/{MU}]  [{AB}]{num}[/{AB}]  "
            f"[white]{label:<16}[/white]  [{MU}]{hint}[/{MU}]"
        )
    console.print(f"[{MU}]|[/{MU}]")


# ── PROMPT HELPERS ────────────────────────────────────────────────────────────

def ask(prompt: str, default: str | None = None) -> str:
    val = Prompt.ask(f"[{MU}]|[/{MU}]  [{A}]{prompt}[/{A}]", default=default)
    return val or (default or "")


def ask_num(prompt: str, choices: list[str], default: str) -> str:
    return Prompt.ask(
        f"[{MU}]|[/{MU}]  [{A}]{prompt}[/{A}]",
        choices=choices,
        default=default,
        show_choices=False,
    )


def ask_secret(prompt: str) -> str:
    return Prompt.ask(f"[{MU}]|[/{MU}]  [{A}]{prompt}[/{A}]", password=True)


def ask_confirm(prompt: str, default: bool = True) -> bool:
    yn = "Yes / > No" if not default else "> Yes / No"
    return Confirm.ask(
        f"[{MU}]|[/{MU}]  [{A}]{prompt}[/{A}]  [{MU}]({yn})[/{MU}]",
        default=default,
    )


def outro(message: str) -> None:
    cols = _cols()
    tail = "-" * max(4, cols - len(message) - 4)
    console.print(f"\n[{OK}]o[/{OK}]  [{WH}]{message}[/{WH}]  [{MU}]{tail}[/{MU}]\n")


# ── LOCAL TTS SCANNER ────────────────────────────────────────────────────────

def _pkg(name: str) -> bool:
    """Return True if a Python package is importable."""
    return importlib.util.find_spec(name) is not None


def _hf_cached(model_id: str) -> bool:
    """Return True if a HuggingFace model is in the local cache.

    Checks both the default HF cache and the common Windows/macOS/Linux locations.
    model_id format:  'Qwen/Qwen3-TTS-0.6B'  →  'models--Qwen--Qwen3-TTS-0.6B'
    """
    folder = "models--" + model_id.replace("/", "--")
    candidates = [
        Path.home() / ".cache" / "huggingface" / "hub" / folder,
        Path(os.environ.get("HF_HOME", "")) / "hub" / folder if os.environ.get("HF_HOME") else None,
        Path(os.environ.get("HUGGINGFACE_HUB_CACHE", "")) / folder if os.environ.get("HUGGINGFACE_HUB_CACHE") else None,
    ]
    return any(p and p.exists() for p in candidates if p)


def _scan_local_tts() -> dict[str, bool]:
    """Scan for locally available TTS engines and cached models.

    Returns a dict of  engine_key -> True/False  indicating what's ready.
    A result of True means it can run immediately without any downloads.
    """
    results: dict[str, bool] = {}

    # ── Qwen3-TTS (via transformers) ──────────────────────────────────────────
    has_transformers = _pkg("transformers")
    results["qwen3-tts-0.6b"] = has_transformers and _hf_cached("Qwen/Qwen3-TTS-0.6B")
    results["qwen3-tts-1.7b"] = has_transformers and _hf_cached("Qwen/Qwen3-TTS-1.7B")

    # ── Kokoro ────────────────────────────────────────────────────────────────
    results["kokoro"] = _pkg("kokoro") or _pkg("kokoro_onnx")

    # ── Coqui TTS / XTTS ─────────────────────────────────────────────────────
    results["coqui"] = _pkg("TTS")

    # ── Piper TTS ─────────────────────────────────────────────────────────────
    results["piper"] = _pkg("piper") or _pkg("piper_tts")

    # ── Bark ──────────────────────────────────────────────────────────────────
    results["bark"] = _pkg("bark")

    # ── edge-tts (cloud but free, no key — flag as "ready" if installed) ──────
    results["edge-tts"] = _pkg("edge_tts")

    return results


def _ready(detected: dict[str, bool], key: str) -> str:
    """Return a short tag shown next to the option label."""
    return "  [green]✓ ready[/green]" if detected.get(key) else ""


# ── WIZARD ────────────────────────────────────────────────────────────────────

def run_wizard() -> None:
    show_logo()

    config_path    = Path.home() / ".serenity" / "config.json"
    workspace_path = Path.home() / ".serenity" / "workspace"

    intro("Serenity setup")

    # ── Licence & Terms acceptance ────────────────────────────────────────────
    note(
        "Serenity is dual-licensed. Please read before continuing.\n"
        "\n"
        "Personal use — FREE forever\n"
        "  Run, modify, and share Serenity for personal or educational use.\n"
        "  Licensed under CC BY-NC 4.0. Attribution required.\n"
        "  No key needed. Just download and run.\n"
        "\n"
        "Commercial use — $80 / month\n"
        "  Any business, commercial deployment, or integration.\n"
        "  Includes a licence key for activation.\n"
        "  [cyan]https://whop.com/serenity[/cyan]\n"
        "\n"
        "By continuing you agree to:\n"
        "  LICENCE.md            — Licence terms (CC BY-NC 4.0 / commercial)\n"
        "  COMMERCIAL_LICENCE.md — Commercial licence terms\n"
        "  TERMS.md              — Acceptable use policy\n"
        "  DISCLAIMER.md         — Liability disclaimer (provided as-is)\n"
        "  PRIVACY.md            — Data & privacy policy (UK/EU GDPR)\n"
        "  CONTRIBUTING.md       — Contributor licence agreement\n"
        "\n"
        "These documents are included in the Serenity repository root.\n"
        "For commercial enquiries: serenitydev32@gmail.com",
        "Licence & Terms",
    )

    if not ask_confirm(
        "I have read and agree to the Serenity Licence, Terms, and Privacy Policy",
        default=False,
    ):
        console.print(f"\n[{MU}]You must accept the terms to use Serenity. Setup cancelled.[/{MU}]\n")
        sys.exit(0)

    # ── Use-case question ─────────────────────────────────────────────────────
    step_label("How will you use Serenity?")
    show_options([
        ("1", "Personal",    "free forever — personal use, research, education"),
        ("2", "Commercial",  "$80/month  — any business or commercial deployment"),
    ])
    use_case = ask_num("Select", choices=["1", "2"], default="1")

    if use_case == "1":
        note(
            "Personal use is free forever — no key, no expiry, no catch.\n"
            "\n"
            "What you can do:\n"
            "  - Use Serenity for yourself, your research, your education\n"
            "  - Modify the code and share it (non-commercially, with attribution)\n"
            "  - Build personal tools and experiments on top of it\n"
            "\n"
            "What you can't do:\n"
            "  - Sell Serenity or charge others to use it\n"
            "  - Deploy it as a product or service for a business\n"
            "  - Remove attribution or claim it as your own\n"
            "\n"
            "If you ever go commercial, grab a licence at:\n"
            "  [cyan]https://whop.com/serenity[/cyan]",
            "Personal Use",
        )
        console.print(f"[{OK}]o[/{OK}]  [white]Got it — personal use. You're good to go.[/white]\n")

    else:
        note(
            "Commercial use requires a licence key.\n"
            "\n"
            "Get yours at:\n"
            "  [cyan]https://whop.com/serenity[/cyan]\n"
            "\n"
            "Once you've purchased, you'll receive a key by email.\n"
            "Enter it in the next step when prompted.\n"
            "\n"
            "If you haven't bought yet, open the link above now —\n"
            "then come back and continue setup.",
            "Commercial Use — Licence Required",
        )
        if not ask_confirm("I have a commercial licence key and am ready to enter it", default=False):
            console.print(
                f"\n[{A}]No problem — grab your key at https://whop.com/serenity[/{A}]\n"
                f"[{MU}]Run `serenity` again once you have it.[/{MU}]\n"
            )
            sys.exit(0)

    # Security note — same pattern as OpenClaws
    note(
        "Security warning — please read.\n"
        "\n"
        "Serenity is an open-source local AI agent by Sera-Team.\n"
        "By default, Serenity is a personal agent: one trusted operator boundary.\n"
        "This bot can read files and run actions if tools are enabled.\n"
        "A bad prompt can trick it into doing unsafe things.\n"
        "\n"
        "Serenity is not a hostile multi-tenant boundary by default.\n"
        "If multiple users can message one tool-enabled agent, they share\n"
        "that delegated tool authority.\n"
        "\n"
        "Recommended baseline:\n"
        "- Keep secrets out of the agent's reachable filesystem.\n"
        "- Use the strongest available model for any tool-enabled bot.\n"
        "- Sandbox + least-privilege tools.\n"
        "\n"
        "Run regularly:\n"
        "sera security audit --deep\n"
        "sera security audit --fix",
        "Security",
    )

    if not ask_confirm(
        "I understand this is personal-by-default and shared/multi-user use "
        "requires lock-down. Continue?",
        default=False,
    ):
        console.print(f"\n[{MU}]Setup cancelled.[/{MU}]\n")
        sys.exit(0)

    # CLI commands reference
    note(
        "serenity              First-run wizard / logo + gateway\n"
        "sera agent            Interactive chat with Sera\n"
        "sera agent -m '...'   One-shot message\n"
        "sera gateway          Start gateway (channels, cron, heartbeat, NNN)\n"
        "sera serve            OpenAI-compatible API server\n"
        "sera status           Show config, workspace, API key status\n"
        "sera visualise        Explore NNN vector space in browser\n"
        "sera reset            Delete config and re-run the setup wizard\n"
        "sera --help           Full command list",
        "CLI Commands",
    )

    if config_path.exists():
        note(
            f"Config already exists at {config_path}\n"
            "Choose whether to reconfigure or keep existing settings.",
            "Existing config",
        )
        if not ask_confirm("Run setup again?", default=False):
            launch_serenity()
            return

    # ══════════════════════════════════════════════════════════
    # 1 — LLM Provider
    # ══════════════════════════════════════════════════════════

    step_label("LLM Provider")

    providers = {
        "1": ("ollama",     "Ollama",      "Local — no API key, runs on your machine"),
        "2": ("openrouter", "OpenRouter",  "Hundreds of models via one key"),
        "3": ("anthropic",  "Anthropic",   "Claude models from Anthropic"),
        "4": ("openai",     "OpenAI",      "GPT-4o and other OpenAI models"),
        "5": ("deepseek",   "DeepSeek",    "Fast, affordable reasoning models"),
    }

    show_options([(n, name, hint) for n, (_, name, hint) in providers.items()])
    choice = ask_num("Provider [1-5]", choices=list(providers.keys()), default="2")
    provider_key, provider_name, _ = providers[choice]
    ok_line(f"Provider  →  {provider_name}")

    # ══════════════════════════════════════════════════════════
    # 2 — Model
    # ══════════════════════════════════════════════════════════

    step_label("Model")

    model_defaults = {
        "ollama":     "ollama/qwen2.5:7b",
        "openrouter": "openrouter/anthropic/claude-sonnet-4-5",
        "anthropic":  "anthropic/claude-sonnet-4-5",
        "openai":     "openai/gpt-4o",
        "deepseek":   "deepseek/deepseek-chat",
    }
    default_model = model_defaults[provider_key]
    info_line(f"Recommended:  {default_model}")
    divider()

    if ask_confirm("Use recommended model?", default=True):
        model = default_model
    else:
        model = ask("Model name", default=default_model)

    ok_line(f"Model  →  {model}")

    # ══════════════════════════════════════════════════════════
    # 3 — API Key  (or Ollama instructions)
    # ══════════════════════════════════════════════════════════

    api_key = ""
    if provider_key == "ollama":
        note(
            "No API key needed — Ollama runs locally.\n"
            "\n"
            "Make sure Ollama is running:     ollama serve\n"
            "Pull at least one model:         ollama pull qwen2.5:7b",
            "Ollama setup",
        )
        ok_line("Ollama — no key required")
    else:
        step_label("API Key")
        key_urls = {
            "openrouter": "https://openrouter.ai/keys",
            "anthropic":  "https://console.anthropic.com/settings/keys",
            "openai":     "https://platform.openai.com/api-keys",
            "deepseek":   "https://platform.deepseek.com/api_keys",
        }
        info_line(f"Get your key at:  {key_urls.get(provider_key, 'your provider dashboard')}")
        divider()
        api_key = ask_secret("Paste API key")
        ok_line("API key saved")

    # ══════════════════════════════════════════════════════════
    # 4 — Persona
    # ══════════════════════════════════════════════════════════

    step_label("Agent Persona")

    info_line("Name your agent and describe her personality.")
    info_line("Press Enter to keep existing values.")
    divider()

    # Read back existing values so re-running the wizard pre-fills what was set before.
    _default_agent_name = "Sera"
    _default_user_name  = "User"
    _default_persona    = "Curious, direct and proactive. Grows smarter from every conversation."

    # Try SOUL.md first line for agent name
    try:
        _soul_path = workspace_path / "Agent" / "SOUL.md"
        if _soul_path.exists():
            _first = _soul_path.read_text(encoding="utf-8").splitlines()[0]
            if _first.startswith("# "):
                _default_agent_name = _first[2:].strip() or _default_agent_name
    except Exception:
        pass

    # Try USER.md for user name and persona
    try:
        _user_path = workspace_path / "Agent" / "USER.md"
        if _user_path.exists():
            _user_text = _user_path.read_text(encoding="utf-8")
            for _line in _user_text.splitlines():
                if _line.startswith("- **Name**:"):
                    _val = _line.split(":", 1)[1].strip()
                    if _val and _val not in ("{user_name}", "(your name)"):
                        _default_user_name = _val
                if _line.startswith("- ") and "## Communication Style" in _user_text:
                    # Find the line right after Communication Style
                    _lines = _user_text.splitlines()
                    for _i, _l in enumerate(_lines):
                        if "## Communication Style" in _l:
                            for _j in range(_i + 1, min(_i + 5, len(_lines))):
                                _cl = _lines[_j].strip()
                                if _cl.startswith("- ") and len(_cl) > 3:
                                    _default_persona = _cl[2:].strip()
                                    break
                            break
    except Exception:
        pass

    agent_name = ask("Agent name", default=_default_agent_name) or _default_agent_name
    user_name  = ask("Your name", default=_default_user_name) or _default_user_name
    agent_persona = (
        ask("Personality (one line)", default=_default_persona)
        or _default_persona
    )
    ok_line(f"Persona  →  {agent_name}  (user: {user_name})")

    # ══════════════════════════════════════════════════════════
    # 5 — Messaging Channel
    # ══════════════════════════════════════════════════════════

    step_label("Messaging Channel")

    channels = {"1": "telegram", "2": "discord", "3": "whatsapp", "4": "skip"}
    show_options([
        ("1", "Telegram",  "Create a bot via @BotFather"),
        ("2", "Discord",   "Create a bot in Discord Developer Portal"),
        ("3", "WhatsApp",  "QR-code login after setup"),
        ("4", "Skip",      "CLI only — add channels later"),
    ])

    channel_choice = ask_num("Channel [1-4]", choices=list(channels.keys()), default="4")
    channel = channels[channel_choice]
    channel_config: dict = {}
    telegram_token = ""
    telegram_allow: list = ["*"]

    if channel == "telegram":
        note("Open Telegram → @BotFather → /newbot → copy the token.", "Telegram")
        telegram_token = ask_secret("Bot token")
        info_line("allowFrom controls who can message Sera.")
        info_line('Enter your Telegram user ID(s), or * to allow everyone.')
        info_line('Find your ID: message @userinfobot on Telegram.')
        telegram_allow_raw = Prompt.ask(
            f"[{A}]allowFrom[/{A}] [dim](comma-separated IDs, or *)[/dim]",
            default="*",
        )
        telegram_allow = [x.strip() for x in telegram_allow_raw.split(",") if x.strip()]
        ok_line("Telegram configured")
    elif channel == "discord":
        note("Discord Developer Portal → Applications → Bot → Reset Token", "Discord")
        discord_token = ask_secret("Bot token")
        channel_config = {"discord": {"enabled": True, "token": discord_token}}
        ok_line("Discord configured")
    elif channel == "whatsapp":
        note("Authenticate via QR code after setup:\n  sera channels login whatsapp", "WhatsApp")
        channel_config = {"whatsapp": {"enabled": True}}
        ok_line("WhatsApp configured")
    else:
        ok_line("CLI only — no channel configured")

    # ══════════════════════════════════════════════════════════
    # 6 — Memory & Workspace
    # ══════════════════════════════════════════════════════════

    step_label("Memory & Workspace")

    note(
        "NNN (Neuro Node Network) gives your agent long-term vector memory.\n"
        "It learns, clusters and abstracts from every conversation.\n"
        "\n"
        "Consolidation runs automatically every 30 minutes in the gateway.",
        "NNN vector memory",
    )

    nnn_enabled  = ask_confirm("Enable NNN vector memory?", default=True)
    nnn_interval = 1800

    if nnn_enabled:
        ok_line("NNN memory enabled")
        if not ask_confirm("Consolidate every 30 min? (recommended)", default=True):
            raw = ask("Consolidation interval in seconds", default="1800")
            try:
                nnn_interval = int(raw)
            except ValueError:
                nnn_interval = 1800
    else:
        ok_line("NNN memory disabled")

    divider()
    info_line("Vault — where your agent stores notes and memories.")
    info_line("The vault has two built-in subfolders:")
    info_line(f"  Agent/         — {agent_name}'s own files (SOUL, memory index, heartbeat)")
    info_line(f"  {user_name}/   — personal notes about you (preferences, traits, goals)")
    divider()

    # Template folder ships alongside this script — contains the full vault structure
    _VAULT_TEMPLATE = Path(__file__).parent / "Vault Memories"

    # Default: Vault Memories/ next to serenity_setup.py — lives inside the Serenity folder.
    default_ws = Path(__file__).parent / "Vault Memories"
    show_options([
        ("1", "Default",  f"Vault Memories/  (inside Serenity folder — recommended)"),
        ("2", "Obsidian", "Wire to an existing Obsidian vault"),
        ("3", "Custom",   "Choose any folder on your machine"),
    ])

    ws_choice = ask_num("Vault location [1-3]", choices=["1", "2", "3"], default="1")
    obsidian_path    = ""
    custom_workspace = ""

    if ws_choice == "2":
        obsidian_path = ask("Absolute path to your Obsidian vault")
        while obsidian_path and not Path(obsidian_path).exists():
            note(f"Path not found: {obsidian_path}", "Error")
            obsidian_path = ask("Absolute path to your Obsidian vault")
        ok_line(f"Obsidian vault  →  {obsidian_path}")
    elif ws_choice == "3":
        custom_workspace = ask("Absolute path to vault folder (e.g. D:\\My Vault)")
        Path(custom_workspace).mkdir(parents=True, exist_ok=True)
        ok_line(f"Vault  →  {custom_workspace}")
    else:
        ok_line(f"Vault  →  {default_ws}")

    # ══════════════════════════════════════════════════════════
    # 7 — Senses (Ears & Eyes)
    # ══════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════
    # 7a — Vision (Eyes)
    # ══════════════════════════════════════════════════════════

    step_label("Vision — Eyes  (optional)")

    note(
        "Serenity can see through your camera and screen.\n"
        "\n"
        "All vision runs on CPU — zero VRAM, runs alongside your LLM.\n"
        "\n"
        "What you get:\n"
        "  'open your eyes'     — opens the camera\n"
        "  'close your eyes'    — stops camera, frees all RAM\n"
        "  'what do you see'    — describes camera frame via MiniCPM-V 4.6\n"
        "  'look at my screen'  — describes your screen via MiniCPM-V 4.6\n"
        "  'save what you see'  — saves image + description to vault/Images/\n"
        "\n"
        "Run sense/install_senses.bat after setup to install vision packages.",
        "Vision",
    )

    eyes_enabled = ask_confirm("Enable vision?", default=False)
    if eyes_enabled:
        ok_line("Vision  →  enabled  (camera + screen, CPU-only)")
    else:
        ok_line("Vision  →  disabled")

    # ══════════════════════════════════════════════════════════
    # 7b — Speech (Ears & Voice)
    # ══════════════════════════════════════════════════════════

    step_label("Speech — Ears & Voice  (optional)")

    note(
        "Serenity can hear you and speak back.\n"
        "\n"
        "Voice input — two ways to talk to Serenity:\n"
        "  Telegram voice notes  — always works, no setup needed.\n"
        "                          Send a voice note in Telegram, it is transcribed\n"
        "                          and delivered to Serenity automatically.\n"
        "  PC wake word          — always-on microphone listener. Say 'Serenity' and\n"
        "                          speak. Uses Faster Whisper small on CPU (no GPU needed).\n"
        "                          Enable below to activate.\n"
        "\n"
        "TTS (text-to-speech) — Serenity speaks responses back via Telegram voice notes.\n"
        "  edge-tts   — free, offline, 200+ voices, no GPU, instant  (recommended).\n"
        "  Local TTS  — Kokoro / Coqui / Qwen3 — run fully offline on your hardware.\n"
        "  Cloud TTS  — ElevenLabs / OpenAI / DashScope — internet required.\n"
        "\n"
        "Run sense/install_senses.bat after setup to install audio packages.",
        "Speech",
    )

    ears_enabled  = ask_confirm("Enable speech (ears + voice)?", default=False)
    whisper_model = "small"
    tts_engine    = "disabled"
    tts_model     = ""
    tts_label     = "disabled"
    tts_api_key   = ""

    if ears_enabled:
        # ── STT ───────────────────────────────────────────────
        step_label("Speech-to-Text model  (Faster Whisper small used for PC wake word)")
        info_line("Choose the model for full transcription after the wake word is detected.")
        divider()
        show_options([
            ("1", "small",    "~490 MB  CPU/int8  ~1-2s  — fast, good quality  (recommended)"),
            ("2", "medium",   "~1.5 GB  CPU/int8  ~3-5s  — better with accents + noise"),
            ("3", "large-v3", "~3 GB    CPU/int8  ~6-10s — best quality, slow"),
        ])
        wm_choice = ask_num("STT model [1-3]", choices=["1", "2", "3"], default="1")
        whisper_model = {"1": "small", "2": "medium", "3": "large-v3"}[wm_choice]
        ok_line(f"STT  →  Faster Whisper small  (wake word)  +  Whisper {whisper_model}  (transcription)")

        # ── TTS ───────────────────────────────────────────────
        step_label("Text-to-Voice  (TTS)")

        info_line("Scanning for locally installed TTS engines...")
        _detected = _scan_local_tts()
        _any_local = any(
            _detected.get(k) for k in
            ("qwen3-tts-0.6b", "qwen3-tts-1.7b", "kokoro", "coqui", "piper", "bark")
        )
        if _any_local:
            info_line("Local engines detected — marked with ✓ ready")
        else:
            info_line("No local engines detected — all local options will download on first use")
        divider()

        console.print(f"  [{MU}]── Local (offline, CPU, zero VRAM) ──────────────────────[/{MU}]")
        console.print(
            f"[{MU}]|[/{MU}]  [{AB}] 1[/{AB}]  [white]{'Qwen3-TTS-0.6B':<18}[/white]"
            f"  [{MU}]CPU  ~1.2GB  ~2-3s  recommended[/{MU}]"
            + (_ready(_detected, "qwen3-tts-0.6b"))
        )
        console.print(
            f"[{MU}]|[/{MU}]  [{AB}] 2[/{AB}]  [white]{'Qwen3-TTS-1.7B':<18}[/white]"
            f"  [{MU}]CPU  ~3.5GB  ~4-6s  better quality[/{MU}]"
            + (_ready(_detected, "qwen3-tts-1.7b"))
        )
        console.print(
            f"[{MU}]|[/{MU}]  [{AB}] 3[/{AB}]  [white]{'Kokoro-82M':<18}[/white]"
            f"  [{MU}]CPU  ~300MB  ~1s    fast + great quality[/{MU}]"
            + (_ready(_detected, "kokoro"))
        )
        console.print(
            f"[{MU}]|[/{MU}]  [{AB}] 4[/{AB}]  [white]{'Coqui XTTS-v2':<18}[/white]"
            f"  [{MU}]CPU  ~1.8GB  ~3-5s  voice cloning[/{MU}]"
            + (_ready(_detected, "coqui"))
        )
        console.print(
            f"[{MU}]|[/{MU}]  [{AB}] 5[/{AB}]  [white]{'Piper TTS':<18}[/white]"
            f"  [{MU}]CPU  ~60MB   instant  lightest option[/{MU}]"
            + (_ready(_detected, "piper"))
        )
        console.print(
            f"[{MU}]|[/{MU}]  [{AB}] 6[/{AB}]  [white]{'Bark':<18}[/white]"
            f"  [{MU}]CPU  ~1.2GB  ~5-10s  expressive/emotive[/{MU}]"
            + (_ready(_detected, "bark"))
        )
        console.print(f"[{MU}]|[/{MU}]")
        console.print(f"  [{MU}]── Cloud (needs internet + API key unless noted) ──────[/{MU}]")
        console.print(
            f"[{MU}]|[/{MU}]  [{AB}] 7[/{AB}]  [white]{'edge-tts':<18}[/white]"
            f"  [{MU}]Microsoft Neural  free  no key  instant[/{MU}]"
            + (_ready(_detected, "edge-tts"))
        )
        console.print(
            f"[{MU}]|[/{MU}]  [{AB}] 8[/{AB}]  [white]{'ElevenLabs':<18}[/white]"
            f"  [{MU}]best voice quality  voice cloning  API key[/{MU}]"
        )
        console.print(
            f"[{MU}]|[/{MU}]  [{AB}] 9[/{AB}]  [white]{'OpenAI TTS':<18}[/white]"
            f"  [{MU}]tts-1 / tts-1-hd  API key[/{MU}]"
        )
        console.print(
            f"[{MU}]|[/{MU}]  [{AB}]10[/{AB}]  [white]{'Google Cloud TTS':<18}[/white]"
            f"  [{MU}]WaveNet / Neural2  API key[/{MU}]"
        )
        console.print(
            f"[{MU}]|[/{MU}]  [{AB}]11[/{AB}]  [white]{'Amazon Polly':<18}[/white]"
            f"  [{MU}]Neural voices  AWS credentials[/{MU}]"
        )
        console.print(
            f"[{MU}]|[/{MU}]  [{AB}]12[/{AB}]  [white]{'Cartesia':<18}[/white]"
            f"  [{MU}]fast + high quality  API key[/{MU}]"
        )
        console.print(
            f"[{MU}]|[/{MU}]  [{AB}]13[/{AB}]  [white]{'PlayHT':<18}[/white]"
            f"  [{MU}]voice cloning  API key[/{MU}]"
        )
        console.print(
            f"[{MU}]|[/{MU}]  [{AB}]14[/{AB}]  [white]{'Deepgram Aura':<18}[/white]"
            f"  [{MU}]very fast streaming TTS  API key[/{MU}]"
        )
        console.print(f"[{MU}]|[/{MU}]")
        console.print(
            f"[{MU}]|[/{MU}]  [{AB}]15[/{AB}]  [white]{'disabled':<18}[/white]"
            f"  [{MU}]no voice output — text only[/{MU}]"
        )
        console.print(f"[{MU}]|[/{MU}]")

        _tts_choices = [str(i) for i in range(1, 16)]
        tts_choice = ask_num("TTS [1-15]", choices=_tts_choices, default="1")

        tts_map = {
            "1":  ("qwen3-local-0.6b", "Qwen/Qwen3-TTS-12Hz-0.6B-Base",  "Qwen3-TTS-0.6B (local CPU)"),
            "2":  ("qwen3-local-1.7b", "",                                 "Qwen3-TTS-1.7B (local, voice clone ✦)"),
            "3":  ("kokoro",           "",                                  "Kokoro-82M (local CPU)"),
            "4":  ("coqui",            "",                                  "Coqui XTTS-v2 (local, voice clone ✦)"),
            "5":  ("piper",            "",                                  "Piper TTS (local CPU)"),
            "6":  ("bark",             "",                                  "Bark (local CPU)"),
            "7":  ("edge-tts",         "",                                  "edge-tts (cloud, free)"),
            "8":  ("elevenlabs",       "",                                  "ElevenLabs (cloud, voice clone ✦)"),
            "9":  ("openai",           "",                                  "OpenAI TTS (cloud)"),
            "10": ("google",           "",                                  "Google Cloud TTS (cloud)"),
            "11": ("amazon",           "",                                  "Amazon Polly (cloud)"),
            "12": ("cartesia",         "",                                  "Cartesia (cloud, voice clone ✦)"),
            "13": ("playht",           "",                                  "PlayHT (cloud, voice clone ✦)"),
            "14": ("deepgram",         "",                                  "Deepgram Aura (cloud)"),
            "15": ("disabled",         "",                                  "disabled"),
        }
        tts_engine, tts_model, tts_label = tts_map[tts_choice]

        # Per-engine pip install command shown to user after selection
        _tts_pip = {
            "qwen3-local-0.6b":  "pip install -U qwen-tts",
            "qwen3-local-1.7b":  "pip install -U qwen-tts",
            "kokoro":            "pip install kokoro soundfile",
            "coqui":             "pip install TTS  (then: pip install \"pandas<2\")",
            "piper":             "pip install piper-tts  (or grab binary from https://github.com/rhasspy/piper/releases)",
            "bark":              "pip install suno-bark scipy",
            # edge-tts is already installed by install_senses
            "openai":            "pip install httpx  (already installed)",
            "elevenlabs":        "pip install httpx  (already installed)",
            "google":            "pip install google-cloud-texttospeech",
            "amazon":            "pip install boto3",
            "cartesia":          "pip install cartesia",
            "playht":            "pip install pyht",
            "deepgram":          "pip install deepgram-sdk",
        }

        if tts_engine in _tts_pip:
            console.print(
                f"\n[dim]  Install command for {tts_label.split('(')[0].strip()}:[/dim]"
            )
            console.print(f"  [bold cyan]  {_tts_pip[tts_engine]}[/bold cyan]\n")

        # Prompt for API key if cloud engine selected
        _cloud_keys = {
            "elevenlabs": "ElevenLabs API key",
            "openai":     "OpenAI API key",
            "google":     "Google Cloud API key",
            "amazon":     "AWS Access Key ID  (secret configured separately)",
            "cartesia":   "Cartesia API key",
            "playht":     "PlayHT API key",
            "deepgram":   "Deepgram API key",
        }
        tts_api_key = ""
        if tts_engine in _cloud_keys:
            tts_api_key = ask_secret(_cloud_keys[tts_engine])
            if tts_api_key:
                ok_line("API key saved")

        ok_line(f"TTS  →  {tts_label}")
    else:
        ok_line("Speech  →  disabled")

    # ══════════════════════════════════════════════════════════
    # 8 — Write Config
    # ══════════════════════════════════════════════════════════

    step_label("Saving configuration")

    Path.home().joinpath(".serenity").mkdir(parents=True, exist_ok=True)

    workspace = obsidian_path or custom_workspace or str(default_ws)
    workspace_path = Path(workspace)
    workspace_path.mkdir(parents=True, exist_ok=True)

    # Copy the Vault Memories template into the chosen location.
    # This seeds the Agent/ and User/ structure regardless of which option was picked.
    # Existing files are never overwritten (SOUL.md is handled separately by _seed_workspace).
    if _VAULT_TEMPLATE.exists():
        _copy_vault_template(_VAULT_TEMPLATE, workspace_path)

    provider_block: dict = {"apiKey": api_key}
    if provider_key == "ollama":
        provider_block["apiBase"] = "http://localhost:11434/v1"

    config: dict = {
        "user": {
            "name":     user_name,
            "agentName": agent_name,
            "persona":  agent_persona,
            "timezone": "",
        },
        "providers": {provider_key: provider_block},
        "agents": {
            "defaults": {
                "model":                model,
                "workspace":            workspace,
                "maxTokens":            8192,
                "temperature":          0.7,
                "maxToolIterations":    20,
                "forceToolUse":         True,   # prevents hallucinated actions
                "reasoningEffort":      "adaptive",
                "contextWindowTokens": 40960,
            }
        },
        "tools": {
            "web":  {"search": {"maxResults": 5}},
            "exec": {"timeout": 60},
            "restrictToWorkspace": False,
        },
        "gateway": {
            "heartbeat": {
                "enabled":  nnn_enabled,
                "intervalS": nnn_interval,
            }
        },
        "senses": {
            "audio": {
                "enabled":            ears_enabled,
                "whisperModel":       whisper_model,
                "whisperDevice":      "cpu",
                "whisperComputeType": "int8",
                "tts": {
                    "engine": tts_engine,
                    "model":  tts_model,
                    "apiKey": tts_api_key,
                },
            },
            "vision": {
                "enabled":     eyes_enabled,
                "cameraIndex": 0,
            },
        },
    }

    if channel == "telegram" and telegram_token:
        config["channels"] = {
            "telegram": {
                "enabled": True,
                "token": telegram_token,
                "allowFrom": telegram_allow,
            }
        }
    elif channel_config:
        config["channels"] = channel_config

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    ok_line(f"Config  →  {config_path}")

    # Always seed — writes SOUL.md with correct name (always overwrites),
    # and creates Agent/ + user subfolder if they don't exist yet.
    _seed_workspace(workspace_path, agent_name, agent_persona, user_name)

    # ── Summary ───────────────────────────────────────────────

    note(
        f"Provider    {provider_name}\n"
        f"Model       {model}\n"
        f"Agent       {agent_name}\n"
        f"Channel     {channel.title() if channel != 'skip' else 'CLI only'}\n"
        f"Memory      {'NNN enabled' if nnn_enabled else 'disabled'}\n"
        f"Vision      {'enabled (CPU — camera + screen)' if eyes_enabled else 'disabled'}\n"
        f"STT         {'Faster Whisper small (wake word) + Whisper ' + whisper_model + ' (transcription)' if ears_enabled else 'disabled'}\n"
        f"TTS         {tts_label if ears_enabled else 'disabled'}\n"
        f"Workspace   {workspace}",
        "Summary",
    )

    time.sleep(0.4)
    outro("Setup complete — launching Serenity gateway")

    # Links
    note(
        "Docs          https://github.com/Malicedp/Serenity\n"
        "Obsidian      https://help.obsidian.md\n"
        "OpenRouter    https://openrouter.ai\n"
        "Anthropic     https://console.anthropic.com\n"
        "Ollama        https://ollama.com",
        "Links",
    )

    # Help tip — shown once at the end so users know it exists
    note(
        f"Run [bold][{A}]serenity help[/{A}][/bold] at any time to see all available commands.\n"
        f"\n"
        f"  Key ones to know:\n"
        f"    [{AB}]sera agent[/{AB}]          — start chatting\n"
        f"    [{AB}]sera rekey[/{AB}]          — replace your licence key when it expires\n"
        f"    [{AB}]sera onboard[/{AB}]        — re-run this setup to change anything\n"
        f"    [{AB}]sera gateway[/{AB}]        — start channels + background tasks",
        "Tip",
    )

    time.sleep(0.3)

    # ── Install mcp silently (needed for Claude Code MCP integration) ─────────
    # This is quick when already installed — pip exits immediately.
    # We do it here so the MCP server works the moment Claude Code connects.
    try:
        import subprocess as _sp
        _sp.run(
            [sys.executable, "-m", "pip", "install", "mcp", "--quiet", "--disable-pip-version-check"],
            check=False,
            stdout=_sp.DEVNULL,
            stderr=_sp.DEVNULL,
        )
    except Exception:
        pass  # non-fatal — Claude Code MCP is optional

    launch_serenity()


# ── VAULT TEMPLATE COPIER ────────────────────────────────────────────────────

_VAULT_SYSTEM_DIRS = frozenset({
    "memory", "sessions", "cron", "state", "skills",
    ".git", ".obsidian",
})

def _copy_vault_template(template_dir: Path, dest_dir: Path) -> None:
    """Copy the Agent/ structure from the default vault into a custom location.

    Rules:
    - Only copies Agent/ and vault-root files — skips system folders entirely
      (memory/, sessions/, cron/, state/, skills/ are runtime data, not templates)
    - Never overwrites files that already exist (safe for re-runs)
    - SOUL.md is excluded — _seed_workspace writes it with name substitution
    - .keep placeholder files are skipped
    """
    import shutil

    for src in template_dir.rglob("*"):
        if not src.is_file():
            continue

        rel = src.relative_to(template_dir)

        # Skip system runtime folders
        if rel.parts and rel.parts[0] in _VAULT_SYSTEM_DIRS:
            continue

        # Skip hidden files/dirs
        if any(part.startswith(".") for part in rel.parts):
            continue

        # Skip placeholders and the SOUL.md (handled by _seed_workspace)
        if src.name in (".keep", "SOUL.md"):
            continue

        dst = dest_dir / rel

        if dst.exists():
            continue  # never overwrite — user may have customised

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        ok_line(f"Copied   {rel}")


# ── WORKSPACE SEEDER ─────────────────────────────────────────────────────────

def _seed_workspace(
    workspace_path: Path,
    agent_name: str = "Sera",
    persona: str = "",
    user_name: str = "User",
) -> None:
    """Seed the vault with the default folder structure and bootstrap files.

    Vault layout
    ────────────
    Vault Memories/
      Agent/          ← agent's own files (SOUL, MEMORY, HEARTBEAT, AGENTS)
      {user_name}/    ← personal notes about the user (empty on first run)
      Experience.md   ← index / home page
    """
    # Create Agent/ and internal memory/ folder. No user subfolder — all user
    # notes go flat in the vault root alongside user-created .md files.
    agent_dir  = workspace_path / "Agent"
    memory_dir = workspace_path / "memory"
    for d in (agent_dir, memory_dir):
        d.mkdir(parents=True, exist_ok=True)

    today = __import__("datetime").date.today().isoformat()

    # Load the full SOUL.md template and substitute agent name + user name.
    # This is always written (even if SOUL.md already exists) so renaming the
    # agent during re-setup correctly propagates the new name everywhere.
    soul_template_path = Path(__file__).parent / "serenity" / "templates" / "SOUL.md"
    if soul_template_path.exists():
        soul_content = soul_template_path.read_text(encoding="utf-8")
        soul_content = soul_content.replace("{agent_name}", agent_name)
        soul_content = soul_content.replace("{user_name}", user_name)
        # Inject persona after the first paragraph if provided
        if persona and persona.strip():
            soul_content = soul_content.replace(
                "I treat the user's time as the scarcest resource, and their trust as the most valuable.",
                f"I treat the user's time as the scarcest resource, and their trust as the most valuable.\n{persona.strip()}"
            )
    else:
        soul_content = (
            f"# {agent_name}\n\n"
            f"I am {agent_name} — a personal AI agent built on Serenity.\n\n"
            f"{persona}\n\n"
            f"I solve problems by doing, not by describing what I would do.\n"
            f"I keep responses short unless depth is asked for.\n"
            f"I say what I know, flag what I don't, and never fake confidence.\n"
        )

    # Build USER.md with real values from the wizard — always overwritten so
    # re-running setup propagates name / persona changes immediately.
    _user_profile = (
        f"---\n"
        f"contexts: [chat]\n"
        f"---\n\n"
        f"# User Profile\n\n"
        f"*Written by the setup wizard. {agent_name} reads this every turn.*\n\n"
        f"## Basic Information\n\n"
        f"- **Name**: {user_name}\n"
        f"- **Timezone**: \n"
        f"- **Language**: English\n\n"
        f"## Communication Style\n\n"
        f"- {persona.strip() if persona and persona.strip() else 'Direct and concise.'}\n\n"
        f"## Work Context\n\n"
        f"- **Role**: \n"
        f"- **Main Projects**: \n"
        f"- **Tools / Stack**: \n\n"
        f"## Preferences\n\n"
        f"- \n\n"
        f"## Notes\n\n"
        f"- \n\n"
        f"---\n\n"
        f"*{agent_name} updates this file as she learns more about {user_name}.*\n"
    )

    files = {
        # ── Agent subfolder ──────────────────────────────────────
        # SOUL.md is always written — ensures name change propagates on re-setup
        "_soul": (agent_dir / "SOUL.md", soul_content, True),
        # USER.md always written — wizard values must be live immediately
        "_user": (agent_dir / "USER.md", _user_profile, True),
        # (path, content, always_overwrite)
        "_memory":    (agent_dir / "MEMORY.md", (
            "# Memory Index\n\n"
            f"*Managed by {agent_name} — do not edit manually.*\n\n"
            "This file is updated automatically by the Dream system (context compression).\n"
            "It summarises what the agent knows about the user and the world.\n"
        ), False),
        "_heartbeat": (agent_dir / "HEARTBEAT.md", (
            "# Heartbeat\n\n"
            "Every heartbeat cycle, check if there is anything worth doing autonomously.\n\n"
            "1. Query NNN for the most active topic from recent sessions\n"
            "2. Check if any vault notes need updating\n"
            "3. Work on any active tasks listed below\n"
            "4. If nothing needs action, respond with `HEARTBEAT_OK`\n\n"
            "## Active Tasks\n\n"
            "<!-- Add tasks here — picked up every heartbeat cycle -->\n\n"
            "## Completed\n\n"
            "<!-- Move completed tasks here -->\n"
        ), False),
        "_agents":    (agent_dir / "AGENTS.md", (
            f"# Agent Instructions\n\n"
            f"Default configuration for {agent_name}.\n\n"
            f"## Scheduling\n\n"
            f"Use the `cron` tool to create/list/remove jobs.\n"
            f"Do not call serenity cron via exec.\n\n"
            f"## Heartbeat\n\n"
            f"`Agent/HEARTBEAT.md` is checked on the configured heartbeat interval.\n"
        ), False),
        "_tools":     (agent_dir / "TOOLS.md", (
            "# Tool Usage Notes\n\n"
            "## vault_write — Memory Notes\n\n"
            "- Title = filename. Keep it SHORT: 'Echo VR', 'Favourite colour'\n"
            "- **No subfolder for user notes.** All memories, preferences, facts go to vault root.\n"
            "- Only use `subfolder=\"Agent\"` for Serenity's own files.\n"
            "- Exact call: `vault_write(title=\"...\", content=\"...\", tags=\"memory\")`\n"
            "- After write: quote the path from the tool result verbatim. Never invent it.\n\n"
            "## cron — Scheduling\n\n"
            "- One-off: `cron(action=\"add\", at=\"<ISO datetime>\", message=\"...\", deliver=true)`\n"
            "- Recurring: `cron(action=\"add\", every_seconds=N, message=\"...\", deliver=true)`\n"
            "- Cancel: `cron(action=\"list\")` → find job ID → `cron(action=\"remove\", job_id=\"...\")`\n"
        ), False),

        # ── Vault root ───────────────────────────────────────────
        "_experience": (workspace_path / "Experience.md", (
            f"# Experience\n"
            f"*{today}*\n\n"
            f"Welcome to your Vault Memories — {agent_name}'s long-term memory.\n\n"
            f"## Structure\n\n"
            f"| Folder | Contents |\n"
            f"|---|---|\n"
            f"| **Agent/** | {agent_name}'s own files — identity, memory index, heartbeat |\n"
            f"| **{user_name}/** | Personal notes about {user_name} — preferences, goals, traits |\n"
            f"| **Vault root** | Quick captures, session summaries, learning notes |\n\n"
            f"## Recent notes\n\n"
            f"*Notes appear here as {agent_name} learns and remembers.*\n"
        ), False),
    }

    for key, (path, content, always_overwrite) in files.items():
        if always_overwrite or not path.exists():
            path.write_text(content, encoding="utf-8")
            rel = path.relative_to(workspace_path)
            action = "Updated" if path.exists() and always_overwrite else "Created"
            ok_line(f"{action}  {rel}")


# ── LAUNCHER ─────────────────────────────────────────────────────────────────

def launch_serenity() -> None:
    # Ensure mcp package is present (needed by Claude Code MCP integration).
    # Silent and fast when already installed — pip returns instantly.
    try:
        import importlib
        if importlib.util.find_spec("mcp") is None:
            import subprocess as _sp
            _sp.run(
                [sys.executable, "-m", "pip", "install", "mcp", "--quiet", "--disable-pip-version-check"],
                check=False,
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
    except Exception:
        pass

    try:
        os.execvp("sera", ["sera", "gateway"])
    except FileNotFoundError:
        os.execvp(sys.executable, [sys.executable, "-m", "serenity", "gateway"])


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("help", "--help", "-h"):
        os.execvp("sera", ["sera", "help"])
        return

    config_path = Path.home() / ".serenity" / "config.json"
    if not config_path.exists():
        run_wizard()
    else:
        show_logo()
        launch_serenity()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print(f"\n[{MU}]Setup cancelled.[/{MU}]\n")
        sys.exit(0)
