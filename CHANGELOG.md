# Changelog

All notable changes to Serenity are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.0.6] — 2026-05-27

### Added
- **"She doesn't wait to be asked" README section** — new prominent section documenting Serenity's autonomous capabilities: proactive reach-out, self-directed task execution, curiosity engine, dream cycle, and the full emotional system (energy / curiosity / boredom / social drive). These features existed in the codebase but were never surfaced in the README.
- **Capabilities table expanded** — added dedicated rows for proactive reach-out, self-directed tasks, curiosity engine, dream cycle, and emotional system so the feature set is immediately visible to anyone landing on the repo.

### Fixed
- **Heartbeat no longer opens a scratchpad** — every 30-minute background check was calling `scratchpad_read` then `scratchpad_write` before doing anything, burning 2 LLM turns on planning overhead for a simple task review. Prepended an explicit `BACKGROUND CHECK — do NOT open a scratchpad` guard to the heartbeat prompt. Clean background cycles now.

---

## [1.0.5] — 2026-05-27

### Fixed
- **Vault deduplication** — `vault_write` now scans existing notes for ≥60% title word overlap before creating a new file. If a similar note already exists, new content is appended under a dated section instead of creating a near-duplicate. Prevents the same memory being stored as 2–3 separate vault files across sessions.
- **NNN duplicate entries eliminated** — `SESSION_REFLECTION` Step 3 was re-encoding conversation vault writes that `_auto_nnn_encode_direct` already handled immediately post-turn. Reflection now only stores patterns and lessons that are genuinely novel to the review pass — same fact no longer lands in vector memory 2–3 times per session.
- **Context window now auto-tunes per model** — removed the hardcoded `contextWindowTokens` override that was bypassing the model-aware auto-detection. Serenity now sets context size based on the loaded model: ≤4b → 20k, ≤9b → 28k, ≤20b → 40k, cloud → 80k. No more OOM on small models, no more cramped context on larger ones.

---

## [1.0.4] — 2026-05-27

### Removed
- **Minecraft integration stripped out entirely** — `minecraft.py` (1,387 lines, 28 tool classes) deleted. Serenity is no longer bundled with the mineflayer bridge, all `minecraft_*` tools, and the Node.js bridge process. This makes the agent loop significantly lighter — fewer tools loaded, less RAM overhead, faster startup. Minecraft support may return as an optional skill install in a future release.

### Fixed
- `open_app` tool description rewritten to explicitly instruct the model to use it — not `web_fetch` — when the user says "open", "launch", "start", or "run" an app. Previously, "open Steam" caused Serenity to fetch `store.steampowered.com` instead of launching Steam locally.
- `filesystem.py` — file bytes were being read twice per request (once for MIME detection, once for content). Now reuses the first read.
- `nnn.py` — missing `serenity_nnn` module now raises a clean `RuntimeError` with install instructions instead of a raw `ImportError` inside a thread lock.
- Resolved all 20 ruff lint errors (`F401`/`F841`) that were blocking CI across 12 files — unused imports and dead variable assignments in `agent/`, `cli/`, `providers/`, `senses/`.
- `contextWindowTokens` corrected to `16,384` in default config (was reverting to `40,960` after gateway restart).

---

## [1.0.3] — 2026-05-26

### Added
- **Code-switching documentation** — full breakdown of dialect auto-detection in README: AAVE/ATL, UK road, Gen-Z, casual, tech registers
- **Claude + Serenity dual-agent section** in README — MCP connection guide, shared memory workflow, practical examples
- **Comparison table** — Serenity vs AutoGPT, Open Interpreter, MemGPT, Open WebUI
- **ROADMAP.md** — phased roadmap from v1.1 through future exploration
- **CHANGELOG.md** — this file

### Fixed
- `contextWindowTokens` reduced from 40,960 → 16,384 — prevents OOM on 16 GB systems while giving full response room
- `start.bat` timestamp comparison replaced with Python `os.path.getmtime()` — was locale-dependent, broke on non-English Windows
- `cli/commands.py` logger format: `"NNN scheduler tick failed: {}"` → `"NNN scheduler tick failed"` (stray format placeholder removed)
- `.gitignore` — added `.deps_installed` and `.serenity_gateway.pid` (runtime files were polluting repo state)

---

## [1.0.2] — 2026-05-25

### Fixed — Third audit (reliability)
- `serenity/licence.py` — `check_grace_period()` now returns `bool` correctly; was returning a truthy dict, breaking the offline grace period gate entirely
- `serenity_nnn/__init__.py` — added missing `get_state()`, `encode()`, `query()`, `simulate_plan()` stubs
- `serenity_nnn/nnn.py` — added missing `rewrite()`, `consolidate()`, `prune()`, `simulate_plan()` stubs
- `cli/commands.py` — `_check_licence()` now prompts interactively on first run instead of hard-quitting when no key is set (H3)
- `cli/commands.py` — `sera agent` now runs `_check_licence()` so NNN is authorised in terminal sessions (H4)

---

## [1.0.1] — 2026-05-25

### Fixed — Second audit (security + reliability)

#### Security
- `security/network.py` — added `::ffff:0:0/96` to blocked networks (IPv4-mapped IPv6 SSRF bypass closed)
- `security/network.py` — percent-encoded URLs now decoded before SSRF check (`http://127%2E0%2E0%2E1/` bypass closed)
- `security/network.py` — DNS failure now returns `False` (fail-safe) instead of allowing the request
- `security/network.py` — DNS lookup now has a 3-second timeout
- `channels/telegram.py` — user display names sanitised before system prompt injection (prompt injection via Telegram names closed)
- `agent/tools/shell.py` — `path_append` validated against character allowlist (shell metacharacter injection closed)
- `agent/tools/vault.py` — subfolder path now asserted to resolve inside workspace (path traversal closed)

#### Reliability
- `agent/tools/filesystem.py` — `MakeDirTool` now routes through `_resolve()` (workspace restriction was bypassable)
- `agent/tools/filesystem.py` — `_find_quote_matches` span uses `len(norm_old)` not `len(old_text)` (garbled edits on quote normalisation fixed)
- `agent/tools/filesystem.py` — `_MAX_EDIT_FILE_SIZE` lowered from 1 GiB → 50 MiB
- `bus/queue.py` — inbound/outbound queues bounded to `maxsize=500` (OOM under Telegram flood prevented)
- `bus/queue.py` — `task_done()` and `drain()` added for clean shutdown
- `agent/tools/goals.py` — `FileLock` wraps `_load` and `_save` (race condition corrupting `goals.json` fixed)
- `cron/service.py` — action log clear is now atomic via `.tmp` + `os.replace()`
- `cron/service.py` — full UUID4 for job IDs (was `[:8]`, collision-prone)
- `agent/tools/nnn.py` — thread-safe initialisation with double-check locking
- `agent/runner.py` — `if True:` replaced with `if _called_tool_names:` (unconditional 100-token overhead removed)
- `channels/telegram.py` — `tempfile.mktemp()` replaced with `NamedTemporaryFile` (deprecated API removed)
- `start.sh` — kill by PID file instead of `pkill -f` (was hitting other users' processes)
- `start.sh` / `start.bat` — skip `pip install` when `pyproject.toml` unchanged (saves 5–30s on every launch)

---

## [1.0.0] — 2026-05-25

### Added — First audit (platform + asyncio)
- `serenity/licence.py` — Python shim for Windows-only compiled `.pyd`; clear error on Linux/macOS instead of ImportError crash
- `serenity_nnn/__init__.py` — package shim with same platform guard
- `serenity_nnn/nnn.py` — full NNN shim with all public stubs
- 68 × `asyncio.get_event_loop()` → `get_running_loop()` across `tts.py`, `minecraft.py`, `mouse_keyboard.py`, `camera.py`, `ears.py`, `open_app.py`, `scratchpad.py`, `vault_image.py`, `transcription.py`, `manager.py`, `serenity_mcp.py`
- `agent/context.py` — NNN query timeout raised 1 s → 15 s (cold Ollama needs up to 90 s)
- `agent/memory.py` — JSONL cursor corruption fix for entries > 4096 bytes (full-scan fallback)
- `senses/daemon.py` — fresh `threading.Event` on each `start()` (singleton reuse bug fixed)
- `config/loader.py` — config saved after migration so it doesn't re-apply on every boot
- `licence_lemon.py` — `get_machine_id()` empty-string collapse fixed on minimal containers
- `pyproject.toml` — licence corrected MIT → CC-BY-NC-4.0, `requires-python` tightened to `~=3.11.0`

---

## [0.9.0] — 2026-05-20

### Added
- Compiled licence module (`licence.cp311-win_amd64.pyd`) — Lemon Squeezy validation, offline grace period, split-secret master key
- Research paper badge — [Serenity S.E.R.A on Zenodo](https://doi.org/10.5281/zenodo.20382162)
- Research paper badge — [Figshare](https://doi.org/10.6084/m9.figshare.32399520)

### Fixed
- Offline grace period messaging — clearer user-facing prompts
- Licence validation — config saved after gateway check, UUID regex tightened

---

## [0.1.0] — 2026-05-01

### Added — Initial release
- NNN (Neural Node Network) — vector memory with ChromaDB
- Agent reasoning loop with full tool registry
- Voice — wake-word, Whisper STT, multi-provider TTS
- Vision — MiniCPM-V 4.6 via Ollama
- Telegram integration
- Obsidian vault read/write
- Autonomous cron scheduling
- Drop-in skill system
- MCP server for Claude Code integration
- Emotional dynamics engine
- Dialect / code-switching auto-detection
- NNN memory atlas visualiser
- Dream — background memory consolidation
- Animated setup wizard
- `start.bat` / `start.sh` one-click launchers

---

*[Full commit history](https://github.com/Malicedp/serenity/commits/main)*
