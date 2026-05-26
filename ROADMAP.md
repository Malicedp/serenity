# Serenity — Roadmap

This is a living document. Items move between phases as priorities shift.
If something here matters to you, open an issue and say so — that's how priorities get reordered.

---

## ✅ Shipped — v1.0

- [x] NNN (Neural Node Network) — vector memory with ChromaDB, persists across all sessions
- [x] Cross-domain reasoning loop — one agent, every domain
- [x] Fully local inference via Ollama — no API key required
- [x] Voice — wake-word detection, Whisper STT, edge-tts / ElevenLabs / Qwen TTS
- [x] Vision — screen capture + camera via MiniCPM-V 4.6
- [x] Telegram channel integration
- [x] Obsidian vault read/write
- [x] Autonomous cron scheduling with natural language
- [x] Drop-in skill system — `.py` file in `skills/` and it's live
- [x] MCP server — Claude Code connects to Serenity's memory
- [x] Emotional dynamics engine — energy, curiosity, boredom, focus, social drive
- [x] Dialect / code-switching — auto-detects AAVE, UK road, Gen-Z, casual, tech
- [x] NNN memory atlas visualiser — `sera visualise`
- [x] Animated first-run setup wizard
- [x] Dream system — background memory consolidation every 2 hours
- [x] Research paper — [Serenity S.E.R.A on Zenodo](https://doi.org/10.5281/zenodo.20382162)

---

## 🔨 In progress — v1.1

- [ ] **WhatsApp full support** — currently in bridge, full integration coming
- [ ] **Linux / macOS compiled NNN** — `.pyd` currently Windows / Python 3.11 only; native builds for Linux (`.so`) and macOS (`.dylib`) in development
- [ ] **Compiled licence module for Linux / macOS** — same cross-platform work as NNN
- [ ] **Skills marketplace** — community-submitted skills with a one-command install (`sera skill install <name>`)
- [ ] **Improved memory consolidation** — Dream runs smarter deduplication and contradiction resolution
- [ ] **Per-channel personality profiles** — different tone defaults for Telegram vs terminal vs voice

---

## 🗺️ Planned — v1.2

- [ ] **Multi-agent mode** — spawn sub-agents for parallel long-running tasks, coordinated by a planner agent
- [ ] **Discord channel integration** — same Serenity, Discord interface
- [ ] **Fine-tuning pipeline** — export your NNN memory as a LoRA training dataset, personalise a base model on your own history
- [ ] **Mobile companion app** — lightweight iOS / Android interface talking to your local Serenity instance over your home network
- [ ] **Proactive memory** — Serenity surfaces relevant memories unprompted when she detects you're about to need them
- [ ] **Skill chaining** — skills that call other skills, enabling complex multi-step automations without writing code
- [ ] **Web dashboard** — browser-based UI alternative to terminal + Telegram

---

## 💡 Exploring — future

- [ ] **Multi-user support** — separate memory spaces per family member or teammate, shared agent
- [ ] **Model fine-tuning from conversation history** — automatic LoRA generation from Dream-processed sessions
- [ ] **Serenity-to-Serenity communication** — two Serenity instances exchanging memory bundles (for shared households or teams)
- [ ] **On-device speech model** — replace Whisper + Ollama TTS with a single end-to-end local voice model
- [ ] **Minecraft / game integration** — already partially built; full bidirectional game-state awareness
- [ ] **Browser extension** — Serenity sees what you're reading and can annotate, summarise, or save to vault without leaving the page

---

## How priorities are set

1. Things that are broken or blocked for a large number of users come first
2. Cross-platform support (Linux / macOS) comes before new features
3. Community-requested features get weighted higher when multiple people ask

Open an issue tagged `roadmap` to discuss or vote on anything here.

---

*Last updated: May 2026 · [Serenity v1.0](https://github.com/Malicedp/serenity)*
