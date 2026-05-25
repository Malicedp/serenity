"""Interactive onboarding questionnaire for serenity."""

import json
import types
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, NamedTuple, get_args, get_origin

try:
    import questionary
except ModuleNotFoundError:  # pragma: no cover - exercised in environments without wizard deps
    questionary = None
from loguru import logger
from pydantic import BaseModel
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from serenity.cli.models import (
    format_token_count,
    get_model_context_limit,
    get_model_suggestions,
)
from serenity.config.loader import get_config_path, load_config
from serenity.config.schema import Config

console = Console()


@dataclass
class OnboardResult:
    """Result of an onboarding session."""

    config: Config
    should_save: bool

# --- Field Hints for Select Fields ---
# Maps field names to (choices, hint_text)
# To add a new select field with hints, add an entry:
#   "field_name": (["choice1", "choice2", ...], "hint text for the field")
_SELECT_FIELD_HINTS: dict[str, tuple[list[str], str]] = {
    "reasoning_effort": (
        ["low", "medium", "high"],
        "low / medium / high - enables LLM thinking mode",
    ),
}

# --- Key Bindings for Navigation ---

_BACK_PRESSED = object()  # Sentinel value for back navigation


def _get_questionary():
    """Return questionary or raise a clear error when wizard deps are unavailable."""
    if questionary is None:
        raise RuntimeError(
            "Interactive onboarding requires the optional 'questionary' dependency. "
            "Install project dependencies and rerun with --wizard."
        )
    return questionary


def _select_with_back(
    prompt: str, choices: list[str], default: str | None = None
) -> str | None | object:
    """Select with Escape/Left arrow support for going back.

    Args:
        prompt: The prompt text to display.
        choices: List of choices to select from. Must not be empty.
        default: The default choice to pre-select. If not in choices, first item is used.

    Returns:
        _BACK_PRESSED sentinel if user pressed Escape or Left arrow
        The selected choice string if user confirmed
        None if user cancelled (Ctrl+C)
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.styles import Style

    # Validate choices
    if not choices:
        logger.warning("Empty choices list provided to _select_with_back")
        return None

    # Find default index
    selected_index = 0
    if default and default in choices:
        selected_index = choices.index(default)

    # State holder for the result
    state: dict[str, str | None | object] = {"result": None}

    # Build menu items (uses closure over selected_index)
    def get_menu_text():
        items = []
        for i, choice in enumerate(choices):
            if i == selected_index:
                items.append(("class:selected", f"> {choice}\n"))
            else:
                items.append(("", f"  {choice}\n"))
        return items

    # Create layout
    menu_control = FormattedTextControl(get_menu_text)
    menu_window = Window(content=menu_control, height=len(choices))

    prompt_control = FormattedTextControl(lambda: [("class:question", f"> {prompt}")])
    prompt_window = Window(content=prompt_control, height=1)

    layout = Layout(HSplit([prompt_window, menu_window]))

    # Key bindings
    bindings = KeyBindings()

    @bindings.add(Keys.Up)
    def _up(event):
        nonlocal selected_index
        selected_index = (selected_index - 1) % len(choices)
        event.app.invalidate()

    @bindings.add(Keys.Down)
    def _down(event):
        nonlocal selected_index
        selected_index = (selected_index + 1) % len(choices)
        event.app.invalidate()

    @bindings.add(Keys.Enter)
    def _enter(event):
        state["result"] = choices[selected_index]
        event.app.exit()

    @bindings.add("escape")
    def _escape(event):
        state["result"] = _BACK_PRESSED
        event.app.exit()

    @bindings.add(Keys.Left)
    def _left(event):
        state["result"] = _BACK_PRESSED
        event.app.exit()

    @bindings.add(Keys.ControlC)
    def _ctrl_c(event):
        state["result"] = None
        event.app.exit()

    # Style
    style = Style.from_dict({
        "selected": "fg:green bold",
        "question": "fg:cyan",
    })

    app = Application(layout=layout, key_bindings=bindings, style=style)
    try:
        app.run()
    except Exception:
        logger.exception("Error in select prompt")
        return None

    return state["result"]

# --- Type Introspection ---


class FieldTypeInfo(NamedTuple):
    """Result of field type introspection."""

    type_name: str
    inner_type: Any


def _get_field_type_info(field_info) -> FieldTypeInfo:
    """Extract field type info from Pydantic field."""
    annotation = field_info.annotation
    if annotation is None:
        return FieldTypeInfo("str", None)

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is types.UnionType:
        non_none_args = [a for a in args if a is not type(None)]
        if len(non_none_args) == 1:
            annotation = non_none_args[0]
            origin = get_origin(annotation)
            args = get_args(annotation)

    _SIMPLE_TYPES: dict[type, str] = {bool: "bool", int: "int", float: "float"}

    if origin is list or (hasattr(origin, "__name__") and origin.__name__ == "List"):
        return FieldTypeInfo("list", args[0] if args else str)
    if origin is dict or (hasattr(origin, "__name__") and origin.__name__ == "Dict"):
        return FieldTypeInfo("dict", None)
    for py_type, name in _SIMPLE_TYPES.items():
        if annotation is py_type:
            return FieldTypeInfo(name, None)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return FieldTypeInfo("model", annotation)
    return FieldTypeInfo("str", None)


def _get_field_display_name(field_key: str, field_info) -> str:
    """Get display name for a field."""
    if field_info and field_info.description:
        return field_info.description
    name = field_key
    suffix_map = {
        "_s": " (seconds)",
        "_ms": " (ms)",
        "_url": " URL",
        "_path": " Path",
        "_id": " ID",
        "_key": " Key",
        "_token": " Token",
    }
    for suffix, replacement in suffix_map.items():
        if name.endswith(suffix):
            name = name[: -len(suffix)] + replacement
            break
    return name.replace("_", " ").title()


# --- Sensitive Field Masking ---

_SENSITIVE_KEYWORDS = frozenset({"api_key", "token", "secret", "password", "credentials"})


def _is_sensitive_field(field_name: str) -> bool:
    """Check if a field name indicates sensitive content."""
    return any(kw in field_name.lower() for kw in _SENSITIVE_KEYWORDS)


def _mask_value(value: str) -> str:
    """Mask a sensitive value, showing only the last 4 characters."""
    if len(value) <= 4:
        return "****"
    return "*" * (len(value) - 4) + value[-4:]


# --- Value Formatting ---


def _format_value(value: Any, rich: bool = True, field_name: str = "") -> str:
    """Single recursive entry point for safe value display. Handles any depth."""
    if value is None or value == "" or value == {} or value == []:
        return "[dim]not set[/dim]" if rich else "[not set]"
    if _is_sensitive_field(field_name) and isinstance(value, str):
        masked = _mask_value(value)
        return f"[dim]{masked}[/dim]" if rich else masked
    if isinstance(value, BaseModel):
        parts = []
        for fname, _finfo in type(value).model_fields.items():
            fval = getattr(value, fname, None)
            formatted = _format_value(fval, rich=False, field_name=fname)
            if formatted != "[not set]":
                parts.append(f"{fname}={formatted}")
        return ", ".join(parts) if parts else ("[dim]not set[/dim]" if rich else "[not set]")
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value)
    return str(value)


def _format_value_for_input(value: Any, field_type: str) -> str:
    """Format a value for use as input default."""
    if value is None or value == "":
        return ""
    if field_type == "list" and isinstance(value, list):
        return ",".join(str(v) for v in value)
    if field_type == "dict" and isinstance(value, dict):
        return json.dumps(value)
    return str(value)


# --- Rich UI Components ---


def _show_config_panel(display_name: str, model: BaseModel, fields: list) -> None:
    """Display current configuration as a rich table."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Field", style="cyan")
    table.add_column("Value")

    for fname, field_info in fields:
        value = getattr(model, fname, None)
        display = _get_field_display_name(fname, field_info)
        formatted = _format_value(value, rich=True, field_name=fname)
        table.add_row(display, formatted)

    console.print(Panel(table, title=f"[bold]{display_name}[/bold]", border_style="blue"))


def _show_main_menu_header() -> None:
    """Display the main menu header."""
    from serenity import __logo__, __version__

    console.print()
    # Use Align.CENTER for the single line of text
    from rich.align import Align

    console.print(
        Align.center(f"{__logo__} [bold cyan]serenity[{__version__}][/bold cyan]")
    )
    console.print()


def _show_section_header(title: str, subtitle: str = "") -> None:
    """Display a section header."""
    console.print()
    if subtitle:
        console.print(
            Panel(f"[dim]{subtitle}[/dim]", title=f"[bold]{title}[/bold]", border_style="blue")
        )
    else:
        console.print(Panel("", title=f"[bold]{title}[/bold]", border_style="blue"))


# --- Input Handlers ---


def _input_bool(display_name: str, current: bool | None) -> bool | None:
    """Get boolean input via confirm dialog."""
    return _get_questionary().confirm(
        display_name,
        default=bool(current) if current is not None else False,
    ).ask()


def _input_text(display_name: str, current: Any, field_type: str) -> Any:
    """Get text input and parse based on field type."""
    default = _format_value_for_input(current, field_type)

    value = _get_questionary().text(f"{display_name}:", default=default).ask()

    if value is None or value == "":
        return None

    if field_type == "int":
        try:
            return int(value)
        except ValueError:
            console.print("[yellow]! Invalid number format, value not saved[/yellow]")
            return None
    elif field_type == "float":
        try:
            return float(value)
        except ValueError:
            console.print("[yellow]! Invalid number format, value not saved[/yellow]")
            return None
    elif field_type == "list":
        return [v.strip() for v in value.split(",") if v.strip()]
    elif field_type == "dict":
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            console.print("[yellow]! Invalid JSON format, value not saved[/yellow]")
            return None

    return value


def _input_with_existing(
    display_name: str, current: Any, field_type: str
) -> Any:
    """Handle input with 'keep existing' option for non-empty values."""
    has_existing = current is not None and current != "" and current != {} and current != []

    if has_existing and not isinstance(current, list):
        choice = _get_questionary().select(
            display_name,
            choices=["Enter new value", "Keep existing value"],
            default="Keep existing value",
        ).ask()
        if choice == "Keep existing value" or choice is None:
            return None

    return _input_text(display_name, current, field_type)


# --- Pydantic Model Configuration ---


def _get_current_provider(model: BaseModel) -> str:
    """Get the current provider setting from a model (if available)."""
    if hasattr(model, "provider"):
        return getattr(model, "provider", "auto") or "auto"
    return "auto"


def _input_model_with_autocomplete(
    display_name: str, current: Any, provider: str
) -> str | None:
    """Get model input with autocomplete suggestions.

    """
    from prompt_toolkit.completion import Completer, Completion

    default = str(current) if current else ""

    class DynamicModelCompleter(Completer):
        """Completer that dynamically fetches model suggestions."""

        def __init__(self, provider_name: str):
            self.provider = provider_name

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            suggestions = get_model_suggestions(text, provider=self.provider, limit=50)
            for model in suggestions:
                # Skip if model doesn't contain the typed text
                if text.lower() not in model.lower():
                    continue
                yield Completion(
                    model,
                    start_position=-len(text),
                    display=model,
                )

    value = _get_questionary().autocomplete(
        f"{display_name}:",
        choices=[""],  # Placeholder, actual completions from completer
        completer=DynamicModelCompleter(provider),
        default=default,
        qmark=">",
    ).ask()

    return value if value else None


def _input_context_window_with_recommendation(
    display_name: str, current: Any, model_obj: BaseModel
) -> int | None:
    """Get context window input with option to fetch recommended value."""
    current_val = current if current else ""

    choices = ["Enter new value"]
    if current_val:
        choices.append("Keep existing value")
    choices.append("[?] Get recommended value")

    choice = _get_questionary().select(
        display_name,
        choices=choices,
        default="Enter new value",
    ).ask()

    if choice is None:
        return None

    if choice == "Keep existing value":
        return None

    if choice == "[?] Get recommended value":
        # Get the model name from the model object
        model_name = getattr(model_obj, "model", None)
        if not model_name:
            console.print("[yellow]! Please configure the model field first[/yellow]")
            return None

        provider = _get_current_provider(model_obj)
        context_limit = get_model_context_limit(model_name, provider)

        if context_limit:
            console.print(f"[green]+ Recommended context window: {format_token_count(context_limit)} tokens[/green]")
            return context_limit
        else:
            console.print("[yellow]! Could not fetch model info, please enter manually[/yellow]")
            # Fall through to manual input

    # Manual input
    value = _get_questionary().text(
        f"{display_name}:",
        default=str(current_val) if current_val else "",
    ).ask()

    if value is None or value == "":
        return None

    try:
        return int(value)
    except ValueError:
        console.print("[yellow]! Invalid number format, value not saved[/yellow]")
        return None


def _fetch_ollama_models_local() -> list[str]:
    """Query Ollama at localhost:11434/api/tags. Returns model names or [] on failure."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as resp:
            data = json.loads(resp.read())
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def _input_ollama_model(field_display: str, current_value: Any) -> str | None:
    """Pick an Ollama model: show installed models from local API, fall back to text input."""
    q = _get_questionary()
    console.print("\n[dim]Scanning Ollama at localhost:11434...[/dim]")
    models = _fetch_ollama_models_local()

    if models:
        console.print(f"[green]Found {len(models)} installed model(s).[/green]\n")
        choices = models + ["(type manually)"]
        default = current_value if current_value in models else choices[0]
        try:
            choice = q.select(
                f"{field_display}:",
                choices=choices,
                default=default,
                qmark=">",
            ).ask()
        except Exception:
            choice = None

        if choice and choice != "(type manually)":
            return choice
        # fall through to text input
    else:
        console.print(
            "[yellow]Ollama not reachable or no models installed.[/yellow]\n"
            "[dim]Make sure Ollama is running: https://ollama.com[/dim]\n"
        )

    # Manual text input
    try:
        value = q.text(
            f"{field_display} (e.g. qwen3:8b, llama3.2, mistral:7b):",
            default=str(current_value) if current_value else "",
            qmark=">",
        ).ask()
        return value.strip() if value and value.strip() else None
    except Exception:
        return None


def _handle_model_field(
    working_model: BaseModel, field_name: str, field_display: str, current_value: Any
) -> None:
    """Handle the 'model' field.

    For Ollama provider: query localhost:11434 and show a picker of installed models.
    For all other providers: autocomplete text input with suggestions.
    """
    provider = _get_current_provider(working_model)
    if provider == "ollama":
        new_value = _input_ollama_model(field_display, current_value)
    else:
        new_value = _input_model_with_autocomplete(field_display, current_value, provider)
    if new_value is not None and new_value != current_value:
        setattr(working_model, field_name, new_value)
        _try_auto_fill_context_window(working_model, new_value)


def _handle_context_window_field(
    working_model: BaseModel, field_name: str, field_display: str, current_value: Any
) -> None:
    """Handle context_window_tokens with recommendation lookup."""
    new_value = _input_context_window_with_recommendation(
        field_display, current_value, working_model
    )
    if new_value is not None:
        setattr(working_model, field_name, new_value)


_FIELD_HANDLERS: dict[str, Any] = {
    "model": _handle_model_field,
    "context_window_tokens": _handle_context_window_field,
}


def _configure_pydantic_model(
    model: BaseModel,
    display_name: str,
    *,
    skip_fields: set[str] | None = None,
) -> BaseModel | None:
    """Configure a Pydantic model interactively.

    Returns the updated model only when the user explicitly selects "Done".
    Back and cancel actions discard the section draft.
    """
    skip_fields = skip_fields or set()
    working_model = model.model_copy(deep=True)

    fields = [
        (name, info)
        for name, info in type(working_model).model_fields.items()
        if name not in skip_fields
    ]
    if not fields:
        console.print(f"[dim]{display_name}: No configurable fields[/dim]")
        return working_model

    def get_choices() -> list[str]:
        items = []
        for fname, finfo in fields:
            value = getattr(working_model, fname, None)
            display = _get_field_display_name(fname, finfo)
            formatted = _format_value(value, rich=False, field_name=fname)
            items.append(f"{display}: {formatted}")
        return items + ["[Done]"]

    while True:
        console.clear()
        _show_config_panel(display_name, working_model, fields)
        choices = get_choices()
        answer = _select_with_back("Select field to configure:", choices)

        if answer is _BACK_PRESSED or answer is None:
            return None
        if answer == "[Done]":
            return working_model

        field_idx = next((i for i, c in enumerate(choices) if c == answer), -1)
        if field_idx < 0 or field_idx >= len(fields):
            return None

        field_name, field_info = fields[field_idx]
        current_value = getattr(working_model, field_name, None)
        ftype = _get_field_type_info(field_info)
        field_display = _get_field_display_name(field_name, field_info)

        # Nested Pydantic model - recurse
        if ftype.type_name == "model":
            nested = current_value
            created = nested is None
            if nested is None and ftype.inner_type:
                nested = ftype.inner_type()
            if nested and isinstance(nested, BaseModel):
                updated = _configure_pydantic_model(nested, field_display)
                if updated is not None:
                    setattr(working_model, field_name, updated)
                elif created:
                    setattr(working_model, field_name, None)
            continue

        # Registered special-field handlers
        handler = _FIELD_HANDLERS.get(field_name)
        if handler:
            handler(working_model, field_name, field_display, current_value)
            continue

        # Select fields with hints (e.g. reasoning_effort)
        if field_name in _SELECT_FIELD_HINTS:
            choices_list, hint = _SELECT_FIELD_HINTS[field_name]
            select_choices = choices_list + ["(clear/unset)"]
            console.print(f"[dim]  Hint: {hint}[/dim]")
            new_value = _select_with_back(
                field_display, select_choices, default=current_value or select_choices[0]
            )
            if new_value is _BACK_PRESSED:
                continue
            if new_value == "(clear/unset)":
                setattr(working_model, field_name, None)
            elif new_value is not None:
                setattr(working_model, field_name, new_value)
            continue

        # Generic field input
        if ftype.type_name == "bool":
            new_value = _input_bool(field_display, current_value)
        else:
            new_value = _input_with_existing(field_display, current_value, ftype.type_name)
        if new_value is not None:
            setattr(working_model, field_name, new_value)


def _try_auto_fill_context_window(model: BaseModel, new_model_name: str) -> None:
    """Try to auto-fill context_window_tokens if it's at default value.

    Note:
        This function imports AgentDefaults from serenity.config.schema to get
        the default context_window_tokens value. If the schema changes, this
        coupling needs to be updated accordingly.
    """
    # Check if context_window_tokens field exists
    if not hasattr(model, "context_window_tokens"):
        return

    current_context = getattr(model, "context_window_tokens", None)

    # Check if current value is the default (65536)
    # We only auto-fill if the user hasn't changed it from default
    from serenity.config.schema import AgentDefaults

    default_context = AgentDefaults.model_fields["context_window_tokens"].default

    if current_context != default_context:
        return  # User has customized it, don't override

    provider = _get_current_provider(model)
    context_limit = get_model_context_limit(new_model_name, provider)

    if context_limit:
        setattr(model, "context_window_tokens", context_limit)
        console.print(f"[green]+ Auto-filled context window: {format_token_count(context_limit)} tokens[/green]")
    else:
        console.print("[dim](i) Could not auto-fill context window (model not in database)[/dim]")


# --- Provider Configuration ---


@lru_cache(maxsize=1)
def _get_provider_info() -> dict[str, tuple[str, bool, bool, str]]:
    """Get provider info from registry (cached)."""
    from serenity.providers.registry import PROVIDERS

    return {
        spec.name: (
            spec.display_name or spec.name,
            spec.is_gateway,
            spec.is_local,
            spec.default_api_base,
        )
        for spec in PROVIDERS
        if not spec.is_oauth
    }


def _get_provider_names() -> dict[str, str]:
    """Get provider display names."""
    info = _get_provider_info()
    return {name: data[0] for name, data in info.items() if name}


def _configure_provider(config: Config, provider_name: str) -> None:
    """Configure a single LLM provider."""
    provider_config = getattr(config.providers, provider_name, None)
    if provider_config is None:
        console.print(f"[red]Unknown provider: {provider_name}[/red]")
        return

    display_name = _get_provider_names().get(provider_name, provider_name)
    info = _get_provider_info()
    default_api_base = info.get(provider_name, (None, None, None, None))[3]

    if default_api_base and not provider_config.api_base:
        provider_config.api_base = default_api_base

    updated_provider = _configure_pydantic_model(
        provider_config,
        display_name,
    )
    if updated_provider is not None:
        setattr(config.providers, provider_name, updated_provider)


def _configure_providers(config: Config) -> None:
    """Configure LLM providers."""

    def get_provider_choices() -> list[str]:
        """Build provider choices with config status indicators."""
        choices = []
        for name, display in _get_provider_names().items():
            provider = getattr(config.providers, name, None)
            if provider and provider.api_key:
                choices.append(f"{display} *")
            else:
                choices.append(display)
        return choices + ["<- Back"]

    while True:
        try:
            console.clear()
            _show_section_header("LLM Providers", "Select a provider to configure API key and endpoint")
            choices = get_provider_choices()
            answer = _select_with_back("Select provider:", choices)

            if answer is _BACK_PRESSED or answer is None or answer == "<- Back":
                break

            # Type guard: answer is now guaranteed to be a string
            assert isinstance(answer, str)
            # Extract provider name from choice (remove " *" suffix if present)
            provider_name = answer.replace(" *", "")
            # Find the actual provider key from display names
            for name, display in _get_provider_names().items():
                if display == provider_name:
                    _configure_provider(config, name)
                    break

        except KeyboardInterrupt:
            console.print("\n[dim]Returning to main menu...[/dim]")
            break


# --- Channel Configuration ---


@lru_cache(maxsize=1)
def _get_channel_info() -> dict[str, tuple[str, type[BaseModel]]]:
    """Get channel info (display name + config class) from channel modules."""
    import importlib

    from serenity.channels.registry import discover_all

    result: dict[str, tuple[str, type[BaseModel]]] = {}
    for name, channel_cls in discover_all().items():
        try:
            mod = importlib.import_module(f"serenity.channels.{name}")
            config_name = channel_cls.__name__.replace("Channel", "Config")
            config_cls = getattr(mod, config_name, None)
            if config_cls and isinstance(config_cls, type) and issubclass(config_cls, BaseModel):
                display_name = getattr(channel_cls, "display_name", name.capitalize())
                result[name] = (display_name, config_cls)
        except Exception:
            logger.warning(f"Failed to load channel module: {name}")
    return result


def _get_channel_names() -> dict[str, str]:
    """Get channel display names."""
    return {name: info[0] for name, info in _get_channel_info().items()}


def _get_channel_config_class(channel: str) -> type[BaseModel] | None:
    """Get channel config class."""
    entry = _get_channel_info().get(channel)
    return entry[1] if entry else None


def _configure_telegram_voice(model: Any, cuda: bool) -> None:
    """Guided voice note setup for the Telegram channel.

    PC voice (wake word → PC speaker) is configured separately in Senses & Voice.
    This covers the Telegram-specific pipeline:
      voice note in  → Whisper STT → agent → TTS → voice note back
    """
    q = _get_questionary()
    console.print("\n[bold]Telegram Voice Notes[/bold]")
    console.print(
        "[dim]When you send Serenity a voice note on Telegram she transcribes it,\n"
        "thinks about it, then sends a voice note back.\n"
        "This is completely separate from the PC speaker / wake word system.[/dim]\n"
    )

    # ── Voice note replies (TTS out) ──────────────────────────────────────────
    try:
        enable_voice = q.confirm(
            "Send voice note replies on Telegram?",
            default=getattr(model, "voice_response", False),
            qmark=">",
        ).ask()
        if enable_voice is not None:
            model.voice_response = enable_voice
    except Exception:
        enable_voice = getattr(model, "voice_response", False)

    if model.voice_response:
        _PROVIDER_CHOICES = [
            "edge-tts      — offline · free · no GPU · 200+ voices  [easiest]",
            "Qwen3 TTS     — local · runs on your GPU · free · ~97 ms",
            "Kokoro        — local · CPU-friendly · fast · no internet",
            "ElevenLabs    — cloud · premium · ultra-realistic",
            "OpenAI TTS    — cloud · standard quality",
        ]
        _PROVIDER_MAP = {
            "edge-tts      — offline · free · no GPU · 200+ voices  [easiest]": "edge-tts",
            "Qwen3 TTS     — local · runs on your GPU · free · ~97 ms": "qwen3-local-1.7b",
            "Kokoro        — local · CPU-friendly · fast · no internet": "kokoro",
            "ElevenLabs    — cloud · premium · ultra-realistic": "elevenlabs",
            "OpenAI TTS    — cloud · standard quality": "openai",
        }
        _current = getattr(model, "tts_provider", "") or "edge-tts"
        _default_choice = next(
            (c for c, v in _PROVIDER_MAP.items() if v == _current),
            _PROVIDER_CHOICES[0],
        )
        try:
            provider_choice = q.select(
                "TTS provider for Telegram voice replies:",
                choices=_PROVIDER_CHOICES,
                default=_default_choice,
                qmark=">",
            ).ask()
            if provider_choice:
                model.tts_provider = _PROVIDER_MAP[provider_choice]
        except Exception:
            pass

        _p = getattr(model, "tts_provider", "edge-tts")

        if _p == "edge-tts":
            _EDGE_VOICES = [
                "en-US-AriaNeural", "en-US-JennyNeural", "en-US-GuyNeural",
                "en-GB-SoniaNeural", "en-GB-RyanNeural",
                "en-AU-NatashaNeural", "en-AU-WilliamNeural",
            ]
            _cur = getattr(model, "tts_voice", "") or "en-GB-SoniaNeural"
            try:
                voice = q.select(
                    "Voice:",
                    choices=_EDGE_VOICES + ["(type manually)"],
                    default=_cur if _cur in _EDGE_VOICES else "en-GB-SoniaNeural",
                    qmark=">",
                ).ask()
                if voice == "(type manually)":
                    voice = q.text("Voice name:", default=_cur, qmark=">").ask()
                if voice and voice != "(type manually)":
                    model.tts_voice = voice.strip()
            except Exception:
                model.tts_voice = "en-GB-SoniaNeural"

        elif _p in ("qwen3-local-0.6b", "qwen3-local-1.7b"):
            _dev = "cuda" if cuda else "cpu"
            model.tts_local_device = _dev
            console.print(f"  [green]✓[/green] Qwen3 local TTS on [bold]{'GPU' if cuda else 'CPU'}[/bold]")

        elif _p == "kokoro":
            _KOKORO_VOICES = ["af_heart", "af_bella", "af_nicole", "am_adam", "am_michael", "bf_emma", "bf_isabella"]
            _cur = getattr(model, "tts_voice", "") or "af_heart"
            try:
                voice = q.select("Voice:", choices=_KOKORO_VOICES,
                                 default=_cur if _cur in _KOKORO_VOICES else "af_heart", qmark=">").ask()
                if voice:
                    model.tts_voice = voice
            except Exception:
                model.tts_voice = "af_heart"

        elif _p == "elevenlabs":
            try:
                key = q.text("ElevenLabs API key:", default=getattr(model, "tts_elevenlabs_api_key", "") or "", qmark=">").ask()
                if key:
                    model.tts_elevenlabs_api_key = key.strip()
                vid = q.text("Voice ID (blank = Rachel):", default=getattr(model, "tts_elevenlabs_voice_id", "") or "", qmark=">").ask()
                model.tts_elevenlabs_voice_id = (vid or "").strip() or "21m00Tcm4TlvDq8ikWAM"
            except Exception:
                pass

        elif _p == "openai":
            try:
                key = q.text("OpenAI API key:", default=getattr(model, "tts_api_key", "") or "", qmark=">").ask()
                if key:
                    model.tts_api_key = key.strip()
            except Exception:
                pass

    # ── Voice note transcription (Whisper STT in) ─────────────────────────────
    console.print()
    console.print("[bold]Voice Note Transcription[/bold]")
    console.print("[dim]Transcribes voice notes you send to Serenity on Telegram.[/dim]\n")
    try:
        enable_stt = q.confirm(
            "Transcribe incoming Telegram voice notes?",
            default=bool(getattr(model, "whisper_model", "")),
            qmark=">",
        ).ask()
    except Exception:
        enable_stt = bool(getattr(model, "whisper_model", ""))

    if enable_stt:
        _dev = "cuda" if cuda else "cpu"
        _compute = "float16" if cuda else "int8"
        model.whisper_model = "small"
        model.whisper_device = _dev
        model.whisper_compute_type = _compute
        console.print(
            f"  [green]✓[/green] Faster Whisper [bold]small[/bold] on "
            f"[bold]{'GPU' if cuda else 'CPU'}[/bold]"
        )
    else:
        model.whisper_model = ""


def _configure_channel(config: Config, channel_name: str) -> None:
    """Configure a single channel."""
    channel_dict = getattr(config.channels, channel_name, None)
    if channel_dict is None:
        channel_dict = {}
        setattr(config.channels, channel_name, channel_dict)

    display_name = _get_channel_names().get(channel_name, channel_name)
    config_cls = _get_channel_config_class(channel_name)

    if config_cls is None:
        console.print(f"[red]No configuration class found for {display_name}[/red]")
        return

    model = config_cls.model_validate(channel_dict) if channel_dict else config_cls()

    # Telegram gets a guided voice note setup before the generic field editor
    if channel_name == "telegram":
        _cuda = False
        try:
            import torch  # type: ignore
            _cuda = torch.cuda.is_available()
        except Exception:
            pass
        _configure_telegram_voice(model, _cuda)
        console.print()

    updated_channel = _configure_pydantic_model(
        model,
        display_name,
    )
    if updated_channel is not None:
        new_dict = updated_channel.model_dump(by_alias=True, exclude_none=True)
        setattr(config.channels, channel_name, new_dict)


def _configure_channels(config: Config) -> None:
    """Configure chat channels."""
    channel_names = list(_get_channel_names().keys())
    choices = channel_names + ["<- Back"]

    while True:
        try:
            console.clear()
            _show_section_header("Chat Channels", "Select a channel to configure connection settings")
            answer = _select_with_back("Select channel:", choices)

            if answer is _BACK_PRESSED or answer is None or answer == "<- Back":
                break

            # Type guard: answer is now guaranteed to be a string
            assert isinstance(answer, str)
            _configure_channel(config, answer)
        except KeyboardInterrupt:
            console.print("\n[dim]Returning to main menu...[/dim]")
            break


# --- General Settings ---

_SETTINGS_SECTIONS: dict[str, tuple[str, str, set[str] | None]] = {
    "Agent Settings": ("Agent Defaults", "Configure default model, temperature, and behavior", None),
    "Gateway": ("Gateway Settings", "Configure server host, port, and heartbeat", None),
    "Tools": ("Tools Settings", "Configure web search, shell exec, and other tools", {"mcp_servers"}),
}

_SETTINGS_GETTER = {
    "Agent Settings": lambda c: c.agents.defaults,
    "Gateway": lambda c: c.gateway,
    "Tools": lambda c: c.tools,
}

_SETTINGS_SETTER = {
    "Agent Settings": lambda c, v: setattr(c.agents, "defaults", v),
    "Gateway": lambda c, v: setattr(c, "gateway", v),
    "Tools": lambda c, v: setattr(c, "tools", v),
}


def _configure_general_settings(config: Config, section: str) -> None:
    """Configure a general settings section (header + model edit + writeback)."""
    meta = _SETTINGS_SECTIONS.get(section)
    if not meta:
        return
    display_name, subtitle, skip = meta
    model = _SETTINGS_GETTER[section](config)
    updated = _configure_pydantic_model(model, display_name, skip_fields=skip)
    if updated is not None:
        _SETTINGS_SETTER[section](config, updated)


# --- Summary ---


def _summarize_model(obj: BaseModel) -> list[tuple[str, str]]:
    """Recursively summarize a Pydantic model. Returns list of (field, value) tuples."""
    items: list[tuple[str, str]] = []
    for field_name, field_info in type(obj).model_fields.items():
        value = getattr(obj, field_name, None)
        if value is None or value == "" or value == {} or value == []:
            continue
        display = _get_field_display_name(field_name, field_info)
        ftype = _get_field_type_info(field_info)
        if ftype.type_name == "model" and isinstance(value, BaseModel):
            for nested_field, nested_value in _summarize_model(value):
                items.append((f"{display}.{nested_field}", nested_value))
            continue
        formatted = _format_value(value, rich=False, field_name=field_name)
        if formatted != "[not set]":
            items.append((display, formatted))
    return items


def _print_summary_panel(rows: list[tuple[str, str]], title: str) -> None:
    """Build a two-column summary panel and print it."""
    if not rows:
        return
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Setting", style="cyan")
    table.add_column("Value")
    for field, value in rows:
        table.add_row(field, value)
    console.print(Panel(table, title=f"[bold]{title}[/bold]", border_style="blue"))


def _show_summary(config: Config) -> None:
    """Display configuration summary using rich."""
    console.print()

    # Providers
    provider_rows = []
    for name, display in _get_provider_names().items():
        provider = getattr(config.providers, name, None)
        status = "[green]configured[/green]" if (provider and provider.api_key) else "[dim]not configured[/dim]"
        provider_rows.append((display, status))
    _print_summary_panel(provider_rows, "LLM Providers")

    # Channels
    channel_rows = []
    for name, display in _get_channel_names().items():
        channel = getattr(config.channels, name, None)
        if channel:
            enabled = (
                channel.get("enabled", False)
                if isinstance(channel, dict)
                else getattr(channel, "enabled", False)
            )
            status = "[green]enabled[/green]" if enabled else "[dim]disabled[/dim]"
        else:
            status = "[dim]not configured[/dim]"
        channel_rows.append((display, status))
    _print_summary_panel(channel_rows, "Chat Channels")

    # Settings sections
    for title, model in [
        ("Agent Settings", config.agents.defaults),
        ("Gateway", config.gateway),
        ("Tools", config.tools),
        ("Channel Common", config.channels),
    ]:
        _print_summary_panel(_summarize_model(model), title)


# --- Main Entry Point ---


def _has_unsaved_changes(original: Config, current: Config) -> bool:
    """Return True when the onboarding session has committed changes."""
    return original.model_dump(by_alias=True) != current.model_dump(by_alias=True)


def _prompt_main_menu_exit(has_unsaved_changes: bool) -> str:
    """Resolve how to leave the main menu."""
    if not has_unsaved_changes:
        return "discard"

    answer = _get_questionary().select(
        "You have unsaved changes. What would you like to do?",
        choices=[
            "[S] Save and Exit",
            "[X] Exit Without Saving",
            "[R] Resume Editing",
        ],
        default="[R] Resume Editing",
        qmark=">",
    ).ask()

    if answer == "[S] Save and Exit":
        return "save"
    if answer == "[X] Exit Without Saving":
        return "discard"
    return "resume"


def _collect_licence_key(config: Config) -> None:
    """Prompt for a licence key, validate it against the server, and store it.

    Called once right after terms acceptance during first-run onboarding.
    Also accessible from the main menu via "[L] Licence Key".

    Loops indefinitely until a valid key is confirmed — there is no skip.
    The only exit is Ctrl+C, which aborts setup entirely.
    """
    from serenity.licence_lemon import validate_licence

    q = _get_questionary()

    while True:
        console.clear()
        console.print()
        console.print("[bold cyan]Serenity Licence Key[/bold cyan]\n")
        console.print(
            "  A valid licence key is [bold]required[/bold] to run Serenity.\n"
            "  Personal use is [green]free[/green] — get your key at:\n"
            "  [bold]https://seraficationkey.lemonsqueezy.com/checkout/buy/9967e436-54fe-4ab3-b7f0-8ce71a348d4e[/bold]\n"
        )

        # Show current key if already set
        existing = config.licence_key
        if existing:
            masked = existing[:4] + "*" * max(0, len(existing) - 8) + existing[-4:]
            console.print(f"  Current key: [dim]{masked}[/dim]\n")

        try:
            raw = q.text(
                "Enter your Lemon Squeezy licence key:",
                default="",
                qmark=">",
            ).ask()
        except (KeyboardInterrupt, Exception):
            # Ctrl+C — abort wizard entirely
            console.print(
                "\n[yellow]Setup cancelled. Serenity cannot start without a valid licence key.[/yellow]"
            )
            raise SystemExit(0)

        if raw is None:
            # questionary returns None on Ctrl+C in some environments
            console.print(
                "\n[yellow]Setup cancelled. Serenity cannot start without a valid licence key.[/yellow]"
            )
            raise SystemExit(0)

        raw = raw.strip()

        if not raw:
            console.print("\n[red]✗ A licence key is required. You cannot leave this blank.[/red]")
            input("\nPress Enter to try again...")
            continue

        key = raw.upper()

        # Basic sanity check — Lemon Squeezy keys are UUID format (8-4-4-4-12 hex)
        import re as _re
        if not _re.match(r'^[A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12}$', key):
            console.print(
                "\n[red]✗ Invalid format.[/red] "
                "Licence keys should look like: XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX\n"
                "  Check your email from Lemon Squeezy and try again."
            )
            input("\nPress Enter to try again...")
            continue

        # Validate against Lemon Squeezy (activates on first use)
        console.print("\n[dim]  Checking key with Lemon Squeezy...[/dim]")
        instance_id = getattr(config, "licence_instance_id", "")
        result = validate_licence(key, instance_id)

        if result.get("valid"):
            from datetime import datetime, timezone
            config.licence_key = key
            config.licence_tier = result.get("tier", "")
            config.licence_instance_id = result.get("instance_id", instance_id)
            config.licence_last_validated = datetime.now(tz=timezone.utc).isoformat()
            tier_display = result.get("tier", "unknown").replace("_", " ").title()
            expires = result.get("expires", "")[:10]
            email = result.get("email", "")
            console.print("\n[green]✓ Licence valid![/green]")
            console.print(f"  Tier    : [bold]{tier_display}[/bold]")
            if email:
                console.print(f"  Email   : {email}")
            if expires:
                console.print(f"  Expires : {expires}")
            input("\nPress Enter to continue...")
            return

        if result.get("offline"):
            # Lemon Squeezy unreachable — save the key and let the startup gate
            # decide based on grace period. Still require a key to be entered.
            console.print(
                "\n[yellow]! Cannot reach Lemon Squeezy right now.[/yellow]\n"
                "  Your key has been saved and will be verified next time Serenity\n"
                "  is online. You have a 7-day grace period before startup is blocked."
            )
            config.licence_key = key
            config.licence_tier = ""
            config.licence_last_validated = ""
            input("\nPress Enter to continue...")
            return

        # Server reachable but key rejected — must try again, no escape
        reason = result.get("reason", "Invalid key")
        console.print(f"\n[red]✗ Licence rejected:[/red] {reason}")
        console.print(
            "  [dim]Get a free personal key at https://seraficationkey.lemonsqueezy.com/checkout/buy/9967e436-54fe-4ab3-b7f0-8ce71a348d4e\n"
            "  Commercial licences at https://seraficationkey.lemonsqueezy.com\n"
            "  or contact serenitydev32@gmail.com for enquiries.[/dim]"
        )
        input("\nPress Enter to try a different key...")


def _configure_user(config: Config) -> None:
    """Collect basic info about the user — name only for now."""
    q = _get_questionary()
    console.clear()
    console.print("\n[bold cyan]About You[/bold cyan]\n")
    console.print(
        "[dim]This helps Serenity feel personal. "
        "Your name is used when she reaches out to you.\n"
        "All fields are optional — leave blank to skip.[/dim]\n"
    )

    try:
        name = q.text(
            "What should Serenity call you?",
            default=config.user.name or "",
            qmark=">",
        ).ask()
        if name is not None:
            config.user.name = name.strip()
    except Exception:
        pass

    console.print()
    display = config.user.name or "[dim](not set)[/dim]"
    console.print(f"[green]✓[/green] Name: {display}")
    console.print("\n[dim]Save and Exit to apply.[/dim]\n")
    input("Press Enter to return to the menu...")


def _configure_personality(config: Config) -> None:
    """Interactive personality configuration — traits and style vector.

    Tone modifier is fully dynamic (auto-detected from how the user speaks)
    and is not configurable here.
    """
    q = _get_questionary()
    p = config.agents.defaults.personality
    console.clear()
    console.print("\n[bold cyan]Personality[/bold cyan]\n")
    console.print(
        "[dim]These choices shape how your agent feels and responds.\n"
        "Traits bias its emotional starting point.\n"
        "Tone is detected automatically from how you speak — no need to set it.[/dim]\n"
    )

    # ── Traits (multi-select) ────────────────────────────────────────────────
    _ALL_TRAITS = [
        "curious", "funny", "direct", "calm", "energetic",
        "focused", "playful", "warm", "analytical", "chill",
        "hype", "goofy",
    ]
    _RANDOM_CHOICE = "* — surprise me (random traits)"
    current_traits = p.traits or []
    try:
        chosen = q.checkbox(
            "Pick traits that describe your ideal agent (space to select, enter to confirm):",
            choices=[
                q.Choice(t, checked=(t in current_traits))
                for t in _ALL_TRAITS
            ] + [q.Choice(_RANDOM_CHOICE)],
        ).ask()
        if chosen is not None:
            if _RANDOM_CHOICE in chosen:
                # Signal the dynamics engine to randomise on first load
                p.traits = ["*"]
            else:
                p.traits = chosen
    except Exception:
        pass

    # ── Verbosity ────────────────────────────────────────────────────────────
    try:
        verb = q.select(
            "Default verbosity:",
            choices=["low — terse, direct", "medium — balanced", "high — detailed"],
            default=f"{p.verbosity} — " + {
                "low": "terse, direct", "high": "detailed"
            }.get(p.verbosity, "balanced"),
            qmark=">",
        ).ask()
        if verb:
            p.verbosity = verb.split(" — ")[0].strip()
    except Exception:
        pass

    # ── Summary ──────────────────────────────────────────────────────────────
    console.print()
    traits_display = "random (assigned on first run)" if p.traits == ["*"] else (', '.join(p.traits) or 'none')
    console.print(f"[green]✓[/green] Traits:    {traits_display}")
    console.print(f"[green]✓[/green] Verbosity: {p.verbosity}")
    console.print(f"[dim]   Tone: auto-detected from your speech[/dim]")
    console.print("\n[dim]Save and Exit to apply.[/dim]\n")
    input("Press Enter to return to the menu...")


def _configure_senses(config: Config) -> None:
    """Configure Eyes (vision) and Ears (audio + TTS).

    Eyes  — screen capture + camera frame grab via OpenCV,
             MiniCPM-V 4.6 via Ollama for all vision analysis (on-demand)
    Ears  — PC microphone capture via Faster Whisper small (wake-word listener).
             Primary voice input is Telegram voice notes — always available without
             enabling this.
    Voice — TTS provider choice: edge-tts (free/offline), Qwen3 local, cloud providers
    """
    q = _get_questionary()

    # ── CUDA detection (used for device defaults throughout) ──────────────────
    _cuda = False
    try:
        import torch  # type: ignore
        _cuda = torch.cuda.is_available()
    except Exception:
        pass

    # ── Header ────────────────────────────────────────────────────────────────
    console.clear()
    console.print("\n[bold cyan]Senses — Eyes & Ears[/bold cyan]\n")
    console.print(
        "[dim]Enable Serenity's senses.\n"
        "  Eyes  — sees your screen and camera\n"
        "          (OpenCV frame grab · MiniCPM-V 4.6 via Ollama for all analysis)\n"
        "  Ears  — always-on PC microphone wake-word listener (Faster Whisper small)\n"
        "          NOTE: Telegram voice notes work independently — no setup needed.\n"
        "  Voice — speaks back to you via Telegram voice notes (TTS provider of your choice)[/dim]\n"
    )

    # ── EYES ──────────────────────────────────────────────────────────────────
    console.print("[bold]Eyes[/bold]")
    try:
        enable_eyes = q.confirm(
            "Enable eyes? (screen capture, object detection, scene understanding)",
            default=config.senses.vision.enabled,
            qmark=">",
        ).ask()
        if enable_eyes is not None:
            config.senses.vision.enabled = enable_eyes
    except Exception:
        pass

    if config.senses.vision.enabled:
        try:
            camera = q.confirm(
                "Also enable camera? (webcam capture in addition to screen)",
                default=config.senses.vision.camera_enabled,
                qmark=">",
            ).ask()
            if camera is not None:
                config.senses.vision.camera_enabled = camera
        except Exception:
            pass
    console.print()

    # ── EARS (STT) ────────────────────────────────────────────────────────────
    console.print("[bold]Ears[/bold]")
    try:
        enable_ears = q.confirm(
            "Enable ears? (microphone capture + speech-to-text)",
            default=config.senses.audio.enabled,
            qmark=">",
        ).ask()
        if enable_ears is not None:
            config.senses.audio.enabled = enable_ears
    except Exception:
        pass

    if config.senses.audio.enabled:
        # STT: Faster Whisper small (low RAM, good accuracy) — device auto-detected
        config.senses.audio.whisper_model = "small"
        config.senses.audio.whisper_device = "cuda" if _cuda else "cpu"
        config.senses.audio.whisper_compute_type = "float16" if _cuda else "int8"
        _dev = "GPU (CUDA)" if _cuda else "CPU"
        console.print(
            f"  [green]✓[/green] STT: Faster Whisper [bold]small[/bold] on [bold]{_dev}[/bold]\n"
            "  [dim]  To change model or device, edit config.json directly.[/dim]\n"
        )

        # Wake word
        console.print(
            "  [dim]Wake word — Serenity listens constantly and wakes up when she\n"
            "  hears this name. Set it to whatever you call your agent.[/dim]\n"
        )
        try:
            wake = q.text(
                "Wake word (what you call your agent):",
                default=config.senses.audio.wake_word or "Serenity",
                qmark=">",
            ).ask()
            if wake and wake.strip():
                config.senses.audio.wake_word = wake.strip()
        except Exception:
            pass
    console.print()

    # ── VOICE / TTS ───────────────────────────────────────────────────────────
    console.print("[bold]Voice[/bold]")
    try:
        enable_tts = q.confirm(
            "Enable voice replies? (Serenity speaks back to you)",
            default=config.voice.tts_enabled,
            qmark=">",
        ).ask()
        if enable_tts is not None:
            config.voice.tts_enabled = enable_tts
    except Exception:
        pass

    if config.voice.tts_enabled:
        # ── Provider choice ───────────────────────────────────────────────────
        _PROVIDER_CHOICES = [
            "edge-tts      — offline · free · no GPU · 200+ voices  [easiest]",
            "Qwen3 TTS     — local · runs on your GPU · free · ~97 ms",
            "Kokoro        — local · CPU-friendly · fast · no internet",
            "Coqui XTTS-v2 — local · multilingual · voice cloning · GPU recommended",
            "Qwen3 DashScope — cloud-hosted Qwen3 · free tier · needs API key",
            "ElevenLabs    — cloud · premium · ~75 ms · ultra-realistic",
            "OpenAI TTS    — cloud · standard quality",
        ]
        _PROVIDER_MAP = {
            "edge-tts      — offline · free · no GPU · 200+ voices  [easiest]": "edge-tts",
            "Qwen3 TTS     — local · runs on your GPU · free · ~97 ms": "qwen3-local",
            "Kokoro        — local · CPU-friendly · fast · no internet": "kokoro",
            "Coqui XTTS-v2 — local · multilingual · voice cloning · GPU recommended": "coqui",
            "Qwen3 DashScope — cloud-hosted Qwen3 · free tier · needs API key": "qwen3",
            "ElevenLabs    — cloud · premium · ~75 ms · ultra-realistic": "elevenlabs",
            "OpenAI TTS    — cloud · standard quality": "openai",
        }
        _current = config.voice.tts_provider or "qwen3-local"
        _default_choice = next(
            (c for c, v in _PROVIDER_MAP.items() if v == _current),
            _PROVIDER_CHOICES[0],
        )

        try:
            provider_choice = q.select(
                "Choose TTS provider:",
                choices=_PROVIDER_CHOICES,
                default=_default_choice,
                qmark=">",
            ).ask()
            if provider_choice:
                config.voice.tts_provider = _PROVIDER_MAP[provider_choice]
        except Exception:
            pass

        _p = config.voice.tts_provider

        # ── Provider-specific setup ───────────────────────────────────────────
        if _p == "edge-tts":
            # edge-tts: 200+ voices, free, no API key, works offline
            _EDGE_VOICES = [
                "en-US-AriaNeural",
                "en-US-JennyNeural",
                "en-US-GuyNeural",
                "en-GB-SoniaNeural",
                "en-GB-RyanNeural",
                "en-AU-NatashaNeural",
                "en-AU-WilliamNeural",
            ]
            _cur_edge = config.voice.tts_voice or "en-US-AriaNeural"
            console.print(
                "\n  [green]✓[/green] edge-tts: Microsoft Edge TTS — free, offline, no API key.\n"
                "  [dim]200+ voices available. Run [bold]edge-tts --list-voices[/bold] to see all.[/dim]\n"
                "  [dim]Already installed — part of the base senses setup.[/dim]\n"
            )
            try:
                voice = q.select(
                    "Choose a voice (or type any name from edge-tts --list-voices):",
                    choices=_EDGE_VOICES + ["(type manually)"],
                    default=_cur_edge if _cur_edge in _EDGE_VOICES else "en-US-AriaNeural",
                    qmark=">",
                ).ask()
                if voice == "(type manually)":
                    voice = q.text(
                        "Voice name (e.g. en-GB-LibbyNeural):",
                        default=_cur_edge,
                        qmark=">",
                    ).ask()
                if voice and voice != "(type manually)":
                    config.voice.tts_voice = voice.strip()
            except Exception:
                config.voice.tts_voice = "en-US-AriaNeural"

        elif _p == "kokoro":
            _dev_str = "GPU (CUDA)" if _cuda else "CPU"
            console.print(
                f"\n  [green]✓[/green] Kokoro TTS — local, CPU-friendly, fast.\n"
                f"  [dim]Will run on {_dev_str}.[/dim]\n"
                "  [dim]Install:[/dim] [bold]pip install kokoro soundfile[/bold]\n"
                "  [dim]Voices: af_heart, af_bella, af_nicole, am_adam, am_michael, bf_emma, bf_isabella[/dim]\n"
            )
            _KOKORO_VOICES = [
                "af_heart", "af_bella", "af_nicole",
                "am_adam", "am_michael",
                "bf_emma", "bf_isabella", "bm_george", "bm_lewis",
            ]
            _cur_kv = config.voice.tts_voice or "af_heart"
            try:
                voice = q.select(
                    "Choose a voice:",
                    choices=_KOKORO_VOICES,
                    default=_cur_kv if _cur_kv in _KOKORO_VOICES else "af_heart",
                    qmark=">",
                ).ask()
                if voice:
                    config.voice.tts_voice = voice
            except Exception:
                config.voice.tts_voice = "af_heart"

        elif _p == "coqui":
            _dev_str = "GPU (CUDA)" if _cuda else "CPU (slow — GPU recommended)"
            console.print(
                f"\n  [green]✓[/green] Coqui XTTS-v2 — local, multilingual, voice cloning.\n"
                f"  [dim]Will run on {_dev_str}.[/dim]\n"
                "  [dim]Install:[/dim] [bold]pip install TTS[/bold]\n"
                "  [dim]First run downloads XTTS-v2 (~2 GB) from HuggingFace.[/dim]\n"
                "  [dim]Voice cloning: drop any 5–30s audio file into[/dim] "
                "[bold]~/.serenity/voice_clone/[/bold]\n"
            )
            # No voice picker — Coqui uses built-in speakers or auto-clones from voice_clone/
            console.print(
                "  [dim]Default speaker will be used unless a voice_clone file is present.[/dim]\n"
            )

        elif _p == "qwen3":
            console.print(
                "\n  [dim]Qwen3 TTS streams audio in ~97 ms via DashScope SSE.[/dim]\n"
                "  [dim]Free tier at:[/dim] [bold]https://dashscope.console.aliyun.com/apiKey[/bold]\n"
            )
            try:
                key = q.text(
                    "DashScope API key (sk-...):",
                    default=config.voice.tts_api_key or "",
                    qmark=">",
                ).ask()
                if key is not None:
                    config.voice.tts_api_key = key.strip()
            except Exception:
                pass
            config.voice.tts_model = "qwen3-tts-flash"
            config.voice.tts_voice = "cherry"

        elif _p == "elevenlabs":
            console.print(
                "\n  [dim]ElevenLabs Flash v2.5 delivers audio in ~75 ms.[/dim]\n"
                "  [dim]API key:[/dim]  [bold]https://elevenlabs.io[/bold]\n"
                "  [dim]Voices:[/dim]   [bold]https://elevenlabs.io/voice-library[/bold]\n"
            )
            try:
                key = q.text(
                    "ElevenLabs API key:",
                    default=config.voice.tts_elevenlabs_api_key or "",
                    qmark=">",
                ).ask()
                if key is not None:
                    config.voice.tts_elevenlabs_api_key = key.strip()

                voice_id = q.text(
                    "Voice ID  (blank = Rachel, 21m00Tcm4TlvDq8ikWAM):",
                    default=config.voice.tts_elevenlabs_voice_id or "",
                    qmark=">",
                ).ask()
                vid = (voice_id or "").strip()
                config.voice.tts_elevenlabs_voice_id = vid or "21m00Tcm4TlvDq8ikWAM"
            except Exception:
                pass
            config.voice.tts_elevenlabs_model = "eleven_flash_v2_5"

        elif _p == "qwen3-local":
            _dev = "GPU (CUDA)" if _cuda else "CPU (slow — GPU recommended)"
            console.print(
                f"\n  [green]✓[/green] Qwen3 TTS will run locally on your [bold]{_dev}[/bold]\n"
                "  [dim]No API key. No internet. Completely free forever.[/dim]\n"
                "  [dim]First run downloads ~3–7 GB from Hugging Face.[/dim]\n"
                "  [dim]Make sure you've run:[/dim] [bold]pip install -U qwen-tts[/bold]\n"
                "  [dim](or run sense/install_senses.bat — it does this for you)[/dim]\n"
            )
            # Voice picker
            _LOCAL_VOICES = ["Cherry", "Vivian", "Ryan", "Sohee", "Alloy", "Echo", "Fable", "Onyx", "Nova"]
            _cur_voice = config.voice.tts_local_voice or "Cherry"
            try:
                voice = q.select(
                    "Choose a voice:",
                    choices=_LOCAL_VOICES,
                    default=_cur_voice if _cur_voice in _LOCAL_VOICES else "Cherry",
                    qmark=">",
                ).ask()
                if voice:
                    config.voice.tts_local_voice = voice
            except Exception:
                config.voice.tts_local_voice = "Cherry"
            config.voice.tts_local_model = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
            config.voice.tts_local_device = "cuda" if _cuda else "cpu"

        elif _p == "openai":
            console.print(
                "\n  [dim]OpenAI TTS or any /v1/audio/speech-compatible local server.[/dim]\n"
            )
            try:
                key = q.text(
                    "OpenAI API key (leave blank to set later):",
                    default=config.voice.tts_api_key or "",
                    qmark=">",
                ).ask()
                if key is not None:
                    config.voice.tts_api_key = key.strip()
            except Exception:
                pass
            config.voice.tts_model = "tts-1"
            config.voice.tts_voice = "alloy"

    # ── Summary ───────────────────────────────────────────────────────────────
    console.print()
    _eyes_on = config.senses.vision.enabled
    _cam_on  = config.senses.vision.camera_enabled
    _ears_on = config.senses.audio.enabled
    _tts_on  = config.voice.tts_enabled

    _eye_str = "[green]enabled[/green]" + (" + camera" if _cam_on else "") if _eyes_on else "[dim]disabled[/dim]"
    _ear_str = "[green]enabled[/green]" if _ears_on else "[dim]disabled[/dim]"

    console.print(f"[green]✓[/green] Eyes:  {_eye_str}")
    console.print(f"[green]✓[/green] Ears:  {_ear_str}")

    if _ears_on:
        _dev = "GPU" if config.senses.audio.whisper_device == "cuda" else "CPU"
        console.print(f"   [dim]STT: Faster Whisper small ({_dev})[/dim]")

    if _tts_on:
        _labels = {
            "edge-tts":    "edge-tts (Microsoft, offline, free, 200+ voices)",
            "qwen3-local": "Qwen3 TTS (local GPU, offline, free)",
            "kokoro":      "Kokoro TTS (local, CPU-friendly, fast)",
            "coqui":       "Coqui XTTS-v2 (local, multilingual, voice cloning)",
            "qwen3":       "Qwen3 TTS via DashScope (cloud-hosted)",
            "elevenlabs":  "ElevenLabs (~75 ms Flash v2.5)",
            "openai":      "OpenAI TTS",
        }
        console.print(f"   [dim]TTS: {_labels.get(config.voice.tts_provider, config.voice.tts_provider)}[/dim]")
    else:
        console.print("   [dim]TTS: disabled[/dim]")

    console.print("\n[dim]Save and Exit to apply.[/dim]\n")
    input("Press Enter to return to the menu...")


def _show_terms_acceptance() -> bool:
    """Display licence terms and require explicit acceptance before setup proceeds.

    Returns True if the user accepts, False if they decline.
    """
    q = _get_questionary()
    console.clear()
    console.print()
    console.print(
        "[bold cyan]Serenity — Licence & Terms[/bold cyan]\n"
    )
    console.print(
        "[bold]Before you continue, please read the following:[/bold]\n"
    )
    console.print(
        "  [cyan]•[/cyan] [bold]Personal / non-commercial use[/bold] is [green]free[/green] "
        "under CC BY-NC 4.0.\n"
        "    Get your free key → [green]https://seraficationkey.lemonsqueezy.com/checkout/buy/9967e436-54fe-4ab3-b7f0-8ce71a348d4e[/green]\n"
        "  [cyan]•[/cyan] [bold]Commercial use[/bold] requires a paid licence.\n"
        "    - Solo commercial (1 user): [bold]£50 one-time[/bold]\n"
        "      [cyan]https://seraficationkey.lemonsqueezy.com/checkout/buy/43022d03-f49d-48ad-b6fd-05a2f6fc3efd[/cyan]\n"
        "    - Small business (≤10):     [bold]£80 / month[/bold]\n"
        "      [yellow]https://seraficationkey.lemonsqueezy.com/checkout/buy/bde05ba3-134c-44bf-a598-7b906bbdcf90[/yellow]\n"
        "    - Growth (≤50 people):      [bold]£200 / month[/bold]\n"
        "      [magenta]https://seraficationkey.lemonsqueezy.com/checkout/buy/8a5c281f-b5f7-41e3-8fb8-9ee293f80dc2[/magenta]\n"
        "    - Enterprise (50+):         [bold]Email to negotiate[/bold]  →  serenitydev32@gmail.com\n"
    )
    console.print(
        "  [cyan]•[/cyan] You may not resell, sublicence, or redistribute Serenity.\n"
        "  [cyan]•[/cyan] Serenity is provided as-is with no warranties.\n"
        "  [cyan]•[/cyan] Your email is collected for licence validation only and is never sold.\n"
    )
    console.print(
        "[dim]Full terms: LICENSE.md · COMMERCIAL_LICENSE.md · TERMS.md · "
        "DISCLAIMER.md · PRIVACY.md[/dim]\n"
    )

    try:
        accepted = q.confirm(
            "Do you accept the Serenity Licence and Terms of Use?",
            default=False,
            qmark=">",
        ).ask()
    except (KeyboardInterrupt, Exception):
        accepted = None

    if not accepted:
        console.print(
            "\n[yellow]You must accept the terms to use Serenity.[/yellow]\n"
            "[dim]If you have questions, contact serenitydev32@gmail.com[/dim]\n"
        )
        return False

    console.print("\n[green]✓ Terms accepted.[/green]\n")
    return True


def run_onboard(initial_config: Config | None = None) -> OnboardResult:
    """Run the interactive onboarding questionnaire.

    Args:
        initial_config: Optional pre-loaded config to use as starting point.
                       If None, loads from config file or creates new default.
    """
    _get_questionary()

    # Terms acceptance gate — must agree before setup proceeds
    if not _show_terms_acceptance():
        config_path = get_config_path()
        fallback = load_config(config_path) if config_path.exists() else Config()
        return OnboardResult(config=fallback, should_save=False)

    if initial_config is not None:
        base_config = initial_config.model_copy(deep=True)
    else:
        config_path = get_config_path()
        if config_path.exists():
            base_config = load_config()
        else:
            base_config = Config()

    original_config = base_config.model_copy(deep=True)
    config = base_config.model_copy(deep=True)

    # Licence key gate — prompt immediately after terms on first run.
    # On re-runs (config already exists) the key is already in config; skip.
    # Master key holders are always exempt.
    from serenity.licence import is_master_key_active
    if not config.licence_key and not is_master_key_active():
        _collect_licence_key(config)

    while True:
        console.clear()
        _show_main_menu_header()

        # Show licence status in menu header
        if is_master_key_active():
            console.print("  [dim]Licence:[/dim] [bold magenta]master key[/bold magenta]\n")
        elif config.licence_key:
            tier = config.licence_tier or "unverified"
            masked = config.licence_key[:4] + "****" + config.licence_key[-4:]
            console.print(
                f"  [dim]Licence:[/dim] [green]{masked}[/green] "
                f"([cyan]{tier}[/cyan])\n"
            )
        else:
            console.print("  [dim]Licence:[/dim] [yellow]not set[/yellow]\n")

        try:
            answer = _get_questionary().select(
                "What would you like to configure?",
                choices=[
                    "[U] About You",
                    "[L] Licence Key",
                    "[P] LLM Provider",
                    "[C] Chat Channel",
                    "[A] Agent Settings",
                    "[G] Gateway",
                    "[T] Tools",
                    "[Y] Personality",
                    "[E] Senses & Voice",
                    "[V] View Configuration Summary",
                    "[S] Save and Exit",
                    "[X] Exit Without Saving",
                ],
                qmark=">",
            ).ask()
        except KeyboardInterrupt:
            answer = None

        if answer is None:
            action = _prompt_main_menu_exit(_has_unsaved_changes(original_config, config))
            if action == "save":
                return OnboardResult(config=config, should_save=True)
            if action == "discard":
                return OnboardResult(config=original_config, should_save=False)
            continue

        _MENU_DISPATCH = {
            "[U] About You": lambda: _configure_user(config),
            "[L] Licence Key": lambda: _collect_licence_key(config),
            "[P] LLM Provider": lambda: _configure_providers(config),
            "[C] Chat Channel": lambda: _configure_channels(config),
            "[A] Agent Settings": lambda: _configure_general_settings(config, "Agent Settings"),
            "[G] Gateway": lambda: _configure_general_settings(config, "Gateway"),
            "[T] Tools": lambda: _configure_general_settings(config, "Tools"),
            "[Y] Personality": lambda: _configure_personality(config),
            "[E] Senses & Voice": lambda: _configure_senses(config),
            "[V] View Configuration Summary": lambda: _show_summary(config),
        }

        if answer == "[S] Save and Exit":
            return OnboardResult(config=config, should_save=True)
        if answer == "[X] Exit Without Saving":
            return OnboardResult(config=original_config, should_save=False)

        action_fn = _MENU_DISPATCH.get(answer)
        if action_fn:
            action_fn()
