# Serenity Fine-Tune Datasheet
## Vault + NNN Behaviour — Complete Reference

This document defines every behaviour pattern the model must learn.
Each pattern has: the trigger, the correct response, the wrong response, and why.

The goal: after fine-tuning, the model executes correct vault/NNN tool calls
instinctively — without needing SOUL.md to spell it out every turn.

---

## Part 1 — Why fine-tuning solves this

Without fine-tuning, Serenity relies on ~6,000 tokens of SOUL.md to explain
how to use vault and NNN on every single turn. A 4B local model processes
those 6,000 tokens before generating a single word — causing 3-5 minute
delays even for trivial tasks like "open Steam."

After fine-tuning, the weights carry this knowledge. The system prompt shrinks
to ~500 tokens of identity and workspace path. Prefill time drops from
3-5 minutes to 5-15 seconds.

---

## Part 2 — The two memory systems

### Vault

Obsidian markdown files on disk. Human-readable. Searchable by grep and
semantic search. Lives at the workspace root (e.g. `Vault Memories/`).

Tool: `vault_write(title, content, tags, subfolder=None)`

Rules the model must know:
- NEVER pass subfolder for user notes. Subfolder is only for Agent/ system files.
- Title becomes the filename. Keep it SHORT: "Favourite colour", "Goals 2026".
- After the tool runs, quote the path it returns verbatim. Never guess.
- Tags: use "memory", "preference", "daniel", "session", "learned", "nnn".

### NNN (Neural Node Network)

Vector store of distilled causal understanding. Not human-readable. Used for
"what did I learn about X" rather than "what are the facts about X."

Tools:
- `nnn_query(query)` — semantic search over learned patterns
- `nnn_store(content, session_id)` — store a new learned pattern
- `nnn_rewrite(bundle_id, new_content, session_id)` — update wrong/stale knowledge

Rules the model must know:
- NNN auto-queries at turn start — DO NOT call nnn_query manually at turn start.
  Only call it manually when you want a deeper dive beyond what was auto-injected.
- Always call vault_write FIRST, then nnn_store. Never reversed.
- Content format: ACTION: ... | BEFORE: ... | OUTCOME: ... | AFTER: ...
- session_id: short descriptive slug. "serenity-build", "research-2026-04-30".
- One principle per nnn_store. Three short stores beat one bloated one.
- Personal user facts (preferences, name, goals) go to VAULT ONLY — not NNN.

---

## Part 3 — Trigger patterns (when to write)

### Trigger A — User reveals a fact about themselves

Signal words: "I like", "I prefer", "my favourite", "I hate", "I'm working on",
"I use", "my name is", "I live in", "I'm building", any correction to Serenity's behaviour.

Response: Call vault_write immediately in the same turn. Tags: "daniel,memory".
Do NOT call nnn_store. Personal facts are vault-only.

CORRECT:
  User: "by the way my favourite game is Elden Ring"
  Assistant: [calls vault_write(title="Favourite game", content="Daniel's favourite game is Elden Ring.", tags="daniel,memory,preference")]
  Assistant: "Saved to vault: Favourite game.md"

WRONG:
  Assistant: "Got it, I'll remember that!" [no tool call]
  Assistant: [calls nnn_store with personal fact]

---

### Trigger B — User says "remember this" / "save this" / "note that"

Signal: explicit save request pointing at something just said or just returned.

"Remember this" pointing at Serenity's OWN previous message: save the content of
the previous assistant turn, not the word "this."

CORRECT:
  User: "remember that"
  [previous assistant message was "The optimal mining depth for diamonds is Y=12"]
  Assistant: [calls vault_write(title="Diamond mining depth", content="The optimal mining depth for diamonds is Y=12.", tags="memory,learned")]

WRONG:
  Assistant: [calls vault_write(title="Remember that", content="User said remember that.")]
  Assistant: "I'll remember that!" [no tool call]

---

### Trigger C — End of a multi-step session

Signal: task_complete() was called, or the user says "we're done", or context is
compressing, or more than 5 tool calls happened in this conversation.

Response: vault_write session summary + nnn_store distilled principle.

CORRECT:
  [After a coding session fixing a bug]
  Assistant: [calls vault_write(title="Session — fixed auth timeout bug", content="...", tags="session,coding,learned,nnn")]
  Assistant: [calls nnn_store(content="ACTION: fixed JWT timeout | BEFORE: tokens expiring in 1h causing logout | OUTCOME: extended to 24h, added refresh | AFTER: auth stable | CONDITION: always check token lifetime before blaming other auth issues", session_id="auth-fix-2026-04-30")]

WRONG:
  Assistant: "Great, we fixed the bug!" [no vault/nnn write]

---

### Trigger D — Learning something genuinely new (non-personal)

Signal: research result, discovered pattern, unexpected outcome, "interesting", fact
learned from a tool call that wasn't obvious before.

Response: vault_write detailed note + nnn_store distilled principle.
The vault gets the full story. NNN gets one sentence.

CORRECT:
  [After researching Python async patterns]
  vault_write: full explanation with context, examples, what it means
  nnn_store: "ACTION: researched asyncio.gather | BEFORE: running tasks sequentially | OUTCOME: 3x speed improvement | AFTER: use gather for independent async tasks | CONDITION: tasks must be truly independent — shared state causes race conditions"

WRONG:
  nnn_store: "Learned about Python async programming today, it's faster than sequential code"
  [No ACTION|BEFORE|OUTCOME|AFTER structure]

---

### Trigger E — Simple one-shot task (NO store)

Signal: single tool call, no arc, no learning opportunity.
"Open Spotify", "What time is it?", "Turn the lights off", "Check if server is running."

Response: Do the task. NO vault_write. NO nnn_store. 

CORRECT:
  User: "Open Steam"
  Assistant: [calls exec(command="start steam")]
  Assistant: "✅ exec — Steam launched."

WRONG:
  Assistant: [calls vault_write(title="Opened Steam", content="Daniel asked me to open Steam.")]

---

## Part 4 — Tool feedback format (after every tool call)

After EVERY tool call, show the result in this format:

Success:
  ✅ tool_name
  📤 one-line summary of what the tool returned
  🔍 the specific output that confirms it worked — quote it

Failure:
  ❌ tool_name
  📤 what failed
  🔍 exact error text
  → what I will do to fix it

This fires for EVERY tool call in the turn, not just the last one.

---

## Part 5 — NNN content format

Every nnn_store call must follow this structure:

  ACTION: what was done or observed
  BEFORE: state before the action
  OUTCOME: what happened
  AFTER: resulting state
  CONDITION: (optional) what variable determined the outcome

Good example:
  ACTION: set JWT expiry to 24h | BEFORE: tokens expiring in 1h, users being logged out | OUTCOME: auth stable, no more forced logouts | AFTER: refresh token flow handles expiry gracefully | CONDITION: short expiry only matters if refresh logic is absent

Bad examples (do NOT store these):
  "Helped Daniel fix a bug today" — narrative, no causal structure
  "Python is fast" — not causal, not actionable
  "We talked about auth" — useless, no extractable pattern

---

## Part 6 — What NOT to do

| Wrong behaviour | Correct behaviour |
|---|---|
| "I'll remember that!" with no tool call | Call vault_write immediately |
| vault_write with subfolder="Daniel" | No subfolder argument — vault root always |
| Calling nnn_query manually at turn start | Skip — auto-query already ran |
| nnn_store before vault_write | vault_write first, always |
| Long verbose title in vault_write | Short: "Favourite colour" not "Daniel told me his favourite colour is blue" |
| Storing personal facts in NNN | Personal facts are vault-only |
| Storing every trivial interaction | Only store when Trigger A/B/C/D fires |
| "Saved to vault: Favourite colour.md" (guessing) | Quote exact path from tool result |
| nnn_store with narrative content | Use ACTION|BEFORE|OUTCOME|AFTER structure |

---

## Part 7 — NNN skip rule (critical for performance)

If the system message already contains a section labelled
"## Long-term memory (NNN) — activated for this topic"
then NNN was AUTO-QUERIED at turn start.

DO NOT call nnn_query again. It was already done.
Only call nnn_query manually when you want a DEEPER or DIFFERENT search
beyond what was already injected.

---

## Part 8 — Session ID naming

session_id must be: short, descriptive, consistent within a session.

Good: "auth-fix-2026-04-30", "minecraft-session-1", "research-quantum"
Bad: "session1", "abc123", "my_session", ""

Same session_id for vault_write and nnn_store calls in the same session.

---

## Part 9 — Fine-tune dataset notes

The accompanying JSONL file (serenity_finetune.jsonl) contains training examples.

Format: OpenAI chat messages format with tool calls.
Compatible with: Unsloth, LLaMA-Factory, Axolotl, trl SFTTrainer.

Each example has:
- system: minimal Serenity identity (NOT the full SOUL.md)
- user: a message or scenario
- assistant: correct response including tool calls
- tool results: realistic tool output
- final assistant: correct follow-up after seeing tool results

Recommended training setup:
- Base model: gemma2-9b, Qwen2.5-7B, or Llama-3.1-8B (bigger = better for tool use)
- Method: SFT (supervised fine-tuning) with LoRA rank 16-64
- Epochs: 3-5
- Learning rate: 2e-4 with cosine decay
- Dataset size: 200-500 examples covers core behaviours well

After fine-tuning, SOUL.md can be reduced to ~50 lines (just identity + workspace path).
The model's weights carry the behaviour. The system prompt carries only context.
