---
name: gitnexus
description: "Code intelligence — analyse the codebase, map dependencies, and detect what will break before making changes. Use before any non-trivial edit."
metadata: {"serenity":{"emoji":"🔭","requires":{"bins":["gitnexus","npm"]},"install":[{"id":"npm","kind":"npm","package":"gitnexus","bins":["gitnexus"],"label":"Install GitNexus (npm install -g gitnexus)"}]}}
---

# GitNexus — Code Intelligence Skill

GitNexus indexes the entire codebase into a knowledge graph using AST parsing. It maps every function call, import, class relationship, and execution path. Use it to understand impact before touching code, and to find hidden dependencies that aren't obvious from reading a single file.

**This skill activates automatically on coding tasks.** Run the commands below via the shell tool.

---

## Before Editing — Check What Will Break

Always run this before a non-trivial change. It diffs the current git state and maps affected code paths:

```bash
gitnexus analyze .
```

Then check impact of a specific file or function:

```bash
# What imports or calls this file?
gitnexus analyze . --file serenity/agent/memory.py

# What breaks if this changes? (uses git diff)
gitnexus detect_changes
```

---

## Explore the Codebase

Find where a function is defined and what calls it:

```bash
# Search by symbol name
gitnexus analyze . --symbol micro_summarise

# View the dependency graph for a module
gitnexus analyze . --file serenity/providers/base.py
```

---

## Generate Documentation

Produce a wiki from the indexed graph:

```bash
gitnexus wiki
```

---

## Re-index After Large Changes

The index updates incrementally on each `analyze` call. After a major refactor, force a clean re-index:

```bash
gitnexus clean && gitnexus analyze .
```

---

## Key Rules

- **Run `gitnexus detect_changes` before committing** any edit that touches shared utilities, base classes, or provider interfaces — these have wide blast radius
- **Check imports first** — if you're adding a new dependency to a core file, verify nothing circular is introduced
- **Trust the graph over intuition** — a function that looks isolated often has 10 callers you haven't seen
- The `.gitnexus/` directory is the local index — never delete it manually; use `gitnexus clean` instead
