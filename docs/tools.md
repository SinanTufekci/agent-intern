# Tool reference

<sub>[← back to the README](../README.md) · [all docs](README.md)</sub>

All 32 tools (29 inside Claude Code, which hides the three Claude Code tools), their arguments and defaults.

## Tools

### 🛰️ Antigravity

| Tool | Purpose |
|---|---|
| `antigravity_ask(prompt, workspace?, model?, timeout_s?=180, watch?=false, plan?=false, schema?)` | Start a **new** Antigravity conversation. `model` selects the model (agy's `--model`, e.g. `"gemini-4-argon-high"`); validated against `agy models`, defaults to your `settings.json` model. `watch=true` opens the live browser view ([Watch mode](watch-and-swarm.md#watch-mode)). `plan=true` runs agy in **plan mode** — it reads and writes a plan, but does not edit files or run commands ([Security](security.md#security); agy 1.1.12+). `schema` (a JSON Schema) returns the **validated object** as JSON text instead of prose — read the caveat in [Status & caveats](status.md#status--caveats) before using it for a judgment (agy 1.1.8+). |
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
| `opencode_ask(prompt, workspace?, sandbox?="read-only", model?, timeout_s?=300, watch?=false)` | Start a **new** opencode session. `sandbox` maps to a permission policy (**agent-enforced**, but the same on every OS — see [opencode bridge](backends.md#opencode-bridge); on opencode 2.x `workspace-write` keeps file edits inside the workspace (verified) but the shell can write **anywhere** and read `.env` files, see [Security](security.md#opencode--agent-enforced-but-the-same-everywhere)); `model` selects the model (`-m`, `"provider/model"`, validated against `opencode models`). `watch=true` opens the live view, streaming opencode's steps from its `--format json` event stream. Note the **300 s** default timeout, longer than the other backends' 180 s. |
| `opencode_continue(prompt, workspace?, sandbox?="read-only", timeout_s?=300, watch?=false)` | Continue the opencode session **rooted at `workspace`** — resumes the exact session id (`-s`), falling back to opencode's own "most recent session for this directory" (`-c`) after a restart. Sessions really are per directory, so pass the same `workspace` you asked in. `sandbox` applies here too. `watch=true` opens the live view. |
| `opencode_status()` | Setup diagnostics: **the bridge's own version + whether a newer release is available**, plus opencode version, how many provider credentials are configured (0 is fine — the free models still answer; on 2.x this comes from `opencode auth list`), the model list, and the data dir. Spends no quota. |

### 🔶 Claude Code *(for hosts other than Claude Code)*

These let a host that isn't Claude Code (Antigravity, Codex, Copilot, Cursor, opencode, …) delegate
to Claude Code, on your Claude plan (a `claude auth login`) or an `ANTHROPIC_API_KEY`. **Inside Claude
Code they are hidden automatically**: the server sees `CLAUDECODE=1`, which Claude Code sets for its
MCP servers, since delegating to yourself spends the same quota twice. Name `claude` in
`AGENT_INTERN_BACKENDS` to turn them on anyway; the `backends` row of any `*_status` tool says which
case you are in. Verified both ways: hidden in Claude Code (29 tools), present in the Antigravity CLI
(32 tools, and `claude_ask` answered). `claude_status` also loaded and answered in Copilot CLI,
Cursor CLI and opencode.

| Tool | Purpose |
|---|---|
| `claude_ask(prompt, workspace?, sandbox?="read-only", model?, timeout_s?=180, watch?=false)` | Start a **new** Claude Code session (`claude -p`). `sandbox` is a permission policy **enforced by Claude Code itself, not an OS boundary for file edits** (see [Claude Code](security.md#claude-code--agent-enforced-no-shell-in-workspace-write)): `read-only` reads and searches files, with no Write, Edit or Bash; `workspace-write` confines file edits to the workspace and runs **no shell**, so it can't run tests or builds; `danger-full-access` skips every permission check (**not verified live**). `model` is an alias (`haiku`, `sonnet`, `opus`, `fable`) or a full model id; an unknown one fails fast with Claude Code's own message. `watch=true` opens the live view. |
| `claude_continue(prompt, workspace?, sandbox?="read-only", timeout_s?=180, watch?=false)` | Continue the Claude Code session **rooted at `workspace`** (the one the last `claude_ask` there started; after a server restart, the newest Claude Code session in that workspace). `sandbox` applies to this turn, so you can analyse read-only and then continue with `workspace-write` to apply the fix. |
| `claude_status()` | Setup diagnostics: **the bridge's own version + whether a newer release is available**, the claude version (2.1.259 or newer is needed) and how you're signed in (method, plan, login expiry; never your e-mail or organisation), and the `backends` row. Spends no quota. There is no usage or limits row, since no free way to read it exists: a run that hits the plan's limit says so in its error. |

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
| `agent_swarm(tasks, max_concurrency?=4, timeout_s?=180, watch?=false)` | Run **several tasks in parallel across eight backends** — each task names its `backend` (`antigravity`, `codex`, `copilot`, `cursor`, `opencode`, `claude`, `grok`, or `muse`; `claude` also answers to `claude-code` and `anthropic`, and is off inside Claude Code unless `AGENT_INTERN_BACKENDS` names it) plus a `prompt` (an optional `model` and `sandbox` for any backend — on Antigravity `sandbox: "read-only"` means **plan mode**). Every answer comes back in one block; `watch=true` opens the live dashboard ([Swarm](watch-and-swarm.md#swarm)). Kimi is not available here — see [Experimental backends](backends.md#experimental-backends). |
| `preset_swarm(preset, material, workspace?, timeout_s?, max_concurrency?=4, watch?=false)` | Run a **predefined swarm**: a named panel of agents from different model families, each in its own role, on the same `material`. Built in: **`jury`** (independent jurors score against a rubric; returns a score table with each criterion's mean and spread and flags disagreements), **`research`** (one topic from four angles, with sources), **`red-team`** (attack a plan from three sides), **`council`** (independent code review). Members run read-only unless the preset says otherwise. Add your own as JSON files ([Preset swarms](watch-and-swarm.md#presets)). |
| `swarm_presets(name?, workspace?)` | List every preset (built-in, yours and the project's) with its members and rubric, and any file that was skipped and why. With `name`, returns that preset as JSON to save and edit. Runs nothing. |

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

## Details the tool descriptions leave out

The text a host's model reads for each tool is kept short on purpose: a host may cut long descriptions
(Claude Code cuts each at 4,096 characters), and a parameter note is no use if it never arrives. The
measurements and fine print behind a few parameters live here instead.

### `antigravity_ask`: `schema`

`schema` (a JSON Schema, agy 1.1.8+) returns the **validated object** as JSON text instead of prose.
Use it for **shape**, not for judgment. Measured on agy 1.1.20 with the sentence *"this broke my
build…"*: a schema with the enum `["positive","negative"]` answered "positive" in 3 of 4 runs; the
reversed enum `["negative","positive"]` flipped it to "negative" in 2 of 2, so the order of the
options was steering the answer; adding a `reason` field didn't help; stating the verdict in the
prompt itself was right in 3 of 3. If the value matters, put the judgment in the prompt and use the
schema only to get a parseable shape. The bridge reads agy's `structured_output`, which is **not
interchangeable** with the prose `response`: the prose carries `toolAction` / `toolSummary` keys and
can be prefixed. See [Status & caveats](status.md#status--caveats).

### `antigravity_ask`: `plan`

`plan=true` runs agy in plan mode. Verified on agy 1.1.20: file writes and shell commands are
diverted into a plan document under agy's own directory, even with `--dangerously-skip-permissions`
(which the bridge still passes). Plan mode is **exclusive with the slash-command shield**: agy silently
disables plan mode when `--disable-slash-commands` is present, so the bridge drops that flag and
rejects a prompt whose first token is a slash command instead. Plan mode is agent-enforced like
Copilot's and Cursor's modes, not an OS boundary ([Security](security.md#security)).

### `antigravity_ask`: `model`

agy renames and drops models often, with no changelog entry. `antigravity_status` and `agy models` are
the live answer; the bridge validates your slug against `agy models` and rejects a typo up front. See
[the FAQ](faq.md) for the history.

### `codex_ask`: `sandbox` on Windows

On Windows, `sandbox="workspace-write"` runs **read-only** unless codex has a Windows sandbox backend:
set `sandbox = "unelevated"` (or `"elevated"` or `"mxc"`) under `[windows]` in `~/.codex/config.toml`.
Separately, on codex 0.149.1 through 0.162.1 a sandboxed run refused **every** command, reads
included, because codex's policy engine can't classify the `pwsh -Command` wrapper codex itself
builds. Shell commands are how codex reads files, so it then answers from its own knowledge or a web
search: the answer looks confident and has no source. In both cases the bridge appends an
`[agent-intern] WARNING` to the answer. Treat a sandboxed Codex answer on Windows as unsourced when
that warning is present. The two problems may share a cause; see
[Security](security.md#codex--a-real-sandbox-you-should-use).

### `agent_swarm`: `tasks`

Each task is `{backend, prompt, workspace?, model?, sandbox?}`. The `backend` name is forgiving:

| Backend | Also accepted as |
|---|---|
| `antigravity` | `agy`, `gemini` |
| `codex` | `openai` |
| `copilot` | `github`, `gh` |
| `cursor` | — |
| `claude` | `claude-code`, `anthropic` |
| `grok` | `grok-build`, `xai` |
| `opencode` | `oc`, `sst` |
| `muse` | `muse-code`, `meta` |

What `sandbox` means depends on the backend, and it defaults to `read-only` everywhere except
Antigravity:

- **Codex:** a real OS sandbox on every OS (mind the Windows caveat above).
- **Grok:** an OS profile on Linux and macOS only; a tool allowlist elsewhere.
- **Copilot, Cursor, Claude Code and opencode:** enforced inside the agent, not by the OS.
- **Muse:** `read-only` switches its write, shell and web tools off.
- **Antigravity:** `"read-only"` is **plan mode**, `"workspace-write"` is **refused**, and **omitting
  `sandbox` leaves the worker unrestricted** (see the note above this section).

Kimi is not a swarm backend.
