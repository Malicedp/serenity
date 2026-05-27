"""Vision RAG tools — recall and search what Serenity has seen.

Vision observations are stored automatically in two ways:
  - Continuous watching  : start vision_watch → background thread captures frames,
                           computes perceptual hash, captions with minicpm-v4.6
                           only when the frame has actually changed.
  - On-demand snapshots  : every eyes_snapshot / eyes_screen call also logs its
                           description here — no second Ollama call needed.
  - Manual saves         : vault_image_store also logs to vision memory.

Storage: ~/.serenity/visual_memory.db  (SQLite, episodic timeline)

Tools:
  vision_watch_start  — start continuous background watching (camera / screen / both)
  vision_watch_stop   — stop watching and release resources
  vision_recall       — read recent observations back into context
  vision_search       — keyword search across all captions
"""

from __future__ import annotations

from typing import Any

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import StringSchema, tool_parameters_schema


# ── vision_watch_start ────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        sources=StringSchema(
            'What to watch: "camera", "screen", or "both". Default: "screen".',
            nullable=True,
        ),
        required=[],
    )
)
class VisionWatchStartTool(Tool):
    """Start continuous background vision watching — saves what Serenity sees to memory.

    Captures frames every 0.5 s. Only captions with MiniCPM-V 4.6 when the
    frame has actually changed (perceptual hash difference). Zero Ollama calls
    on static frames.

    Call this when the user says any of:
      "start watching", "keep an eye on my screen", "watch my screen",
      "remember what you see", "start vision memory", "keep watching",
      "watch the camera", "watch me", "track what you see",
      "record what's happening", "watch both", "monitor my screen"
    """

    @property
    def name(self) -> str:
        return "vision_watch_start"

    @property
    def description(self) -> str:
        return (
            "Start continuous vision watching — Serenity captures frames in the background "
            "and stores a description whenever the scene changes. "
            'sources="screen" watches the display, "camera" watches the webcam, '
            '"both" watches both simultaneously. '
            "Only calls MiniCPM-V 4.6 when the frame actually changes — "
            "no calls on static frames. Stored to ~/.serenity/visual_memory.db. "
            "Trigger phrases: 'start watching', 'watch my screen', 'keep an eye on screen', "
            "'remember what you see', 'monitor my screen', 'track what you see'."
        )

    async def execute(self, sources: str | None = None, **kwargs: Any) -> str:
        src = (sources or "screen").lower().strip()
        source_list: list[str]
        if src == "both":
            source_list = ["screen", "camera"]
        elif src == "camera":
            source_list = ["camera"]
        else:
            source_list = ["screen"]

        try:
            from serenity.senses.visual_memory import get_service
            svc = get_service()
            if svc.is_running:
                return (
                    "Vision watching is already running. "
                    "Tell me 'stop watching' first if you want to change sources."
                )
            svc.start(sources=source_list)
            src_label = " + ".join(source_list)
            return (
                f"Vision watching started ({src_label}). "
                "I'll remember what I see whenever the scene changes. "
                "Say 'what did you see' to recall, or 'stop watching' to stop."
            )
        except Exception as exc:
            return f"Could not start vision watching: {exc}"


# ── vision_watch_stop ─────────────────────────────────────────────────────────

@tool_parameters(tool_parameters_schema(required=[]))
class VisionWatchStopTool(Tool):
    """Stop continuous vision watching and release camera/screen resources.

    Call this when the user says any of:
      "stop watching", "stop vision memory", "stop tracking",
      "stop monitoring", "stop recording what you see", "eyes off screen"
    """

    @property
    def name(self) -> str:
        return "vision_watch_stop"

    @property
    def description(self) -> str:
        return (
            "Stop the continuous vision watching background thread. "
            "Everything already captured stays in memory — nothing is deleted. "
            "Trigger phrases: 'stop watching', 'stop vision memory', 'stop monitoring', "
            "'stop tracking what you see', 'eyes off screen'."
        )

    async def execute(self, **kwargs: Any) -> str:
        try:
            from serenity.senses.visual_memory import get_service
            svc = get_service()
            if not svc.is_running:
                return "Vision watching is not running."
            svc.stop()
            return "Vision watching stopped. Everything I captured is still saved in memory."
        except Exception as exc:
            return f"Could not stop vision watching: {exc}"


# ── vision_recall ─────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        n=StringSchema(
            "How many recent observations to return. Default: 8.",
            nullable=True,
        ),
        source=StringSchema(
            'Filter by source: "camera", "screen", or leave blank for both.',
            nullable=True,
        ),
        required=[],
    )
)
class VisionRecallTool(Tool):
    """Recall what Serenity has seen recently from visual memory.

    Call this when the user says any of:
      "what did you see", "what have you seen", "what did you see earlier",
      "recall what you saw", "what was on my screen", "what did you see recently",
      "what happened on screen", "show me your visual memory",
      "what did you observe", "what were you looking at"
    """

    @property
    def name(self) -> str:
        return "vision_recall"

    @property
    def description(self) -> str:
        return (
            "Recall recent visual observations from Serenity's visual memory. "
            "Returns a timestamped list of what was seen on screen or camera. "
            "Trigger phrases: 'what did you see', 'what have you seen recently', "
            "'what was on my screen earlier', 'recall what you saw', "
            "'what did you observe', 'what were you looking at'."
        )

    async def execute(
        self,
        n: str | None = None,
        source: str | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            count = max(1, min(30, int(n or 8)))
        except (TypeError, ValueError):
            count = 8

        src = (source or "").lower().strip() or None
        if src not in (None, "camera", "screen"):
            src = None

        try:
            from serenity.senses.visual_memory import get_service
            svc = get_service()
            block = svc.format_for_prompt(n=count, source=src)
            if not block:
                return (
                    "Nothing in visual memory yet. "
                    "Say 'start watching' to begin, or take a snapshot first."
                )
            return block
        except Exception as exc:
            return f"Could not read visual memory: {exc}"


# ── vision_search ─────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        query=StringSchema("Keyword or phrase to search for in visual memory captions."),
        limit=StringSchema(
            "Max results to return. Default: 10.",
            nullable=True,
        ),
        required=["query"],
    )
)
class VisionSearchTool(Tool):
    """Search visual memory for a specific keyword or phrase.

    Call this when the user says any of:
      "did you see X", "have you seen X", "search your visual memory for X",
      "look through what you saw for X", "when did you see X",
      "find X in what you saw", "did you notice X"
    """

    @property
    def name(self) -> str:
        return "vision_search"

    @property
    def description(self) -> str:
        return (
            "Search all visual memory captions for a keyword or phrase. "
            "Returns matching observations with timestamps and source. "
            "Trigger phrases: 'did you see X', 'have you seen X', "
            "'search what you saw for X', 'find X in your visual memory', "
            "'when did you see X', 'did you notice X'."
        )

    async def execute(
        self,
        query: str,
        limit: str | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            lim = max(1, min(50, int(limit or 10)))
        except (TypeError, ValueError):
            lim = 10

        try:
            from serenity.senses.visual_memory import get_service
            svc = get_service()
            rows = svc.search(query.strip(), limit=lim)
            if not rows:
                return f"Nothing found in visual memory matching '{query}'."

            from datetime import datetime
            lines = [f"[Vision search: '{query}' — {len(rows)} result(s)]"]
            for row in rows:
                ts_str = datetime.fromtimestamp(row["ts"]).strftime("%Y-%m-%d %H:%M:%S")
                src    = row["source"].upper()
                cap    = row["caption"] or "(no caption)"
                lines.append(f"  {ts_str}  {src}  {cap}")
            return "\n".join(lines)
        except Exception as exc:
            return f"Could not search visual memory: {exc}"
