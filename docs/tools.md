# Tool reference

<sub>[← back to the README](../README.md) · [all docs](README.md)</sub>

All 27 tools, their arguments and defaults.

## Tools

### 🛰️ Antigravity

| Tool | Purpose |
|---|---|
| `antigravity_ask(prompt, workspace?, model?, timeout_s?=180, watch?=false, plan?=false, schema?)` | Start a **new** Antigravity conversation. `model` selects the model (agy's `--model`, e.g. `"claude-sonnet-4-6"`); validated against `agy models`, defaults to your `settings.json` model. `watch=true` opens the live browser view ([Watch mode](watch-and-swarm.md#watch-mode)). `plan=true` runs agy in **plan mode** — it reads and writes a plan, but does not edit files or run commands ([Security](security.md#security); agy 1.1.12+). `schema` (a JSON Schema) returns the **validated object** as JSON text instead of prose — read the caveat in [Status & caveats](status.md#status--caveats) before using it for a judgment (agy 1.1.8+). |
| `antigravity_continue(prompt, workspace?, model?, timeout_s?=180, watch?=false, plan?=false, schema?)` | Continue the conversation **rooted at `workspace`** (pinned by id). agy's model is per-invocation, so `model` can differ from the original ask — and so are `plan` and `schema`, so a follow-up can be restricted, or shaped, even if the original ask was not. `watch=true` opens the live view. |
| `antigravity_image(prompt, output_path?, workspace?, timeout_s?=240, watch?=false)` | Generate an image; saves the file (extension corrected to the real bytes) and returns its path + format/size. `watch=true` streams progress and **shows the image** inline. |
| `antigravity_image_swarm(prompts, output_paths?, workspaces?, max_concurrency?=4, timeout_s?=240, watch?=false)` | Generate **several images in parallel** (one worker per prompt). |
| `antigravity_status()` | Setup diagnostics: **the bridge's own version + whether a newer release is available**, **remaining AI Pro quota per model family** (agy 1.1.11+), plus agy version/compat, state dirs, and newest-transcript readability. Spends no quota. |

### 🤖 Codex

| Tool | Purpose |
|---|---|
| `codex_ask(prompt, workspace?, sandbox?="read-only", model?, timeout_s?=180, watch?=false)` | Start a **new** Codex session. `sandbox` is a **real** boundary (see [Codex bridge](backends.md#codex-bridge)); `model` selects the model (`-m`). `watch=true` opens the live view, streaming codex's steps from its `--json` event stream. |
| `codex_continue(prompt, workspace?, timeout_s?=180, watch?=false)` | Continue the Codex session **rooted at `workspace`** — resumes the exact session id, falling back to the newest on-disk session for that cwd after a server restart. The resumed session keeps its original sandbox and model. `watch=true` opens the live view. |
| `codex_status()` | Setup diagnostics: codex version, login status (`codex login status`), sessions dir. Spends no quota. |

### 🐙 Copilot

| Tool | Purpose |
|---|---|
| `copilot_ask(prompt, workspace?, sandbox?="read-only", model?, timeout_s?=180, watch?=false)` | Start a **new** Copilot session. `sandbox` maps to copilot's tool/path permissions (**best-effort**, not an OS sandbox — see [Copilot bridge](backends.md#copilot-bridge)); `model` selects the model (`--model`). `watch=true` opens the live view, streaming copilot's steps from its `--output-format json` event stream. |
| `copilot_continue(prompt, workspace?, sandbox?="read-only", timeout_s?=180, watch?=false)` | Continue the Copilot session **rooted at `workspace`** — resumes the exact self-set session id, falling back to the newest on-disk session for that cwd after a restart. Unlike Codex, `sandbox` applies here too (copilot re-applies permissions each turn). `watch=true` opens the live view. |
| `copilot_status()` | Setup diagnostics: copilot version, an auth hint (no `login status` command exists, so best-effort), session-state dir. Spends no quota. |

### ✳️ Cursor

| Tool | Purpose |
|---|---|
| `cursor_ask(prompt, workspace?, sandbox?="read-only", model?, timeout_s?=180, watch?=false)` | Start a **new** Cursor chat. `sandbox` maps to cursor's mode/force flags (**agent-enforced**, not an OS sandbox — see [Cursor bridge](backends.md#cursor-bridge)); `model` selects the model (`--model`, validated against `cursor-agent models`). `watch=true` opens the live view, streaming cursor's steps from its `--output-format stream-json` event stream. |
| `cursor_continue(prompt, workspace?, sandbox?="read-only", timeout_s?=180, watch?=false)` | Continue the Cursor chat **rooted at `workspace`** — resumes the exact chat id the bridge minted (`create-chat` + `--resume`), falling back to the newest on-disk chat for that cwd after a restart. `watch=true` opens the live view. |
| `cursor_status()` | Setup diagnostics: **the bridge's own version + whether a newer release is available**, plus cursor version and login status (`cursor-agent status`). Spends no quota. |

### 🧩 opencode

| Tool | Purpose |
|---|---|
| `opencode_ask(prompt, workspace?, sandbox?="read-only", model?, timeout_s?=300, watch?=false)` | Start a **new** opencode session. `sandbox` maps to an `OPENCODE_PERMISSION` policy (**agent-enforced**, but the same on every OS — see [opencode bridge](backends.md#opencode-bridge)); `model` selects the model (`-m`, `"provider/model"`, validated against `opencode models`). `watch=true` opens the live view, streaming opencode's steps from its `--format json` event stream. Note the **300 s** default timeout — the free models are slow. |
| `opencode_continue(prompt, workspace?, sandbox?="read-only", timeout_s?=300, watch?=false)` | Continue the opencode session **rooted at `workspace`** — resumes the exact session id (`-s`), falling back to opencode's own "most recent session for this directory" (`-c`) after a restart. Sessions really are per directory, so pass the same `workspace` you asked in. `sandbox` applies here too. `watch=true` opens the live view. |
| `opencode_status()` | Setup diagnostics: **the bridge's own version + whether a newer release is available**, plus opencode version, how many provider credentials are configured (0 is fine — the free models still answer), the model list, and the data dir. Spends no quota. |

### 🧪 Grok Build *(experimental — [unverified](backends.md#experimental-backends))*

| Tool | Purpose |
|---|---|
| `grok_ask(prompt, workspace?, sandbox?="read-only", model?, timeout_s?=180, watch?=false)` | Start a **new** Grok session. `sandbox` maps to grok's `--sandbox` profile plus a tool allowlist — a **real OS boundary on Linux/macOS only** (see [Grok bridge](backends.md#grok-bridge)); `model` selects the model (`-m`, validated against `grok models`). `watch=true` opens the live view, streaming grok's steps from its `--output-format streaming-json` event stream. |
| `grok_continue(prompt, workspace?, sandbox?="read-only", timeout_s?=180, watch?=false)` | Continue the Grok session **rooted at `workspace`** — resumes the exact session id grok returned (`-r`), falling back to grok's own "most recent session for this cwd" (`-c`) after a restart. `sandbox` applies here too. `watch=true` opens the live view. |
| `grok_status()` | Setup diagnostics: **the bridge's own version + whether a newer release is available**, plus grok version, auth state, and the model list — the last two both from `grok models`, which answers even while logged out. Spends no quota. |

### 🌙 Kimi Code *(experimental — [unverified](backends.md#experimental-backends))*

| Tool | Purpose |
|---|---|
| `kimi_ask(prompt, workspace?, model?, timeout_s?=180)` | Start a **new** Kimi session. **No `sandbox` argument** — Kimi print mode has no sandbox and auto-executes every tool. `model` is a lenient pass-through (`-m`, an alias from your `config.toml`); Kimi has no model list to validate against. No watch mode. |
| `kimi_continue(prompt, workspace?, timeout_s?=180)` | Continue the Kimi session **rooted at `workspace`** (`-c`). Kimi scopes sessions per working directory, so there's no id to track — and no restart problem either. |
| `kimi_status()` | Setup diagnostics: bridge version + update check, kimi version, whether a provider is configured (`kimi provider list` — the auth proxy), and the data dir. Spends no quota. |

### 🎼 Muse Code *(experimental — [real model unverified](backends.md#experimental-backends))*

| Tool | Purpose |
|---|---|
| `muse_ask(prompt, workspace?, sandbox?="read-only", model?, timeout_s?=180, watch?=false)` | Start a **new** Muse session (`muse exec --json`). The prompt travels in a file (`--prompt-file`), never argv. `sandbox`: `read-only` switches muse's write, shell and web tools off — it can only read and answer, on every OS; `workspace-write` lets the shell run inside muse's OS sandbox; `danger-full-access` is `--yolo` (see [Muse bridge](backends.md#muse-bridge)). `model` is a lenient pass-through (`--model`) — muse accepts any id. `watch=true` opens the live view, rendering muse's task events. |
| `muse_continue(prompt, workspace?, sandbox?="read-only", timeout_s?=180, watch?=false)` | Continue the Muse session **rooted at `workspace`** — the bridge names each session itself (`--session-id`), and after a server restart recovers the workspace's most recent session with `muse export --last`. With no session there it errors instead of silently starting fresh. `sandbox` applies here too. |
| `muse_status()` | Setup diagnostics: bridge version + update check, which muse binary the bridge runs, whether credentials exist (`META_API_KEY` or a `muse login`), any cached model catalog, the Windows OS sandbox state, and the data dir. Spends no quota. |

### 🐝 Shared

| Tool | Purpose |
|---|---|
| `agent_swarm(tasks, max_concurrency?=4, timeout_s?=180, watch?=false)` | Run **several tasks in parallel across seven backends** — each task names its `backend` (`antigravity`, `codex`, `copilot`, `cursor`, `opencode`, `grok`, or `muse`) plus a `prompt` (an optional `model` and `sandbox` for any backend — on Antigravity `sandbox: "read-only"` means **plan mode**). Every answer comes back in one block; `watch=true` opens the live dashboard ([Swarm](watch-and-swarm.md#swarm)). Kimi is not available here — see [Experimental backends](backends.md#experimental-backends). |

`workspace` defaults to the MCP server's current working directory. Point it at a real project dir
for context-aware answers — every backend gives the model access to files under that root (Codex,
Copilot, Cursor, and opencode honoring their `sandbox`).

**`sandbox` now applies to Antigravity too.** It used to be silently ignored there, so an agy task
written as `{"backend": "agy", "sandbox": "read-only"}` ran completely unrestricted while reading as
though it were fenced. `"read-only"` maps to agy's plan mode, `"danger-full-access"` says plainly
that the worker is unrestricted, and `"workspace-write"` is **refused** — agy has no write scoping to
offer, and accepting it would promise a fence that doesn't exist. **Omitting `sandbox` leaves an
Antigravity worker unrestricted**, unlike every other backend, whose default is `read-only`: that
long-standing default is left alone so existing file-writing swarms keep working, so fence agy
explicitly when you want it fenced.

`antigravity_image` forces agy to save to an explicit absolute path — without one, agy
falls back to its own scratch dir (`~/.gemini/antigravity-cli/scratch/`). It then
corrects the file extension to match the real bytes: agy's image model picks the
format itself (JPEG for photo-like images, PNG for flat graphics), so a requested
`out.png` may come back as `out.jpg`. The returned path always reflects the true
format.
