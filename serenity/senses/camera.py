"""Camera vision stack — simple, clean, minicpm-v4.6 only.

Components:
  mss              — fast screen capture
  OpenCV           — camera frame grab (VideoCapture only, no analysis)
  minicpm-v4.6     — scene/camera description via Ollama API (1.6 GB, no torch)

Lifecycle:

    stack = get_stack()
    stack.open()              # opens camera device (no model loading)
    stack.ambient             # {"eyes_open": bool, "last_description": str}
    stack.format_ambient()    # human-readable block for system prompt injection
    await stack.snapshot("camera")   # minicpm-v4.6 describes camera frame → text
    await stack.snapshot("screen")   # screenshot → minicpm-v4.6 describes → text
    stack.close()             # releases camera → zero overhead
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from typing import Any

from loguru import logger

# ── Module-level singleton ────────────────────────────────────────────────────
_stack: "VisionStack | None" = None
_stack_lock = threading.Lock()


def get_stack() -> "VisionStack":
    global _stack
    with _stack_lock:
        if _stack is None:
            _stack = VisionStack()
    return _stack


# ── VisionStack ───────────────────────────────────────────────────────────────

class VisionStack:
    """Minimal vision stack — camera grab + minicpm-v4.6 via Ollama. No background loop."""

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self._cap: Any = None

        self._ambient: dict[str, Any] = {
            "eyes_open":        False,
            "last_description": "",
        }

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    @property
    def ambient(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._ambient)

    def format_ambient(self) -> str:
        """Format ambient state as a system-prompt block."""
        a = self.ambient
        if not a.get("eyes_open"):
            return ""
        desc = a.get("last_description", "")
        if not desc:
            return ""
        return "\n".join([
            "[Vision — Serenity's camera awareness]",
            desc,
            "[/Vision]",
        ])

    def open(self, camera_index: int = 0) -> str:
        """Open the camera device."""
        if self.is_open:
            return "Vision is already open."

        try:
            import cv2  # type: ignore
        except ImportError:
            return "Camera requires opencv-python. Run: pip install opencv-python"

        cap = cv2.VideoCapture(camera_index)
        if not cap.isOpened():
            return f"Cannot open camera at index {camera_index}."
        self._cap = cap

        with self._state_lock:
            self._ambient["eyes_open"] = True

        logger.info("VisionStack: opened (camera {})", camera_index)
        return "ok"

    def close(self) -> None:
        """Release camera — zero overhead."""
        if self._cap is not None:
            self._cap.release()
            self._cap = None

        with self._state_lock:
            self._ambient = {
                "eyes_open":        False,
                "last_description": "",
            }
        logger.info("VisionStack: closed")

    async def snapshot(self, source: str = "camera") -> str:
        """Capture a frame and describe it with minicpm-v4.6 via Ollama."""
        import asyncio
        loop = asyncio.get_running_loop()
        description, _ = await loop.run_in_executor(None, self._snapshot_sync, source, False)
        return description

    async def snapshot_with_image(self, source: str = "camera") -> tuple[str, Path | None]:
        """Like snapshot() but also returns the saved image path.

        The caller is responsible for deleting the file when done.
        Returns (description, path) — path is None if capture failed.
        """
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._snapshot_sync, source, True)

    async def snapshot_ascii(self, source: str = "screen", width: int = 120) -> str:
        """Capture a frame and convert to ASCII art — no vision model needed.

        Returns a text block the LLM can reason about directly.
        Useful for text-only LLMs that don't have vision capability.
        """
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._snapshot_ascii_sync, source, width)

    def _snapshot_ascii_sync(self, source: str, width: int) -> str:
        """Blocking: capture frame → PIL → ASCII art block."""
        image_path = self._capture_frame(source)
        if image_path is None:
            return "Could not capture frame for ASCII conversion."
        try:
            return _image_to_ascii(image_path, width=width)
        finally:
            try:
                image_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ── Snapshot (minicpm-v4.6 via Ollama) ───────────────────────────────────

    def _snapshot_sync(self, source: str, keep_image: bool = False) -> tuple[str, Path | None]:
        """Blocking: capture frame, describe with minicpm-v4.6 via Ollama."""
        image_path = self._capture_frame(source)
        if image_path is None:
            return "Could not capture frame.", None
        try:
            description = self._describe_with_ollama_vision(image_path)
        except Exception as exc:
            description = f"Vision description failed: {exc}"
        else:
            with self._state_lock:
                self._ambient["last_description"] = description
        if keep_image:
            return description, image_path
        try:
            image_path.unlink(missing_ok=True)
        except Exception:
            pass
        return description, None

    def _capture_frame(self, source: str) -> Path | None:
        """Capture a single frame to a temp file. Returns path or None."""
        if source == "screen":
            try:
                import mss        # type: ignore
                import mss.tools  # type: ignore
                tmp = Path(tempfile.mktemp(suffix=".png"))
                with mss.mss() as sct:
                    monitor = sct.monitors[1]
                    shot    = sct.grab(monitor)
                    mss.tools.to_png(shot.rgb, shot.size, output=str(tmp))
                return tmp
            except Exception as exc:
                logger.warning("VisionStack: screen capture failed — {}", exc)
                return None

        # Camera
        try:
            import cv2  # type: ignore
            tmp = Path(tempfile.mktemp(suffix=".jpg"))
            if self._cap is not None and self._cap.isOpened():
                ret, frame = self._cap.read()
            else:
                cap = cv2.VideoCapture(0)
                ret, frame = cap.read()
                cap.release()
            if not ret:
                return None
            cv2.imwrite(str(tmp), frame)
            return tmp
        except Exception as exc:
            logger.warning("VisionStack: camera capture failed — {}", exc)
            return None

    # Cap vision output so a verbose model doesn't bloat LLM context.
    _VISION_MAX_CHARS = 600

    # Ollama vision model — minicpm-v4.6 is 1.6 GB, runs on CPU, no torch needed.
    _VISION_MODEL = "openbmb/minicpm-v4.6:latest"
    _OLLAMA_BASE  = "http://localhost:11434"

    def _describe_with_ollama_vision(self, image_path: Path) -> str:
        """Describe an image using minicpm-v4.6 via Ollama API.

        No model loading/unloading — Ollama manages the lifecycle.
        Requires Ollama running locally with openbmb/minicpm-v4.6:latest pulled.
        """
        import base64
        import json
        import urllib.request

        try:
            image_b64 = base64.b64encode(image_path.read_bytes()).decode()
        except Exception as exc:
            return f"Vision: could not read image — {exc}"

        payload = json.dumps({
            "model":  self._VISION_MODEL,
            "prompt": "Briefly describe what you see in 2-3 sentences. Be concise.",
            "images": [image_b64],
            "stream": False,
        }).encode()

        req = urllib.request.Request(
            f"{self._OLLAMA_BASE}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        try:
            logger.info("VisionStack: sending frame to {} via Ollama…", self._VISION_MODEL)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            raw = data.get("response", "").strip()
            if len(raw) > self._VISION_MAX_CHARS:
                raw = raw[: self._VISION_MAX_CHARS].rsplit(" ", 1)[0] + "…"
            logger.info("VisionStack: description received ({} chars)", len(raw))
            return raw or "No description returned."
        except Exception as exc:
            logger.warning("VisionStack: Ollama vision call failed — {}", exc)
            return f"Vision description failed: {exc}"


# ── Image-to-ASCII ────────────────────────────────────────────────────────────
# Converts a screenshot or camera frame to ASCII art using Pillow (already
# installed).  No extra dependencies.  Gives text-only LLMs a way to reason
# about screen content without needing a vision model.

_ASCII_CHARS = " .:-=+*#%@"   # dark → light gradient, 10 levels

def _image_to_ascii(image_path: "Path", width: int = 120) -> str:
    """Convert an image file to an ASCII art block.

    Args:
        image_path: path to a PNG/JPG image.
        width:      number of ASCII columns.

    Returns:
        Multi-line ASCII string wrapped in a code block, plus a brief legend.
    """
    try:
        from PIL import Image  # type: ignore

        img = Image.open(image_path).convert("L")   # grayscale

        # Terminal chars are ~2× taller than wide — compensate
        orig_w, orig_h = img.size
        height = max(1, int((width / orig_w) * orig_h * 0.45))
        img    = img.resize((width, height), Image.LANCZOS)

        rows: list[str] = []
        pixels = list(img.getdata())
        for row_idx in range(height):
            row_pixels = pixels[row_idx * width : (row_idx + 1) * width]
            row_chars  = "".join(
                _ASCII_CHARS[int(p / 256 * len(_ASCII_CHARS))] for p in row_pixels
            )
            rows.append(row_chars)

        body = "\n".join(rows)
        legend = (
            f"[ASCII art — {width}×{height} chars from {orig_w}×{orig_h}px image. "
            "Dark chars = dark regions; light/space = bright regions. "
            "Read as a rough spatial map of the screen.]"
        )
        return f"```\n{body}\n```\n{legend}"

    except Exception as exc:
        return f"ASCII conversion failed: {exc}"
