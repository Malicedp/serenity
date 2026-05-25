"""Eyes tools — camera and screen vision for Serenity.

Stack:
  opencv-python  — camera frame grab (VideoCapture only)
  mss            — screen capture
  minicpm-v4.6   — all vision analysis via Ollama (on-demand, no background loop)

Tools:
  eyes_open              — open camera device
  eyes_close             — close camera, release resources
  eyes_snapshot          — describe what the camera sees right now (minicpm-v4.6)
  eyes_screen            — describe what is on screen right now (minicpm-v4.6)
  eyes_send_screenshot   — capture + send screenshot image to user on Telegram
  eyes_screen_ascii      — capture screen as ASCII art (text-only LLM friendly)
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from loguru import logger

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import StringSchema, tool_parameters_schema


# ── Config helpers ────────────────────────────────────────────────────────────

def _is_vision_enabled() -> bool:
    """Return True if senses.vision.enabled is set in config."""
    try:
        from serenity.config.loader import load_config
        return load_config().senses.vision.enabled
    except Exception:
        return False


def _is_camera_enabled() -> bool:
    """Return True if senses.vision.camera_enabled is set in config."""
    try:
        from serenity.config.loader import load_config
        cfg = load_config().senses.vision
        return cfg.enabled and cfg.camera_enabled
    except Exception:
        return False


def _camera_index() -> int:
    try:
        from serenity.config.loader import load_config
        return load_config().senses.vision.camera_index
    except Exception:
        return 0


_VISION_DISABLED_MSG = (
    "Vision is not enabled. To enable it run the setup wizard "
    "(`serenity onboard` → [E] Senses & Vision) and turn on Vision."
)
_CAMERA_DISABLED_MSG = (
    "Camera access is not enabled. To enable it run the setup wizard "
    "(`serenity onboard` → [E] Senses & Vision) and turn on Camera."
)


# ── eyes_open ─────────────────────────────────────────────────────────────────

@tool_parameters(tool_parameters_schema(required=[]))
class EyesOpenTool(Tool):
    """Open Serenity's eyes — start the camera and background awareness loop.

    Call this when the user says any of:
      "open your eyes", "look at me", "can you see me", "watch me",
      "start watching", "use your camera", "activate camera", "camera on",
      "enable vision", "i want you to see me", "look through the camera",
      "are you watching", "turn on camera", "start camera", "check on me",
      "look up", "open camera", "i want you to look at me", "see me",
      "can you look at me", "look at this", "eyes on", "are you looking"
    """

    @property
    def name(self) -> str:
        return "eyes_open"

    @property
    def description(self) -> str:
        return (
            "Open Serenity's camera so she can capture frames on demand. "
            "Trigger phrases: 'open your eyes', 'look at me', 'can you see me', "
            "'watch me', 'start watching', 'use your camera', 'activate camera', "
            "'camera on', 'enable vision', 'i want you to see me', 'are you watching', "
            "'turn on camera', 'start camera', 'check on me', 'look up', 'open camera', "
            "'eyes on', 'are you looking', 'see me', 'can you look at me'. "
            "All vision analysis runs via MiniCPM-V 4.6 on Ollama — no VRAM used."
        )

    async def execute(self, **kwargs: Any) -> str:
        if not _is_camera_enabled():
            return _CAMERA_DISABLED_MSG
        from serenity.senses.camera import get_stack
        stack = get_stack()
        if stack.is_open:
            return (
                "My eyes are already open. "
                f"I can see you — {stack.format_ambient()}"
            )
        result = stack.open(camera_index=_camera_index())
        if result != "ok":
            return f"Could not open camera: {result}"

        # Also start vision RAG camera watching
        try:
            from serenity.senses.visual_memory import get_service as _get_vm
            _vm = _get_vm()
            if not _vm.is_running:
                _vm.start(sources=["camera"])
        except Exception:
            pass

        return (
            "My eyes are open. Camera is ready — "
            "say 'what do you see' and I'll describe what's in front of me."
        )


# ── eyes_close ────────────────────────────────────────────────────────────────

@tool_parameters(tool_parameters_schema(required=[]))
class EyesCloseTool(Tool):
    """Close Serenity's eyes — stop the camera and release all vision models.

    Call this when the user says any of:
      "close your eyes", "stop watching", "look away", "camera off",
      "disable vision", "stop seeing", "eyes off", "don't look",
      "stop looking", "stop camera", "turn off camera", "disable camera",
      "close camera", "stop watching me", "i don't want you to see me"
    """

    @property
    def name(self) -> str:
        return "eyes_close"

    @property
    def description(self) -> str:
        return (
            "Close Serenity's camera — stops all background vision processing "
            "and releases all models from memory (zero overhead after close). "
            "Trigger phrases: 'close your eyes', 'stop watching', 'look away', "
            "'camera off', 'disable vision', 'stop seeing', 'eyes off', "
            "'don't look', 'stop looking', 'stop camera', 'turn off camera', "
            "'disable camera', 'close camera', 'stop watching me', "
            "'i don't want you to see me'."
        )

    async def execute(self, **kwargs: Any) -> str:
        from serenity.senses.camera import get_stack
        stack = get_stack()
        if not stack.is_open:
            return "My eyes are already closed."
        stack.close()

        # Stop vision RAG camera watching
        try:
            from serenity.senses.visual_memory import get_service as _get_vm
            _vm = _get_vm()
            if _vm.is_running:
                _vm.stop()
        except Exception:
            pass

        return "My eyes are closed. Camera released."


# ── eyes_snapshot ─────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        question=StringSchema(
            "Optional specific question about what to look for in the image. "
            "Default: general scene description.",
            nullable=True,
        ),
        required=[],
    )
)
class EyesSnapshotTool(Tool):
    """Describe what the camera sees right now using minicpm-v4.6 via Ollama.

    Call this when the user says any of:
      "what do you see", "describe what you see", "what's in front of you",
      "take a snapshot", "describe the scene", "tell me what you see",
      "what's there", "look around", "describe what's happening",
      "what can you see", "take a picture", "snap", "look at this"
    Eyes do NOT need to be open — this works independently.
    """

    @property
    def name(self) -> str:
        return "eyes_snapshot"

    @property
    def description(self) -> str:
        return (
            "Point the WEBCAM at the real world, capture a frame, and describe what "
            "Serenity sees through the camera using MiniCPM-V 4.6 via Ollama. "
            "Use this for anything about the PHYSICAL WORLD in front of the camera — "
            "NOT for the screen. "
            "Trigger phrases: 'what do you see', 'what is in front of you', "
            "'look around', 'describe the room', 'what is around you', "
            "'can you see me', 'describe the scene', 'look at this object'."
        )

    async def execute(self, question: str | None = None, **kwargs: Any) -> str:
        if not _is_camera_enabled():
            return _CAMERA_DISABLED_MSG
        from serenity.senses.camera import get_stack
        stack = get_stack()
        logger.info("EyesSnapshot: capturing camera frame…")
        description = await stack.snapshot("camera")

        # Log to vision RAG — reuse the description, no second Ollama call
        try:
            from serenity.senses.visual_memory import get_service as _get_vm
            _get_vm().store_caption("camera", description)
        except Exception:
            pass

        if question:
            return f"Camera snapshot — {description}\n\n(Re: '{question}')"
        return f"Camera snapshot — {description}"


# ── eyes_screen ───────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        question=StringSchema(
            "Optional specific question about the screen content.",
            nullable=True,
        ),
        required=[],
    )
)
class EyesScreenTool(Tool):
    """Describe what is on screen right now using minicpm-v4.6 via Ollama.

    Call this when the user says any of:
      "look at my screen", "what's on my screen", "read my screen",
      "describe my screen", "what am i working on visually",
      "check my screen", "can you see my screen", "look at what's on screen",
      "describe what i'm looking at", "what do you see on my screen"
    """

    @property
    def name(self) -> str:
        return "eyes_screen"

    @property
    def description(self) -> str:
        return (
            "Capture the MONITOR/DISPLAY and describe its contents to Serenity "
            "using MiniCPM-V 4.6 via Ollama. Use this whenever the user asks what is "
            "ON THEIR SCREEN — this is the primary tool for screen questions. "
            "Returns a text description Serenity can read and respond to. "
            "Trigger phrases: 'what can you see on my screen', 'what is on my screen', "
            "'look at my screen', 'read my screen', 'describe my screen', "
            "'what am I looking at', 'what is on the display', 'check my screen'."
        )

    async def execute(self, question: str | None = None, **kwargs: Any) -> str:
        if not _is_vision_enabled():
            return _VISION_DISABLED_MSG
        from serenity.senses.camera import get_stack
        stack = get_stack()
        logger.info("EyesScreen: capturing screenshot…")
        description = await stack.snapshot("screen")

        # Log to vision RAG — reuse the description, no second Ollama call
        try:
            from serenity.senses.visual_memory import get_service as _get_vm
            _get_vm().store_caption("screen", description)
        except Exception:
            pass

        if question:
            return f"Screen snapshot — {description}\n\n(Re: '{question}')"
        return f"Screen snapshot — {description}"


# ── eyes_send_screenshot ──────────────────────────────────────────────────────

from serenity.bus.events import OutboundMessage


@tool_parameters(
    tool_parameters_schema(
        source=StringSchema(
            'What to capture: "screen" (default) or "camera".',
            nullable=True,
        ),
        caption=StringSchema(
            "Optional caption to send alongside the image.",
            nullable=True,
        ),
        required=[],
    )
)
class EyesSendScreenshotTool(Tool):
    """Capture a screenshot or camera frame and send the actual image to the user.

    Unlike eyes_screen (which only sends a text description), this tool sends
    the real PNG image file to the user's Telegram chat so they can see it
    visually. MiniCPM-V 4.6 also describes the image in the tool result.

    Call this when the user says any of:
      "send me a screenshot", "show me what you see", "send the image",
      "take a screenshot and send it", "send me a picture of the screen",
      "show me my screen", "send me a photo of the camera", "capture and send"
    """

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
    ) -> None:
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id

    def set_context(self, channel: str, chat_id: str, *_: Any) -> None:
        self._default_channel = channel
        self._default_chat_id = chat_id

    def set_send_callback(
        self, callback: Callable[[OutboundMessage], Awaitable[None]]
    ) -> None:
        self._send_callback = callback

    @property
    def name(self) -> str:
        return "eyes_send_screenshot"

    @property
    def description(self) -> str:
        return (
            "Capture a screenshot or camera frame and SEND THE ACTUAL IMAGE FILE to "
            "the user via Telegram so they can see it. Use this when the user wants "
            "to RECEIVE an image in chat — not just have Serenity describe it. "
            'source="screen" sends a screenshot. source="camera" sends a webcam photo. '
            "Trigger phrases: 'send me a screenshot', 'send me the image', "
            "'take a screenshot and send it to me', 'send me a photo of the screen', "
            "'send me a picture from your camera', 'capture and send it'."
        )

    async def execute(
        self,
        source: str | None = None,
        caption: str | None = None,
        **kwargs: Any,
    ) -> str:
        source = (source or "screen").lower()
        if source == "camera":
            if not _is_camera_enabled():
                return _CAMERA_DISABLED_MSG
        else:
            if not _is_vision_enabled():
                return _VISION_DISABLED_MSG
        channel = self._default_channel
        chat_id = self._default_chat_id

        from serenity.senses.camera import get_stack
        stack = get_stack()
        logger.info("EyesSendScreenshot: capturing {} frame…", source)

        description, image_path = await stack.snapshot_with_image(source)

        if image_path is None:
            return f"Could not capture {source} frame — nothing sent."

        try:
            if self._send_callback and channel and chat_id:
                msg_content = caption or description[:200]
                msg = OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=msg_content,
                    media=[str(image_path)],
                )
                await self._send_callback(msg)
                logger.info("EyesSendScreenshot: image sent to {}:{}", channel, chat_id)
                return f"Screenshot sent ✓\n\n{description}"
            else:
                # No send callback — still return description + path hint
                return (
                    f"Screenshot saved to {image_path} (no channel configured to send). "
                    f"\n\n{description}"
                )
        finally:
            # Clean up temp file after send
            try:
                if image_path and image_path.exists():
                    image_path.unlink(missing_ok=True)
            except Exception:
                pass


# ── eyes_screen_ascii ─────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        source=StringSchema(
            'What to capture: "screen" (default) or "camera".',
            nullable=True,
        ),
        width=StringSchema(
            "ASCII art width in characters (default 120). Wider = more detail.",
            nullable=True,
        ),
        required=[],
    )
)
class EyesScreenAsciiTool(Tool):
    """Convert a screenshot or camera frame to ASCII art for text-based vision.

    No vision model needed — converts the image to ASCII art (~0.1 s) that the
    LLM can read directly as a spatial text map.  Useful for local text-only
    LLMs (Ollama) that lack built-in vision capability.

    Call this when:
      - a fast, lightweight screen read is needed
      - the LLM needs to reason about layout/text positions on the screen

    Trigger phrases: 'quick look at the screen', 'ascii vision',
      'what is roughly on screen', 'fast screen check'
    """

    @property
    def name(self) -> str:
        return "eyes_screen_ascii"

    @property
    def description(self) -> str:
        return (
            "Capture the screen or camera and convert to ASCII art instantly (~0.1s). "
            "No vision model needed — use this when speed matters. "
            "Returns a spatial text map Serenity can reason about. "
            "NOT as accurate as eyes_screen but 60x faster. "
            "Trigger phrases: 'quick look at the screen', 'fast screen check', "
            "'roughly what is on screen', 'ascii vision'."
        )

    async def execute(
        self,
        source: str | None = None,
        width: str | None = None,
        **kwargs: Any,
    ) -> str:
        source = (source or "screen").lower()
        if source == "camera":
            if not _is_camera_enabled():
                return _CAMERA_DISABLED_MSG
        else:
            if not _is_vision_enabled():
                return _VISION_DISABLED_MSG
        try:
            w = max(40, min(200, int(width or 120)))
        except (TypeError, ValueError):
            w = 120

        from serenity.senses.camera import get_stack
        stack = get_stack()
        logger.info("EyesScreenAscii: capturing {} → ASCII (width={})", source, w)
        return await stack.snapshot_ascii(source, width=w)
