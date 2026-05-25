"""Spotify control tools — Serenity can play, queue, and control Spotify.

Setup:
  1. Create a free app at https://developer.spotify.com/dashboard
  2. Set Redirect URI to: http://localhost:8888/callback
  3. Add to ~/.serenity/config.json:
       "spotify": { "clientId": "...", "clientSecret": "..." }
  4. First use: Serenity will give you an auth URL — visit it, then paste
     the redirect URL back using spotify_auth(redirect_url="...")

Token is cached at ~/.serenity/spotify_token.json and auto-refreshed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema

_TOKEN_CACHE = str(Path.home() / ".serenity" / "spotify_token.json")
_SCOPES = " ".join([
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "playlist-read-private",
    "user-library-read",
    "streaming",
])


def _load_spotify_cfg() -> tuple[str, str, str]:
    """Load client_id, client_secret, redirect_uri from config."""
    cfg_path = Path.home() / ".serenity" / "config.json"
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        sp = raw.get("spotify", {})
        client_id = sp.get("clientId") or sp.get("client_id", "")
        client_secret = sp.get("clientSecret") or sp.get("client_secret", "")
        redirect_uri = sp.get("redirectUri") or sp.get("redirect_uri", "http://localhost:8888/callback")
        return client_id, client_secret, redirect_uri
    except Exception:
        return "", "", "http://localhost:8888/callback"


def _get_auth_manager():
    try:
        from spotipy.oauth2 import SpotifyOAuth  # type: ignore
    except ImportError:
        raise RuntimeError("spotipy not installed. Run: pip install spotipy")

    client_id, client_secret, redirect_uri = _load_spotify_cfg()
    if not client_id or not client_secret:
        raise RuntimeError(
            "Spotify credentials not set. Add to ~/.serenity/config.json:\n"
            '  "spotify": {"clientId": "YOUR_ID", "clientSecret": "YOUR_SECRET"}\n'
            "Get them free at: https://developer.spotify.com/dashboard"
        )

    return SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=_SCOPES,
        cache_path=_TOKEN_CACHE,
        open_browser=False,
    )


def _get_client():
    """Get an authenticated Spotify client or raise with auth URL."""
    try:
        import spotipy  # type: ignore
    except ImportError:
        raise RuntimeError("spotipy not installed. Run: pip install spotipy")

    auth = _get_auth_manager()
    token = auth.get_cached_token()
    if not token:
        url = auth.get_authorize_url()
        raise RuntimeError(
            f"Spotify not authorised yet. Visit this URL:\n\n{url}\n\n"
            "After Spotify redirects you (to localhost:8888), copy the FULL URL from "
            "your browser and give it to me — I'll call spotify_auth(redirect_url=...) to finish."
        )
    return spotipy.Spotify(auth_manager=auth)


def _first_active_device(sp) -> str | None:
    """Return the ID of the first active device, or None."""
    try:
        devices = sp.devices().get("devices", [])
        active = [d for d in devices if d.get("is_active")]
        if active:
            return active[0]["id"]
        if devices:
            return devices[0]["id"]
    except Exception:
        pass
    return None


# ── spotify_auth ──────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        redirect_url=StringSchema(
            "The full URL your browser was redirected to after Spotify authorisation "
            "(starts with http://localhost:8888/callback?code=...)."
        ),
        required=["redirect_url"],
    )
)
class SpotifyAuthTool(Tool):
    """Complete Spotify OAuth by providing the redirect URL from the browser.

    Only needed once — the token is cached and auto-refreshed after that.
    """

    @property
    def name(self) -> str:
        return "spotify_auth"

    @property
    def description(self) -> str:
        return (
            "Complete Spotify authorisation. Call this with the redirect URL "
            "the browser showed after the user visited the Spotify auth page. "
            "Only needed once — token is cached after that."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, redirect_url: str, **kwargs: Any) -> str:
        def _do() -> str:
            auth = _get_auth_manager()
            code = auth.parse_response_code(redirect_url)
            token = auth.get_access_token(code)
            if token:
                return "✅ Spotify authorised and token cached. You can now use all Spotify tools."
            return "❌ Failed to get token — check the redirect URL and try again."
        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:
            return f"❌ {exc}"


# ── spotify_current ───────────────────────────────────────────────────────────

class SpotifyCurrentTool(Tool):
    """Show what's currently playing on Spotify."""

    @property
    def name(self) -> str:
        return "spotify_current"

    @property
    def description(self) -> str:
        return "Show the currently playing track on Spotify — title, artist, album, progress."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        def _do() -> str:
            sp = _get_client()
            current = sp.current_playback()
            if not current or not current.get("item"):
                return "Nothing is currently playing."
            item = current["item"]
            artists = ", ".join(a["name"] for a in item.get("artists", []))
            name = item.get("name", "Unknown")
            album = item.get("album", {}).get("name", "")
            progress_ms = current.get("progress_ms", 0)
            duration_ms = item.get("duration_ms", 1)
            progress = f"{progress_ms // 60000}:{(progress_ms % 60000) // 1000:02d}"
            duration = f"{duration_ms // 60000}:{(duration_ms % 60000) // 1000:02d}"
            playing = "▶" if current.get("is_playing") else "⏸"
            return f"{playing} {name} — {artists}\nAlbum: {album}\nProgress: {progress} / {duration}"
        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:
            return f"❌ {exc}"


# ── spotify_play ──────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        query=StringSchema(
            "Search query — song name, artist, or 'artist name song title'. "
            "E.g. 'Kendrick Lamar HUMBLE' or 'Blinding Lights'. "
            "Leave blank to resume paused playback."
        ),
        type=StringSchema(
            "What to search for: 'track' (default), 'album', or 'playlist'."
        ),
        required=[],
    )
)
class SpotifyPlayTool(Tool):
    """Search Spotify and play a track, album, or playlist."""

    @property
    def name(self) -> str:
        return "spotify_play"

    @property
    def description(self) -> str:
        return (
            "Play a track, album, or playlist on Spotify by searching for it. "
            "Leave query blank to resume paused playback."
        )

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, query: str = "", type: str = "track", **kwargs: Any) -> str:
        def _do() -> str:
            sp = _get_client()
            device_id = _first_active_device(sp)

            if not query.strip():
                sp.start_playback(device_id=device_id)
                return "▶ Resumed playback."

            results = sp.search(q=query, type=type, limit=1)
            items = results.get(f"{type}s", {}).get("items", [])
            if not items:
                return f"❌ No {type} found for '{query}'."

            item = items[0]
            name = item.get("name", "?")

            if type == "track":
                uri = item["uri"]
                artists = ", ".join(a["name"] for a in item.get("artists", []))
                sp.start_playback(device_id=device_id, uris=[uri])
                return f"▶ Playing: {name} — {artists}"
            else:
                uri = item["uri"]
                sp.start_playback(device_id=device_id, context_uri=uri)
                return f"▶ Playing {type}: {name}"

        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:
            return f"❌ {exc}"


# ── spotify_pause ─────────────────────────────────────────────────────────────

class SpotifyPauseTool(Tool):
    @property
    def name(self) -> str:
        return "spotify_pause"

    @property
    def description(self) -> str:
        return "Pause Spotify playback."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> str:
        def _do() -> str:
            sp = _get_client()
            sp.pause_playback()
            return "⏸ Paused."
        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:
            return f"❌ {exc}"


# ── spotify_skip ──────────────────────────────────────────────────────────────

class SpotifySkipTool(Tool):
    @property
    def name(self) -> str:
        return "spotify_skip"

    @property
    def description(self) -> str:
        return "Skip to the next track on Spotify."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> str:
        def _do() -> str:
            sp = _get_client()
            sp.next_track()
            import time; time.sleep(0.5)
            current = sp.current_playback()
            if current and current.get("item"):
                item = current["item"]
                artists = ", ".join(a["name"] for a in item.get("artists", []))
                return f"⏭ Skipped — now playing: {item['name']} — {artists}"
            return "⏭ Skipped to next track."
        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:
            return f"❌ {exc}"


# ── spotify_queue ─────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        query=StringSchema("Song name to search and add to the queue."),
        required=["query"],
    )
)
class SpotifyQueueTool(Tool):
    @property
    def name(self) -> str:
        return "spotify_queue"

    @property
    def description(self) -> str:
        return "Add a track to the Spotify queue by searching for it."

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, query: str, **kwargs: Any) -> str:
        def _do() -> str:
            sp = _get_client()
            results = sp.search(q=query, type="track", limit=1)
            items = results.get("tracks", {}).get("items", [])
            if not items:
                return f"❌ No track found for '{query}'."
            item = items[0]
            artists = ", ".join(a["name"] for a in item.get("artists", []))
            sp.add_to_queue(item["uri"])
            return f"➕ Added to queue: {item['name']} — {artists}"
        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:
            return f"❌ {exc}"


# ── spotify_volume ────────────────────────────────────────────────────────────

@tool_parameters(
    tool_parameters_schema(
        level=IntegerSchema("Volume level from 0 (silent) to 100 (max)."),
        required=["level"],
    )
)
class SpotifyVolumeTool(Tool):
    @property
    def name(self) -> str:
        return "spotify_volume"

    @property
    def description(self) -> str:
        return "Set Spotify volume (0–100)."

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, level: int, **kwargs: Any) -> str:
        def _do() -> str:
            level_clamped = max(0, min(100, level))
            sp = _get_client()
            sp.volume(level_clamped)
            return f"🔊 Volume set to {level_clamped}%."
        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:
            return f"❌ {exc}"
