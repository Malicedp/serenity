"""OBS Studio control tools — Serenity can control OBS via WebSocket.

Setup:
  1. In OBS: Tools → WebSocket Server Settings → Enable
  2. Set a password (recommended) and note the port (default 4455)
  3. Add to ~/.serenity/config.json:
       "obs": { "host": "localhost", "port": 4455, "password": "yourpassword" }

Requires: pip install obsws-python
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from serenity.agent.tools.base import Tool

# ── Config loader ─────────────────────────────────────────────────────────────

def _load_obs_cfg() -> dict:
    cfg_path = Path.home() / ".serenity" / "config.json"
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        return raw.get("obs", {})
    except Exception:
        return {}


def _get_obs():
    """Return a connected obsws_python ReqClient."""
    try:
        import obsws_python as obs  # type: ignore
    except ImportError:
        raise RuntimeError(
            "obsws-python not installed. Run: pip install obsws-python\n"
            "Also make sure OBS WebSocket server is enabled: "
            "OBS → Tools → WebSocket Server Settings → Enable"
        )

    cfg = _load_obs_cfg()
    host = cfg.get("host", "localhost")
    port = int(cfg.get("port", 4455))
    password = cfg.get("password", "")

    if not password:
        raise RuntimeError(
            "OBS password not set. Add to ~/.serenity/config.json:\n"
            '  "obs": {"host": "localhost", "port": 4455, "password": "yourpassword"}'
        )

    try:
        client = obs.ReqClient(host=host, port=port, password=password, timeout=5)
        return client
    except Exception as exc:
        raise RuntimeError(
            f"Cannot connect to OBS at {host}:{port}. "
            "Make sure OBS is open and WebSocket server is enabled. "
            f"Error: {exc}"
        )


# ── obs_status ────────────────────────────────────────────────────────────────

class OBSStatusTool(Tool):
    """Check OBS recording and streaming status."""

    @property
    def name(self) -> str:
        return "obs_status"

    @property
    def description(self) -> str:
        return "Get OBS status — current scene, whether recording, whether streaming."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        def _do() -> str:
            cl = _get_obs()
            scene = cl.get_current_program_scene().current_program_scene_name
            rec = cl.get_record_status()
            stream = cl.get_stream_status()

            rec_active = rec.output_active
            stream_active = stream.output_active
            rec_time = getattr(rec, "output_timecode", "—")
            stream_time = getattr(stream, "output_timecode", "—")

            lines = [f"Scene: {scene}"]
            lines.append(f"Recording: {'🔴 ' + rec_time if rec_active else 'stopped'}")
            lines.append(f"Streaming: {'🔴 ' + stream_time if stream_active else 'stopped'}")
            return "\n".join(lines)

        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:
            return f"❌ {exc}"


# ── obs_scenes ────────────────────────────────────────────────────────────────

class OBSSceneListTool(Tool):
    """List all OBS scenes."""

    @property
    def name(self) -> str:
        return "obs_scenes"

    @property
    def description(self) -> str:
        return "List all available OBS scenes and show which one is active."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        def _do() -> str:
            cl = _get_obs()
            current = cl.get_current_program_scene().current_program_scene_name
            scenes = cl.get_scene_list().scenes
            names = [s.get("sceneName", str(s)) for s in reversed(scenes)]
            lines = [f"{'▶ ' if n == current else '  '}{n}" for n in names]
            return "OBS Scenes:\n" + "\n".join(lines)

        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:
            return f"❌ {exc}"


# ── obs_set_scene ─────────────────────────────────────────────────────────────

from serenity.agent.tools.base import tool_parameters
from serenity.agent.tools.schema import StringSchema, tool_parameters_schema


@tool_parameters(
    tool_parameters_schema(
        scene=StringSchema("Name of the scene to switch to (must match exactly)."),
        required=["scene"],
    )
)
class OBSSetSceneTool(Tool):
    """Switch the active OBS scene."""

    @property
    def name(self) -> str:
        return "obs_set_scene"

    @property
    def description(self) -> str:
        return "Switch OBS to a different scene by name. Use obs_scenes to see available scene names."

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, scene: str, **kwargs: Any) -> str:
        def _do() -> str:
            cl = _get_obs()
            cl.set_current_program_scene(scene)
            return f"✅ Switched to scene: {scene}"

        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:
            return f"❌ {exc}"


# ── obs_start_recording / obs_stop_recording ──────────────────────────────────

class OBSStartRecordingTool(Tool):
    @property
    def name(self) -> str:
        return "obs_start_recording"

    @property
    def description(self) -> str:
        return "Start recording in OBS."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> str:
        def _do() -> str:
            cl = _get_obs()
            rec = cl.get_record_status()
            if rec.output_active:
                return "⚠ Already recording."
            cl.start_record()
            return "🔴 Recording started."

        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:
            return f"❌ {exc}"


class OBSStopRecordingTool(Tool):
    @property
    def name(self) -> str:
        return "obs_stop_recording"

    @property
    def description(self) -> str:
        return "Stop recording in OBS."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> str:
        def _do() -> str:
            cl = _get_obs()
            rec = cl.get_record_status()
            if not rec.output_active:
                return "⚠ Not currently recording."
            resp = cl.stop_record()
            path = getattr(resp, "output_path", "")
            return f"⏹ Recording stopped.{' Saved to: ' + path if path else ''}"

        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:
            return f"❌ {exc}"


# ── obs_start_streaming / obs_stop_streaming ──────────────────────────────────

class OBSStartStreamingTool(Tool):
    @property
    def name(self) -> str:
        return "obs_start_streaming"

    @property
    def description(self) -> str:
        return "Start streaming in OBS (uses whatever stream destination is configured in OBS)."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> str:
        def _do() -> str:
            cl = _get_obs()
            stream = cl.get_stream_status()
            if stream.output_active:
                return "⚠ Already streaming."
            cl.start_stream()
            return "🔴 Stream started."

        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:
            return f"❌ {exc}"


class OBSStopStreamingTool(Tool):
    @property
    def name(self) -> str:
        return "obs_stop_streaming"

    @property
    def description(self) -> str:
        return "Stop the active OBS stream."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> str:
        def _do() -> str:
            cl = _get_obs()
            stream = cl.get_stream_status()
            if not stream.output_active:
                return "⚠ Not currently streaming."
            cl.stop_stream()
            return "⏹ Stream stopped."

        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:
            return f"❌ {exc}"


# ── obs_toggle_mute ───────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        source=StringSchema(
            "Name of the audio source to mute/unmute (e.g. 'Mic/Aux', 'Desktop Audio'). "
            "Use obs_status to see the current scene, then check OBS for source names."
        ),
        required=["source"],
    )
)
class OBSToggleMuteTool(Tool):
    @property
    def name(self) -> str:
        return "obs_toggle_mute"

    @property
    def description(self) -> str:
        return "Toggle mute on an OBS audio source (e.g. 'Mic/Aux', 'Desktop Audio')."

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, source: str, **kwargs: Any) -> str:
        def _do() -> str:
            cl = _get_obs()
            cl.toggle_input_mute(source)
            muted = cl.get_input_mute(source).input_muted
            state = "muted" if muted else "unmuted"
            return f"🎙 '{source}' is now {state}."

        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:
            return f"❌ {exc}"
