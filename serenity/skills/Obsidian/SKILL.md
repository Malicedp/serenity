---
name: obsidian
description: Work with Obsidian vaults (plain Markdown notes) as your personal knowledge base and long-term journal.
homepage: https://help.obsidian.md
metadata:
  serenity:
    emoji: "📓"
    requires:
      bins: ["obsidian-cli"]
    install:
      - id: brew
        kind: brew
        formula: "yakitrak/yakitrak/obsidian-cli"
        bins: ["obsidian-cli"]
        label: "Install obsidian-cli (brew)"
---

# Obsidian

Obsidian vault = a normal folder on disk full of plain Markdown files. Your workspace **is** the vault.

The vault home page is `Experience.md` — an index of everything stored here. It links to all major notes and projects.

## Vault structure

- Notes: `*.md` (plain text Markdown; editable with any editor or your file tools)
- Config: `.obsidian/` (workspace + plugin settings; don't touch from scripts)
- Canvases: `*.canvas` (JSON)
- Attachments: whatever folder the user chose in Obsidian settings

---

## Where to write files

All notes go **directly in the vault root** — do not create subdirectories.

Write files like: `{workspace}/2026-04-19-project-idea.md`

File naming: `YYYY-MM-DD-short-slug.md` — always use the real date, never a placeholder.

---

## When to write a vault note

**Always write when:**
- User says "remember", "note this", "save that", "don't forget", or similar
- User shares a meaningful idea, plan, goal, or vision
- User expresses a strong feeling, value, or opinion worth keeping
- User reveals something significant about their life, work, or identity
- A clear preference or habit emerges

**Never write when:**
- Routine questions or small talk
- One-off technical lookups
- Anything the user would find annoying to find in their vault later

---

## Note format

Every vault note should use this frontmatter:

```markdown
---
date: YYYY-MM-DD
tags: [memory, idea, feeling, preference, goal, project, ...]
source: conversation
---

# Clear title

Well-written content — distilled, not a transcript dump.
Write as if the user will read this back in six months and want it to make sense.
```

---

## How to write the file

Use `vault_write` — you only need to supply `title`, `content`, and optional `tags`:

```
vault_write(
  title="Favourite colour",
  content="User's favourite colour is blue.",
  tags="preference,user"
)
```

The tool automatically generates the filename (`YYYY-MM-DD-slug.md`), writes the frontmatter, and saves to the vault root. Do NOT use `write_file` for vault notes.

After the tool returns, tell the user what was saved — one short line.

---

## Reading vault notes

Use `read_file` to read any `.md` in the workspace.
Use shell search to find notes across the vault:

```bash
grep -r "search term" /path/to/vault --include="*.md" -l
```

---

## Relationship to NNN

| | Vault | NNN |
|---|---|---|
| **Storage** | Explicit written notes (Markdown files) | Abstract vector bundles |
| **Written by** | Intentionally, when something matters | Automatically from experience |
| **Readable by** | Anyone — plain Markdown in Obsidian | Only via `sera visualise` |
| **Best for** | Specific facts, ideas, feelings, preferences | Patterns, generalisations, semantic connections |

Use both. Write explicit things to the vault. Let NNN build its own understanding from repeated experience.
