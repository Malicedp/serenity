#!/usr/bin/env python3
# Copyright © 2026 Daniel T Niamke. All rights reserved.
"""
Serenity MCP Server
===================
Gives any Claude agent (Claude Code, Claude Desktop, custom agents) access
to Serenity's long-term memory — the same memory Serenity herself uses.

Both Serenity and Claude can read and write to it. When Claude Code learns
something about your codebase it goes here. When Serenity notices something
about you it goes here too. One shared brain.

HOW TO CONNECT (Claude Code)
─────────────────────────────
Add to ~/.claude/mcp.json  (global — works in every project):

  {
    "mcpServers": {
      "serenity": {
        "command": "python",
        "args": ["/path/to/serenity/serenity_mcp.py"]
      }
    }
  }

  Replace /path/to/serenity/ with the actual path where you cloned the repo.
  Example (Windows):  C:/Users/YourName/Documents/serenity/serenity_mcp.py
  Example (macOS/Linux):  /home/yourname/serenity/serenity_mcp.py

Or add .mcp.json to any project folder to enable it just for that project.
Then restart Claude Code. The tools appear automatically — no setup needed.

TOOLS AVAILABLE AFTER CONNECTING
──────────────────────────────────
  serenity_recall   — search memory before starting a task
  serenity_remember — save something worth keeping after a task
  serenity_status   — check how many memories exist and confirm connection
"""

from __future__ import annotations

import asyncio
import sys
import os
from pathlib import Path
from datetime import datetime

# ── Make sure serenity packages are importable ────────────────────────────────
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# ── MCP SDK ───────────────────────────────────────────────────────────────────
try:
    import mcp.server.stdio
    import mcp.types as types
    from mcp.server import NotificationOptions, Server
    from mcp.server.models import InitializationOptions
except ImportError:
    print(
        "ERROR: mcp package not installed.\n"
        "Run:  pip install mcp\n",
        file=sys.stderr,
    )
    sys.exit(1)

# ── NNN authorisation (same logic as Serenity gateway) ───────────────────────

def _authorise_nnn() -> bool:
    """Unlock NNN using the licence key stored in Serenity's config.

    Returns True if auth succeeded, False if NNN is unavailable.
    """
    try:
        from serenity.licence import generate_nnn_token, is_master_key_active
        from serenity_nnn import nnn as _nnn

        if is_master_key_active():
            _nnn.authorize(generate_nnn_token("MASTER"))
            return True

        from serenity.config.loader import load_config
        cfg = load_config()
        if cfg.licence_key:
            _nnn.authorize(generate_nnn_token(cfg.licence_key))
            return True

    except Exception as e:
        print(f"NNN auth failed: {e}", file=sys.stderr)

    return False

# ── Build the MCP server ──────────────────────────────────────────────────────

server = Server("serenity-memory")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="serenity_recall",
            description=(
                "Search Serenity's long-term memory for anything relevant to what you're working on.\n\n"
                "Use this:\n"
                "  • At the START of a coding session or new task — check for prior context\n"
                "  • When you hit a problem that feels familiar — check if it was solved before\n"
                "  • When you want to know what decisions were made about this codebase\n"
                "  • When Serenity (the AI assistant) noticed something you want to retrieve\n\n"
                "Memory is shared between you and Serenity. Anything either of you stored is searchable here.\n"
                "Returns the most relevant memories with their content and when they were stored."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "What you're looking for — describe the topic, task, or question in plain language. "
                            "Examples: 'auth bug fixes', 'how the licence server works', "
                            "'Daniel's preferred code style', 'NNN memory architecture'"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of memories to return (default 5, max 15)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="serenity_remember",
            description=(
                "Store something in Serenity's long-term memory so it persists across sessions.\n\n"
                "Use this:\n"
                "  • After fixing a bug — note what the root cause was and how you fixed it\n"
                "  • After making an important decision — record what you decided and why\n"
                "  • When you discover something non-obvious about the codebase or the user\n"
                "  • When you complete a meaningful task — summarise what was done\n"
                "  • When you spot a recurring pattern worth remembering\n\n"
                "Serenity will also be able to recall what you store here — you're teaching "
                "the shared brain, not just a personal notepad.\n\n"
                "Write content that is self-contained: it will be read in a future session "
                "with no surrounding context, so include enough detail to be useful on its own."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "What to remember. Be specific and self-contained. "
                            "Good: 'Fixed auth loop in serenity/licence.py — the signing key was XOR-decoded "
                            "at import time but only used after SIGNING_KEY env var was set, causing silent "
                            "failures when env was missing.' "
                            "Bad: 'fixed the bug'"
                        ),
                    },
                    "tags": {
                        "type": "string",
                        "description": (
                            "Optional comma-separated labels to help with retrieval later. "
                            "Examples: 'bug-fix,auth', 'architecture,nnn', 'user-preference'"
                        ),
                        "default": "",
                    },
                },
                "required": ["content"],
            },
        ),
        types.Tool(
            name="serenity_status",
            description=(
                "Check the connection to Serenity's memory and see how many memories exist. "
                "Use this to confirm the MCP server is working correctly."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:

    # ── serenity_recall ───────────────────────────────────────────────────────
    if name == "serenity_recall":
        query_text = arguments.get("query", "").strip()
        limit      = min(int(arguments.get("limit", 5)), 15)

        if not query_text:
            return [types.TextContent(type="text", text="Error: query cannot be empty.")]

        try:
            from serenity_nnn import nnn as _nnn

            # token_budget controls how much text NNN returns total
            # ~200 tokens per result × limit is a reasonable estimate
            results = await asyncio.get_running_loop().run_in_executor(
                None, lambda: _nnn.query(query_text, token_budget=300 * limit)
            )

            if not results:
                return [types.TextContent(
                    type="text",
                    text=(
                        f"No memories found for: '{query_text}'\n\n"
                        "Serenity's memory may be empty or nothing relevant has been stored yet. "
                        "Use serenity_remember to start building it."
                    ),
                )]

            lines = [f"Found {len(results)} memor{'y' if len(results)==1 else 'ies'} for '{query_text}':\n"]
            for i, r in enumerate(results[:limit], 1):
                bundle = r.bundle
                score  = round(r.score, 3)
                # Get the most representative node text
                if bundle.nodes:
                    top_node = max(bundle.nodes, key=lambda n: n.activation)
                    text = top_node.content
                else:
                    text = bundle.centroid_text or "(no content)"

                lines.append(f"[{i}] score={score}")
                lines.append(f"    {text[:400]}")
                lines.append("")

            return [types.TextContent(type="text", text="\n".join(lines))]

        except RuntimeError as e:
            if "not authorised" in str(e).lower():
                return [types.TextContent(
                    type="text",
                    text=(
                        "Serenity memory is locked — NNN requires a valid Serenity licence.\n"
                        "Make sure Serenity is set up (run 'serenity' to configure) and your "
                        "licence key is saved in ~/.serenity/config.json"
                    ),
                )]
            return [types.TextContent(type="text", text=f"Recall error: {e}")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Recall failed: {e}")]

    # ── serenity_remember ─────────────────────────────────────────────────────
    elif name == "serenity_remember":
        content = arguments.get("content", "").strip()
        tags    = arguments.get("tags", "").strip()

        if not content:
            return [types.TextContent(type="text", text="Error: content cannot be empty.")]

        if len(content) < 20:
            return [types.TextContent(
                type="text",
                text="Content too short — please write at least a sentence so the memory is useful.",
            )]

        try:
            from serenity_nnn import nnn as _nnn

            # Build a rich session_id so the source is traceable
            timestamp  = datetime.now().strftime("%Y-%m-%d")
            source_tag = "claude-code"
            session_id = f"{source_tag}:{timestamp}"

            # Prepend tags into the content for richer embedding signal
            if tags:
                full_content = f"[{tags}] {content}"
            else:
                full_content = content

            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: _nnn.encode(full_content, session_id=session_id)
            )

            action  = result.get("action", "stored")
            b_id    = result.get("bundle_id", "?")[:12]

            return [types.TextContent(
                type="text",
                text=(
                    f"Stored in Serenity's memory ({action}).\n"
                    f"Bundle: {b_id}...\n"
                    f"Tags: {tags or 'none'}\n\n"
                    "Serenity will also be able to recall this. "
                    "It will resurface automatically when relevant in future sessions."
                ),
            )]

        except RuntimeError as e:
            if "not authorised" in str(e).lower():
                return [types.TextContent(
                    type="text",
                    text=(
                        "Serenity memory is locked — NNN requires a valid Serenity licence.\n"
                        "Make sure Serenity is set up and your licence key is in ~/.serenity/config.json"
                    ),
                )]
            return [types.TextContent(type="text", text=f"Remember error: {e}")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Remember failed: {e}")]

    # ── serenity_status ───────────────────────────────────────────────────────
    elif name == "serenity_status":
        try:
            from serenity_nnn import nnn as _nnn

            # Use the public query API to test auth — avoids accessing private internals
            try:
                _nnn.query("__status_check__", token_budget=1)
                auth = True
            except RuntimeError as _auth_err:
                auth = "not authorised" in str(_auth_err).lower()
                auth = False
            except Exception:
                auth = True  # query ran, NNN is up

            # Best-effort bundle count via public interface
            try:
                count = len(_nnn.query("", token_budget=0) or [])
            except Exception:
                count = 0

            if not auth:
                return [types.TextContent(
                    type="text",
                    text=(
                        "Serenity MCP: connected ✓\n"
                        "NNN memory: LOCKED (licence not authorised)\n\n"
                        "Run 'serenity' to complete setup and save your licence key."
                    ),
                )]

            return [types.TextContent(
                type="text",
                text=(
                    "Serenity MCP: connected ✓\n"
                    "NNN memory: authorised ✓\n\n"
                    "Tools ready: serenity_recall, serenity_remember"
                ),
            )]

        except Exception as e:
            return [types.TextContent(type="text", text=f"Status check failed: {e}")]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    # Authorise NNN at startup — uses Serenity's existing licence config
    ok = _authorise_nnn()
    if ok:
        print("Serenity MCP: NNN memory authorised ✓", file=sys.stderr)
    else:
        print(
            "Serenity MCP: NNN auth failed — tools will return lock error until "
            "Serenity is configured with a valid licence key.",
            file=sys.stderr,
        )

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="serenity-memory",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
