"""vault_image — store and recall images in the vault.

Images are saved to:
    <workspace>/Images/<uuid>.<ext>       — the image file
    <workspace>/Images/<uuid>.md          — Obsidian-compatible companion note

The Images/ folder lives INSIDE the agent workspace (vault), so:
  - It moves with the vault if the user changes their workspace path
  - It is never hardcoded to any user's home directory
  - Obsidian can display the images inline via ![[uuid.jpg]] syntax
  - vault grep can search the descriptions without touching NNN

Companion note format::

    ---
    date: 2026-05-13
    tags: [image, vision, camera]
    source: camera | screen
    ---

    # My coffee setup
    *2026-05-13 14:32:01*

    ![[abc123.jpg]]

    ## Description
    A white ceramic mug on a wooden desk next to a laptop...

    ## Label
    My coffee setup

Tools:
  vault_image_store   — snap + describe + save to workspace/Images/
  vault_image_recall  — list recent saved images with their descriptions
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema


def _images_dir(workspace: Path) -> Path:
    """Return the Images folder inside the vault. Created on first use."""
    d = workspace / "Images"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── vault_image_store ─────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        source=StringSchema(
            '"camera" (default) or "screen". '
            "camera snaps from the webcam. screen captures the display.",
            nullable=True,
        ),
        label=StringSchema(
            "Short title / label for this image — becomes the note heading. "
            "E.g. 'My desk setup', 'Error message', 'Whiteboard diagram'. "
            "If not provided, the MiniCPM-V 4.6 description is used as the label.",
            nullable=True,
        ),
        describe=StringSchema(
            '"yes" (default) — describe the image with MiniCPM-V 4.6 before saving. '
            '"no" — save the raw image without description (faster).',
            nullable=True,
        ),
        required=[],
    )
)
class VaultImageStoreTool(Tool):
    """Snap a frame and save it to workspace/Images/ with an Obsidian companion note.

    Call this when the user says any of:
      "save what you see", "remember this", "take a picture and save it",
      "save this image", "remember what you see", "save a photo",
      "capture this", "keep this image", "save to vault",
      "remember my screen", "save a screenshot", "store this image"
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "vault_image_store"

    @property
    def description(self) -> str:
        return (
            "Snap a frame (camera or screen) and save it to vault/Images/ "
            "with an Obsidian-compatible companion note containing the description. "
            "Trigger phrases: 'save what you see', 'remember this', 'take a picture and save it', "
            "'save this image', 'remember what you see', 'save a photo', "
            "'capture this', 'keep this image', 'save to vault', "
            "'remember my screen', 'save a screenshot', 'store this image'. "
            "describe='yes' (default) runs MiniCPM-V 4.6 via Ollama. describe='no' saves raw only."
        )

    async def execute(
        self,
        source: str | None = None,
        label: str | None = None,
        describe: str | None = None,
        **kwargs: Any,
    ) -> str:
        import asyncio

        src = (source or "camera").lower().strip()
        should_describe = (describe or "yes").lower().strip() not in ("no", "false", "0")

        from serenity.senses.camera import get_stack
        stack = get_stack()

        # Capture frame
        loop = asyncio.get_running_loop()
        image_path: Path | None = await loop.run_in_executor(
            None, stack._capture_frame, src
        )
        if image_path is None:
            return f"Could not capture frame from {src}."

        # Optionally describe with minicpm-v4.6 via Ollama
        description = ""
        if should_describe:
            logger.info("VaultImage: running MiniCPM-V 4.6 description…")
            description = await loop.run_in_executor(
                None, stack._describe_with_ollama_vision, image_path
            )

        # Persist to vault/Images/
        try:
            result = await loop.run_in_executor(
                None, self._save, image_path, src, label, description
            )
            # Temp file already moved — no need to unlink
        except Exception as exc:
            try:
                image_path.unlink(missing_ok=True)
            except Exception:
                pass
            return f"Failed to save image: {exc}"

        # Also log to vision RAG — reuse description, no second Ollama call
        if description:
            try:
                from serenity.senses.visual_memory import get_service as _get_vm
                _get_vm().store_caption(src, f"[saved to vault] {description}")
            except Exception:
                pass

        return result

    def _save(
        self,
        image_path: Path,
        source: str,
        label: str | None,
        description: str,
    ) -> str:
        entry_id  = str(uuid.uuid4())
        images_dir = _images_dir(self._workspace)
        dest_img  = images_dir / f"{entry_id}{image_path.suffix}"

        # Move temp file into vault
        shutil.move(str(image_path), str(dest_img))

        now        = datetime.now()
        date_str   = now.strftime("%Y-%m-%d")
        ts_str     = now.strftime("%Y-%m-%d %H:%M:%S")
        note_title = label or (description[:60].rstrip() + "…" if description else "Captured image")
        tags       = f"image, vision, {source}"

        # Obsidian wiki-link embeds the image inline
        embed_link = f"![[{dest_img.name}]]"

        note_lines = [
            "---",
            f"date: {date_str}",
            f"tags: [{tags}]",
            f"source: {source}",
            f"image: {dest_img.name}",
            "---",
            "",
            f"# {note_title}",
            f"*{ts_str}*",
            "",
            embed_link,
            "",
        ]
        if description:
            note_lines += [
                "## Description",
                description.strip(),
                "",
            ]
        if label:
            note_lines += [
                "## Label",
                label.strip(),
                "",
            ]

        # Write JSON sidecar for machine-readable recall
        meta = {
            "id":          entry_id,
            "timestamp":   time.time(),
            "source":      source,
            "label":       note_title,
            "description": description,
            "image_file":  dest_img.name,
        }
        json_path = images_dir / f"{entry_id}.json"
        json_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        # Write companion .md (Obsidian note)
        md_path = images_dir / f"{entry_id}.md"
        md_path.write_text("\n".join(note_lines), encoding="utf-8")

        logger.info("VaultImage: saved {} ({})", dest_img.name, source)
        return (
            f"✅ Image saved to vault/Images/\n"
            f"   File:  {dest_img.name}\n"
            f"   Note:  {md_path.name}\n"
            f"   Label: {note_title}\n"
            + (f"   Description: {description[:120]}…" if len(description) > 120 else
               f"   Description: {description}" if description else "")
        )


# ── vault_image_recall ────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        limit=IntegerSchema(
            5,
            description="Number of most recent images to return (default 5, max 20).",
            minimum=1,
            maximum=20,
        ),
        source=StringSchema(
            'Filter by source: "camera", "screen", or leave blank for all.',
            nullable=True,
        ),
        required=[],
    )
)
class VaultImageRecallTool(Tool):
    """List recently saved images from vault/Images/ with their descriptions.

    Call this when the user says any of:
      "what pictures have you saved", "show me saved images",
      "what have you remembered visually", "recall saved images",
      "what images are in the vault", "show me your image memory",
      "what photos have you taken"
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "vault_image_recall"

    @property
    def description(self) -> str:
        return (
            "List recently saved images from vault/Images/ with their descriptions. "
            "Trigger phrases: 'what pictures have you saved', 'show me saved images', "
            "'what have you remembered visually', 'recall saved images', "
            "'what images are in the vault', 'show me your image memory', "
            "'what photos have you taken'."
        )

    async def execute(
        self,
        limit: int = 5,
        source: str | None = None,
        **kwargs: Any,
    ) -> str:
        images_dir = _images_dir(self._workspace)
        json_files = sorted(
            images_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        src_filter = source.lower().strip() if source else None
        results: list[dict] = []

        for jf in json_files:
            try:
                meta = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:
                continue
            if src_filter and meta.get("source", "").lower() != src_filter:
                continue
            results.append(meta)
            if len(results) >= limit:
                break

        if not results:
            return "No images saved in vault/Images/ yet."

        lines = [f"vault/Images/ — {len(results)} image(s):"]
        for r in results:
            ts  = datetime.fromtimestamp(r.get("timestamp", 0)).strftime("%Y-%m-%d %H:%M")
            lbl = r.get("label", "Untitled")
            src = r.get("source", "?")
            desc = r.get("description", "")
            snippet = (desc[:100] + "…") if len(desc) > 100 else desc
            lines.append(f"\n  [{ts}] [{src}] {lbl}")
            if snippet:
                lines.append(f"    {snippet}")

        return "\n".join(lines)
