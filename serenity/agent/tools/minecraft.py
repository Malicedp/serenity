"""Minecraft tools — connect Serenity to a Minecraft server via mineflayer.

The tools talk to a lightweight Node.js bridge (mineflayer_bridge.js) that
runs as a background process on http://127.0.0.1:25561.  The bridge manages
the actual bot connection; Serenity just sends HTTP requests.

First-time setup:
    cd serenity/skills/Minecraft/bridge && npm install
Then the bridge starts automatically on the first minecraft_connect call.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from loguru import logger

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema

BRIDGE_URL  = "http://127.0.0.1:25561"
BRIDGE_DIR  = Path(__file__).parent.parent.parent / "skills" / "Minecraft" / "bridge"
BRIDGE_JS   = BRIDGE_DIR / "mineflayer_bridge.js"

_bridge_proc: subprocess.Popen | None = None

# Appended to every action tool result so the model always calls tick() next.
_TICK_NOW = (
    "\n\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "⚡ ACTION COMPLETE — call minecraft_tick() NOW.\n"
    "ERROR OR SUCCESS — it does not matter. Call minecraft_tick() regardless.\n"
    "Do NOT write a text response. Do NOT report to Daniel. Do NOT say done.\n"
    "tick() will read the current world state and decide the next action.\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
)


# ── Bridge management ─────────────────────────────────────────────────────────

def _bridge_alive() -> bool:
    try:
        with urllib.request.urlopen(f"{BRIDGE_URL}/ping", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def _start_bridge() -> str | None:
    """Start the bridge process if not running. Returns error string or None."""
    global _bridge_proc

    if _bridge_alive():
        return None

    if not BRIDGE_JS.exists():
        return (
            f"Bridge script not found at {BRIDGE_JS}. "
            "Make sure the Minecraft skill is installed."
        )

    if not (BRIDGE_DIR / "node_modules").exists():
        return (
            f"Node modules not installed. Run:\n"
            f"  cd {BRIDGE_DIR} && npm install"
        )

    logger.info("Starting mineflayer bridge…")
    _bridge_proc = subprocess.Popen(
        ["node", str(BRIDGE_JS)],
        cwd=str(BRIDGE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=0x00000008,  # DETACHED_PROCESS on Windows
    )

    for _ in range(20):
        time.sleep(0.5)
        if _bridge_alive():
            logger.info("Mineflayer bridge started (pid {})", _bridge_proc.pid)
            return None

    return "Bridge failed to start within 10 s. Check Node.js is installed."


def _post(endpoint: str, data: dict | None = None, timeout: float = 20.0) -> str:
    payload = json.dumps(data or {}).encode()
    req     = urllib.request.Request(
        f"{BRIDGE_URL}{endpoint}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
    except urllib.error.URLError as e:
        return f"Bridge unreachable: {e.reason}"
    except Exception as e:
        return f"Bridge error: {e}"

    if not body.get("ok"):
        return f"Error: {body.get('error', 'unknown')}"
    result = body["result"]
    return json.dumps(result, indent=2) if isinstance(result, (dict, list)) else str(result)


def _get(endpoint: str, params: dict | None = None, timeout: float = 10.0) -> str:
    qs  = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = f"{BRIDGE_URL}{endpoint}" + (f"?{qs}" if qs else "")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = json.loads(r.read())
    except urllib.error.URLError as e:
        return f"Bridge unreachable: {e.reason}"
    except Exception as e:
        return f"Bridge error: {e}"

    if not body.get("ok"):
        return f"Error: {body.get('error', 'unknown')}"
    result = body["result"]
    return json.dumps(result, indent=2) if isinstance(result, (dict, list)) else str(result)


# ── Tools ─────────────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        host=StringSchema("Minecraft server hostname or IP (default: localhost)"),
        port=IntegerSchema(25565, description="Server port (default: 25565)", minimum=1, maximum=65535),
        username=StringSchema("Bot username (default: Serenity)"),
        version=StringSchema(
            "Minecraft version string, e.g. '1.20.1'. Leave blank to auto-detect.",
            nullable=True,
        ),
        required=[],
    )
)
class MinecraftConnectTool(Tool):
    """Connect to a Minecraft server using the mineflayer bot.

    Starts the Node.js bridge automatically if it isn't running.
    The bot joins in offline mode by default (no Microsoft account needed
    for cracked/local servers). For online-mode servers, see your server's
    auth settings.
    """

    @property
    def name(self) -> str:
        return "minecraft_connect"

    @property
    def description(self) -> str:
        return (
            "Connect to a Minecraft server as a bot. "
            "Default: localhost:25565, username=Serenity, offline mode. "
            "Start bridge automatically if needed."
        )

    async def execute(
        self,
        host: str = "localhost",
        port: int = 25565,
        username: str = "Serenity",
        version: str | None = None,
        **kwargs: Any,
    ) -> str:
        import asyncio
        err = await asyncio.get_event_loop().run_in_executor(None, _start_bridge)
        if err:
            return err
        return await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _post("/connect", {
                "host": host, "port": port,
                "username": username,
                "version": version or False,
                "auth": "offline",
            }, timeout=20),
        )


@tool_parameters(tool_parameters_schema(required=[]))
class MinecraftDisconnectTool(Tool):
    """Disconnect the Minecraft bot from the server."""

    @property
    def name(self) -> str:
        return "minecraft_disconnect"

    @property
    def description(self) -> str:
        return "Disconnect the Minecraft bot from the server."

    async def execute(self, **kwargs: Any) -> str:
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/disconnect")
        )


@tool_parameters(tool_parameters_schema(required=[]))
class MinecraftStatusTool(Tool):
    """Get the current bot state: health, food, position, inventory, time."""

    @property
    def name(self) -> str:
        return "minecraft_status"

    @property
    def description(self) -> str:
        return (
            "Get current bot state: health (0-20), food (0-20), XYZ position, "
            "game mode, time of day, inventory contents, whether pathfinding is active."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _get("/status")
        )


@tool_parameters(
    tool_parameters_schema(
        message=StringSchema("Message to send in chat"),
        required=["message"],
    )
)
class MinecraftChatTool(Tool):
    """Send a chat message or command in Minecraft."""

    @property
    def name(self) -> str:
        return "minecraft_chat"

    @property
    def description(self) -> str:
        return (
            "Send a chat message in Minecraft. "
            "Prefix with / to run a server command, e.g. '/give @s diamond 64'."
        )

    async def execute(self, message: str, **kwargs: Any) -> str:
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/chat", {"message": message})
        )


@tool_parameters(
    tool_parameters_schema(
        x=StringSchema("Target X coordinate"),
        y=StringSchema("Target Y coordinate"),
        z=StringSchema("Target Z coordinate"),
        range=StringSchema("How close to get (default: 1 block)", nullable=True),
        required=["x", "y", "z"],
    )
)
class MinecraftNavigateTool(Tool):
    """Pathfind to a position in the world (non-blocking — fires and returns)."""

    @property
    def name(self) -> str:
        return "minecraft_navigate"

    @property
    def description(self) -> str:
        return (
            "Move the bot to XYZ coordinates using pathfinding. "
            "Returns immediately — poll minecraft_events for 'arrived' or 'navigate_failed'. "
            "Use minecraft_stop to cancel. "
            "Bot will jump, ladder-climb, and swim to reach the target."
        )

    async def execute(self, x: str, y: str, z: str, range: str | None = None, **kwargs: Any) -> str:
        import asyncio
        data: dict[str, Any] = {"x": x, "y": y, "z": z}
        if range:
            data["range"] = range
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/navigate", data)
        )


@tool_parameters(tool_parameters_schema(required=[]))
class MinecraftStopTool(Tool):
    """Cancel active pathfinding / movement."""

    @property
    def name(self) -> str:
        return "minecraft_stop"

    @property
    def description(self) -> str:
        return "Stop the bot's current pathfinding movement immediately."

    async def execute(self, **kwargs: Any) -> str:
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/stop")
        )


@tool_parameters(
    tool_parameters_schema(
        type=StringSchema(
            "Block type name to find and mine, e.g. 'oak_log', 'stone', 'coal_ore'. "
            "OR leave blank and supply x/y/z to mine a specific block.",
            nullable=True,
        ),
        x=StringSchema("X coordinate of block to mine (optional, overrides type)", nullable=True),
        y=StringSchema("Y coordinate of block to mine", nullable=True),
        z=StringSchema("Z coordinate of block to mine", nullable=True),
        max_distance=IntegerSchema(
            4, description="Search radius when using type (default 4)", minimum=1, maximum=16
        ),
        required=[],
    )
)
class MinecraftMineTool(Tool):
    """Mine a block — by type (nearest) or by exact coordinates."""

    @property
    def name(self) -> str:
        return "minecraft_mine"

    @property
    def description(self) -> str:
        return (
            "Mine a block. "
            "Use type='oak_log' to mine the nearest matching block, "
            "or x/y/z to mine a specific block at those coordinates. "
            "Requires the correct tool to be equipped (use minecraft_equip first). "
            "Bot must be within reach (~4 blocks) — navigate there first if needed."
        )

    async def execute(
        self,
        type: str | None = None,
        x: str | None = None,
        y: str | None = None,
        z: str | None = None,
        max_distance: int = 4,
        **kwargs: Any,
    ) -> str:
        import asyncio
        data: dict[str, Any] = {"maxDistance": max_distance}
        if x is not None:
            data.update({"x": x, "y": y, "z": z})
        elif type:
            data["type"] = type
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/mine", data, timeout=30)
        )
        return result + _TICK_NOW


@tool_parameters(
    tool_parameters_schema(
        name=StringSchema(
            "Name/username of entity to attack. Leave blank for nearest hostile mob.",
            nullable=True,
        ),
        required=[],
    )
)
class MinecraftAttackTool(Tool):
    """Attack an entity — nearest mob, or by name."""

    @property
    def name(self) -> str:
        return "minecraft_attack"

    @property
    def description(self) -> str:
        return (
            "Attack an entity. With no name, attacks the nearest hostile mob. "
            "Provide name to target a specific player or named mob. "
            "Bot must be close (within 3 blocks) to hit."
        )

    async def execute(self, name: str | None = None, **kwargs: Any) -> str:
        import asyncio
        data = {"name": name} if name else {}
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/attack", data)
        )
        return result + _TICK_NOW


@tool_parameters(
    tool_parameters_schema(
        item=StringSchema("Item name to equip, e.g. 'diamond_sword', 'iron_pickaxe'"),
        destination=StringSchema(
            "Where to equip: 'hand', 'off-hand', 'head', 'torso', 'legs', 'feet' (default: hand)",
            nullable=True,
        ),
        required=["item"],
    )
)
class MinecraftEquipTool(Tool):
    """Equip an item from inventory to hand or armour slot."""

    @property
    def name(self) -> str:
        return "minecraft_equip"

    @property
    def description(self) -> str:
        return (
            "Equip an item from the bot's inventory. "
            "Use destination='hand' for weapons/tools, 'head'/'torso'/'legs'/'feet' for armour."
        )

    async def execute(self, item: str, destination: str = "hand", **kwargs: Any) -> str:
        import asyncio
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/equip", {"item": item, "destination": destination})
        )
        return result + _TICK_NOW


@tool_parameters(
    tool_parameters_schema(
        item=StringSchema("Item to drop, e.g. 'cobblestone'"),
        count=IntegerSchema(1, description="How many to drop (default: all of that type)", minimum=1),
        required=["item"],
    )
)
class MinecraftDropTool(Tool):
    """Drop an item from the bot's inventory onto the ground."""

    @property
    def name(self) -> str:
        return "minecraft_drop"

    @property
    def description(self) -> str:
        return "Drop an item (or stack) from the bot's inventory onto the ground."

    async def execute(self, item: str, count: int | None = None, **kwargs: Any) -> str:
        import asyncio
        data: dict[str, Any] = {"item": item}
        if count:
            data["count"] = count
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/drop", data)
        )


@tool_parameters(
    tool_parameters_schema(
        item=StringSchema(
            "Food item name to eat, e.g. 'bread', 'cooked_beef'. Leave blank to eat whatever's available.",
            nullable=True,
        ),
        required=[],
    )
)
class MinecraftEatTool(Tool):
    """Eat food to restore the bot's hunger bar."""

    @property
    def name(self) -> str:
        return "minecraft_eat"

    @property
    def description(self) -> str:
        return (
            "Eat food from inventory. "
            "Leave item blank to auto-pick the first available food. "
            "Bot must have food < 20 or this may fail."
        )

    async def execute(self, item: str | None = None, **kwargs: Any) -> str:
        import asyncio
        data = {"item": item} if item else {}
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/eat", data, timeout=10)
        )
        return result + _TICK_NOW


@tool_parameters(
    tool_parameters_schema(
        item=StringSchema("Item to craft, e.g. 'crafting_table', 'stick', 'torch'"),
        count=IntegerSchema(1, description="How many to craft (default: 1)", minimum=1),
        required=["item"],
    )
)
class MinecraftCraftTool(Tool):
    """Craft an item using materials from the bot's inventory."""

    @property
    def name(self) -> str:
        return "minecraft_craft"

    @property
    def description(self) -> str:
        return (
            "Craft an item from the bot's inventory. "
            "Requires the right materials to be present. "
            "For 3x3 recipes a crafting table must be nearby."
        )

    async def execute(self, item: str, count: int = 1, **kwargs: Any) -> str:
        import asyncio
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/craft", {"item": item, "count": count}, timeout=15)
        )
        return result + _TICK_NOW


@tool_parameters(
    tool_parameters_schema(
        type=StringSchema("Block type to search for, e.g. 'diamond_ore', 'oak_log', 'chest'"),
        max_distance=IntegerSchema(32, description="Search radius in blocks (default: 32)", minimum=1, maximum=128),
        count=IntegerSchema(10, description="Max results to return (default: 10)", minimum=1, maximum=50),
        required=["type"],
    )
)
class MinecraftScanBlocksTool(Tool):
    """Find nearby blocks of a specific type and return their positions."""

    @property
    def name(self) -> str:
        return "minecraft_scan_blocks"

    @property
    def read_only(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return (
            "Find blocks of a given type near the bot and return their XYZ positions. "
            "Use to locate ores, trees, chests, crafting tables, etc. "
            "Then navigate to the nearest result and mine/interact."
        )

    async def execute(self, type: str, max_distance: int = 32, count: int = 10, **kwargs: Any) -> str:
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _get("/scan_blocks", {"type": type, "maxDistance": max_distance, "count": count})
        )


@tool_parameters(
    tool_parameters_schema(
        filter=StringSchema(
            "Filter by entity type or name, e.g. 'Zombie', 'Creeper', 'player'. "
            "Leave blank for all nearby entities.",
            nullable=True,
        ),
        required=[],
    )
)
class MinecraftScanEntitiesTool(Tool):
    """List nearby entities — mobs, players, items on the ground."""

    @property
    def name(self) -> str:
        return "minecraft_scan_entities"

    @property
    def read_only(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return (
            "List entities near the bot: mobs, players, dropped items. "
            "Returns type, name, position, and health for each. "
            "Use to find threats, players, or items to pick up."
        )

    async def execute(self, filter: str | None = None, **kwargs: Any) -> str:
        import asyncio
        params = {"filter": filter} if filter else {}
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _get("/scan_entities", params)
        )


@tool_parameters(
    tool_parameters_schema(
        since=StringSchema(
            "Unix timestamp in milliseconds. Only return events after this time. "
            "Pass the 't' value of the last event you received to get only new ones.",
            nullable=True,
        ),
        required=[],
    )
)
class MinecraftEventsTool(Tool):
    """Poll recent bot events: chat, health changes, deaths, arrivals, etc."""

    @property
    def name(self) -> str:
        return "minecraft_events"

    @property
    def read_only(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return (
            "Get recent bot events (up to 300 buffered). "
            "Event types: spawn, death, health, chat, kicked, error, arrived, navigate_failed, mined, attacked. "
            "Pass since=<timestamp_ms> to only get new events since your last poll. "
            "Use this to know if pathfinding finished, if the bot died, or if a player spoke."
        )

    async def execute(self, since: str | None = None, **kwargs: Any) -> str:
        import asyncio
        params = {"since": since} if since else {}
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _get("/events", params)
        )


# ── Sense ─────────────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        max_distance=IntegerSchema(20, description="Scan radius for threats and resources (default 20)", minimum=5, maximum=64),
        event_count=IntegerSchema(15, description="How many recent events to include (default 15)", minimum=1, maximum=50),
        required=[],
    )
)
class MinecraftSenseTool(Tool):
    """Full world snapshot in one call — the heartbeat of autonomous play.

    Returns in a single response:
    - health, food, position, time of day, isDay flag
    - full inventory
    - nearby threats (hostile mobs) sorted by distance
    - nearby key resources (logs, ores, crafting tables, chests)
    - last N events from the event log

    Call this at the start of every action loop tick instead of calling
    minecraft_status + minecraft_scan_entities + minecraft_scan_blocks separately.
    """

    @property
    def name(self) -> str:
        return "minecraft_sense"

    @property
    def read_only(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return (
            "Full world snapshot: health, food, pos, time, inventory, nearby threats, "
            "nearby resources (logs/ores/chests), and recent events — all in one call. "
            "Use this at the start of every loop tick instead of calling status/scan/events separately. "
            "Check threats first, then survival (food/health), then proceed with goals."
        )

    async def execute(self, max_distance: int = 20, event_count: int = 15, **kwargs: Any) -> str:
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _get("/sense", {"maxDistance": max_distance, "eventCount": event_count})
        )


# ── Navigate and wait ──────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        x=StringSchema("Target X coordinate"),
        y=StringSchema("Target Y coordinate"),
        z=StringSchema("Target Z coordinate"),
        range=StringSchema("Acceptable arrival distance in blocks (default: 1)", nullable=True),
        required=["x", "y", "z"],
    )
)
class MinecraftNavigateWaitTool(Tool):
    """Pathfind to XYZ and BLOCK until arrived or failed.

    Unlike minecraft_navigate (which returns immediately), this waits for the
    result before returning. Use this for all movement in autonomous play —
    it eliminates the manual event-polling loop.

    The bridge handles stuck detection internally (5s without movement triggers
    escape manoeuvres; up to 3 attempts before giving up). No timeout parameter
    needed — the bot will walk anywhere given enough time.

    Returns one of:
    - "Arrived at (x, y, z) — travelled Xm <Direction>" — success
    - "Navigation failed after 3 escape attempts: <reason>" — truly blocked
    - "Bot died during navigation" — died mid-travel, check position
    - "No path found to (x, y, z)" — target is inside a wall or unreachable
    """

    @property
    def name(self) -> str:
        return "minecraft_navigate_wait"

    @property
    def description(self) -> str:
        return (
            "Move to XYZ and wait until arrived or failed (blocking). "
            "Use this instead of minecraft_navigate for autonomous play — "
            "no polling needed. The bridge handles stuck detection and escape manoeuvres. "
            "Returns 'Arrived', 'Navigation failed', 'Bot died', or 'No path found'."
        )

    async def execute(
        self, x: str, y: str, z: str,
        range: str = "1",
        **kwargs: Any,
    ) -> str:
        import asyncio
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post(
                "/navigate_wait",
                {"x": x, "y": y, "z": z, "range": range},
                timeout=630.0,  # 10.5 min — matches SAFETY_MS=600000 in bridge
            )
        )
        return result + _TICK_NOW


# ── Fight ──────────────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        name=StringSchema(
            "Name of entity to fight. Leave blank for nearest hostile mob.",
            nullable=True,
        ),
        timeout=IntegerSchema(30, description="Max seconds to fight before stopping (default 30)", minimum=5, maximum=120),
        retreat_hp=IntegerSchema(4, description="Retreat when health drops to this value (default 4)", minimum=1, maximum=19),
        required=[],
    )
)
class MinecraftFightTool(Tool):
    """Fight loop — attack until mob dead, bot retreats, or timeout.

    Handles the full combat loop automatically:
    - Looks at target, swings every 0.6 s (attack cooldown)
    - Retreats if health drops below retreat_hp
    - Stops when no more enemies are found (mob died)
    - Times out if the fight takes too long

    Always equip a sword first with minecraft_equip. Navigate within
    3 blocks of the target before calling this.
    """

    @property
    def name(self) -> str:
        return "minecraft_fight"

    @property
    def description(self) -> str:
        return (
            "Fight the nearest hostile mob (or named target) until it dies, bot retreats (low health), or timeout. "
            "Handles attack cooldown and looks at target automatically. "
            "Equip a sword first. Navigate within 3 blocks of the target first. "
            "Returns: 'Defeated N mobs', 'Retreating — health N', or 'Fight timeout'."
        )

    async def execute(
        self,
        name: str | None = None,
        timeout: int = 30,
        retreat_hp: int = 4,
        **kwargs: Any,
    ) -> str:
        import asyncio
        data: dict[str, Any] = {"timeout": timeout, "retreat_hp": retreat_hp}
        if name:
            data["name"] = name
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/fight", data, timeout=timeout + 5.0)
        )
        return result + _TICK_NOW


# ── Place block ────────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        ref_x=StringSchema("X of the existing block to place against"),
        ref_y=StringSchema("Y of the existing block to place against"),
        ref_z=StringSchema("Z of the existing block to place against"),
        face_x=StringSchema("X component of face vector (default 0). Use -1/0/1.", nullable=True),
        face_y=StringSchema("Y component of face vector (default 1 = top face). Use -1/0/1.", nullable=True),
        face_z=StringSchema("Z component of face vector (default 0). Use -1/0/1.", nullable=True),
        required=["ref_x", "ref_y", "ref_z"],
    )
)
class MinecraftPlaceBlockTool(Tool):
    """Place a block from the bot's hand against an existing block.

    The bot must have the block item equipped in hand first (use minecraft_equip).
    ref_x/y/z = position of the solid block to place against.
    face = which face of that block to place on (top=0,1,0 / north=0,0,-1 / etc).
    Example: place a block on top of the ground at (10, 64, 10):
        ref=(10,63,10), face=(0,1,0) → places at (10,64,10).
    """

    @property
    def name(self) -> str:
        return "minecraft_place_block"

    @property
    def description(self) -> str:
        return (
            "Place a block held in hand against an existing block. "
            "Equip the block item first with minecraft_equip. "
            "ref_x/y/z = the block to place against. "
            "face_x/y/z = which face (top=0,1,0  bottom=0,-1,0  north=0,0,-1  south=0,0,1  west=-1,0,0  east=1,0,0). "
            "Default face is top (0,1,0) — places block on top of the ref block."
        )

    async def execute(
        self,
        ref_x: str, ref_y: str, ref_z: str,
        face_x: str = "0", face_y: str = "1", face_z: str = "0",
        **kwargs: Any,
    ) -> str:
        import asyncio
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/place_block", {
                "ref_x": ref_x, "ref_y": ref_y, "ref_z": ref_z,
                "face_x": face_x, "face_y": face_y, "face_z": face_z,
            })
        )
        return result + _TICK_NOW


# ── Open container ─────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        x=StringSchema("X coordinate of the chest / furnace / barrel"),
        y=StringSchema("Y coordinate"),
        z=StringSchema("Z coordinate"),
        action=StringSchema(
            "What to do: 'view' (default) — list contents, "
            "'withdraw' — take items out, 'deposit' — put items in.",
            nullable=True,
        ),
        item=StringSchema(
            "Item name for withdraw/deposit actions, e.g. 'diamond', 'bread'.",
            nullable=True,
        ),
        count=IntegerSchema(1, description="How many items to withdraw/deposit (default: 1)", minimum=1),
        required=["x", "y", "z"],
    )
)
class MinecraftOpenContainerTool(Tool):
    """Open a chest, barrel, or furnace — view contents or move items.

    Works with chests, barrels, shulker boxes, blast furnaces, and smokers.
    For withdraw/deposit supply item name and count.
    """

    @property
    def name(self) -> str:
        return "minecraft_open_container"

    @property
    def description(self) -> str:
        return (
            "Open a chest, barrel, furnace, or shulker box at XYZ. "
            "action='view' → returns slot contents. "
            "action='withdraw' + item + count → takes items from container into bot inventory. "
            "action='deposit' + item + count → puts items from bot inventory into container. "
            "Bot must be within 4 blocks of the container."
        )

    async def execute(
        self,
        x: str, y: str, z: str,
        action: str = "view",
        item: str | None = None,
        count: int = 1,
        **kwargs: Any,
    ) -> str:
        import asyncio
        data: dict[str, Any] = {"x": x, "y": y, "z": z, "action": action}
        if item:
            data["item"] = item
            data["count"] = count
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/open_container", data, timeout=15)
        )
        return result + _TICK_NOW


# ── Sleep / wake ───────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        x=StringSchema("X coordinate of the bed (optional — auto-finds nearest if omitted)", nullable=True),
        y=StringSchema("Y coordinate of the bed", nullable=True),
        z=StringSchema("Z coordinate of the bed", nullable=True),
        required=[],
    )
)
class MinecraftSleepTool(Tool):
    """Sleep in a bed to skip the night and reset spawn point.

    It must be night (timeOfDay > 12542) or a thunderstorm.
    All players on the server must sleep simultaneously on most servers.
    Leave x/y/z blank to auto-find the nearest bed.
    """

    @property
    def name(self) -> str:
        return "minecraft_sleep"

    @property
    def description(self) -> str:
        return (
            "Sleep in a nearby bed to skip the night. "
            "Leave coordinates blank to find the nearest bed automatically. "
            "Check timeOfDay in minecraft_status — sleeping only works after ~12542."
        )

    async def execute(
        self,
        x: str | None = None, y: str | None = None, z: str | None = None,
        **kwargs: Any,
    ) -> str:
        import asyncio
        data: dict[str, Any] = {}
        if x is not None:
            data.update({"x": x, "y": y, "z": z})
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/sleep", data, timeout=20)
        )
        return result + _TICK_NOW


@tool_parameters(tool_parameters_schema(required=[]))
class MinecraftWakeTool(Tool):
    """Wake the bot up from a bed (leave sleep early)."""

    @property
    def name(self) -> str:
        return "minecraft_wake"

    @property
    def description(self) -> str:
        return "Wake the bot up from sleeping in a bed."

    async def execute(self, **kwargs: Any) -> str:
        import asyncio
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/wake")
        )
        return result + _TICK_NOW


# ── Activate item ──────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        offhand=StringSchema(
            "Set to 'true' to activate the off-hand item instead of main hand.",
            nullable=True,
        ),
        required=[],
    )
)
class MinecraftActivateItemTool(Tool):
    """Right-click / use the item currently held in hand.

    Use cases:
    - Hold to eat food (hold until animation completes)
    - Draw a bow (hold, then deactivate to fire)
    - Throw ender pearl / splash potion
    - Drink a potion
    - Place a boat / minecart
    - Use a flint and steel
    """

    @property
    def name(self) -> str:
        return "minecraft_activate_item"

    @property
    def description(self) -> str:
        return (
            "Right-click / use the item in the bot's main hand (or off-hand if offhand=true). "
            "Used for: eating food, drawing bows, throwing pearls, drinking potions, "
            "placing boats, using flint+steel, etc. "
            "For bows: call activate_item to draw, then minecraft_deactivate_item to release and fire."
        )

    async def execute(self, offhand: str | None = None, **kwargs: Any) -> str:
        import asyncio
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/activate_item", {"offhand": offhand == "true"})
        )
        return result + _TICK_NOW


@tool_parameters(tool_parameters_schema(required=[]))
class MinecraftDeactivateItemTool(Tool):
    """Stop using / release the item in hand (releases bow, stops eating early)."""

    @property
    def name(self) -> str:
        return "minecraft_deactivate_item"

    @property
    def description(self) -> str:
        return (
            "Stop using the held item. "
            "Releases a drawn bow (firing the arrow), stops eating mid-animation, etc."
        )

    async def execute(self, **kwargs: Any) -> str:
        import asyncio
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/deactivate_item")
        )
        return result + _TICK_NOW


# ── Activate block ─────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        x=StringSchema("X coordinate of the block to interact with"),
        y=StringSchema("Y coordinate"),
        z=StringSchema("Z coordinate"),
        required=["x", "y", "z"],
    )
)
class MinecraftActivateBlockTool(Tool):
    """Right-click / interact with a block in the world.

    Use cases:
    - Open/close a door or trapdoor
    - Press a button
    - Toggle a lever
    - Open a crafting table, enchanting table, anvil
    - Open a villager trade UI
    - Use a bed (alternative to minecraft_sleep)
    """

    @property
    def name(self) -> str:
        return "minecraft_activate_block"

    @property
    def description(self) -> str:
        return (
            "Right-click / interact with a block at XYZ. "
            "Use for: opening doors, pressing buttons, toggling levers, "
            "opening crafting tables, enchanting tables, villager trades. "
            "Bot must be within reach (~4 blocks)."
        )

    async def execute(self, x: str, y: str, z: str, **kwargs: Any) -> str:
        import asyncio
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/activate_block", {"x": x, "y": y, "z": z})
        )
        return result + _TICK_NOW


# ── Plan — generate own goals from current state ──────────────────────────────

@tool_parameters(tool_parameters_schema(required=[]))
class MinecraftPlanTool(Tool):
    """Assess current game state and generate a goal list — Serenity plans for herself.

    Looks at the current inventory and world state to determine:
    - What tech tier the bot is at (fist / wood / stone / iron / diamond)
    - What goals are already complete
    - What the next logical goals are

    Returns a ready-to-write markdown checklist and tells you to save it to the vault:
        vault_write(title="Minecraft Goals", content=<markdown>, tags="minecraft,goals")

    Call this at the start of a fresh session or when the current goal list is empty/stale.
    After writing goals to the vault, start ticking.
    """

    @property
    def name(self) -> str:
        return "minecraft_plan"

    @property
    def description(self) -> str:
        return (
            "Assess current inventory + world state and generate your own goal list. "
            "Returns a markdown checklist based on tech tier (wood→stone→iron→diamond). "
            "After calling this, write the result to vault: "
            "vault_write(title='Minecraft Goals', content=<markdown field>, tags='minecraft,goals'). "
            "Call at session start or when your goal list is empty."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        import asyncio
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _get("/plan")
        )
        return (
            result
            + "\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ EXECUTE NOW — do these three things in order, no text between them:\n"
            "1. vault_write(title=\"Minecraft Goals\", content=<the markdown field above>, tags=\"minecraft,goals\")\n"
            "2. minecraft_tick()\n"
            "Do NOT write a response. Do NOT say done. Just call vault_write then minecraft_tick.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )


# ── Tick — single call for small models ───────────────────────────────────────

@tool_parameters(tool_parameters_schema(required=[]))
class MinecraftTickTool(Tool):
    """One-call game loop tick — sense the world, survive, get told what to do next.

    Returns plain English:
      HP / Food / Position / Time
      THREATS: list of nearby mobs
      NEARBY: key resources and their coordinates
      INVENTORY: what you're carrying
      CURRENT GOAL: what you should be working on
      WHAT TO DO NEXT: one clear instruction

    Use this at the start of every turn instead of calling
    minecraft_sense + minecraft_auto_survive + checking goals manually.
    After reading the output, execute the one action it suggests, then tick again.
    """

    @property
    def name(self) -> str:
        return "minecraft_tick"

    @property
    def description(self) -> str:
        return (
            "Game loop tick — senses world, handles survival, returns plain-English summary + "
            "one clear instruction for what to do next. "
            "Call this at the start of every turn. Read the output. Do the one thing it says. Tick again. "
            "Includes: HP, food, position, threats, nearby resources, inventory, current goal, next action hint."
        )

    @property
    def read_only(self) -> bool:
        return False  # auto_survive may eat/fight

    async def execute(self, **kwargs: Any) -> str:
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _get("/tick")
        )


# ── Auto-survive ───────────────────────────────────────────────────────────────

@tool_parameters(tool_parameters_schema(required=[]))
class MinecraftAutoSurviveTool(Tool):
    """Handle all survival automatically — eating, fighting, fleeing.

    Checks in order:
    1. Health ≤ 6 → eat immediately
    2. Food ≤ 10 → eat
    3. Hostile mob within 5 blocks → equip best sword + attack (or flee if low HP)

    Returns what it did, or "all good" if nothing was needed.
    Call this at the start of any long task to make sure survival is handled
    before you focus on goals.
    """

    @property
    def name(self) -> str:
        return "minecraft_auto_survive"

    @property
    def description(self) -> str:
        return (
            "Automatically handle survival: eat if hungry, fight or flee if a mob is nearby. "
            "Call this before starting any goal-oriented action. "
            "Returns what it did ('ate bread | attacked Zombie') or 'all good'."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> str:
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/auto_survive")
        )


# ── Goal management ────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        goals=StringSchema(
            "JSON array of goal strings, e.g. "
            '[\"chop 10 oak logs\", \"craft crafting table\", \"mine 20 stone\"]. '
            "Goals are worked through in order. Store them here so you don't forget them."
        ),
        required=["goals"],
    )
)
class MinecraftGoalSetTool(Tool):
    """Set the goal list — stored in the bridge so it survives between turns.

    Pass a JSON array of goal strings. Goals are worked through in order.
    The bridge remembers them — you don't have to keep them in your head.

    Example:
      goals='["chop 10 oak logs", "craft crafting table", "craft wooden pickaxe", "mine 20 stone"]'
    """

    @property
    def name(self) -> str:
        return "minecraft_goal_set"

    @property
    def description(self) -> str:
        return (
            "Set your goal list (stored in the bridge, persists between turns). "
            "Pass a JSON array of strings. Goals are done in order. "
            "After finishing a goal call minecraft_goal_done to advance to the next one. "
            "Check current goal with minecraft_goal_get or via minecraft_tick."
        )

    async def execute(self, goals: str, **kwargs: Any) -> str:
        import asyncio, json as _json
        try:
            parsed = _json.loads(goals)
        except Exception:
            parsed = [g.strip() for g in goals.split(",") if g.strip()]
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/goal/set", {"goals": parsed})
        )


@tool_parameters(tool_parameters_schema(required=[]))
class MinecraftGoalDoneTool(Tool):
    """Mark the current goal as complete and advance to the next one."""

    @property
    def name(self) -> str:
        return "minecraft_goal_done"

    @property
    def description(self) -> str:
        return (
            "Mark the current goal as complete — advances to the next goal in the list. "
            "Call this as soon as you finish a goal. "
            "Returns what was completed and what comes next."
        )

    async def execute(self, **kwargs: Any) -> str:
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _post("/goal/done")
        )


@tool_parameters(tool_parameters_schema(required=[]))
class MinecraftGoalGetTool(Tool):
    """Get the current goal list and which goal is active."""

    @property
    def name(self) -> str:
        return "minecraft_goal_get"

    @property
    def read_only(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return (
            "Get the full goal list and which goal is currently active. "
            "Use this if you've lost track of what you were doing."
        )

    async def execute(self, **kwargs: Any) -> str:
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _get("/goal/get")
        )


# ── Boot — single call that starts the autonomous loop ────────────────────────

@tool_parameters(tool_parameters_schema(required=[]))
class MinecraftBootTool(Tool):
    """Boot the autonomous game loop — sense surroundings, check goals, get first directive.

    Call this immediately after minecraft_connect(). It:
    1. Does a full 32-block sense of the world (biome, threats, resources)
    2. Reads your current goal list from the vault
    3. Returns a BOOT ASSESSMENT (world state summary + goal status)
    4. Ends with an ⚡ FIRST ACTION directive — the exact tool call to make next

    After boot, you're in the main loop:
        minecraft_boot() → execute directive → minecraft_tick() → execute → tick() → forever

    Only call this once per session, right after connect.
    """

    @property
    def name(self) -> str:
        return "minecraft_boot"

    @property
    def description(self) -> str:
        return (
            "Boot the autonomous game loop after connecting. "
            "Senses the full world (32-block radius), checks your goal list, "
            "and returns a BOOT ASSESSMENT + ⚡ FIRST ACTION directive. "
            "Call once, immediately after minecraft_connect(). "
            "Then execute the directive, then tick() forever."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _get("/boot")
        )
