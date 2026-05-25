# Getting Started with Serenity

> **Serenity** — a personal AI agent with long-term vector memory (NNN), an Obsidian-powered journal, and a setup wizard.
> **Serenity** is an open-source local AI agent built by Sera-Team.

---

## 1. Prerequisites

Before you install Serenity, make sure you have:

- **Python 3.11 or 3.12** — check with `python --version`
- **pip** — bundled with modern Python
- **Git** — for cloning / pulling updates
- **~3 GB free disk** — the LLM embedding model + PyTorch are large
- **One of**:
  - An API key for OpenRouter / Anthropic / OpenAI / DeepSeek, **OR**
  - [Ollama](https://ollama.com/) installed locally for free local models

Optional but recommended:

- **[Obsidian](https://obsidian.md/)** — Serenity can use your vault as her long-term journal
- **Node.js ≥ 18** — only needed if you plan to use the ClawHub bridge for Telegram/WhatsApp

---

## 2. Install

### Option A — One-command installer (recommended)

**Windows:**
```bat
install.bat
```

**Linux / macOS:**
```bash
bash install.sh
```

Both scripts check your Python version, install all dependencies, and launch the setup wizard automatically.

---

### Option B — Manual install

Clone the repo and install in editable mode:

```bash
git clone <your-fork-url> serenity
cd serenity
pip install -e .
```

> The first install pulls PyTorch, sentence-transformers, and chromadb. Expect **5–15 minutes** and ~2.5 GB of downloads. This only happens once.

When it finishes you will have two commands on your PATH:

| Command | What it does |
|---|---|
| `serenity` | Setup wizard on first run; logo + gateway on subsequent runs |
| `sera` | The full Serenity CLI (all subcommands) |

Verify:

```bash
sera --version
```

You should see: `✿ Serenity v0.1.5.post1`

---

## 3. First Run — the Setup Wizard

Run:

```bash
serenity
```

The wizard walks through **7 steps**:

| Step | What you configure |
|---|---|
| **1 — LLM Provider** | Ollama, OpenRouter, Anthropic, OpenAI, or DeepSeek |
| **2 — Model** | Accept the recommended model or enter your own |
| **3 — API Key** | Paste from your provider (skipped for Ollama) |
| **4 — Persona** | Name your agent and give her a one-line personality |
| **5 — Messaging Channel** | Telegram, Discord, WhatsApp, or skip (CLI only) |
| **6 — Memory & Workspace** | Toggle NNN, set consolidation interval, pick Obsidian / custom / default workspace |
| **7 — Save** | Writes `~/.serenity/config.json`, seeds workspace files, launches gateway |

Once complete, Serenity automatically starts the gateway. You are live.

---

## 4. Talking to Sera

Once the gateway is running, open a **second terminal** and start an interactive chat:

```bash
sera agent
```

You'll see her prompt. Type anything. Sera will:

- Check her long-term memory (**NNN**) before answering if the topic is familiar
- Store distilled understanding back into NNN when she learns something worth keeping
- Write to your Obsidian vault (if configured) as her explicit journal

To send a one-shot message without an interactive session:

```bash
sera agent -m "What did we talk about last week?"
```

Exit interactive mode with `exit`, `quit`, `:q`, or **Ctrl+C**.

---

## 5. Useful Commands

```bash
serenity              # Wizard on first run / logo + gateway on return runs
sera gateway          # Start the gateway (channels, cron, heartbeat, NNN scheduler)
sera agent            # Interactive chat with Sera
sera agent -m "…"     # One-shot message
sera serve            # Start OpenAI-compatible API server
sera status           # Show config, workspace, API keys
sera visualise        # Open Embedding Atlas to explore NNN vector space
sera --help           # Full command list
```

---

## 6. Where Everything Lives

```
~/.serenity/
├── config.json              ← Your configuration (API keys, model, channels)
├── workspace/               ← Sera's notes and memory (if no Obsidian vault)
│   ├── SOUL.md              ← Her identity and persona
│   ├── HEARTBEAT.md         ← Proactive loop instructions
│   ├── AGENTS.md
│   └── memory/
│       └── MEMORY.md        ← Explicit long-term memory
├── serenity_nnn_data/       ← NNN vector database (ChromaDB)
└── atlas_export/            ← Embedding Atlas visualiser exports
```

If you pointed the wizard at an Obsidian vault, `workspace/` becomes that vault instead — she reads and writes notes directly in Obsidian.

---

## 7. Visualising Her Memory

After using Serenity for a while, explore the vector space she has built:

```bash
sera visualise
```

This exports all NNN bundles to `~/.serenity/atlas_export/embeddings.json` and launches **Embedding Atlas** in your browser. Each dot is a memory bundle. Clusters that form are her emerging abstractions — colour-coded by type (episodic, abstract, world_model, relational).

> First launch installs `embedding-atlas` automatically if it isn't present.

---

## 8. Troubleshooting

**`serenity` or `sera` command not found**
`pip install -e .` didn't complete, or Python's Scripts folder is not on your PATH. Re-run the installer script, or add the Scripts folder manually.

**UnicodeEncodeError on Windows**
Use **Windows Terminal** (not the classic `cmd.exe`). If you must use cmd, run:
```bat
chcp 65001
set PYTHONIOENCODING=utf-8
serenity
```

**NNN tools don't appear**
They register silently if `serenity_nnn` is not importable. Check with:
```bash
python -c "import serenity_nnn; print('ok')"
```

**Ollama isn't responding**
Make sure it's running: `ollama serve`. And that you've pulled a model: `ollama pull qwen2.5:7b`.

**I want to re-run the wizard**
Delete (or rename) `~/.serenity/config.json` and run `serenity` again.

---

## 9. Next Steps

- **Connect Telegram**: Create a bot with [@BotFather](https://t.me/BotFather), run the wizard and paste the token in step 5.
- **Point at your Obsidian vault**: Re-run the wizard and choose **Obsidian** in step 6. Sera will write her journal as real `.md` files you can open in Obsidian graph view.
- **Read the NNN skill**: `serenity/skills/Neuro Node Network/SKILL.md` — explains how NNN stores, clusters, and abstracts memory.
- **Read the Obsidian skill**: `serenity/skills/Obsidian/SKILL.md` — how Sera uses the vault.

---

✿ Welcome to Serenity. — Sera-Team
