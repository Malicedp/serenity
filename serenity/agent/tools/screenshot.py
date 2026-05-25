# Copyright © 2026 Daniel T Niamke. All rights reserved.
"""ScreenshotTool — capture screen or webcam and open it in the OS image viewer.

source="screen"  → fullscreen grab via MSS
source="camera"  → webcam frame via OpenCV
source="both"    → screen + camera (two viewer windows)

LLM routing:
  "show me your screen / what's on screen"   → screen
  "look around / what do you see around you" → camera
  "show me what you see" (ambiguous)         → screen (default)
  "show me everything / screen and camera"   → both
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from loguru import logger

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import StringSchema, tool_parameters_schema


@tool_parameters(
    tool_parameters_schema(
        source=StringSchema(
            description=(
                "What to capture. "
                '"screen" = monitor/display (default). '
                '"camera" = webcam / real-world view. '
                '"both" = screen and camera together.'
            ),
            nullable=True,
        ),
        required=[],
    )
)
class ScreenshotTool(Tool):
    """Capture screen or webcam and open the image so the user can see what Serenity sees."""

    @property
    def name(self) -> str:
        return "screenshot"

    @property
    def description(self) -> str:
        return (
            "Capture the screen or webcam and open the image in the user's viewer — "
            "this shows the USER the image but does NOT give Serenity a description. "
            "Use this ONLY when the user wants to SEE an image opened on their PC. "
            'source="screen": open a screenshot in their image viewer. '
            'source="camera": open a webcam photo in their image viewer. '
            'source="both": open both. '
            "Do NOT use this to answer 'what is on my screen' or 'what do you see' — "
            "use eyes_screen or eyes_snapshot instead (those give Serenity a description)."
        )

    async def execute(self, source: str | None = None, **_kwargs: Any) -> str:
        src = (source or "screen").strip().lower()
        if src not in ("screen", "camera", "both"):
            src = "screen"

        # ── Config gates (mirror eyes.py logic) ──────────────────────────────
        try:
            from serenity.config.loader import load_config
            _vis = load_config().senses.vision
            _vision_on = _vis.enabled
            _camera_on = _vis.enabled and _vis.camera_enabled
        except Exception:
            _vision_on = _camera_on = False

        if src in ("camera", "both") and not _camera_on:
            return (
                "Camera access is not enabled. "
                "Run `serenity onboard` → [E] Senses & Vision and enable Camera."
            )
        if src == "screen" and not _vision_on:
            return (
                "Vision is not enabled. "
                "Run `serenity onboard` → [E] Senses & Vision to enable it."
            )
        # ─────────────────────────────────────────────────────────────────────

        paths: list[Path] = []

        if src in ("screen", "both"):
            p = _grab_screen()
            if p:
                paths.append(p)
            else:
                return (
                    "I couldn't capture the screen. "
                    "Make sure `mss` and `Pillow` are installed "
                    "(run `sense/install_senses.bat` to set up)."
                )

        if src in ("camera", "both"):
            p = _grab_camera()
            if p:
                paths.append(p)
            else:
                if src == "camera":
                    return (
                        "I couldn't access the webcam. "
                        "Make sure `opencv-python` is installed and a camera is connected "
                        "(run `sense/install_senses.bat` to set up)."
                    )
                # 'both' but camera unavailable — fall back to screen only
                logger.warning("ScreenshotTool: camera unavailable, showing screen only")

        if not paths:
            return "I couldn't capture anything."

        for p in paths:
            _open(p)
            logger.info("ScreenshotTool: opened {}", p)

        labels = {"screen": "screen", "camera": "camera", "both": "screen and camera"}
        return f"I've opened the {labels.get(src, 'capture')} in your image viewer."


# ── capture helpers ───────────────────────────────────────────────────────────

def _grab_screen() -> Path | None:
    """Fullscreen capture via MSS. Returns saved PNG path or None."""
    try:
        import mss              # type: ignore
        from PIL import Image   # type: ignore

        with mss.MSS() as sct:
            shot = sct.grab(sct.monitors[0])

        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        if img.width > 1920:
            ratio = 1920 / img.width
            img   = img.resize((1920, int(img.height * ratio)), Image.LANCZOS)

        out = Path(tempfile.gettempdir()) / f"serenity_screen_{int(time.time())}.png"
        img.save(out, "PNG", optimize=True)
        return out

    except ImportError:
        logger.warning("ScreenshotTool: mss or Pillow not installed")
        return None
    except Exception as e:
        logger.warning("ScreenshotTool: screen capture failed — {}", e)
        return None


def _grab_camera() -> Path | None:
    """Single webcam frame via OpenCV. Returns saved PNG path or None."""
    try:
        import cv2  # type: ignore

        cam_index = 0
        try:
            from serenity.config.loader import load_config
            cam_index = load_config().senses.vision.camera_index
        except Exception:
            pass

        cap = cv2.VideoCapture(cam_index)
        if not cap.isOpened():
            logger.warning("ScreenshotTool: could not open camera {}", cam_index)
            return None

        # Skip a few frames so auto-exposure settles
        for _ in range(3):
            cap.read()
        ret, frame = cap.read()
        cap.release()

        if not ret or frame is None:
            logger.warning("ScreenshotTool: camera read returned no frame")
            return None

        out = Path(tempfile.gettempdir()) / f"serenity_camera_{int(time.time())}.png"
        cv2.imwrite(str(out), frame)
        return out

    except ImportError:
        logger.warning("ScreenshotTool: opencv-python not installed")
        return None
    except Exception as e:
        logger.warning("ScreenshotTool: camera capture failed — {}", e)
        return None


def _open(path: Path) -> None:
    """Open an image in the OS default viewer. Fire-and-forget."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        logger.warning("ScreenshotTool: could not open viewer — {}", e)
