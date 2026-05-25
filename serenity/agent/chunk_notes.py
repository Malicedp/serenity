"""Chunk notes — fast, zero-LLM summarisation of conversation overflow.

When messages fall outside the active 40-message context window mid-conversation,
this module generates a brief structured note for each 10-message chunk using:

  1. TextRank  — extracts the most semantically central sentence (pure Python)
  2. First/last — anchors the chunk: how it opened, how it closed
  3. spaCy NER  — pulls names, places, orgs mentioned (falls back to regex)

These notes are breadcrumbs only — keeping the thread coherent during long
conversations. The real learning (NNN/vault writes, running_summary) happens
during idle deep-summarise and session reflection.

Notes are stored in session.metadata["chunk_notes"] and cleared after a
successful deep_summarise supersedes them.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from loguru import logger

# ── spaCy NER (optional — falls back to regex) ────────────────────────────────

_nlp = None
_NLP_TRIED = False


def _get_nlp():
    global _nlp, _NLP_TRIED
    if _NLP_TRIED:
        return _nlp
    _NLP_TRIED = True
    try:
        import spacy
        _nlp = spacy.load("en_core_web_sm", disable=["parser", "tagger", "lemmatizer"])
        logger.debug("chunk_notes: spaCy en_core_web_sm loaded")
    except Exception:
        _nlp = None
        logger.debug("chunk_notes: spaCy unavailable — using regex NER fallback")
    return _nlp


# ── Text utilities ────────────────────────────────────────────────────────────

def _extract_text(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Return (role, clean_text) pairs — strips tool calls and blank content."""
    out: list[tuple[str, str]] = []
    for msg in messages:
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content") or ""
        if isinstance(content, list):
            # Multi-part content — join text parts
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        content = content.strip()
        if content:
            out.append((role, content))
    return out


def _sentences(text: str) -> list[str]:
    """Split text into sentences."""
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if len(p.strip()) > 10]


def _tokenise(text: str) -> set[str]:
    """Simple word tokeniser — lowercase, strip punctuation."""
    return set(re.findall(r"[a-z]+", text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── TextRank ──────────────────────────────────────────────────────────────────

def _textrank(sentences: list[str], iterations: int = 15) -> str | None:
    """Return the most central sentence using TextRank (PageRank on sentence graph)."""
    n = len(sentences)
    if n == 0:
        return None
    if n == 1:
        return sentences[0]

    tokens = [_tokenise(s) for s in sentences]

    # Build similarity matrix
    matrix: list[list[float]] = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j:
                matrix[i][j] = _jaccard(tokens[i], tokens[j])

    # Normalise rows
    for i in range(n):
        row_sum = sum(matrix[i])
        if row_sum > 0:
            matrix[i] = [v / row_sum for v in matrix[i]]

    # Power iteration
    damping = 0.85
    scores = [1.0 / n] * n
    for _ in range(iterations):
        new_scores = [(1 - damping) / n] * n
        for i in range(n):
            for j in range(n):
                new_scores[i] += damping * scores[j] * matrix[j][i]
        scores = new_scores

    best_idx = max(range(n), key=lambda i: scores[i])
    return sentences[best_idx]


# ── Named entity extraction ───────────────────────────────────────────────────

_ENTITY_LABELS = {"PERSON", "ORG", "PRODUCT", "GPE", "LOC", "WORK_OF_ART", "EVENT"}

# Regex fallback: capitalised words that aren't common sentence starters
_CAPS_RE = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*)\b")
_STOP_CAPS = {
    "I", "The", "This", "That", "These", "Those", "It", "You", "We", "They",
    "He", "She", "But", "And", "So", "Yes", "No", "Ok", "Okay",
}


def _extract_entities(text: str) -> list[str]:
    nlp = _get_nlp()
    if nlp is not None:
        try:
            doc = nlp(text[:2000])  # cap to avoid slow processing on walls of text
            seen: set[str] = set()
            out: list[str] = []
            for ent in doc.ents:
                if ent.label_ in _ENTITY_LABELS:
                    clean = ent.text.strip()
                    if clean and clean not in seen:
                        seen.add(clean)
                        out.append(clean)
            return out[:8]
        except Exception:
            pass

    # Regex fallback
    seen: set[str] = set()
    out: list[str] = []
    for m in _CAPS_RE.finditer(text):
        word = m.group(1)
        if word not in _STOP_CAPS and word not in seen:
            seen.add(word)
            out.append(word)
    return out[:8]


# ── Main public function ──────────────────────────────────────────────────────

def generate_chunk_note(
    messages: list[dict[str, Any]],
    start_idx: int,
    end_idx: int,
) -> str:
    """Generate a brief structured note for a chunk of messages.

    Args:
        messages: The raw message dicts for this chunk.
        start_idx: Absolute index in session.messages where this chunk starts.
        end_idx: Absolute index where it ends (exclusive).

    Returns:
        A short multi-line note string.
    """
    pairs = _extract_text(messages)
    if not pairs:
        return ""

    ts = datetime.now().strftime("%H:%M")
    header = f"[Msgs {start_idx}–{end_idx} | {ts}]"

    # 1. First user message + last assistant message
    user_msgs = [text for role, text in pairs if role == "user"]
    asst_msgs = [text for role, text in pairs if role == "assistant"]
    opened = (user_msgs[0][:80] + "…") if user_msgs else ""
    closed = (asst_msgs[-1][:80] + "…") if asst_msgs else ""

    # 2. TextRank — most central sentence from the whole chunk
    all_text = " ".join(text for _, text in pairs)
    sentences = _sentences(all_text)
    central = _textrank(sentences)
    topic = (central[:100] + "…") if central and len(central) > 100 else central

    # 3. Named entities
    entities = _extract_entities(all_text)
    mentions = ", ".join(entities) if entities else ""

    lines = [header]
    if topic:
        lines.append(f"Topic: \"{topic}\"")
    if mentions:
        lines.append(f"Mentions: {mentions}")
    if opened:
        lines.append(f"Opened: \"{opened}\"")
    if closed:
        lines.append(f"Closed: \"{closed}\"")

    return "\n".join(lines)
