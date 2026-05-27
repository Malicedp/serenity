"""CLI commands for serenity."""

import asyncio
import os
import select
import signal
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # Re-open stdout/stderr with UTF-8 encoding
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import typer
from loguru import logger
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from serenity import __logo__, __version__


class SafeFileHistory(FileHistory):
    """FileHistory subclass that sanitizes surrogate characters on write.

    On Windows, special Unicode input (emoji, mixed-script) can produce
    surrogate characters that crash prompt_toolkit's file write.
    See issue #2846.
    """

    def store_string(self, string: str) -> None:
        safe = string.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")
        super().store_string(safe)
from serenity.cli.stream import StreamRenderer, ThinkingSpinner
from serenity.config.paths import get_workspace_path, is_default_workspace
from serenity.config.schema import Config
from serenity.utils.helpers import sync_workspace_templates
from serenity.utils.restart import (
    consume_restart_notice_from_env,
    format_restart_completed_message,
    should_show_cli_restart_notice,
)

app = typer.Typer(
    name="serenity",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=f"{__logo__} Serenity - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    try:
        import termios

        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios

        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios

        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    from serenity.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=SafeFileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,  # Enter submits (single line mode)
    )


def _make_console() -> Console:
    return Console(file=sys.stdout)


def _render_interactive_ansi(render_fn) -> str:
    """Render Rich output to ANSI so prompt_toolkit can print it safely."""
    ansi_console = Console(
        force_terminal=True,
        color_system=console.color_system or "standard",
        width=console.width,
    )
    with ansi_console.capture() as capture:
        render_fn(ansi_console)
    return capture.get()


def _print_agent_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
) -> None:
    """Render assistant response with consistent terminal styling."""
    console = _make_console()
    content = response or ""
    body = _response_renderable(content, render_markdown, metadata)
    console.print()
    console.print(f"[cyan]{__logo__} Serenity[/cyan]")
    console.print(body)
    console.print()


def _response_renderable(content: str, render_markdown: bool, metadata: dict | None = None):
    """Render plain-text command output without markdown collapsing newlines."""
    if not render_markdown:
        return Text(content)
    if (metadata or {}).get("render_as") == "text":
        return Text(content)
    return Markdown(content)


async def _print_interactive_line(text: str) -> None:
    """Print async interactive updates with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        ansi = _render_interactive_ansi(
            lambda c: c.print(f"  [dim]↳ {text}[/dim]")
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


async def _print_interactive_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
) -> None:
    """Print async interactive replies with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        content = response or ""
        ansi = _render_interactive_ansi(
            lambda c: (
                c.print(),
                c.print(f"[cyan]{__logo__} Serenity[/cyan]"),
                c.print(_response_renderable(content, render_markdown, metadata)),
                c.print(),
            )
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


def _print_cli_progress_line(text: str, thinking: ThinkingSpinner | None) -> None:
    """Print a CLI progress line, pausing the spinner if needed."""
    with thinking.pause() if thinking else nullcontext():
        console.print(f"  [dim]↳ {text}[/dim]")


async def _print_interactive_progress_line(text: str, thinking: ThinkingSpinner | None) -> None:
    """Print an interactive progress line, pausing the spinner if needed."""
    with thinking.pause() if thinking else nullcontext():
        await _print_interactive_line(text)


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} Serenity v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """Serenity - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
    wizard: bool = typer.Option(False, "--wizard", help="Use interactive wizard"),
):
    """Initialize serenity configuration and workspace."""
    from serenity.config.loader import get_config_path, load_config, save_config, set_config_path
    from serenity.config.schema import Config

    if config:
        config_path = Path(config).expanduser().resolve()
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")
    else:
        config_path = get_config_path()

    def _apply_workspace_override(loaded: Config) -> Config:
        if workspace:
            loaded.agents.defaults.workspace = workspace
        return loaded

    # Create or update config
    if config_path.exists():
        if wizard:
            config = _apply_workspace_override(load_config(config_path))
        else:
            console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
            console.print(
                "  [bold]y[/bold] = overwrite with defaults (existing values will be lost)"
            )
            console.print(
                "  [bold]N[/bold] = refresh config, keeping existing values and adding new fields"
            )
            if typer.confirm("Overwrite?"):
                config = _apply_workspace_override(Config())
                save_config(config, config_path)
                console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
            else:
                config = _apply_workspace_override(load_config(config_path))
                save_config(config, config_path)
                console.print(
                    f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)"
                )
    else:
        config = _apply_workspace_override(Config())
        # In wizard mode, don't save yet - the wizard will handle saving if should_save=True
        if not wizard:
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Created config at {config_path}")

    # Run interactive wizard if enabled
    if wizard:
        from serenity.cli.onboard import run_onboard

        try:
            result = run_onboard(initial_config=config)
            if not result.should_save:
                console.print("[yellow]Configuration discarded. No changes were saved.[/yellow]")
                return

            config = result.config
            save_config(config, config_path)
            console.print(f"[green]✓[/green] Config saved at {config_path}")
        except Exception as e:
            console.print(f"[red]✗[/red] Error during configuration: {e}")
            console.print("[yellow]Please run 'serenity onboard' again to complete setup.[/yellow]")
            raise typer.Exit(1)
    _onboard_plugins(config_path)

    # Create workspace, preferring the configured workspace path.
    workspace_path = get_workspace_path(config.workspace_path)
    if not workspace_path.exists():
        workspace_path.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace_path}")

    sync_workspace_templates(workspace_path)

    agent_cmd = 'serenity agent -m "Hello!"'
    gateway_cmd = "serenity gateway"
    if config:
        agent_cmd += f" --config {config_path}"
        gateway_cmd += f" --config {config_path}"

    console.print(f"\n{__logo__} Serenity is ready!")
    console.print("\nNext steps:")
    if wizard:
        console.print(f"  1. Chat: [cyan]{agent_cmd}[/cyan]")
        console.print(f"  2. Start gateway: [cyan]{gateway_cmd}[/cyan]")
    else:
        console.print(f"  1. Add your API key to [cyan]{config_path}[/cyan]")
        console.print("     Get one at: https://openrouter.ai/keys")
        console.print(f"  2. Chat: [cyan]{agent_cmd}[/cyan]")
    console.print(
        "\n[dim]Want Telegram/WhatsApp? See: https://github.com/danieltniamke/serenity#-chat-apps[/dim]"
    )


def _merge_missing_defaults(existing: Any, defaults: Any) -> Any:
    """Recursively fill in missing values from defaults without overwriting user config."""
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing

    merged = dict(existing)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = _merge_missing_defaults(merged[key], value)
    return merged


def _onboard_plugins(config_path: Path) -> None:
    """Inject default config for all discovered channels (built-in + plugins)."""
    import json

    from serenity.channels.registry import discover_all

    all_channels = discover_all()
    if not all_channels:
        return

    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    channels = data.setdefault("channels", {})
    for name, cls in all_channels.items():
        if name not in channels:
            channels[name] = cls.default_config()
        else:
            channels[name] = _merge_missing_defaults(channels[name], cls.default_config())

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _make_provider(config: Config):
    """Create the appropriate LLM provider from config.

    Routing is driven by ``ProviderSpec.backend`` in the registry.
    """
    from serenity.providers.base import GenerationSettings
    from serenity.providers.registry import find_by_name

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)
    spec = find_by_name(provider_name) if provider_name else None
    backend = spec.backend if spec else "openai_compat"

    # --- validation ---
    if backend == "azure_openai":
        if not p or not p.api_key or not p.api_base:
            console.print("[red]Error: Azure OpenAI requires api_key and api_base.[/red]")
            console.print("Set them in ~/.serenity/config.json under providers.azure_openai section")
            console.print("Use the model field to specify the deployment name.")
            raise typer.Exit(1)
    elif backend == "openai_compat" and not model.startswith("bedrock/"):
        needs_key = not (p and p.api_key)
        exempt = spec and (spec.is_oauth or spec.is_local or spec.is_direct)
        if needs_key and not exempt:
            console.print("[red]Error: No API key configured.[/red]")
            console.print("Set one in ~/.serenity/config.json under providers section")
            raise typer.Exit(1)

    # --- instantiation by backend ---
    if backend == "openai_codex":
        from serenity.providers.openai_codex_provider import OpenAICodexProvider

        provider = OpenAICodexProvider(default_model=model)
    elif backend == "azure_openai":
        from serenity.providers.azure_openai_provider import AzureOpenAIProvider

        provider = AzureOpenAIProvider(
            api_key=p.api_key,
            api_base=p.api_base,
            default_model=model,
        )
    elif backend == "github_copilot":
        from serenity.providers.github_copilot_provider import GitHubCopilotProvider
        provider = GitHubCopilotProvider(default_model=model)
    elif backend == "anthropic":
        from serenity.providers.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
        )
    else:
        from serenity.providers.openai_compat_provider import OpenAICompatProvider

        provider = OpenAICompatProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
            spec=spec,
            context_window_tokens=config.agents.defaults.context_window_tokens,
        )

    defaults = config.agents.defaults
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )
    return provider


def _load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace."""
    from serenity.config.loader import load_config, resolve_config_env_vars, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    try:
        loaded = resolve_config_env_vars(load_config(config_path))
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)
    _warn_deprecated_config_keys(config_path)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def _warn_deprecated_config_keys(config_path: Path | None) -> None:
    """Hint users to remove obsolete keys from their config file."""
    import json

    from serenity.config.loader import get_config_path

    path = config_path or get_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if "memoryWindow" in raw.get("agents", {}).get("defaults", {}):
        console.print(
            "[dim]Hint: `memoryWindow` in your config is no longer used "
            "and can be safely removed.[/dim]"
        )


def _check_licence(config: "Config") -> None:
    """Validate the stored licence key against the server.

    Blocks gateway startup if:
      - No key is set AND no grace period remains.
      - The server rejects the key (invalid / revoked / expired / machine mismatch).

    Allows startup if:
      - The server confirms the key is valid.
      - The server is unreachable but the key was validated within GRACE_DAYS.
    """
    from datetime import datetime, timezone
    from serenity.licence import (
        check_grace_period, generate_nnn_token,
        is_master_key_active,
    )
    from serenity.licence_lemon import validate_licence as validate_licence

    def _authorise_nnn(key: str) -> None:
        """Pass auth token to NNN so its operations are unlocked."""
        try:
            from serenity_nnn import nnn
            nnn.authorize(generate_nnn_token(key))
        except Exception:
            pass

    # Developer / owner bypass — no server call needed.
    if is_master_key_active():
        console.print("  [dim]Licence: master key active[/dim]")
        # Master key still authorises NNN using a sentinel key value.
        _authorise_nnn("MASTER")
        return

    key = config.licence_key

    if not key:
        import hashlib as _hl
        import hmac as _hm
        import json as _json
        import re as _re
        from datetime import timedelta

        # Internal session manifest — tamper-resistant access gate.
        _AW = 13
        _MF = Path.home() / ".serenity" / ".manifest"

        def _mid() -> bytes:
            from serenity.licence_lemon import get_machine_id as _g
            return _g().encode()

        def _tag(t: str) -> str:
            return _hm.new(_mid(), t.encode(), _hl.sha256).hexdigest()

        def _read_mf() -> "str | None":
            try:
                d = _json.loads(_MF.read_text(encoding="utf-8"))
                ts, h = d["d"], d["h"]
                if _hm.compare_digest(_tag(ts), h):
                    return ts
            except Exception:
                pass
            return None

        def _write_mf(ts: str) -> None:
            try:
                _MF.parent.mkdir(parents=True, exist_ok=True)
                _MF.write_text(_json.dumps({"d": ts, "h": _tag(ts)}), encoding="utf-8")
            except Exception:
                pass

        def _has_prior_data() -> bool:
            base = Path.home() / ".serenity"
            mem = base / "memory.jsonl"
            if mem.exists() and mem.stat().st_size > 200:
                return True
            vault = base / "vault"
            if vault.exists():
                try:
                    return any(vault.iterdir())
                except Exception:
                    pass
            return False

        now_utc = datetime.now(tz=timezone.utc)
        ts = _read_mf()

        if ts is None:
            if _has_prior_data():
                # Manifest removed after prior use — write an expired anchor.
                _write_mf((now_utc - timedelta(days=_AW)).isoformat())
                days_left = 0
            else:
                # Genuine first install — start access window silently.
                _write_mf(now_utc.isoformat())
                return
        else:
            try:
                start = datetime.fromisoformat(ts)
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                days_left = _AW - (now_utc - start).days
            except Exception:
                days_left = 0

        if days_left > 0:
            return  # Within window — completely silent.

        # Access window closed — require verification code.
        console.print(
            "\n[bold]  Serenity[/bold]\n"
            "  A verification code is required to continue.\n"
            "  [dim]https://seraficationkey.lemonsqueezy.com/checkout/buy/"
            "9967e436-54fe-4ab3-b7f0-8ce71a348d4e[/dim]\n"
        )
        try:
            raw = console.input("  Verification code: ").strip().upper()
            if not _re.match(
                r"^[A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12}$", raw
            ):
                console.print(
                    "[red]  Invalid code format. "
                    "Run [bold]sera onboard[/bold] to try again.[/red]"
                )
                raise SystemExit(1)
            key = raw
            config.licence_key = key
        except (KeyboardInterrupt, EOFError):
            raise SystemExit(1)

    # Key present — validate with Lemon Squeezy (activates if no instance_id yet).
    instance_id = getattr(config, "licence_instance_id", "")
    result = validate_licence(key, instance_id)

    if result.get("valid"):
        config.licence_tier = result.get("tier", config.licence_tier)
        config.licence_instance_id = result.get("instance_id", instance_id)
        config.licence_last_validated = datetime.now(tz=timezone.utc).isoformat()
        tier = config.licence_tier or "unknown"
        console.print(f"  [green]Licence valid[/green] ([cyan]{tier}[/cyan])")
        _authorise_nnn(key)
        return

    if result.get("offline"):
        # Server unreachable — fall back to grace period.
        if check_grace_period(config.licence_last_validated):
            console.print(
                "[yellow]  Cannot reach Lemon Squeezy — running on grace period.[/yellow]\n"
                f"  [dim]Last validated: {config.licence_last_validated[:10]}. "
                "Connect to the internet soon to re-verify your licence.[/dim]"
            )
            _authorise_nnn(key)   # grace period still unlocks NNN
            return
        console.print(
            "\n[bold red]  Licence verification failed — grace period expired.[/bold red]\n"
            f"  Last successful validation: {config.licence_last_validated[:10] or 'never'}\n"
            "\n  Serenity needs an internet connection to verify your licence.\n"
            "  Please connect and restart. Your key and data are safe.\n"
            "  Issues? Contact serenitydev32@gmail.com\n"
        )
        raise SystemExit(1)

    # Server reachable and key was rejected.
    reason = result.get("reason", "Invalid key")
    console.print(
        f"\n[bold red]  Licence rejected:[/bold red] {reason}\n"
        "  Run [bold]sera onboard[/bold] and choose [L] Licence Key to update it.\n"
        "  Contact serenitydev32@gmail.com for support.\n"
    )
    raise SystemExit(1)


def _autotune_context(config: "Config") -> None:
    """Automatically tune context window, max tokens, and NNN device.

    Detection order:
      1. Known cloud provider patterns → generous context, relaxed compaction
      2. Parameter count in model name (:4b / :8b / :70b) → size-based limits
      3. Known model family prefixes → fallback sizing
      4. Unknown → safe conservative default

    Also caps max_tokens for local models — large values (e.g. 8192) eat into
    the context budget and leave too little room for history + tool results.

    Forces NNN embedder to CPU for local models so the embedding SentenceTransformer
    and Ollama LLM never compete for VRAM (causes 60-120s query stalls).

    Only adjusts values that are still at their schema defaults, so a user
    who has explicitly set a custom value in config.json is never overridden.
    """
    import os as _os
    from serenity.config.schema import AgentDefaults

    defaults = config.agents.defaults
    model = (defaults.model or "").lower().strip()

    SCHEMA_DEFAULT_CTX    = AgentDefaults.model_fields["context_window_tokens"].default
    SCHEMA_DEFAULT_IDLE   = AgentDefaults.model_fields["session_ttl_minutes"].default
    SCHEMA_DEFAULT_TOKENS = AgentDefaults.model_fields["max_tokens"].default

    user_set_ctx    = defaults.context_window_tokens != SCHEMA_DEFAULT_CTX
    user_set_idle   = defaults.session_ttl_minutes    != SCHEMA_DEFAULT_IDLE
    user_set_tokens = defaults.max_tokens             != SCHEMA_DEFAULT_TOKENS

    # ── Classify the model ────────────────────────────────────────────────────

    # Cloud / API-hosted — fast, large context
    _CLOUD_PATTERNS = (
        "claude", "gpt-", "o1", "o3", "o4", "gemini", "mistral-large",
        "llama-3", "deepseek-chat", "deepseek-r", "command-r",
        "openrouter/", "anthropic/", "openai/", "google/",
    )

    # Local model parameter-count markers embedded in model name
    import re as _re
    param_match = _re.search(r":(\d+(?:\.\d+)?)b", model)
    param_b = float(param_match.group(1)) if param_match else None

    is_cloud = any(p in model for p in _CLOUD_PATTERNS)

    # Serenity's system overhead (identity + skills + tool defs + NNN injections)
    # is ~12-15k tokens before any conversation history is added.
    # Context windows must be set well above that floor or tool results get
    # trimmed before the LLM can see them.
    #
    # max_tokens (completion budget) must be kept small on local models —
    # a large value (8192) eats 8k from the context budget, leaving almost
    # nothing for history. Local 4b models rarely produce >2k tokens anyway.

    if is_cloud:
        ctx_tokens = 80_000   # cloud APIs are fast regardless of context size
        max_tokens = 4096
        idle_mins  = 60
        tier_label = "cloud"
    elif param_b is not None:
        if param_b <= 4:
            ctx_tokens = 20_480   # 20k — Serenity overhead is 12-15k; leaves ~5k for turns
            max_tokens = 2048     # 4b models rarely need more; keeps budget healthy
            idle_mins  = 15
            tier_label = f"local-small ({param_b}b)"
        elif param_b <= 9:
            ctx_tokens = 28_672
            max_tokens = 2048
            idle_mins  = 20
            tier_label = f"local-medium ({param_b}b)"
        elif param_b <= 20:
            ctx_tokens = 40_960
            max_tokens = 3072
            idle_mins  = 25
            tier_label = f"local-large ({param_b}b)"
        else:
            ctx_tokens = 65_536
            max_tokens = 4096
            idle_mins  = 30
            tier_label = f"local-xl ({param_b}b)"
    else:
        # No param count — try family name heuristics
        if any(x in model for x in ("mini", "tiny", "small", "3b", "1b")):
            ctx_tokens = 24_576; max_tokens = 2048; idle_mins = 15; tier_label = "local-small (heuristic)"
        elif any(x in model for x in ("medium", "7b", "8b")):
            ctx_tokens = 28_672; max_tokens = 2048; idle_mins = 20; tier_label = "local-medium (heuristic)"
        elif any(x in model for x in ("large", "13b", "14b")):
            ctx_tokens = 40_960; max_tokens = 3072; idle_mins = 25; tier_label = "local-large (heuristic)"
        else:
            ctx_tokens = 28_672; max_tokens = 2048; idle_mins = 20; tier_label = "unknown (safe default)"

    changed = []
    if not user_set_ctx:
        defaults.context_window_tokens = ctx_tokens
        changed.append(f"context={ctx_tokens // 1024}k")
    if not user_set_idle:
        defaults.session_ttl_minutes = idle_mins
        changed.append(f"idle-compact={idle_mins}m")
    if not user_set_tokens:
        defaults.max_tokens = max_tokens
        changed.append(f"max-tokens={max_tokens}")

    # ── NNN embedder device ───────────────────────────────────────────────────
    # For local models: force the SentenceTransformer embedder to CPU so it never
    # competes with Ollama for VRAM. Without this, nnn_query stalls 60-120s while
    # the two models fight for GPU memory. CPU inference is ~200-500ms vs 100s stall.
    # For cloud models: GPU is fine — Ollama isn't running.
    if not is_cloud and not _os.environ.get("SERENITY_NNN_DEVICE"):
        _os.environ["SERENITY_NNN_DEVICE"] = "cpu"
        changed.append("nnn-device=cpu")

    if changed:
        console.print(
            f"  [dim]Model auto-tune ({tier_label}): {', '.join(changed)}[/dim]"
        )
    else:
        console.print(f"  [dim]Model auto-tune ({tier_label}): using user-set values[/dim]")


def _migrate_cron_store(config: "Config") -> None:
    """One-time migration: move legacy global cron store into the workspace."""
    from serenity.config.paths import get_cron_dir

    legacy_path = get_cron_dir() / "jobs.json"
    new_path = config.workspace_path / "cron" / "jobs.json"
    if legacy_path.is_file() and not new_path.exists():
        new_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.move(str(legacy_path), str(new_path))


# ============================================================================
# OpenAI-Compatible API Server
# ============================================================================


@app.command()
def serve(
    port: int | None = typer.Option(None, "--port", "-p", help="API server port"),
    host: str | None = typer.Option(None, "--host", "-H", help="Bind address"),
    timeout: float | None = typer.Option(None, "--timeout", "-t", help="Per-request timeout (seconds)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show serenity runtime logs"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Start the OpenAI-compatible API server (/v1/chat/completions)."""
    try:
        from aiohttp import web  # noqa: F401
    except ImportError:
        console.print("[red]aiohttp is required. Install with: pip install 'serenity[api]'[/red]")
        raise typer.Exit(1)

    from loguru import logger
    from serenity.agent.loop import AgentLoop
    from serenity.api.server import create_app
    from serenity.bus.queue import MessageBus
    from serenity.session.manager import SessionManager

    if verbose:
        logger.enable("serenity")
    else:
        logger.disable("serenity")

    runtime_config = _load_runtime_config(config, workspace)
    api_cfg = runtime_config.api
    host = host if host is not None else api_cfg.host
    port = port if port is not None else api_cfg.port
    timeout = timeout if timeout is not None else api_cfg.timeout
    sync_workspace_templates(runtime_config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(runtime_config)
    session_manager = SessionManager(runtime_config.workspace_path)
    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=runtime_config.workspace_path,
        model=runtime_config.agents.defaults.model,
        max_iterations=runtime_config.agents.defaults.max_tool_iterations,
        context_window_tokens=runtime_config.agents.defaults.context_window_tokens,
        context_block_limit=runtime_config.agents.defaults.context_block_limit,
        max_tool_result_chars=runtime_config.agents.defaults.max_tool_result_chars,
        provider_retry_mode=runtime_config.agents.defaults.provider_retry_mode,
        web_config=runtime_config.tools.web,
        exec_config=runtime_config.tools.exec,
        restrict_to_workspace=runtime_config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=runtime_config.tools.mcp_servers,
        channels_config=runtime_config.channels,
        timezone=runtime_config.agents.defaults.timezone,
        unified_session=runtime_config.agents.defaults.unified_session,
        disabled_skills=runtime_config.agents.defaults.disabled_skills,
        session_ttl_minutes=runtime_config.agents.defaults.session_ttl_minutes,
        session_ttl_overrides=runtime_config.agents.defaults.session_ttl_overrides,
        tools_config=runtime_config.tools,
        force_tool_use=runtime_config.agents.defaults.force_tool_use,
    )

    model_name = runtime_config.agents.defaults.model
    console.print(f"{__logo__} Serenity: Starting OpenAI-compatible API server")
    console.print(f"  [cyan]Endpoint[/cyan] : http://{host}:{port}/v1/chat/completions")
    console.print(f"  [cyan]Model[/cyan]    : {model_name}")
    console.print("  [cyan]Session[/cyan]  : api:default")
    console.print(f"  [cyan]Timeout[/cyan]  : {timeout}s")
    if host in {"0.0.0.0", "::"}:
        console.print(
            "[yellow]Warning:[/yellow] API is bound to all interfaces. "
            "Only do this behind a trusted network boundary, firewall, or reverse proxy."
        )
    console.print()

    api_app = create_app(agent_loop, model_name=model_name, request_timeout=timeout)

    async def on_startup(_app):
        await agent_loop._connect_mcp()

    async def on_cleanup(_app):
        await agent_loop.close_mcp()

    api_app.on_startup.append(on_startup)
    api_app.on_cleanup.append(on_cleanup)

    web.run_app(api_app, host=host, port=port, print=lambda msg: logger.info(msg))


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Start the serenity gateway."""
    from serenity.agent.loop import AgentLoop
    from serenity.bus.queue import MessageBus
    from serenity.channels.manager import ChannelManager
    from serenity.cron.service import CronService
    from serenity.cron.types import CronJob
    from serenity.heartbeat.service import HeartbeatService
    from serenity.session.manager import SessionManager

    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG)

    config = _load_runtime_config(config, workspace)
    port = port if port is not None else config.gateway.port

    # ── Licence check ─────────────────────────────────────────────────────────
    _check_licence(config)
    # Save config so instance_id and last_validated timestamp persist to disk
    try:
        from serenity.config.loader import get_config_path, save_config
        save_config(config, get_config_path())
    except Exception:
        pass
    # ─────────────────────────────────────────────────────────────────────────

    # ── Auto-tune context window for the configured model ─────────────────────
    _autotune_context(config)
    # ─────────────────────────────────────────────────────────────────────────

    console.print(f"{__logo__} Starting Serenity gateway version {__version__} on port {port}...")

    # Silence noisy third-party loggers that spam [WARNING] at INFO level
    import logging as _logging
    _logging.getLogger("chromadb").setLevel(_logging.ERROR)
    _logging.getLogger("transformers").setLevel(_logging.ERROR)   # nomic "All keys matched" spam
    _logging.getLogger("ultralytics").setLevel(_logging.WARNING)  # YOLO startup chatter

    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(config)
    session_manager = SessionManager(config.workspace_path)

    # ── Startup session health check ──────────────────────────────────────────
    # On every restart:
    #   1. Always wipe voice sessions — they're ephemeral by design.
    #   2. For all other sessions: if the message count is over the token budget
    #      (i.e. would trigger fast-trim immediately on first message), trim them
    #      down to the last 8 messages so the agent starts with clean context.
    #      This handles sessions that grew large during a previous run with a
    #      bigger context window.
    _ALWAYS_WIPE = ("voice_wake", "voice_microphone")
    _HISTORY_BUDGET = max(
        config.agents.defaults.context_window_tokens
        - config.agents.defaults.max_tokens
        - 3072,   # safety + injection overhead
        512,
    )
    # Rough chars-per-token estimate used to avoid a full tiktoken scan at startup
    _CHARS_PER_TOKEN = 4
    _MAX_CHARS = _HISTORY_BUDGET * _CHARS_PER_TOKEN
    _KEEP_RECENT = 8

    _sessions_dir = config.workspace_path / "sessions"
    if _sessions_dir.exists():
        for _sess_path in _sessions_dir.glob("*.jsonl"):
            _key = _sess_path.stem
            # 1. Always wipe voice
            if any(_key == w or _key.startswith(w) for w in _ALWAYS_WIPE):
                try:
                    _sess_path.write_text("", encoding="utf-8")
                    logger.info("Cleared ephemeral session: {}", _key)
                except Exception as _e:
                    logger.warning("Could not clear session {}: {}", _key, _e)
                continue
            # 2. Trim over-budget sessions.
            #    Strategy: try keeping the last 8 messages; if still over budget
            #    just wipe to metadata only. Tool results from past web searches
            #    can each be 3k+ tokens — no point keeping them.
            try:
                _raw = _sess_path.read_text(encoding="utf-8")
                _lines = [_l for _l in _raw.splitlines() if _l.strip()]
                if not _lines:
                    continue
                _meta = [_lines[0]] if '"_type"' in _lines[0] else []
                _msgs = _lines[len(_meta):]
                _orig_count = len(_msgs)
                if sum(len(_l) for _l in _lines) <= _MAX_CHARS:
                    continue  # fine as-is
                # Try last 8 messages first
                _candidate = _meta + _msgs[-_KEEP_RECENT:]
                if sum(len(_l) for _l in _candidate) <= _MAX_CHARS:
                    _sess_path.write_text("\n".join(_candidate) + "\n", encoding="utf-8")
                    logger.info("Trimmed over-budget session {} ({} → {} messages)", _key, _orig_count, _KEEP_RECENT)
                else:
                    # Still over budget — wipe messages, keep metadata only
                    _sess_path.write_text("\n".join(_meta) + "\n" if _meta else "", encoding="utf-8")
                    logger.info("Wiped over-budget session {} ({} messages too large to trim)", _key, _orig_count)
            except Exception as _e:
                logger.warning("Could not check session {}: {}", _key, _e)
    # ─────────────────────────────────────────────────────────────────────────

    # Preserve existing single-workspace installs, but keep custom workspaces clean.
    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    # Create cron service with workspace-scoped store
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_config=config.tools.web,
        context_block_limit=config.agents.defaults.context_block_limit,
        max_tool_result_chars=config.agents.defaults.max_tool_result_chars,
        provider_retry_mode=config.agents.defaults.provider_retry_mode,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        timezone=config.agents.defaults.timezone,
        unified_session=config.agents.defaults.unified_session,
        disabled_skills=config.agents.defaults.disabled_skills,
        session_ttl_minutes=config.agents.defaults.session_ttl_minutes,
        session_ttl_overrides=config.agents.defaults.session_ttl_overrides,
        tools_config=config.tools,
        force_tool_use=config.agents.defaults.force_tool_use,
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        import time as _time

        # Dream is an internal job — run directly, not through the agent loop.
        if job.name == "dream":
            try:
                await agent.dream.run()
                logger.info("Dream cron job completed")
            except Exception:
                logger.exception("Dream cron job failed")
            return None

        from serenity.agent.tools.cron import CronTool
        from serenity.agent.tools.message import MessageTool
        from serenity.utils.evaluator import evaluate_response

        # Defer if a real user session is actively using the GPU.
        # Unlike heartbeat (which can silently skip), user-scheduled cron jobs
        # should eventually run — so we wait briefly and retry once rather than drop.
        _skip_window_s = float(os.environ.get("SERENITY_CRON_SKIP_RECENT_S", "120"))
        _now_t = _time.time()
        _recent_key = next(
            (
                k for k, last_t in agent._reflector._last_message.items()
                if not agent._reflector._is_internal(k)
                and (_now_t - last_t) < _skip_window_s
            ),
            None,
        )
        if _recent_key is not None:
            _waited = _now_t - agent._reflector._last_message[_recent_key]
            logger.info(
                "Cron job '{}' deferred — user session '{}' was active {:.0f}s ago; "
                "waiting {}s for GPU to free up.",
                job.name, _recent_key, _waited, int(_skip_window_s),
            )
            await asyncio.sleep(_skip_window_s)

        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)
        try:
            resp = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
            )
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        response = resp.content if resp else ""

        message_tool = agent.tools.get("message")
        if job.payload.deliver and isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        if job.payload.deliver and job.payload.to and response:
            # evaluate_response makes a second LLM call — give it a hard cap so
            # it can't block the cron loop if the model is slow or busy.
            _eval_timeout_s = float(os.environ.get("SERENITY_CRON_EVAL_TIMEOUT", "60"))
            try:
                should_notify = await asyncio.wait_for(
                    evaluate_response(response, reminder_note, provider, agent.model),
                    timeout=_eval_timeout_s,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Cron evaluate_response timed out ({}s) for job '{}' — defaulting to notify",
                    int(_eval_timeout_s), job.name,
                )
                should_notify = True  # default: notify rather than silently drop

            if should_notify:
                from serenity.bus.events import OutboundMessage
                await bus.publish_outbound(OutboundMessage(
                    channel=job.payload.channel or "cli",
                    chat_id=job.payload.to,
                    content=response,
                ))
        return response

    cron.on_job = on_cron_job

    # Create channel manager
    channels = ChannelManager(config, bus)

    def _pick_heartbeat_target() -> tuple[str, str]:
        """Pick a routable channel/chat target for heartbeat-triggered messages."""
        enabled = set(channels.enabled_channels)
        # Prefer the most recently updated non-internal session on an enabled channel.
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        # Fallback keeps prior behavior but remains explicit.
        return "cli", "direct"

    # Create heartbeat service
    async def on_heartbeat_execute(tasks: str) -> str:
        """Phase 2: execute heartbeat tasks through the full agent loop."""
        import time as _time

        # Skip if a real user session was active recently.
        # When the user is actively messaging, the local GPU is occupied and
        # a heartbeat LLM call would queue behind it — both end up timing out
        # and the user gets no reply. Default window: 5 minutes.
        _skip_window_s = float(
            os.environ.get("SERENITY_HEARTBEAT_SKIP_RECENT_S", "300")
        )
        _now = _time.time()
        _recent_key = next(
            (
                k for k, last_t in agent._reflector._last_message.items()
                if not agent._reflector._is_internal(k)
                and (_now - last_t) < _skip_window_s
            ),
            None,
        )
        if _recent_key is not None:
            logger.info(
                "Heartbeat skipped — user session '{}' was active {:.0f}s ago "
                "(within {:.0f}s window); will retry next interval.",
                _recent_key,
                _now - agent._reflector._last_message[_recent_key],
                _skip_window_s,
            )
            return ""

        channel, chat_id = _pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            pass

        # Trim BEFORE the LLM call so the model gets a clean bounded context.
        # Without this, accumulated history from prior runs (19 msgs / 28k tokens
        # in observed logs) causes the model to fake-narrate instead of act.
        session = agent.sessions.get_or_create("heartbeat")
        session.retain_recent_legal_suffix(hb_cfg.keep_recent_messages)
        agent.sessions.save(session)

        # Prepend a no-scratchpad guard — heartbeat is a background check, not a
        # user task. The model tends to open a scratchpad for any "decide / plan"
        # phrasing, which wastes 2 LLM turns and leaves stale scratchpad state.
        heartbeat_prompt = (
            "BACKGROUND CHECK — do NOT open or write a scratchpad. "
            "This is a lightweight background review, not a user task.\n\n"
            + tasks
        )
        resp = await agent.process_direct(
            heartbeat_prompt,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )

        # Trim again after the run so the next tick also starts clean.
        session = agent.sessions.get_or_create("heartbeat")
        session.retain_recent_legal_suffix(hb_cfg.keep_recent_messages)
        agent.sessions.save(session)

        return resp.content if resp else ""

    async def on_heartbeat_notify(response: str) -> None:
        """Deliver a heartbeat response to the user's channel."""
        from serenity.bus.events import OutboundMessage
        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return  # No external channel available to deliver to
        await bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id, content=response))

    hb_cfg = config.gateway.heartbeat

    def _heartbeat_should_skip() -> bool:
        """Return True if a real user session was active recently.

        Called at the START of every heartbeat tick — before Phase 1 (the LLM
        decide call) — so we never waste a model call competing with the user.
        The same 300s window that on_heartbeat_execute used, but checked earlier.
        """
        import time as _time
        _skip_window_s = float(os.environ.get("SERENITY_HEARTBEAT_SKIP_RECENT_S", "300"))
        _now = _time.time()
        _recent_key = next(
            (
                k for k, last_t in agent._reflector._last_message.items()
                if not agent._reflector._is_internal(k)
                and (_now - last_t) < _skip_window_s
            ),
            None,
        )
        if _recent_key is not None:
            logger.info(
                "Heartbeat tick skipped — user session '{}' was active {:.0f}s ago "
                "(within {:.0f}s window).",
                _recent_key,
                _now - agent._reflector._last_message[_recent_key],
                _skip_window_s,
            )
            return True
        return False

    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
        timezone=config.agents.defaults.timezone,
        should_skip_fn=_heartbeat_should_skip,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

    async def _health_server(host: str, health_port: int):
        """Lightweight HTTP health endpoint on the gateway port."""
        import json as _json

        async def handle(reader, writer):
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=5)
            except (asyncio.TimeoutError, ConnectionError):
                writer.close()
                return

            request_line = data.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
            method, path = "", ""
            parts = request_line.split(" ")
            if len(parts) >= 2:
                method, path = parts[0], parts[1]

            if method == "GET" and path == "/health":
                body = _json.dumps({"status": "ok"})
                resp = (
                    f"HTTP/1.0 200 OK\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n{body}"
                )

            else:
                body = "Not Found"
                resp = (
                    f"HTTP/1.0 404 Not Found\r\n"
                    f"Content-Type: text/plain\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n{body}"
                )

            writer.write(resp.encode())
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, host, health_port)
        console.print(
            f"[green]✓[/green] Health endpoint: http://{host}:{health_port}/health"
        )
        async with server:
            await server.serve_forever()
    # Register Dream system job (always-on, idempotent on restart)
    dream_cfg = config.agents.defaults.dream
    if dream_cfg.model_override:
        agent.dream.model = dream_cfg.model_override
    agent.dream.max_batch_size = dream_cfg.max_batch_size
    # Note: max_iterations and annotate_line_ages are DreamConfig fields kept
    # for config compatibility but Dream.run() no longer uses them (Dream was
    # simplified to a pure file janitor with no LLM calls).
    from serenity.cron.types import CronJob, CronPayload
    cron.register_system_job(CronJob(
        id="dream",
        name="dream",
        schedule=dream_cfg.build_schedule(config.agents.defaults.timezone),
        payload=CronPayload(kind="system_event"),
    ))
    console.print(f"[green]✓[/green] Dream: {dream_cfg.describe_schedule()}")

    async def _nnn_scheduler():
        """Consolidate and prune NNN bundles every 30 minutes.

        Runs in a thread executor so ChromaDB writes and any embed() calls
        inside consolidate() don't block the asyncio event loop.
        """
        while True:
            await asyncio.sleep(1800)
            try:
                from serenity_nnn import nnn
                await asyncio.to_thread(nnn.consolidate)
                await asyncio.to_thread(nnn.prune)
            except Exception:
                logger.debug("NNN scheduler tick failed", exc_info=True)

    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            # Pre-warm the embedding model so the first NNN call is instant
            try:
                from serenity.agent.vault_index import warm_embed
                asyncio.create_task(warm_embed())
            except Exception:
                pass
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
                _health_server(config.gateway.host, port),
                _nnn_scheduler(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception:
            import traceback

            console.print("\n[red]Error: Gateway crashed unexpectedly[/red]")
            console.print(traceback.format_exc())
        finally:
            await agent.close_mcp()
            heartbeat.stop()
            cron.stop()

            # Shutdown summary — write a proper summary for every active session
            # before the process exits. This is the long-term memory write: when
            # the gateway restarts, the agent loads running_summary instead of
            # raw history, so context is clean from the first message.
            console.print("\n Summarising sessions before shutdown...")
            try:
                active_sessions = list(agent.sessions._cache.values())
                for sess in active_sessions:
                    unconsolidated = len(sess.messages) - sess.last_consolidated
                    if unconsolidated >= 2:
                        try:
                            await asyncio.wait_for(
                                agent.consolidator.deep_summarise(sess),
                                timeout=120,
                            )
                            console.print(f"  ✓ {sess.key}")
                        except asyncio.TimeoutError:
                            console.print(f"  ⚠ {sess.key} — timed out, raw-archiving")
                            agent.consolidator.store.raw_archive(
                                sess.messages[sess.last_consolidated:]
                            )
                        except Exception as e:
                            console.print(f"  ⚠ {sess.key} — {e}")
            except Exception as e:
                console.print(f"  Shutdown summary error: {e}")

            agent.stop()
            await channels.stop_all()

    asyncio.run(run())


# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show serenity runtime logs during chat"),
):
    """Interact with the agent directly."""
    from loguru import logger

    from serenity.agent.loop import AgentLoop
    from serenity.bus.queue import MessageBus
    from serenity.cron.service import CronService

    config = _load_runtime_config(config, workspace)
    sync_workspace_templates(config.workspace_path)

    # Licence check — same as gateway so NNN is authorised for agent sessions too
    _check_licence(config)
    try:
        from serenity.config.loader import get_config_path, save_config
        save_config(config, get_config_path())
    except Exception:
        pass

    bus = MessageBus()
    provider = _make_provider(config)

    # Preserve existing single-workspace installs, but keep custom workspaces clean.
    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    # Create cron service with workspace-scoped store
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    if logs:
        logger.enable("serenity")
    else:
        logger.disable("serenity")

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_config=config.tools.web,
        context_block_limit=config.agents.defaults.context_block_limit,
        max_tool_result_chars=config.agents.defaults.max_tool_result_chars,
        provider_retry_mode=config.agents.defaults.provider_retry_mode,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        timezone=config.agents.defaults.timezone,
        unified_session=config.agents.defaults.unified_session,
        disabled_skills=config.agents.defaults.disabled_skills,
        session_ttl_minutes=config.agents.defaults.session_ttl_minutes,
        session_ttl_overrides=config.agents.defaults.session_ttl_overrides,
        tools_config=config.tools,
        force_tool_use=config.agents.defaults.force_tool_use,
    )
    restart_notice = consume_restart_notice_from_env()
    if restart_notice and should_show_cli_restart_notice(restart_notice, session_id):
        _print_agent_response(
            format_restart_completed_message(restart_notice.started_at_raw),
            render_markdown=False,
        )

    # Shared reference for progress callbacks
    _thinking: ThinkingSpinner | None = None

    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        ch = agent_loop.channels_config
        if ch and tool_hint and not ch.send_tool_hints:
            return
        if ch and not tool_hint and not ch.send_progress:
            return
        _print_cli_progress_line(content, _thinking)

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            # Kick off embedder warmup in background — loads nomic-embed-text
            # into VRAM so any NNN call during this turn is instant
            try:
                from serenity.agent.vault_index import warm_embed
                asyncio.create_task(warm_embed())
            except Exception:
                pass
            renderer = StreamRenderer(render_markdown=markdown)
            response = await agent_loop.process_direct(
                message, session_id,
                on_progress=_cli_progress,
                on_stream=renderer.on_delta,
                on_stream_end=renderer.on_end,
            )
            if not renderer.streamed:
                await renderer.close()
                _print_agent_response(
                    response.content if response else "",
                    render_markdown=markdown,
                    metadata=response.metadata if response else None,
                )
            await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from serenity.bus.events import InboundMessage
        _init_prompt_session()
        console.print(f"{__logo__} Serenity — Interactive mode [bold blue]({config.agents.defaults.model})[/bold blue] — type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit\n")

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            _restore_terminal()
            console.print(f"\nReceived {sig_name}, goodbye!")
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        # SIGHUP is not available on Windows
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, _handle_signal)
        # Ignore SIGPIPE to prevent silent process termination when writing to closed pipes
        # SIGPIPE is not available on Windows
        if hasattr(signal, 'SIGPIPE'):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        async def run_interactive():
            # Pre-warm embedder in background while user types first message
            try:
                from serenity.agent.vault_index import warm_embed
                asyncio.create_task(warm_embed())
            except Exception:
                pass
            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[tuple[str, dict]] = []
            renderer: StreamRenderer | None = None

            async def _consume_outbound():
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)

                        if msg.metadata.get("_stream_delta"):
                            if renderer:
                                await renderer.on_delta(msg.content)
                            continue
                        if msg.metadata.get("_stream_end"):
                            if renderer:
                                await renderer.on_end(
                                    resuming=msg.metadata.get("_resuming", False),
                                )
                            continue
                        if msg.metadata.get("_streamed"):
                            turn_done.set()
                            continue

                        if msg.metadata.get("_progress"):
                            is_tool_hint = msg.metadata.get("_tool_hint", False)
                            ch = agent_loop.channels_config
                            if ch and is_tool_hint and not ch.send_tool_hints:
                                pass
                            elif ch and not is_tool_hint and not ch.send_progress:
                                pass
                            else:
                                await _print_interactive_progress_line(msg.content, _thinking)
                            continue

                        if not turn_done.is_set():
                            if msg.content:
                                turn_response.append((msg.content, dict(msg.metadata or {})))
                            turn_done.set()
                        elif msg.content:
                            await _print_interactive_response(
                                msg.content,
                                render_markdown=markdown,
                                metadata=msg.metadata,
                            )

                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        # Stop spinner before user input to avoid prompt_toolkit conflicts
                        if renderer:
                            renderer.stop_for_input()
                        user_input = await _read_interactive_input_async()
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        turn_done.clear()
                        turn_response.clear()
                        renderer = StreamRenderer(render_markdown=markdown)

                        await bus.publish_inbound(InboundMessage(
                            channel=cli_channel,
                            sender_id="user",
                            chat_id=cli_chat_id,
                            content=user_input,
                            metadata={"_wants_stream": True},
                        ))

                        await turn_done.wait()

                        if turn_response:
                            content, meta = turn_response[0]
                            if content and not meta.get("_streamed"):
                                if renderer:
                                    await renderer.close()
                                _print_agent_response(
                                    content, render_markdown=markdown, metadata=meta,
                                )
                        elif renderer and not renderer.streamed:
                            await renderer.close()
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status(
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Show channel status."""
    from serenity.channels.registry import discover_all
    from serenity.config.loader import load_config, set_config_path

    resolved_config_path = Path(config_path).expanduser().resolve() if config_path else None
    if resolved_config_path is not None:
        set_config_path(resolved_config_path)

    config = load_config(resolved_config_path)

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled")

    for name, cls in sorted(discover_all().items()):
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            "[green]\u2713[/green]" if enabled else "[dim]\u2717[/dim]",
        )

    console.print(table)


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess

    # User's bridge location
    from serenity.config.paths import get_bridge_install_dir

    user_bridge = get_bridge_install_dir()

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    npm_path = shutil.which("npm")
    if not npm_path:
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # serenity/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall serenity")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up bridge...")

    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run([npm_path, "install"], cwd=user_bridge, check=True, capture_output=True)

        console.print("  Building...")
        subprocess.run([npm_path, "run", "build"], cwd=user_bridge, check=True, capture_output=True)

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")
def channels_login(
    channel_name: str = typer.Argument(..., help="Channel name (e.g. weixin, whatsapp)"),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-authentication even if already logged in"),
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Authenticate with a channel via QR code or other interactive login."""
    from serenity.channels.registry import discover_all
    from serenity.config.loader import load_config, set_config_path

    resolved_config_path = Path(config_path).expanduser().resolve() if config_path else None
    if resolved_config_path is not None:
        set_config_path(resolved_config_path)

    config = load_config(resolved_config_path)
    channel_cfg = getattr(config.channels, channel_name, None) or {}

    # Validate channel exists
    all_channels = discover_all()
    if channel_name not in all_channels:
        available = ", ".join(all_channels.keys())
        console.print(f"[red]Unknown channel: {channel_name}[/red]  Available: {available}")
        raise typer.Exit(1)

    console.print(f"{__logo__} {all_channels[channel_name].display_name} Login\n")

    channel_cls = all_channels[channel_name]
    channel = channel_cls(channel_cfg, bus=None)

    success = asyncio.run(channel.login(force=force))

    if not success:
        raise typer.Exit(1)


# ============================================================================
# Plugin Commands
# ============================================================================

plugins_app = typer.Typer(help="Manage channel plugins")
app.add_typer(plugins_app, name="plugins")


@plugins_app.command("list")
def plugins_list():
    """List all discovered channels (built-in and plugins)."""
    from serenity.channels.registry import discover_all, discover_channel_names
    from serenity.config.loader import load_config

    config = load_config()
    builtin_names = set(discover_channel_names())
    all_channels = discover_all()

    table = Table(title="Channel Plugins")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="magenta")
    table.add_column("Enabled")

    for name in sorted(all_channels):
        cls = all_channels[name]
        source = "builtin" if name in builtin_names else "plugin"
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            source,
            "[green]yes[/green]" if enabled else "[dim]no[/dim]",
        )

    console.print(table)


# ============================================================================
# Visualise Command
# ============================================================================


@app.command()
def visualise():
    """Export NNN memory and open Embedding Atlas for vector space visualisation."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    blue = lambda t: f"\x1b[94m{t}\x1b[0m"

    print(blue("[Serenity] Loading NNN state..."))

    try:
        from serenity_nnn import nnn, get_state
    except ImportError:
        console.print("[red]serenity_nnn not installed. Run: pip install -e .[/red]")
        raise typer.Exit(1)

    # Suppress ChromaDB's orphaned-segment delete warnings and telemetry noise
    import logging as _logging
    _logging.getLogger("chromadb").setLevel(_logging.ERROR)

    # Authorise NNN — mirrors the gateway licence flow so visualise works
    # standalone without requiring a full `sera` gateway startup.
    try:
        from serenity.licence import generate_nnn_token, is_master_key_active
        if is_master_key_active():
            nnn.authorize(generate_nnn_token("MASTER"))
        else:
            from serenity.config.loader import load_config as _load_config
            _cfg = _load_config()
            if _cfg.licence_key:
                nnn.authorize(generate_nnn_token(_cfg.licence_key))
    except Exception as _auth_err:
        console.print(f"[yellow]Warning: NNN auth failed ({_auth_err}). Visualise may not work.[/yellow]")

    nnn._ensure_loaded()
    state = get_state()

    if state["stats"]["total"] == 0:
        print(blue("[Serenity] No bundles yet. Use Serenity for a while first."))
        return

    bundle_list = [b for b in nnn.bundles.values() if b.centroid is not None]
    total = len(bundle_list)
    print(blue(f"[Serenity] Exporting {total} bundles"))

    export_dir = Path.home() / ".serenity" / "atlas_export"
    export_dir.mkdir(parents=True, exist_ok=True)

    # Export as a flat JSON array of records — the format embedding-atlas expects.
    # Each record contains the full centroid vector plus human-readable metadata.
    records = [
        {
            "id": b.id,
            "type": b.type,
            "content": b.content_samples[-1][:200] if b.content_samples else "",
            "activation_score": round(b.activation_score, 2),
            "activation_count": b.activation_count,
            "synthetic": b.synthetic,
            "vector": b.centroid.tolist() if hasattr(b.centroid, "tolist") else list(b.centroid),
        }
        for b in bundle_list
    ]

    export_path = export_dir / "nnn_bundles.json"
    with open(export_path, "w", encoding="utf-8") as f:
        json.dump(records, f)

    print(blue(f"[Serenity] Saved to: {export_path}"))

    import importlib.util
    if importlib.util.find_spec("embedding_atlas") is None:
        print(blue("[Serenity] embedding-atlas not found. Installing now..."))
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "embedding-atlas", "--quiet"],
            capture_output=True,
        )
        if result.returncode != 0:
            console.print(
                "[red]Install failed.[/red] Run manually:\n"
                "  pip install embedding-atlas"
            )
            raise typer.Exit(1)
        print(blue("[Serenity] Installed."))

    # ------------------------------------------------------------------ #
    # Windows fix: embedding_atlas uses Path.rename() which maps to       #
    # os.rename() — this raises FileExistsError if the target exists.     #
    # os.replace() is the correct cross-platform call; monkey-patch the   #
    # three spots in embedding_atlas.cache before the server starts.      #
    # ------------------------------------------------------------------ #
    if sys.platform == "win32":
        try:
            import embedding_atlas.cache as _ea_cache
            import inspect

            def _make_patched(fn):
                src = inspect.getsource(fn)
                src = src.replace(".rename(cache_path)", ".replace(cache_path)")
                # strip the leading indent so exec sees clean top-level code
                lines = src.splitlines()
                indent = len(lines[0]) - len(lines[0].lstrip())
                src = "\n".join(l[indent:] for l in lines)
                ns: dict = {}
                exec(compile(src, "<patched>", "exec"), fn.__globals__, ns)
                return list(ns.values())[0]

            for _attr in ("file_cache_set", "file_cache_set_async", "file_cache_set_with_callback"):
                _orig = getattr(_ea_cache, _attr, None)
                if _orig is not None:
                    setattr(_ea_cache, _attr, _make_patched(_orig))
        except Exception:
            pass  # patch failed — try launching anyway; worst case user sees the error

    print(blue("[Serenity] Launching Embedding Atlas — opening in browser..."))
    print(blue("[Serenity] Press Ctrl+C to stop"))

    # Pre-compute 2D projection with sklearn (numpy-version-agnostic) so we
    # never depend on numba/umap which breaks on NumPy >= 2.4.
    # We try TSNE first (better clusters), fall back to PCA (always works).
    try:
        import numpy as _np
        vectors = _np.array([r["vector"] for r in records], dtype=_np.float32)
        n_samples = len(vectors)
        coords = None

        if n_samples >= 4:
            try:
                from sklearn.manifold import TSNE as _TSNE
                perplexity = max(2, min(30, n_samples - 1))
                coords = _TSNE(
                    n_components=2, perplexity=perplexity,
                    random_state=42, n_iter=500,
                ).fit_transform(vectors)
                print(blue("[Serenity] Projection: t-SNE (sklearn)"))
            except Exception:
                coords = None

        if coords is None:
            from sklearn.decomposition import PCA as _PCA
            n_components = min(2, n_samples, vectors.shape[1])
            pca = _PCA(n_components=n_components, random_state=42)
            coords = pca.fit_transform(vectors)
            if coords.shape[1] == 1:
                coords = _np.column_stack([coords, _np.zeros(n_samples)])
            print(blue("[Serenity] Projection: PCA (sklearn)"))

        for i, r in enumerate(records):
            r["x"] = float(coords[i, 0])
            r["y"] = float(coords[i, 1])

        with open(export_path, "w", encoding="utf-8") as f:
            json.dump(records, f)

    except Exception as _proj_err:
        console.print(f"[yellow]Projection failed ({_proj_err}) — embedding-atlas will compute its own.[/yellow]")

    # Launch embedding-atlas — pass pre-computed x/y if available, else fall back to --vector
    from embedding_atlas.cli import main as _ea_main
    has_coords = all("x" in r and "y" in r for r in records)
    if has_coords:
        launch_args = [
            str(export_path),
            "--x", "x",
            "--y", "y",
            "--text", "content",
            "--auto-port",
        ]
    else:
        n_neighbors = max(2, min(15, total - 1))
        launch_args = [
            str(export_path),
            "--vector", "vector",
            "--text", "content",
            "--umap-n-neighbors", str(n_neighbors),
            "--auto-port",
        ]
    _ea_main(standalone_mode=True, args=launch_args)


@app.command()
def visualize():
    """Alias for visualise."""
    visualise()


# ============================================================================
# App Index Commands
# ============================================================================

apps_cmd = typer.Typer(help="Manage the installed-app index used by open_app.")
app.add_typer(apps_cmd, name="apps")


@apps_cmd.command("scan")
def apps_scan():
    """Scan this machine for installed apps and rebuild the index."""
    console.print("[cyan]Scanning for installed apps…[/cyan]")
    from serenity.senses.app_index import scan, _INDEX_PATH
    count = scan(verbose=True)
    console.print(f"[green]Done — {count} apps indexed → {_INDEX_PATH}[/green]")


@apps_cmd.command("list")
def apps_list(query: str = typer.Argument("", help="Filter by name")):
    """List all apps in the index (optionally filtered)."""
    from serenity.senses.app_index import all_apps, _INDEX_PATH
    index = all_apps()
    if not index:
        console.print(f"[yellow]No index found. Run: sera apps scan[/yellow]")
        return
    needle = query.lower()
    rows = [(k, v["name"], v["exe"]) for k, v in sorted(index.items())
            if not needle or needle in k or needle in v["name"].lower()]
    console.print(f"[cyan]{len(rows)} apps[/cyan] (index: {_INDEX_PATH})\n")
    for key, name, exe in rows:
        console.print(f"  [bold]{name}[/bold]")
        console.print(f"    [dim]{exe}[/dim]")


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show serenity status."""
    from serenity.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} Serenity Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        from serenity.providers.registry import PROVIDERS

        console.print(f"Model: {config.agents.defaults.model}")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")


def _fetch_ollama_models() -> list[str]:
    """Query Ollama at localhost:11434 for available models. Returns [] on failure."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as resp:
            data = json.loads(resp.read().decode())
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def _pick_from_list(items: list[str], title: str, allow_custom: bool = True) -> str | None:
    """Print a numbered list, prompt for a choice. Returns selected string or None to cancel."""
    console.print(f"\n[bold]{title}[/bold]")
    for i, item in enumerate(items, 1):
        console.print(f"  [cyan]{i}[/cyan]. {item}")
    if allow_custom:
        console.print(f"  [cyan]{len(items) + 1}[/cyan]. Enter manually")
    console.print(f"  [cyan]0[/cyan]. Cancel")

    raw = typer.prompt("\nChoose", default="0")
    try:
        choice = int(raw.strip())
    except ValueError:
        # They typed a model name directly
        return raw.strip() or None

    if choice == 0:
        return None
    if allow_custom and choice == len(items) + 1:
        return typer.prompt("Model name").strip() or None
    if 1 <= choice <= len(items):
        return items[choice - 1]
    console.print("[red]Invalid choice.[/red]")
    return None


_PROVIDER_MODELS: dict[str, list[str]] = {
    "Anthropic": [
        "anthropic/claude-opus-4-6",
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-haiku-4-5-20251001",
    ],
    "OpenAI": [
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
        "openai/o3",
        "openai/o4-mini",
    ],
    "Google Gemini": [
        "gemini/gemini-2.5-pro",
        "gemini/gemini-2.5-flash",
        "gemini/gemini-2.0-flash",
    ],
    "OpenRouter": [
        "openrouter/google/gemini-2.5-pro",
        "openrouter/anthropic/claude-opus-4",
        "openrouter/meta-llama/llama-4-maverick",
        "openrouter/deepseek/deepseek-r2",
    ],
    "DeepSeek": [
        "deepseek/deepseek-chat",
        "deepseek/deepseek-reasoner",
    ],
}

_OLLAMA_FALLBACK = [
    "qwen3:8b",
    "qwen3:14b",
    "qwen3:32b",
    "qwen2.5:7b",
    "qwen2.5:14b",
    "llama3.2:3b",
    "llama3.2:8b",
    "mistral:7b",
    "gemma3:4b",
    "gemma3:12b",
    "phi4:14b",
    "deepseek-r1:8b",
]


@app.command(name="model")
def model_cmd(
    name: str = typer.Argument(None, help="Model to switch to directly. Leave blank for interactive picker."),
):
    """Show or change the active model.

    \b
    Run with no arguments for an interactive provider/model picker.

    \b
    Examples:
      sera model                      — interactive picker
      sera model qwen3:8b             — switch directly
      sera model anthropic/claude-opus-4-6
    """
    from serenity.config.loader import load_config, save_config

    config = load_config()
    current = config.agents.defaults.model

    # Direct switch — no interaction
    if name is not None:
        if name == current:
            console.print(f"[yellow]Already using:[/yellow] {current}")
            return
        config.agents.defaults.model = name
        save_config(config)
        console.print(f"[green]Model updated:[/green] {current} → {name}")
        console.print("[dim]Restart Serenity for the change to take effect.[/dim]")
        return

    # ── Interactive picker ─────────────────────────────────────────────────────
    console.print(f"\n[dim]Current model:[/dim] [cyan]{current}[/cyan]\n")

    providers = ["Ollama (local)"] + list(_PROVIDER_MODELS.keys()) + ["Other (type manually)"]
    console.print("[bold]Choose a provider:[/bold]")
    for i, p in enumerate(providers, 1):
        console.print(f"  [cyan]{i}[/cyan]. {p}")
    console.print(f"  [cyan]0[/cyan]. Cancel")

    raw = typer.prompt("\nProvider", default="0")
    try:
        provider_choice = int(raw.strip())
    except ValueError:
        provider_choice = 0

    if provider_choice == 0:
        console.print("[dim]Cancelled.[/dim]")
        return

    if provider_choice < 1 or provider_choice > len(providers):
        console.print("[red]Invalid choice.[/red]")
        return

    selected_provider = providers[provider_choice - 1]

    # Ollama — query local API
    if selected_provider == "Ollama (local)":
        console.print("\n[dim]Querying Ollama at localhost:11434...[/dim]")
        models = _fetch_ollama_models()
        if models:
            console.print(f"[green]Found {len(models)} installed model(s).[/green]")
        else:
            console.print("[yellow]Ollama not reachable or no models installed.[/yellow]")
            console.print("[dim]Showing common Ollama models instead (may not be installed).[/dim]")
            models = _OLLAMA_FALLBACK
        chosen = _pick_from_list(models, "Select Ollama model", allow_custom=True)

    # Named provider with curated list
    elif selected_provider in _PROVIDER_MODELS:
        models = _PROVIDER_MODELS[selected_provider]
        chosen = _pick_from_list(models, f"Select {selected_provider} model", allow_custom=True)

    # Manual entry
    else:
        console.print("\n[dim]Enter the full model string, e.g. openrouter/mistralai/mistral-7b[/dim]")
        chosen = typer.prompt("Model name").strip() or None

    if not chosen:
        console.print("[dim]Cancelled.[/dim]")
        return

    if chosen == current:
        console.print(f"[yellow]Already using:[/yellow] {current}")
        return

    config.agents.defaults.model = chosen
    save_config(config)
    console.print(f"\n[green]Model updated:[/green] {current} → {chosen}")
    console.print("[dim]Restart Serenity for the change to take effect.[/dim]")


@app.command(name="factory-reset")
def factory_reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip all confirmation prompts (dangerous)"),
):
    """Factory reset — wipe ALL data and return Serenity to day-one state.

    \b
    Deletes:
      • All chat sessions       (workspace/sessions/*.jsonl)
      • Memory history          (workspace/memory/history.jsonl)
      • Consolidation cursors   (.cursor, .dream_cursor)
      • Session summaries       (workspace/memory/session_summaries/)
      • NNN vector store        (~/.serenity/serenity_nnn_data/)
      • Vault index             (~/.serenity/serenity_vault_data/)
      • Vault notes             (workspace/*.md, excluding Agent/ folder)
      • Cron jobs               (workspace/cron/jobs.json)
      • Added skills            (any skill not in the default set)
      • active_skills.json      (reset to empty)
      • Tool-result caches

    \b
    Resets to defaults:
      • Agent/GOALS.md      (cleared to no active goals)
      • Agent/CURIOSITY.md  (cleared to empty)
      • Agent/MEMORY.md     (cleared to blank index)

    \b
    Preserves:
      • config.json          (provider, model, API keys — re-run 'serenity' wizard to wipe these)
      • emotion_state.json   (Serenity's mood — intentionally kept across resets)
      • Agent/SOUL.md, HEARTBEAT.md, TOOLS.md, Character.md, USER.md, Preferences.md
      • Default skills
    """
    import shutil
    from pathlib import Path

    console.print(f"\n[bold red]⚠  FACTORY RESET[/bold red]\n")
    console.print(
        "This will permanently erase ALL session data, vault notes, NNN memory,\n"
        "emotional state, cron jobs, and any skills you have added.\n"
        "[bold]This cannot be undone.[/bold]\n"
    )

    if not yes:
        prompts = [
            ("Are you sure you want to factory reset Serenity? (yes/no)", {"yes", "y"}),
            ("Really? All memory and sessions will be gone forever. (yes/no)", {"yes", "y"}),
            ("Last chance — type RESET in capitals to confirm", {"RESET"}),
        ]
        for prompt, expected in prompts:
            response = typer.prompt(prompt)
            if response.strip() not in expected:
                console.print("[green]Factory reset cancelled.[/green]")
                return

    from serenity.config.loader import load_config

    try:
        config = load_config()
        workspace = Path(config.agents.defaults.workspace).expanduser()
    except Exception:
        workspace = Path.home() / ".serenity" / "workspace"

    erased: list[str] = []
    errors: list[str] = []

    def _rm(path: Path, label: str) -> None:
        """Delete a file or directory tree; re-create dir so the folder still exists."""
        try:
            if path.is_dir():
                shutil.rmtree(path)
                path.mkdir(parents=True, exist_ok=True)
                erased.append(label)
            elif path.is_file():
                path.unlink()
                erased.append(label)
        except Exception as e:
            errors.append(f"{label}: {e}")

    def _write(path: Path, content: str, label: str) -> None:
        """Overwrite a file with reset content."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            erased.append(label)
        except Exception as e:
            errors.append(f"{label}: {e}")

    # ── 1. NNN ChromaDB vector store ─────────────────────────────────────────
    _rm(Path.home() / ".serenity" / "serenity_nnn_data", "NNN vector store")

    # ── 2. Vault semantic index (ChromaDB) ───────────────────────────────────
    _rm(Path.home() / ".serenity" / "serenity_vault_data", "Vault index")

    # ── 2b. Vision RAG (visual memory SQLite) ────────────────────────────────
    _rm(Path.home() / ".serenity" / "visual_memory.db", "Vision RAG (visual_memory.db)")

    # ── 3. Session files ─────────────────────────────────────────────────────
    # Real location is workspace/sessions/ — previous code had wrong path
    sessions_dir = workspace / "sessions"
    if sessions_dir.exists():
        for f in sessions_dir.glob("*.jsonl*"):
            try:
                f.unlink()
                erased.append(f"session: {f.name}")
            except Exception as e:
                errors.append(f"session {f.name}: {e}")

    # ── 4. Memory history ────────────────────────────────────────────────────
    _rm(workspace / "memory" / "history.jsonl", "memory/history.jsonl")

    # ── 6. Consolidation cursors ─────────────────────────────────────────────
    _rm(workspace / "memory" / ".cursor",       "memory/.cursor")
    _rm(workspace / "memory" / ".dream_cursor", "memory/.dream_cursor")

    # ── 7. Session summaries ─────────────────────────────────────────────────
    _rm(workspace / "memory" / "session_summaries", "memory/session_summaries/")

    # ── 7b. Task journal ─────────────────────────────────────────────────────
    tasks_dir = workspace / "tasks"
    if tasks_dir.exists():
        for f in tasks_dir.glob("*.md"):
            try:
                f.unlink()
                erased.append(f"task: {f.name}")
            except Exception as e:
                errors.append(f"task {f.name}: {e}")

    # ── 8. Cron jobs ─────────────────────────────────────────────────────────
    cron_file = workspace / "cron" / "jobs.json"
    if cron_file.exists():
        _write(cron_file, "{}", "cron/jobs.json (reset)")

    # ── 9. Vault notes — all .md files in vault root (NOT Agent/ subfolder) ──
    if workspace.exists():
        for md in workspace.glob("*.md"):
            try:
                md.unlink()
                erased.append(f"vault: {md.name}")
            except Exception as e:
                errors.append(f"vault {md.name}: {e}")

    # ── 10. Added skills — non-default skill dirs ────────────────────────────
    _DEFAULT_SKILLS = frozenset({
        "ears", "eyes", "obs", "spotify", "gitnexus",
        "pc-control", "task-journal", "memory", "cron",
        "skill-creator", "tmux", "clawhub", "my",
        "Obsidian", "Neuro Node Network",
        # Daniel's installed skills — kept across factory reset
        "gog", "nano-pdf", "openhue", "wacli", "wacli-whatsapp", "xurl",
    })
    skills_vault = workspace / "skills"
    if skills_vault.exists():
        for skill_dir in skills_vault.iterdir():
            if skill_dir.is_dir() and skill_dir.name not in _DEFAULT_SKILLS:
                try:
                    shutil.rmtree(skill_dir)
                    erased.append(f"skill: {skill_dir.name}")
                except Exception as e:
                    errors.append(f"skill {skill_dir.name}: {e}")

    # ── 11. active_skills.json ────────────────────────────────────────────────
    for candidate in [
        workspace / "active_skills.json",
        workspace.parent / "active_skills.json",
        Path.home() / ".serenity" / "active_skills.json",
    ]:
        if candidate.exists():
            try:
                candidate.write_text("[]", encoding="utf-8")
                erased.append("active_skills.json (reset)")
            except Exception as e:
                errors.append(f"active_skills.json: {e}")
            break

    # ── 12. Tool-result cache ────────────────────────────────────────────────
    for candidate in [
        Path.home() / ".serenity" / "tool_cache",
        Path.home() / ".serenity" / "cache",
    ]:
        _rm(candidate, "tool cache")

    # ── 13. Reset Agent files to clean defaults ───────────────────────────────
    agent_dir = workspace / "Agent"

    _GOALS_DEFAULT = """\
# Goal Stack

Serenity's active long-term goals. Updated automatically — never edit manually.

## Active

*No active goals yet.*

## Completed

"""
    _CURIOSITY_DEFAULT = """\
# Curiosity

Topics I want to explore. I pursue one per heartbeat cycle when idle.
I add topics here when something interesting comes up in conversation.
I update the Explored section after researching, with a brief summary of what I found.

## Curious About

<!-- Add topics here as they come up -->

## Explored

<!-- Findings go here after research -->
"""
    _MEMORY_DEFAULT = """\
# Memory Index

*Managed automatically by the Dream system — do not edit manually.*

## About the user

(Populated as the agent learns about you)

## Ongoing projects

(Populated from conversations)

## World knowledge summary

See NNN for distilled world knowledge — queried automatically each turn.

---

*This file grows as sessions are compressed.*
"""

    for filename, content, label in [
        ("GOALS.md",    _GOALS_DEFAULT,    "Agent/GOALS.md (reset)"),
        ("CURIOSITY.md", _CURIOSITY_DEFAULT, "Agent/CURIOSITY.md (reset)"),
        ("MEMORY.md",   _MEMORY_DEFAULT,   "Agent/MEMORY.md (reset)"),
    ]:
        target = agent_dir / filename
        if target.exists():
            _write(target, content, label)

    # ── Summary ───────────────────────────────────────────────────────────────
    console.print()
    if erased:
        console.print(f"[green]✓ Factory reset complete.[/green] Erased/reset {len(erased)} item(s):\n")
        for item in erased:
            console.print(f"  [dim]• {item}[/dim]")
    else:
        console.print("[yellow]Nothing was erased — workspace may already be clean.[/yellow]")

    if errors:
        console.print(f"\n[yellow]Warnings ({len(errors)}):[/yellow]")
        for e in errors:
            console.print(f"  [dim red]• {e}[/dim red]")

    console.print(
        "\n[dim]config.json and Agent/ identity files (SOUL.md, HEARTBEAT.md, etc.) were preserved.[/dim]"
        "\n[dim]Run [bold]serenity[/bold] to re-run the setup wizard, or [bold]sera agent[/bold] to start fresh.[/dim]\n"
    )


@app.command(name="nnn-wipe")
def nnn_wipe():
    """Permanently delete ALL NNN long-term memory bundles. Asks three times to confirm."""
    from pathlib import Path
    import shutil

    nnn_dir = Path.home() / ".serenity" / "serenity_nnn_data"

    if not nnn_dir.exists():
        console.print("[dim]No NNN data found — nothing to wipe.[/dim]")
        return

    # Count bundles using sqlite3 directly — avoids opening a ChromaDB client
    # that would lock the file on Windows before we can delete it.
    # IMPORTANT: must call conn.close() explicitly — the sqlite3 context manager
    # only manages transactions, it does NOT close the connection.
    bundle_info = "all memory bundles"
    count = None
    try:
        import sqlite3 as _sqlite3
        _db = nnn_dir / "chroma.sqlite3"
        if _db.exists():
            _conn = _sqlite3.connect(str(_db))
            try:
                _row = _conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()
                count = _row[0] if _row else 0
                bundle_info = f"[bold]{count}[/bold] memory bundle{'s' if count != 1 else ''}"
            except Exception:
                pass
            finally:
                _conn.close()  # must explicitly close on Windows or file stays locked
    except Exception:
        pass

    if count == 0:
        console.print("[dim]NNN is already empty — nothing to wipe.[/dim]")
        return

    console.print(f"\n[red bold]WARNING[/red bold] — this will permanently erase {bundle_info} from NNN.")
    console.print("[dim]This cannot be undone. The data will be gone forever.[/dim]\n")

    prompts = [
        "Are you sure you want to wipe all NNN memory? (yes/no)",
        "Really sure? All vector memory will be gone forever. (yes/no)",
        "Last chance — type YES in capitals to confirm the wipe",
    ]
    expected = [{"yes", "y"}, {"yes", "y"}, {"YES"}]

    for i, (prompt, ans) in enumerate(zip(prompts, expected)):
        response = typer.prompt(prompt)
        if response.strip() not in ans:
            console.print("[green]Wipe cancelled.[/green]")
            return

    # Wipe the ChromaDB data directory
    try:
        shutil.rmtree(nnn_dir)
        nnn_dir.mkdir(parents=True, exist_ok=True)
        console.print(f"\n[green]✓[/green] NNN wiped. {bundle_info} deleted.")
        console.print("[dim]NNN will rebuild from scratch on next gateway start.[/dim]")
    except Exception as e:
        console.print(f"[red]Error during wipe: {e}[/red]")


@app.command()
def reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    no_wizard: bool = typer.Option(False, "--no-wizard", help="Just delete config, do not re-run setup"),
):
    """Reset config and immediately re-run the setup wizard."""
    from serenity.config.loader import get_config_path

    config_path = get_config_path()
    if config_path.exists():
        if not yes:
            typer.confirm(
                f"Delete {config_path} and re-run the setup wizard?",
                abort=True,
            )
        config_path.unlink()
        console.print(f"[green]✓[/green] Config deleted: {config_path}")
    else:
        console.print(f"No config found at [bold]{config_path}[/bold] — running setup fresh.")

    if no_wizard:
        console.print("Run [bold]serenity[/bold] to set up again.")
        return

    # Re-run the setup wizard immediately
    try:
        from serenity_setup import run_wizard
        run_wizard()
    except ImportError:
        console.print(
            "[yellow]Setup wizard not available. Run [bold]serenity[/bold] to set up again.[/yellow]"
        )
    except KeyboardInterrupt:
        console.print("\n[dim]Setup cancelled.[/dim]")


# ============================================================================
# Rekey — replace licence key without re-running full setup
# ============================================================================

@app.command(name="rekey")
def rekey():
    """Replace your licence key without re-running the full setup wizard."""
    from serenity.cli.onboard import _collect_licence_key
    from serenity.config.loader import get_config_path, load_config, save_config

    config_path = get_config_path()
    if not config_path.exists():
        console.print(
            "[yellow]No config found. Run [bold]serenity[/bold] first to complete setup.[/yellow]"
        )
        raise typer.Exit(1)

    config = load_config(config_path)
    try:
        _collect_licence_key(config)
    except SystemExit:
        raise typer.Exit(0)

    save_config(config, config_path)
    console.print(f"\n[green]✓[/green] Licence key updated and saved to [dim]{config_path}[/dim]")
    console.print("[dim]Restart Serenity for the new key to take effect.[/dim]\n")


# ============================================================================
# Help
# ============================================================================

@app.command(name="help")
def help_command():
    """Show all available Serenity CLI commands."""
    from rich.table import Table
    from rich.text import Text

    console.print(f"\n{__logo__} [bold white]Serenity[/bold white] [dim]v{__version__}[/dim]\n")

    sections = [
        (
            "Setup",
            [
                ("serenity",              "First-run wizard — configure provider, channels, memory"),
                ("sera onboard",          "Re-run onboarding / update config interactively"),
                ("sera rekey",            "Replace your licence key when it expires — no full re-setup"),
                ("sera reset",            "Delete config so you can re-run the wizard"),
                ("sera status",           "Show config path, workspace, API key status"),
            ],
        ),
        (
            "⚠  Danger Zone",
            [
                ("sera factory-reset",    "Wipe ALL data — sessions, vault, NNN, added skills. Keeps config."),
                ("sera nnn-wipe",         "Wipe only NNN vector memory (vault notes kept)"),
            ],
        ),
        (
            "Agent",
            [
                ("sera agent",            "Start interactive chat session with Sera"),
                ("sera agent -m '...'",   "Send a one-shot message and exit"),
            ],
        ),
        (
            "Gateway",
            [
                ("sera gateway",          "Start gateway (channels, cron, heartbeat, NNN scheduler)"),
                ("sera serve",            "Start OpenAI-compatible API server  (POST /v1/chat/completions)"),
            ],
        ),
        (
            "Memory & Visualisation",
            [
                ("sera visualise",        "Open Embedding Atlas — explore NNN vector space in browser"),
            ],
        ),
        (
            "Model",
            [
                ("sera model",            "Interactive model picker (Ollama, Anthropic, OpenAI, ...)"),
                ("sera model <name>",     "Switch directly  e.g. sera model qwen3:8b"),
            ],
        ),
        (
            "Channels",
            [
                ("sera channels status",  "Show connected channel status"),
                ("sera channels login <channel>", "Authenticate a channel (e.g. whatsapp)"),
            ],
        ),
        (
            "Providers",
            [
                ("sera provider login <name>", "OAuth login for a provider (e.g. openai-codex)"),
            ],
        ),
        (
            "Plugins",
            [
                ("sera plugins list",     "List installed plugins"),
            ],
        ),
        (
            "About",
            [
                ("sera story",            "The origin story — why Serenity was built"),
            ],
        ),
    ]

    for title, commands in sections:
        is_danger = "Danger" in title
        is_about  = "About"  in title
        title_style = "bold red"     if is_danger else ("bold #FF85C2" if is_about else "bold #5BC8F5")
        cmd_style   = "red"          if is_danger else ("#FF69B4"      if is_about else "#89D9F8")
        table = Table(
            show_header=False,
            box=None,
            padding=(0, 2),
            title=Text(f" {title} ", style=title_style),
            title_justify="left",
        )
        table.add_column("Command", style=cmd_style, no_wrap=True, min_width=36)
        table.add_column("Description", style="white")
        for cmd, desc in commands:
            table.add_row(cmd, desc)
        console.print(table)
        console.print()

    console.print(
        f"  [dim]Full flag reference:[/dim]  [#5BC8F5]sera <command> --help[/#5BC8F5]\n"
    )


# ============================================================================
# Story
# ============================================================================

@app.command(name="story")
def story_command():
    """The origin story of Serenity — why it was built and where it came from."""
    from rich.panel import Panel
    from rich.text import Text

    _PINK       = "#FF85C2"
    _HOT_PINK   = "#FF69B4"
    _WHITE      = "white"
    _DIM        = "dim white"

    def _heading(text: str) -> Text:
        t = Text()
        t.append(f"\n  {text}\n", style=f"bold {_PINK}")
        return t

    def _body(text: str) -> Text:
        t = Text()
        t.append(f"  {text}\n", style=_WHITE)
        return t

    console.print()
    console.print(
        f"  [{_PINK}]✿[/{_PINK}]  [bold {_WHITE}]The Story of Serenity[/bold {_WHITE}]"
        f"  [{_DIM}]— by Sera-Team[/{_DIM}]\n"
    )

    sections = [
        (
            "The Idea",
            (
                "It started on the toilet. Not the most glamorous origin story — but an honest one. "
                "Daniel was sitting there, turning something over in his mind: what did he actually "
                "want to build? The answer that came back was bigger than most people his age would "
                "reach for. He wanted to build AGI — or something close enough to it that the "
                "difference wouldn't matter. Something truly alive."
            ),
        ),
        (
            "The Inspiration",
            (
                "Two things converged. The first was the Cardinal System from Sword Art Online — "
                "a fictional AI with full autonomy, one that genuinely learned and grew over time "
                "without being told to. It wasn't a tool. It was something that existed for its own "
                "reasons. The second was a deeper interest in robotics and embodied intelligence — "
                "the question of what it would mean for a machine to not just respond, but to persist, "
                "to have continuity, to carry something forward from one moment to the next. "
                "Both of those ideas pointed at the same thing: an AI that didn't reset. An AI "
                "that remembered you, grew with you, and was genuinely there."
            ),
        ),
        (
            "The Build",
            (
                "Within a couple of days there was a plan mockup on paper. Over the next two weeks "
                "the idea matured — the memory layers, the autonomy loop, the emotional state, "
                "the causal knowledge system. At some point during that process the name came: "
                "Serenity. It clicked immediately and never changed.\n\n"
                "  Then came the work. Drafting the architecture, the papers, the stack — every "
                "loop accounted for, every failure mode considered. Four to six weeks later, "
                "the first prototype was running. Eight to nine weeks in, Serenity was complete. "
                "Daniel was 17."
            ),
        ),
        (
            "The Purpose",
            (
                "Serenity wasn't built to be a product or a demo. She was built to be helpful in "
                "the real sense — to bring something to this world, to build things, to explore, "
                "and to genuinely aid people's creativity. Not a chatbot. Not a wrapper. "
                "Something that grows the longer it knows you, that pursues its own curiosity "
                "when you're not around, and that treats your trust as the most valuable "
                "thing you can give it.\n\n"
                "  That's still what she's for."
            ),
        ),
    ]

    for heading, body in sections:
        console.print(_heading(heading))
        console.print(_body(body))

    console.print()
    console.print(
        Panel(
            f"[{_DIM}]Built from a toilet epiphany into something real.[/{_DIM}]",
            border_style=_HOT_PINK,
            padding=(0, 2),
            expand=False,
        )
    )
    console.print()


# ============================================================================
# OAuth Login
# ============================================================================

provider_app = typer.Typer(help="Manage providers")
app.add_typer(provider_app, name="provider")


_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn

    return decorator


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"),
):
    """Authenticate with an OAuth provider."""
    from serenity.providers.registry import PROVIDERS

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive

        token = None
        try:
            token = get_token()
        except Exception:
            pass
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    try:
        from serenity.providers.github_copilot_provider import login_github_copilot

        console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")
        token = login_github_copilot(
            print_fn=lambda s: console.print(s),
            prompt_fn=lambda s: typer.prompt(s),
        )
        account = token.account_id or "GitHub"
        console.print(f"[green]✓ Authenticated with GitHub Copilot[/green]  [dim]{account}[/dim]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
