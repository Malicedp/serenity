"""Serenity Q-learning reinforcement loop.

A lightweight Q-table that learns which actions work best given Serenity's
current emotional state. No neural network, no dependencies — pure Python
with a JSON backing store.

State space:
  energy       × curiosity × boredom × task_context
  (low/med/hi)   (low/med/hi) (low/med/hi)  (5 types)
  = 3 × 3 × 3 × 5 = 135 states

Actions (7):
  research      — web search, nnn_query, learn something
  build         — capability_build, write a script, make a tool
  advance_goal  — take a step toward an active goal
  explore       — curiosity loop, follow an interest
  simulate      — nnn_simulate before acting
  reach_out     — message Daniel unprompted
  rest          — skip, do nothing

Reward signals (observable, no LLM self-judgment):
  +1.0   task completed successfully
  +0.5   capability built and tested (exit_code=0)
  +0.3   goal step recorded (goal_progress called)
  +0.2   prediction error was low (NNN was right)
  +0.1   Daniel replied after a reach-out
  -0.3   task abandoned mid-way
  -0.5   task failed (TASK_STOP fired)
  -0.1   capability build failed all 3 attempts

Q-update rule (standard Q-learning):
  Q(s,a) ← Q(s,a) + α × (r + γ × max Q(s') - Q(s,a))
  α = 0.1  (learning rate — slow, stable)
  γ = 0.9  (discount — future rewards matter but not as much as now)

The table biases action selection in curiosity loop and heartbeat.
Serenity still makes the final choice — the table nudges, not forces.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    pass

# ── Constants ──────────────────────────────────────────────────────────────────

ACTIONS = [
    "research",
    "build",
    "advance_goal",
    "explore",
    "simulate",
    "reach_out",
    "rest",
]

_LEVELS   = ("low", "medium", "high")
_CONTEXTS = ("research", "build", "goal", "social", "idle")

# Q-learning hyperparameters
_ALPHA = 0.1   # learning rate
_GAMMA = 0.9   # discount factor
_EPSILON = 0.1 # exploration rate — 10% chance of random action to keep learning

# Reward values
REWARD = {
    "task_complete":      1.0,
    "capability_built":   0.5,
    "goal_progress":      0.3,
    "prediction_accurate": 0.2,
    "daniel_replied":     0.1,
    "task_abandoned":    -0.3,
    "task_failed":       -0.5,
    "capability_failed": -0.1,
}

_lock = threading.Lock()


# ── State encoding ─────────────────────────────────────────────────────────────

def _encode_state(
    energy: str,
    curiosity: str,
    boredom: str,
    context: str = "idle",
) -> str:
    """Encode the current state as a compact string key."""
    e = energy   if energy   in _LEVELS   else "medium"
    c = curiosity if curiosity in _LEVELS  else "medium"
    b = boredom  if boredom  in _LEVELS   else "low"
    x = context  if context  in _CONTEXTS else "idle"
    return f"{e}|{c}|{b}|{x}"


# ── Q-table ────────────────────────────────────────────────────────────────────

class QTable:
    """The Q-table. Backed by a JSON file in the workspace state folder.

    Thread-safe. Loads lazily on first use. Saves after every update.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._table: dict[str, dict[str, float]] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        try:
            if self._path.exists():
                raw = self._path.read_text(encoding="utf-8")
                self._table = json.loads(raw)
        except Exception as e:
            logger.warning("Q-table load failed (starting fresh): {}", e)
            self._table = {}
        self._loaded = True

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._table, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Q-table save failed: {}", e)

    def get(self, state: str, action: str) -> float:
        """Return Q(state, action). Default 0.5 — neutral, not pessimistic."""
        with _lock:
            self._ensure_loaded()
            return self._table.get(state, {}).get(action, 0.5)

    def get_all(self, state: str) -> dict[str, float]:
        """Return Q values for all actions in this state."""
        with _lock:
            self._ensure_loaded()
            defaults = {a: 0.5 for a in ACTIONS}
            stored   = self._table.get(state, {})
            return {**defaults, **stored}

    def update(self, state: str, action: str, reward: float, next_state: str) -> None:
        """Q-learning update: Q(s,a) ← Q(s,a) + α(r + γ·maxQ(s') - Q(s,a))."""
        with _lock:
            self._ensure_loaded()
            current_q = self._table.get(state, {}).get(action, 0.5)
            next_qs   = list(self._table.get(next_state, {}).values()) or [0.5]
            max_next  = max(next_qs)
            new_q     = current_q + _ALPHA * (reward + _GAMMA * max_next - current_q)
            new_q     = max(0.0, min(1.0, new_q))  # clamp to [0, 1]

            if state not in self._table:
                self._table[state] = {}
            self._table[state][action] = round(new_q, 4)
            self._save()

            logger.debug(
                "Q-update: state={} action={} reward={:+.1f} "
                "Q: {:.3f} → {:.3f}",
                state, action, reward, current_q, new_q,
            )

    def best_action(self, state: str, exclude: list[str] | None = None) -> str:
        """Return the action with the highest Q value for this state."""
        import random
        scores = self.get_all(state)
        if exclude:
            scores = {a: v for a, v in scores.items() if a not in exclude}
        if not scores:
            return "rest"
        # ε-greedy: small chance of random action to keep exploring
        if random.random() < _EPSILON:
            return random.choice(list(scores.keys()))
        return max(scores, key=lambda a: scores[a])

    def bias_string(self, state: str) -> str:
        """Return a natural language bias string for injection into prompts.

        Describes which actions have historically worked best in this state
        so the LLM can factor it into its choice — without being forced.
        """
        scores = self.get_all(state)
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        top    = [(a, s) for a, s in ranked if s >= 0.65]
        avoid  = [(a, s) for a, s in ranked if s <= 0.30]

        parts: list[str] = []
        if top:
            top_str = ", ".join(a for a, _ in top[:3])
            parts.append(f"historically works well in this state: {top_str}")
        if avoid:
            avoid_str = ", ".join(a for a, _ in avoid[:2])
            parts.append(f"historically poor in this state: {avoid_str}")

        return " | ".join(parts) if parts else ""


# ── Module-level singleton ─────────────────────────────────────────────────────

_qtable: QTable | None = None

# Stores (prev_state, action, next_state_at_record_time) per session.
# State is snapshotted at record_action time — not recomputed at reward time.
# This prevents BUG-06: RL reward being tagged to a stale/default emotion state
# fetched from get_dynamics() after the real session state is gone.
_last_state: dict[str, tuple[str, str, str]] = {}  # session_key → (prev_state, action, snap_next_state)


def init(workspace: Path) -> None:
    """Call once at agent startup with the workspace path."""
    global _qtable
    _qtable = QTable(workspace / "state" / "qtable.json")
    logger.info("Q-table initialised at {}", _qtable._path)


def _get_table() -> QTable | None:
    return _qtable


# ── Public API ─────────────────────────────────────────────────────────────────

def get_bias(session_key: str, energy: str, curiosity: str, boredom: str, context: str = "idle") -> str:
    """Return a natural language bias string for the current state.

    Injected into CURIOSITY_LOOP and heartbeat prompts so the LLM knows
    what has historically worked. Empty string if no history yet.

    Does NOT write to _last_state — that only happens in record_action so
    a reward cannot be silently dropped because get_bias overwrote the action.
    """
    qt = _get_table()
    if qt is None:
        return ""
    state = _encode_state(energy, curiosity, boredom, context)
    return qt.bias_string(state)


def record_action(session_key: str, action: str, energy: str, curiosity: str, boredom: str, context: str = "idle") -> None:
    """Call when Serenity commits to an action so we can associate reward later.

    Snapshots both the current state (prev) and a next-state estimate so
    reward() doesn't need to call get_dynamics() on a potentially stale session.
    """
    state = _encode_state(energy, curiosity, boredom, context)
    # next_state is the same encoding — it will be updated if another
    # record_action fires, otherwise reward uses this as the next-state estimate.
    _last_state[session_key] = (state, action, state)


def has_pending_action(session_key: str) -> bool:
    """Return True if record_action() has been called for this session this turn.

    Used by the post-turn RL section to detect user-initiated turns where
    record_action() was never explicitly called, so rewards aren't silently dropped.
    """
    entry = _last_state.get(session_key)
    return bool(entry and entry[1])  # (prev_state, action, snap_next) — action must be non-empty


def record_reward(session_key: str, event: str, energy: str | None = None,
                  curiosity: str | None = None, boredom: str | None = None,
                  context: str = "idle") -> None:
    """Call when an observable outcome happens.

    event must be a key in REWARD dict.
    Uses the state snapshotted at record_action time — not a fresh get_dynamics()
    call which could return a stale/default object (BUG-06 fix).
    energy/curiosity/boredom args are used to build next_state if provided;
    otherwise the snapshot from record_action is reused.
    """
    qt = _get_table()
    if qt is None:
        return

    reward = REWARD.get(event, 0.0)
    if reward == 0.0:
        return

    prev = _last_state.get(session_key)
    if not prev or not prev[1]:
        return  # no action recorded yet — reward safely dropped (BUG-07 fix)

    prev_state, action, snap_next = prev
    # Build next_state from live emotion if provided, else reuse snapshot
    if energy and curiosity and boredom:
        next_state = _encode_state(energy, curiosity, boredom, context)
    else:
        next_state = snap_next
    qt.update(prev_state, action, reward, next_state)
    logger.info(
        "RL reward: session={} event={} reward={:+.1f} action={}",
        session_key, event, reward, action,
    )
