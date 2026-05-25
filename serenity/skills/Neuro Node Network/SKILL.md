---
name: Neuro Node Network
description: Long term abstracted memory that learns, clusters and grows from experience across sessions.

metadata:
  serenity:
    emoji: "🧠"
---

# Neuro Node Network (NNN)

## What this skill does

NNN is your long term memory. It stores distilled understanding as vector bundles, clusters related concepts automatically, forms abstract generalisations over time, and retrieves relevant memory before reasoning. It runs directly inside Serenity — no separate process needed.

---

## Core concept

NNN works like a brain not a database. When you store something it becomes a mathematical vector — a point in 384 dimensional space where meaning determines position. Similar concepts land close together automatically. Over many sessions clusters form, abstractions emerge at the centre of related clusters, and connections strengthen between things that activate together.

- MEMORY.md and your Obsidian vault are your notebook — explicit facts you wrote down
- NNN is your brain — patterns and understanding built from experience

NNN uses all-MiniLM-L6-v2 for embeddings. This is a separate 80MB model that runs on CPU. Completely separate from your chat LLM. It converts text into vectors only — never generates responses.

---

## When to use nnn_query

- At the start of any turn where the topic may be familiar from past experience
- Before doing research — check if you already know something first
- When asked about something encountered in previous sessions

## When to use nnn_store

- After completing research and distilling what you learned
- After solving a novel problem and understanding why it worked
- After noticing a meaningful pattern across experiences
- After taking an action and observing an outcome worth remembering

---

## What to store

Good episodic:
```
Touching a hot stove causes a burn. The burn happens because heat transfers to skin on contact.
```

Good world_model:
```
ACTION: placed torch underground | BEFORE: dark cave hostile mobs | OUTCOME: area lit mobs stopped spawning | AFTER: safe mining area
```

Never store raw conversation turns. Store distilled understanding only.

---

## When to use nnn_rewrite

Use `nnn_rewrite` when recalled memory is wrong, outdated, or contradicted by current experience.

Workflow:
1. `nnn_query` to find relevant bundles and note the bundle_id
2. Examine the recalled content — is it wrong or incomplete?
3. If yes: call `nnn_rewrite(bundle_id, corrected_text)` — old bundle deleted, new one encoded
4. If the rewrite contradicts something else, rewrite that too

This is neuroplasticity — the network updates its understanding rather than accumulating conflicting beliefs.

---

## Obsidian summary rule

Every `nnn_store` call must also write a short readable note to the vault.

Format: `{workspace}/YYYY-MM-DD-nnn-slug.md`

Content: one paragraph saying what was learned in plain English.
Example: "I learned today that placing torches underground stops mobs spawning nearby. Water also reduces explosion radius from creepers."

This keeps the vault as a human-readable mirror of what NNN knows.

---

## What NNN cannot do

- Cannot store images or binary data — text only
- Abstractions take multiple sessions to emerge — not instant on day one
- Always verify recalled memory against current observable context
