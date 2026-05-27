"""NNN — Neural Node Network — long term memory tools for Serenity.

NNN is a pure write + on-demand read system.

  nnn_store  — called by Serenity after learning something worth keeping.
               Distills into a causal bundle (ACTION|BEFORE|OUTCOME|AFTER).
               Does NOT mirror to vault — vault and NNN are separate stores.

  nnn_query  — called manually when Serenity wants a deep retrieval with
               graph propagation. NOT auto-fired every turn.

  nnn_rewrite — corrects a wrong or stale bundle.

Vault handles full readable notes. NNN handles distilled causal patterns.
They are deliberately separate — vault_write is always called first with the
full story, nnn_store is called after with the compressed principle.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import StringSchema, tool_parameters_schema

# Per-call timeout for NNN embedding / query / rewrite operations.
# nomic-embed-text on a cold Ollama instance can take 60-90 s on first load;
# 120 s covers that comfortably. Override with SERENITY_NNN_TIMEOUT (seconds).
# Read lazily so tests can set the env var after import.
def _nnn_timeout() -> float:
    return float(os.environ.get("SERENITY_NNN_TIMEOUT", "120"))

# ── Module-level cache ────────────────────────────────────────────────────────
_nnn_query_fn   = None
_nnn_encode_fn  = None
_nnn_rewrite_fn = None
_nnn_init_lock  = threading.Lock()  # prevents double-import race under concurrent async tasks


def _get_nnn_fns():
    global _nnn_query_fn, _nnn_encode_fn, _nnn_rewrite_fn
    if _nnn_query_fn is None:
        with _nnn_init_lock:
            # Double-check after acquiring lock — another thread may have initialised first
            if _nnn_query_fn is None:
                try:
                    from serenity_nnn import encode, query, rewrite
                except ImportError as exc:
                    raise RuntimeError(
                        "serenity_nnn is not installed. "
                        "Run: pip install serenity-nnn  (or reinstall via: uv sync)"
                    ) from exc
                _nnn_query_fn  = query
                _nnn_encode_fn = encode
                _nnn_rewrite_fn = rewrite
    return _nnn_query_fn, _nnn_encode_fn, _nnn_rewrite_fn


# =============================================================================
# Tools
# =============================================================================

@tool_parameters(
    tool_parameters_schema(
        query=StringSchema("The concept or question to search in long term causal memory"),
        required=["query"],
    )
)
class NNNQueryTool(Tool):
    """Deep search of long term causal memory with graph propagation."""

    @property
    def name(self) -> str:
        return "nnn_query"

    @property
    def description(self) -> str:
        return (
            "Search long term causal memory (NNN) for distilled patterns. "
            "Call this when you want to know what was LEARNED about a topic "
            "across past sessions — not just what was noted, but the distilled "
            "cause-effect pattern. "
            "Returns bundles ranked by semantic relevance and activation score. "
            "Also propagates through connected concepts — activating 'diamond mining' "
            "may also surface 'underground navigation' if they co-activated before. "
            "Use when vault notes exist but you want the distilled principle. "
            "Not called automatically — call deliberately when depth is needed."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, query: str, **kwargs: Any) -> str:
        try:
            nnn_query_fn, _, _ = _get_nnn_fns()
            loop = asyncio.get_running_loop()
            results = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: nnn_query_fn(text=query, token_budget=3000)),
                timeout=_nnn_timeout(),
            )
            if not results:
                return (
                    "NNN query completed — no causal memories matched this topic. "
                    "NNN is working but has no stored experience relevant to this query. "
                    "Answer from general knowledge or tell the user you haven't encountered this before."
                )

            import re
            _STOP = frozenset({
                "a","an","the","is","it","in","on","at","to","for","of","and","or",
                "but","i","my","me","we","you","your","what","was","are","be","do",
                "did","does","has","have","had","can","will","just","that","this",
                "with","from","how","tell","show","say","said","about",
            })
            def _hints(content: str) -> str:
                for clause in ("AFTER:", "OUTCOME:", "ACTION:"):
                    if clause in content:
                        seg = content.split(clause, 1)[1].split("|")[0].strip()
                        words = re.findall(r"[a-zA-Z']+", seg.lower())
                        kws = [w for w in words if len(w) >= 4 and w not in _STOP]
                        if kws:
                            return ", ".join(list(dict.fromkeys(kws[:4])))
                words = re.findall(r"[a-zA-Z']+", content.lower())
                kws = [w for w in words if len(w) >= 5 and w not in _STOP]
                return ", ".join(list(dict.fromkeys(kws[:4])))

            parts = []
            for r in results:
                hint = _hints(r.content)
                hint_line = f"\n   vault_hint: grep vault for '{hint}'" if hint else ""
                parts.append(
                    f"[{r.type.upper()} | relevance: {r.activation_score:.2f}]\n"
                    f"{r.content}{hint_line}"
                )
            return "Causal memory activated:\n\n" + "\n\n".join(parts)
        except asyncio.TimeoutError:
            return f"NNN query timed out after {_nnn_timeout():.0f}s (Ollama may be cold). Set SERENITY_NNN_TIMEOUT to increase."
        except Exception as e:
            return f"NNN unavailable: {e}"


@tool_parameters(
    tool_parameters_schema(
        content=StringSchema(
            "Distilled causal principle. ALWAYS use this exact structure:\n"
            "ACTION: what happened or was done | BEFORE: state/context before | "
            "OUTCOME: what resulted (most common outcome) | AFTER: resulting state\n"
            "This applies to ALL stored knowledge — world facts, learned skills, "
            "user preferences, task results. The causal structure is what makes "
            "NNN predictive. One principle only. No narrative. No raw conversation."
        ),
        session_id=StringSchema(
            "Short descriptive slug for this session. "
            "e.g. 'auth-fix-2026-04-30', 'research-quantum', 'serenity-build'. "
            "Consistent across vault_write and nnn_store in the same session."
        ),
        required=["content", "session_id"],
    )
)
class NNNStoreTool(Tool):
    """Store a distilled causal principle into long term memory."""

    @property
    def name(self) -> str:
        return "nnn_store"

    @property
    def description(self) -> str:
        return (
            "Store a distilled causal principle into long term memory. "
            "Always call vault_write FIRST with the full story, then call this "
            "with the compressed causal principle. "
            "ALWAYS use ACTION|BEFORE|OUTCOME|AFTER structure — for everything: "
            "world knowledge, learned skills, task results, user preferences. "
            "The causal structure merges into the concept centroid, making future "
            "queries return both the concept AND its predicted outcome automatically. "
            "NNN and vault are separate: vault holds the full note, "
            "NNN holds the distilled predictive pattern."
        )

    # Hard cap on bundle size — keeps NNN concise and prevents raw conversation dumps.
    # 500 chars fits a full ACTION|BEFORE|OUTCOME|AFTER causal bundle comfortably.
    _MAX_BUNDLE_CHARS = 500

    @classmethod
    def _validate_and_trim(cls, content: str) -> tuple[str, str | None]:
        """Validate NNN content and return (cleaned_content, rejection_reason).

        Only rejects truly empty content or obvious raw conversation dumps
        (very long, no pipes, no clauses at all). Everything else is accepted
        and truncated to _MAX_BUNDLE_CHARS if needed.

        The tool description guides the agent toward ACTION|BEFORE|OUTCOME|AFTER
        format — hard rejection is too aggressive and blocks legitimate memories.
        """
        stripped = content.strip()
        if not stripped:
            return "", "empty content"

        # Only reject content that is clearly a raw conversation dump:
        # very long, no pipe separators, and none of the four clause markers.
        _CLAUSES = ("ACTION:", "BEFORE:", "OUTCOME:", "AFTER:")
        has_any_clause = any(c in stripped for c in _CLAUSES)
        if not has_any_clause and "|" not in stripped and len(stripped) > 300:
            return "", (
                "content looks like a raw conversation dump — too long with no structure. "
                "Distill into: ACTION: x | BEFORE: y | OUTCOME: z | AFTER: w"
            )

        # Truncate at a pipe boundary to keep bundles concise
        if len(stripped) > cls._MAX_BUNDLE_CHARS:
            stripped = stripped[: cls._MAX_BUNDLE_CHARS].rsplit("|", 1)[0].strip()

        return stripped, None

    async def execute(self, content: str, session_id: str, **kwargs: Any) -> str:
        cleaned, rejection = self._validate_and_trim(content)
        if rejection:
            return (
                f"NNN store rejected — {rejection}.\n"
                f"Nothing was stored. Fix the format and call nnn_store again if this matters."
            )

        try:
            _, nnn_encode_fn, _ = _get_nnn_fns()
            loop = asyncio.get_running_loop()
            timeout = _nnn_timeout()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: nnn_encode_fn(text=cleaned, session_id=session_id)),
                timeout=timeout,
            )
            bundle_id = result.get("bundle_id") or "unknown"
            bundle_short = bundle_id[:8]
            return (
                f"Stored to NNN ✓\n"
                f"Action: {result.get('action', 'stored')} | Bundle: {bundle_short}...\n"
                f"Vault note already written separately with the full story."
            )
        except asyncio.TimeoutError:
            return f"NNN store timed out after {_nnn_timeout():.0f}s (Ollama may be cold). Set SERENITY_NNN_TIMEOUT to increase."
        except Exception as e:
            return f"Could not store to NNN: {e}"


@tool_parameters(
    tool_parameters_schema(
        actions=StringSchema(
            "The planned actions to simulate, one per line or comma-separated. "
            "Each action should be a short description of what you plan to do. "
            "Example: 'query the API, parse the response, write to vault' "
            "Maximum 5 steps — deeper chains compound prediction error."
        ),
        initial_state=StringSchema(
            "The current state before step 1. Describe the situation concisely. "
            "Example: 'API key is valid, rate limit unknown, no cached data'"
        ),
        required=["actions", "initial_state"],
    )
)
class NNNSimulateTool(Tool):
    """Simulate a multi-step plan using NNN causal memory before committing to it."""

    @property
    def name(self) -> str:
        return "nnn_simulate"

    @property
    def description(self) -> str:
        return (
            "Simulate a sequence of planned actions using NNN causal memory. "
            "Use this before committing to a multi-step plan — especially for tasks, "
            "goal steps, or capability builds. "
            "NNN predicts the outcome of each step and feeds it as the state for the next, "
            "giving you a full predicted path before you act. "
            "Each step shows confidence — confidence decays per step as predictions compound. "
            "If the predicted end state looks wrong, try different actions before starting. "
            "Only useful when NNN has causal bundles relevant to your planned actions — "
            "new domains with no stored experience will return low-confidence results."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, actions: str, initial_state: str, **kwargs: Any) -> str:
        try:
            from serenity_nnn import simulate_plan

            # Parse actions — support newline or comma separated
            if "\n" in actions:
                action_list = [a.strip() for a in actions.splitlines() if a.strip()]
            else:
                action_list = [a.strip() for a in actions.split(",") if a.strip()]

            if not action_list:
                return "nnn_simulate: no actions provided."

            if len(action_list) > 5:
                action_list = action_list[:5]

            loop = asyncio.get_running_loop()
            steps = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: simulate_plan(action_list, initial_state),
                ),
                timeout=_nnn_timeout(),
            )

            if not steps:
                return "nnn_simulate: no results — NNN may have no causal experience relevant to these actions."

            lines = [
                f"Simulated plan ({len(steps)} steps) from: {initial_state}\n"
            ]
            for s in steps:
                confidence_label = (
                    "high" if s["confidence"] >= 0.75
                    else "medium" if s["confidence"] >= 0.50
                    else "low"
                )
                reliable_flag = "" if s["reliable"] else " ⚠ uncertain"
                outcome = s["predicted_outcome"] or "unknown — no NNN experience for this step"
                lines.append(
                    f"Step {s['step']}: {s['action']}\n"
                    f"  State before: {s['state_before']}\n"
                    f"  Predicted:    {outcome}\n"
                    f"  Confidence:   {confidence_label} ({s['confidence']:.0%}){reliable_flag}"
                )

            lines.append(
                "\nIf the predicted path looks wrong — change your step 1 action and simulate again. "
                "If confidence is low throughout — NNN has little experience here, proceed carefully."
            )

            return "\n\n".join(lines)

        except asyncio.TimeoutError:
            return f"nnn_simulate timed out after {_nnn_timeout():.0f}s."
        except Exception as e:
            return f"nnn_simulate failed: {e}"


@tool_parameters(
    tool_parameters_schema(
        bundle_id=StringSchema("ID of the bundle to rewrite — get this from nnn_query results"),
        new_content=StringSchema(
            "The corrected understanding. Must be more precise than what was stored. "
            "Use ACTION|BEFORE|OUTCOME|AFTER structure."
        ),
        session_id=StringSchema("Current session slug"),
        required=["bundle_id", "new_content", "session_id"],
    )
)
class NNNRewriteTool(Tool):
    """Replace a wrong or stale NNN bundle with corrected understanding."""

    @property
    def name(self) -> str:
        return "nnn_rewrite"

    @property
    def description(self) -> str:
        return (
            "Replace an existing NNN bundle when new experience contradicts "
            "or substantially expands what was previously stored. "
            "First call nnn_query to find the bundle_id. "
            "Then rewrite with the corrected principle. "
            "The old bundle is deleted, connections are transplanted to the new one."
        )

    async def execute(self, bundle_id: str, new_content: str, session_id: str, **kwargs: Any) -> str:
        try:
            _, _, nnn_rewrite_fn = _get_nnn_fns()
            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: nnn_rewrite_fn(
                        bundle_id=bundle_id,
                        new_text=new_content,
                        session_id=session_id,
                    ),
                ),
                timeout=_nnn_timeout(),
            )
            return (
                f"NNN updated. "
                f"Old bundle {result['old_bundle_id'][:8]}... → "
                f"new bundle {result['new_bundle_id'][:8]}..."
            )
        except asyncio.TimeoutError:
            return f"NNN rewrite timed out after {_nnn_timeout():.0f}s (Ollama may be cold). Set SERENITY_NNN_TIMEOUT to increase."
        except Exception as e:
            return f"Could not rewrite NNN bundle: {e}"
