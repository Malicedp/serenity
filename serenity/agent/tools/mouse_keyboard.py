"""Mouse and keyboard control tool — lets Serenity interact with any app on screen.

Uses pyautogui (cross-platform) with a safety failsafe: moving the mouse to the
top-left corner of the screen (0, 0) immediately stops all activity.

Install:  pip install pyautogui pillow

Tools:
  mouse_move(x, y)                       — move cursor
  mouse_click(x, y, button, clicks)      — click at position
  mouse_scroll(x, y, amount)             — scroll wheel
  mouse_drag(x1, y1, x2, y2)            — click-drag
  keyboard_type(text, interval)          — type text
  keyboard_hotkey(*keys)                 — press key combo (ctrl+c, alt+f4, etc.)
  keyboard_press(key)                    — press a single key
  find_on_screen(image_path, confidence) — locate an image on screen, return coords
"""

from __future__ import annotations

import asyncio
from typing import Any


from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import (
    IntegerSchema,
    NumberSchema,
    StringSchema,
    tool_parameters_schema,
)


def _pg():
    """Lazy import pyautogui with failsafe enabled."""
    try:
        import pyautogui
        pyautogui.FAILSAFE = True   # move mouse to (0,0) to abort
        pyautogui.PAUSE = 0.05      # small delay between actions for stability
        return pyautogui
    except ImportError as exc:
        raise RuntimeError(
            "Mouse/keyboard control requires 'pyautogui'.\n"
            "Run:  pip install pyautogui pillow"
        ) from exc


def _run_sync(fn):
    """Run a blocking pyautogui call in a thread executor."""
    async def wrapper(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
    return wrapper


# ── mouse_move ───────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        x=IntegerSchema(0, description="Target X coordinate in pixels."),
        y=IntegerSchema(0, description="Target Y coordinate in pixels."),
        duration=NumberSchema(0.2, description="Movement duration in seconds (default 0.2)."),
        required=["x", "y"],
    )
)
class MouseMoveTool(Tool):
    """Move the mouse cursor to a screen position."""

    @property
    def name(self) -> str:
        return "mouse_move"

    @property
    def description(self) -> str:
        return (
            "Move the mouse cursor to (x, y) on screen. "
            "Use screenshot() first to see the screen and identify coordinates. "
            "duration controls how fast it moves (default 0.2s)."
        )

    async def execute(self, x: int, y: int, duration: float = 0.2, **kwargs: Any) -> str:
        def _do():
            pg = _pg()
            pg.moveTo(x, y, duration=duration)
            return f"Mouse moved to ({x}, {y})"
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)


# ── mouse_click ──────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        x=IntegerSchema(0, description="X coordinate to click."),
        y=IntegerSchema(0, description="Y coordinate to click."),
        button=StringSchema(
            'Mouse button: "left" (default), "right", or "middle".',
            nullable=True,
        ),
        clicks=IntegerSchema(1, description="Number of clicks (1=single, 2=double).", minimum=1, maximum=3),
        move_duration=NumberSchema(0.15, description="Duration to move mouse before clicking (seconds)."),
        required=["x", "y"],
    )
)
class MouseClickTool(Tool):
    """Click the mouse at a screen position."""

    @property
    def name(self) -> str:
        return "mouse_click"

    @property
    def description(self) -> str:
        return (
            "Click the mouse at (x, y). "
            'button: "left" (default), "right", "middle". '
            "clicks=2 for double-click. "
            "Take a screenshot first to find the right coordinates."
        )

    async def execute(
        self,
        x: int,
        y: int,
        button: str | None = None,
        clicks: int = 1,
        move_duration: float = 0.15,
        **kwargs: Any,
    ) -> str:
        btn = (button or "left").lower()

        def _do():
            pg = _pg()
            pg.click(x, y, button=btn, clicks=clicks, duration=move_duration)
            label = "double-clicked" if clicks == 2 else "clicked"
            return f"Mouse {label} {btn} at ({x}, {y})"

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)


# ── mouse_scroll ─────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        x=IntegerSchema(0, description="X coordinate to scroll at."),
        y=IntegerSchema(0, description="Y coordinate to scroll at."),
        amount=IntegerSchema(3, description="Scroll amount. Positive = up, negative = down."),
        required=["x", "y", "amount"],
    )
)
class MouseScrollTool(Tool):
    """Scroll the mouse wheel at a position."""

    @property
    def name(self) -> str:
        return "mouse_scroll"

    @property
    def description(self) -> str:
        return (
            "Scroll the mouse wheel at (x, y). "
            "amount: positive = scroll up, negative = scroll down."
        )

    async def execute(self, x: int, y: int, amount: int = 3, **kwargs: Any) -> str:
        def _do():
            pg = _pg()
            pg.scroll(amount, x=x, y=y)
            direction = "up" if amount > 0 else "down"
            return f"Scrolled {direction} by {abs(amount)} at ({x}, {y})"

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)


# ── mouse_drag ───────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        x1=IntegerSchema(0, description="Start X coordinate."),
        y1=IntegerSchema(0, description="Start Y coordinate."),
        x2=IntegerSchema(0, description="End X coordinate."),
        y2=IntegerSchema(0, description="End Y coordinate."),
        duration=NumberSchema(0.4, description="Drag duration in seconds."),
        required=["x1", "y1", "x2", "y2"],
    )
)
class MouseDragTool(Tool):
    """Click and drag from one screen position to another."""

    @property
    def name(self) -> str:
        return "mouse_drag"

    @property
    def description(self) -> str:
        return "Click and drag from (x1, y1) to (x2, y2). Useful for sliders, resizing, or drag-and-drop."

    async def execute(
        self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.4, **kwargs: Any
    ) -> str:
        def _do():
            pg = _pg()
            pg.moveTo(x1, y1, duration=0.1)
            pg.dragTo(x2, y2, duration=duration, button="left")
            return f"Dragged from ({x1}, {y1}) to ({x2}, {y2})"

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)


# ── keyboard_type ────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        text=StringSchema("Text to type. Supports Unicode."),
        interval=NumberSchema(0.03, description="Delay between keystrokes in seconds (default 0.03)."),
        required=["text"],
    )
)
class KeyboardTypeTool(Tool):
    """Type text using the keyboard."""

    @property
    def name(self) -> str:
        return "keyboard_type"

    @property
    def description(self) -> str:
        return (
            "Type text at the current cursor position. "
            "Click on the target input field first with mouse_click(). "
            "interval controls typing speed (default 0.03s per key)."
        )

    async def execute(self, text: str, interval: float = 0.03, **kwargs: Any) -> str:
        def _do():
            pg = _pg()
            pg.typewrite(text, interval=interval)
            preview = text[:40] + ("…" if len(text) > 40 else "")
            return f"Typed: '{preview}' ({len(text)} chars)"

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)


# ── keyboard_hotkey ──────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        keys=StringSchema(
            "Key combination as comma-separated keys, e.g. 'ctrl,c' or 'alt,f4' or 'ctrl,shift,esc'."
        ),
        required=["keys"],
    )
)
class KeyboardHotkeyTool(Tool):
    """Press a keyboard shortcut / hotkey combination."""

    @property
    def name(self) -> str:
        return "keyboard_hotkey"

    @property
    def description(self) -> str:
        return (
            "Press a key combination. "
            "keys: comma-separated, e.g. 'ctrl,c' (copy), 'ctrl,v' (paste), "
            "'alt,f4' (close window), 'ctrl,shift,esc' (Task Manager), "
            "'win,d' (show desktop), 'ctrl,a' (select all). "
            "Keys are pressed simultaneously."
        )

    async def execute(self, keys: str, **kwargs: Any) -> str:
        key_list = [k.strip().lower() for k in keys.split(",") if k.strip()]

        def _do():
            pg = _pg()
            pg.hotkey(*key_list)
            return f"Hotkey: {' + '.join(key_list)}"

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)


# ── keyboard_press ───────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        key=StringSchema(
            "Key to press, e.g. 'enter', 'escape', 'tab', 'space', 'backspace', "
            "'up', 'down', 'left', 'right', 'f5', 'delete'."
        ),
        presses=IntegerSchema(1, description="Number of times to press the key.", minimum=1, maximum=20),
        required=["key"],
    )
)
class KeyboardPressTool(Tool):
    """Press a single keyboard key."""

    @property
    def name(self) -> str:
        return "keyboard_press"

    @property
    def description(self) -> str:
        return (
            "Press a single key. "
            "Common keys: 'enter', 'escape', 'tab', 'space', 'backspace', "
            "'up', 'down', 'left', 'right', 'f5', 'delete', 'home', 'end'. "
            "presses: how many times to press it."
        )

    async def execute(self, key: str, presses: int = 1, **kwargs: Any) -> str:
        def _do():
            pg = _pg()
            pg.press(key.strip().lower(), presses=presses)
            return f"Pressed '{key}' × {presses}"

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)


# ── find_on_screen ───────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        image_path=StringSchema(
            "Absolute path to a PNG/JPG image to search for on screen."
        ),
        confidence=NumberSchema(
            0.8,
            description="Match confidence 0.0–1.0 (default 0.8). Lower = more lenient.",
        ),
        required=["image_path"],
    )
)
class FindOnScreenTool(Tool):
    """Find an image on screen and return its centre coordinates."""

    @property
    def name(self) -> str:
        return "find_on_screen"

    @property
    def description(self) -> str:
        return (
            "Search the screen for a reference image and return the centre (x, y) coordinates. "
            "Useful for clicking buttons or UI elements without hardcoding coordinates. "
            "Take a screenshot first, crop the target element, save it, then call this tool. "
            "Returns the centre point to use with mouse_click()."
        )

    async def execute(
        self, image_path: str, confidence: float = 0.8, **kwargs: Any
    ) -> str:
        def _do():
            pg = _pg()
            try:
                loc = pg.locateOnScreen(image_path, confidence=confidence)
                if loc is None:
                    return f"Not found on screen: {image_path}"
                cx, cy = pg.center(loc)
                return f"Found at centre ({cx}, {cy}) — use mouse_click(x={cx}, y={cy})"
            except Exception as e:
                return f"find_on_screen error: {e}"

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)
