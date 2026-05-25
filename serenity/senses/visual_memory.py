"""Visual Memory — Serenity's persistent visual episodic store.

Serenity watches two sources continuously:
  - screen  : mss screen capture  (what is happening on the computer)
  - camera  : OpenCV webcam frame (what Daniel is doing / how he looks)

Every 500 ms the background thread grabs a frame from each active source,
computes a perceptual hash, and only calls MiniCPM-V 4.6 when the frame
has actually changed (hash distance > threshold).  Captions are stored in a
local SQLite database — completely separate from NNN — so Serenity builds up a
true episodic visual timeline that survives across sessions.

This module is GENERAL PURPOSE — any part of Serenity can use it.
The session_id tag lets callers group observations by task.

Public API
----------
    svc = get_service()
    svc.start(sources=["screen"])          # or ["screen", "camera"] or ["camera"]
    svc.set_session("my-task-name")  # optional tagging
    svc.format_for_prompt(n=5)             # inject into LLM context
    svc.capture_now("screen")             # force an immediate capture
    svc.stop()

Database
--------
    ~/.serenity/visual_memory.db
    Table: observations
        id, ts, source, session_id, img_hash, caption, width, height

The DB is readable with any SQLite viewer (DB Browser, DBeaver, etc.).
"""

from __future__ import annotations

import base64
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from loguru import logger

# ── Constants ─────────────────────────────────────────────────────────────────

_DB_PATH        = Path.home() / ".serenity" / "visual_memory.db"
_POLL_INTERVAL  = 0.5          # seconds between capture ticks
_HASH_THRESHOLD = 5            # dhash bit distance to count as "changed"
_CAPTION_MODEL  = "openbmb/minicpm-v4.6"
_OLLAMA_BASE    = "http://localhost:11434"
_CAPTION_PROMPT = (
    "Describe what you see in this image in 1-2 concise sentences. "
    "Focus on: what is on screen / who is present, their position or action, "
    "and any notable text or UI elements visible."
)
_MAX_IMG_DIM    = 800          # resize longest edge to this before sending to VLM

# ── Module-level singleton ────────────────────────────────────────────────────

_service: "VisualMemoryService | None" = None
_service_lock = threading.Lock()


def get_service() -> "VisualMemoryService":
    """Return the module-level singleton VisualMemoryService."""
    global _service
    with _service_lock:
        if _service is None:
            _service = VisualMemoryService()
    return _service


# ── Database helpers ──────────────────────────────────────────────────────────

def _init_db(path: Path) -> None:
    """Create the observations table if it does not exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL    NOT NULL,
                source      TEXT    NOT NULL,
                session_id  TEXT,
                img_hash    TEXT,
                caption     TEXT,
                width       INTEGER,
                height      INTEGER
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_ts     ON observations(ts)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_source ON observations(source)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_session ON observations(session_id)")
        con.commit()


@contextmanager
def _db(path: Path):
    con = sqlite3.connect(str(path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


# ── Image helpers ─────────────────────────────────────────────────────────────

def _pil_to_b64(img: Any) -> str:
    """Convert a PIL Image to base64 PNG string."""
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def _resize_for_vlm(img: Any) -> Any:
    """Resize image so longest edge ≤ _MAX_IMG_DIM — keeps tokens low."""
    w, h = img.size
    if max(w, h) <= _MAX_IMG_DIM:
        return img
    scale = _MAX_IMG_DIM / max(w, h)
    return img.resize((int(w * scale), int(h * scale)))


def _dhash(img: Any) -> Any | None:
    """Return a perceptual dhash of img, or None if imagehash not available."""
    try:
        import imagehash
        return imagehash.dhash(img)
    except ImportError:
        return None


def _hash_changed(prev: Any, curr: Any) -> bool:
    """Return True if the two hashes differ beyond threshold (or if hashing unavailable)."""
    if prev is None or curr is None:
        return True   # can't compare → treat as changed
    try:
        return (prev - curr) > _HASH_THRESHOLD
    except Exception:
        return True


# ── MiniCPM-V captioning via Ollama ──────────────────────────────────────────

_last_caption_warn: float = 0.0   # rate-limit the "Ollama busy" warning to once/min

def _caption_image(b64_png: str) -> str | None:
    """Send image to MiniCPM-V 4.6 via Ollama and return caption text.

    Retries up to _CAPTION_RETRIES times with _CAPTION_RETRY_DELAY seconds between
    attempts.  Ollama serialises requests by default (OLLAMA_NUM_PARALLEL=1), so
    the main LLM generating tokens will return 500 here.  A short retry loop lets
    us pick up the caption as soon as the main model finishes its turn rather than
    skipping the frame entirely.

    To allow true concurrency (caption while main LLM runs) set the environment
    variable OLLAMA_NUM_PARALLEL=2 before starting Ollama.  On Windows:
      setx OLLAMA_NUM_PARALLEL 2
    then restart Ollama from the system tray.
    """
    global _last_caption_warn
    _CAPTION_RETRIES    = 3
    _CAPTION_RETRY_DELAY = 2.0   # seconds between retries
    try:
        import requests as _req
        for attempt in range(_CAPTION_RETRIES):
            resp = _req.post(
                f"{_OLLAMA_BASE}/api/generate",
                json={
                    "model":  _CAPTION_MODEL,
                    "prompt": _CAPTION_PROMPT,
                    "images": [b64_png],
                    "stream": False,
                    "options": {"temperature": 0.2, "num_predict": 120},
                },
                timeout=30,
            )
            if resp.status_code == 200:
                _last_caption_warn = 0.0   # reset on success
                return resp.json().get("response", "").strip()
            # 500 / 503 = Ollama busy — wait and retry
            if attempt < _CAPTION_RETRIES - 1:
                time.sleep(_CAPTION_RETRY_DELAY)
                continue
            # All retries exhausted — warn at most once/min
            now = time.time()
            if now - _last_caption_warn > 60:
                logger.debug(
                    "VisualMemory: Ollama busy ({}) after {} retries — captions paused. "
                    "Set OLLAMA_NUM_PARALLEL=2 env var for true concurrency.",
                    resp.status_code, _CAPTION_RETRIES,
                )
                _last_caption_warn = now
        return None
    except Exception as exc:
        logger.debug("VisualMemory: captioning failed — {}", exc)
        return None


# ── Capture helpers ───────────────────────────────────────────────────────────

def _grab_screen() -> tuple[Any, int, int] | None:
    """Grab a screenshot using mss. Returns (PIL.Image, w, h) or None."""
    try:
        import mss
        from PIL import Image
        with mss.mss() as sct:
            monitor = sct.monitors[1]   # primary monitor
            raw = sct.grab(monitor)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            return img, img.width, img.height
    except Exception as exc:
        logger.debug("VisualMemory: screen grab failed — {}", exc)
        return None


def _grab_camera(cap: Any) -> tuple[Any, int, int] | None:
    """Read one frame from an OpenCV VideoCapture. Returns (PIL.Image, w, h) or None."""
    try:
        import cv2
        from PIL import Image
        ret, frame = cap.read()
        if not ret or frame is None:
            return None
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        return img, img.width, img.height
    except Exception as exc:
        logger.debug("VisualMemory: camera grab failed — {}", exc)
        return None


# ── VisualMemoryService ───────────────────────────────────────────────────────

class VisualMemoryService:
    """Continuous visual watcher with episodic SQLite memory.

    Sources
    -------
    "screen"  — mss screen capture
    "camera"  — OpenCV webcam

    Both can run simultaneously. Each source tracks its own previous hash so
    change detection is independent.
    """

    def __init__(self) -> None:
        self._db_path      = _DB_PATH
        self._lock         = threading.Lock()
        self._stop_event   = threading.Event()
        self._thread: threading.Thread | None = None

        self._active_sources: set[str] = set()
        self._session_id: str | None   = None

        # Per-source state
        self._prev_hash: dict[str, Any] = {"screen": None, "camera": None}
        self._cap: Any = None   # OpenCV VideoCapture (camera only)

        _init_db(self._db_path)
        logger.info("VisualMemory: DB ready at {}", self._db_path)

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_session(self, session_id: str | None) -> None:
        """Tag subsequent observations with a session label (e.g. 'my-task-name')."""
        with self._lock:
            self._session_id = session_id

    def start(self, sources: list[str] | None = None) -> None:
        """Start the background capture thread.

        Args:
            sources: list of source names to watch — any of ["screen", "camera"].
                     Defaults to ["screen"] if not specified.
        """
        if sources is None:
            sources = ["screen"]

        with self._lock:
            self._active_sources = {s for s in sources if s in ("screen", "camera")}

        if not self._active_sources:
            logger.warning("VisualMemory: no valid sources specified — not starting.")
            return

        if self.is_running:
            logger.debug("VisualMemory: already running.")
            return

        # Open camera if needed
        if "camera" in self._active_sources:
            self._open_camera()

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="visual-memory",
            daemon=True,
        )
        self._thread.start()
        logger.info("VisualMemory: started watching {}", self._active_sources)

    def stop(self) -> None:
        """Stop the background thread and release camera if open."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        self._close_camera()
        logger.info("VisualMemory: stopped.")

    def capture_now(self, source: str = "screen") -> str | None:
        """Force an immediate capture + caption, bypassing change detection.

        Returns the caption string, or None if capture/captioning failed.
        Useful for: 'Serenity, look at my screen right now.'
        """
        caption = self._capture_and_store(source, force=True)
        return caption

    def get_recent(
        self,
        n: int = 5,
        source: str | None = None,
        session_id: str | None = None,
    ) -> list[dict]:
        """Return the n most recent observations as a list of dicts.

        Args:
            n:          how many rows to return
            source:     filter by 'screen' or 'camera' (None = both)
            session_id: filter by session tag (None = all sessions)
        """
        clauses: list[str] = []
        params:  list[Any] = []

        if source:
            clauses.append("source = ?")
            params.append(source)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql   = f"SELECT * FROM observations {where} ORDER BY ts DESC LIMIT ?"
        params.append(n)

        with _db(self._db_path) as con:
            rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in reversed(rows)]   # oldest first

    def format_for_prompt(
        self,
        n: int = 5,
        source: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Return a formatted [Visual Memory] block ready to inject into a prompt."""
        rows = self.get_recent(n=n, source=source, session_id=session_id)
        if not rows:
            return ""

        lines = ["[Visual Memory — what Serenity has seen recently]"]
        for row in rows:
            ts_str = datetime.fromtimestamp(row["ts"]).strftime("%H:%M:%S")
            src    = row["source"].upper()
            cap    = row["caption"] or "(no caption)"
            sess   = f" [{row['session_id']}]" if row.get("session_id") else ""
            lines.append(f"  {ts_str} {src}{sess}  {cap}")
        lines.append("")
        return "\n".join(lines)

    def search(self, keyword: str, limit: int = 10) -> list[dict]:
        """Full-text search across captions — useful for recall queries."""
        with _db(self._db_path) as con:
            rows = con.execute(
                "SELECT * FROM observations WHERE caption LIKE ? ORDER BY ts DESC LIMIT ?",
                (f"%{keyword}%", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def store_caption(
        self,
        source: str,
        caption: str,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        """Store an externally-computed caption without re-capturing or re-calling the VLM.

        Used when eyes_snapshot / eyes_screen already called minicpm — we log the
        result here so vision RAG stays current without a second Ollama call.
        """
        with self._lock:
            session_id = self._session_id
        ts = time.time()
        with _db(self._db_path) as con:
            con.execute(
                "INSERT INTO observations "
                "(ts, source, session_id, img_hash, caption, width, height) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, source, session_id, None, caption, width, height),
            )
            con.commit()
        logger.debug("VisualMemory: stored external caption [{}] {}", source, caption[:80])

    # ── Internal ──────────────────────────────────────────────────────────────

    def _open_camera(self) -> None:
        try:
            import cv2
            self._cap = cv2.VideoCapture(0)
            if not self._cap.isOpened():
                logger.warning("VisualMemory: camera index 0 not available.")
                self._cap = None
                self._active_sources.discard("camera")
        except ImportError:
            logger.warning("VisualMemory: OpenCV not installed — camera source disabled.")
            self._active_sources.discard("camera")

    def _close_camera(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

    def _loop(self) -> None:
        """Background polling loop — runs every _POLL_INTERVAL seconds."""
        while not self._stop_event.is_set():
            try:
                for source in list(self._active_sources):
                    self._capture_and_store(source, force=False)
            except Exception as exc:
                logger.debug("VisualMemory: tick error — {}", exc)
            self._stop_event.wait(_POLL_INTERVAL)

    def _capture_and_store(self, source: str, force: bool) -> str | None:
        """Capture one frame from source, check for change, caption, store. Returns caption."""
        # Grab frame
        if source == "screen":
            result = _grab_screen()
        elif source == "camera":
            result = _grab_camera(self._cap) if self._cap else None
        else:
            return None

        if result is None:
            return None

        img, w, h = result

        # Perceptual hash + change detection
        img_small = img.copy()
        img_small.thumbnail((256, 256))   # small copy for hashing
        curr_hash = _dhash(img_small)

        with self._lock:
            prev_hash  = self._prev_hash.get(source)
            session_id = self._session_id

        if not force and not _hash_changed(prev_hash, curr_hash):
            return None   # nothing changed — skip captioning

        # Update hash
        with self._lock:
            self._prev_hash[source] = curr_hash

        # Resize and encode for VLM
        img_vlm   = _resize_for_vlm(img)
        b64_image = _pil_to_b64(img_vlm)

        # Caption with MiniCPM-V
        caption = _caption_image(b64_image)
        if caption is None:
            # Ollama busy or unavailable — store the frame change but skip caption
            # Don't log anything here — the rate-limited warning in _caption_image is enough
            return None

        # Store in SQLite — only when we have a real caption
        ts = time.time()
        img_hash_str = str(curr_hash) if curr_hash is not None else None
        with _db(self._db_path) as con:
            con.execute(
                "INSERT INTO observations (ts, source, session_id, img_hash, caption, width, height) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, source, session_id, img_hash_str, caption, w, h),
            )
            con.commit()

        # Only log when something real was seen
        logger.info("VisualMemory: [{}] {}", source, caption[:100])
        return caption


# ── Convenience functions ─────────────────────────────────────────────────────

def start_watching(sources: list[str] | None = None, session_id: str | None = None) -> None:
    """Module-level shortcut: start the singleton service."""
    svc = get_service()
    if session_id:
        svc.set_session(session_id)
    svc.start(sources=sources)


def stop_watching() -> None:
    """Module-level shortcut: stop the singleton service."""
    get_service().stop()


def remember_what_i_see(source: str = "screen") -> str | None:
    """Capture and store one frame immediately. Returns caption."""
    return get_service().capture_now(source=source)


def recall(n: int = 5, source: str | None = None, session_id: str | None = None) -> str:
    """Return a formatted visual memory block for prompt injection."""
    return get_service().format_for_prompt(n=n, source=source, session_id=session_id)


def search_memory(keyword: str) -> list[dict]:
    """Search visual memory captions for a keyword."""
    return get_service().search(keyword)
