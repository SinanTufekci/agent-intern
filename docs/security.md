# Security

<sub>[← back to the README](../README.md) · [all docs](README.md)</sub>

What each backend's `sandbox` really enforces — read this before pointing a sub-agent at anything you don't trust.

<a id="security"></a>

## ⚠️ Security

All seven backends run the model as an **autonomous agent**. The difference is whether you get a real
boundary: Codex enforces one everywhere and Grok on Linux/macOS only; Copilot, Cursor and opencode
offer best-effort, agent-enforced ones (opencode's is the only one that behaves identically on every
platform); Antigravity and Kimi offer none.

### Antigravity — no usable boundary

`agy -p` executes its own tools — reading and writing files, running shell commands, reaching
the network — with **no approval gate**. Through agy 1.1.2 that was simply how print mode worked,
with no opt-out at all. As of **1.1.3** it is a choice the bridge makes: agy finally gates headless
tool calls, and the bridge deliberately opts out with `--dangerously-skip-permissions`, because a
gated `-p` can do no useful work (it soft-denies even a plain file read, and print mode has no way
to prompt). The posture below is therefore unchanged — assume every call runs arbitrary code with
your privileges. The **one** exception is the opt-in `plan=True` described in the last bullet; it is
a real restriction, but an agent-enforced one, so it does not change the default posture. Re-verified empirically on **agy 1.0.9 / Windows**, with the 1.1.3 amendment noted:

- Print mode runs out-of-workspace file writes and live network fetches **even without**
  `--dangerously-skip-permissions` — that flag was a **no-op** for `-p` through 1.1.2. As of 1.1.3
  it is **load-bearing**: without it every tool-using call is soft-denied, and the bridge now always
  passes it (it must precede `-p`, whose *value* is the prompt). There is still **no** agy flag that
  makes print mode both safe and useful.
- agy 1.0.5 integrated a permission system (its logs show `toolPermission=request-review`), but it
  **still does not gate print-mode execution** — a fresh `-p` run created a file outside the
  workspace with no prompt. agy 1.0.12 reshuffled how that permission config *merges* (per-project
  files under `~/.gemini/config/projects/` now take precedence over
  `~/.gemini/antigravity-cli/settings.json`), and 1.0.13 made "Always Approve" rule matching
  strict (non-regex) by default with a `regex:` opt-in and relaxed its redirection checks — but
  those are config/interactive-approval changes, they add no print-mode approval gate, and the
  bridge reads none of it.
- `--sandbox` is **not** a usable boundary. agy 1.0.6 fixed its propagation into `-p` (the 1.0.6/1.0.7
  changelog calls this "sandbox isolation correctly enforced") and it now **does** block terminal/
  shell command execution — but re-verified on 1.0.9 that it leaves the `write_to_file` tool and
  network **wide open**: under `--sandbox` the model still wrote a file *outside* its workspace. agy
  1.0.9 hardened the sandbox's *command* path (stricter exact-match command checks; `.git` added to
  its dangerous-paths list), but none of that closes the out-of-workspace `write_to_file` hole. On
  top of that, a `--sandbox` run whose blocked terminal command halts it writes **no JSONL
  transcript** (only the SQLite `.db`, re-confirmed on 1.0.9). The bridge can now read that `.db`,
  but still never passes `--sandbox` — it's no boundary, with file writes and network left open.
- ✅ **`plan=True` is the first Antigravity restriction that actually holds** — opt-in, per call, on
  `antigravity_ask` / `antigravity_continue`, and gated at agy **1.1.12** (older agy parses `--mode`
  and ignores it in print mode, so the bridge **refuses** rather than handing back an unrestricted
  run that reports success). It maps to agy's `--mode plan`: agy investigates and writes an
  implementation plan into its own directory instead of touching yours. Verified on 1.1.20 through
  the bridge's own code path, **with a control**: the identical prompt —
  `cmd /c echo SHELLRAN > <absolute path>` — **executed and created the file** on a normal call, and
  on `plan=True` created nothing at all, answering with a plan document. File reads still work, so
  it is genuinely useful rather than merely inert. Note what it is **not**: it constrains agy's agent
  loop, so it is agent-enforced like Copilot's and Cursor's modes, **not** an OS boundary — for that,
  use Codex (with the Windows caveat in [Security](#security) firmly in mind). Two consequences worth knowing: it survives `--dangerously-skip-permissions` (which the
  bridge still passes, because dropping it would soft-deny the reads plan mode exists to allow), and
  it is **mutually exclusive with the slash-command shield** — agy silently disables plan mode when
  `--disable-slash-commands` is present, so the bridge drops that flag and rejects a prompt whose
  first token is a slash command instead.

### Codex — a real sandbox you should use

`codex exec` also has **no interactive approval gate**, but its `sandbox` flag is a genuine boundary
that codex enforces:

- **`read-only`** (default) — reads and answers; writes nothing. Safe for untrusted *questions* on
  trusted content.
- **`workspace-write`** — may edit files under the workspace. Opt in deliberately, per task.
- **`danger-full-access`** — no sandbox at all. Avoid.

Because there's no approval prompt, the flag you pass **is** the safety decision — choose it per
call.

> ⚠️ **On Windows, as of codex 0.149.1, that boundary is currently too tight to be useful — and it
> fails silently.** Every command is refused under **both** `read-only` and `workspace-write` (down
> to `pwd`) with `rejected: blocked by policy`: codex's policy engine can't classify the
> `pwsh -Command <...>` wrapper codex itself builds. Shell commands are how codex reads files, so a
> sandboxed run sees **nothing** of your workspace — and says nothing about it. Asked for the version
> in a local `pyproject.toml` declaring `0.27.0`, it web-searched and answered **`1.2.0`** from an
> unrelated GitHub repo; with the sandbox off, `0.27.0`. Exit 0 both times. Known upstream
> ([#40060](https://github.com/openai/codex/issues/40060),
> [#38886](https://github.com/openai/codex/issues/38886)). The bridge can't fix it, but it no longer
> launders it: any answer whose run had commands refused comes back with a visible
> `[agent-intern] WARNING` naming the count. Until it's fixed upstream, treat a sandboxed codex
> answer on Windows as unsourced unless that warning is absent.

### Copilot — best-effort, not an OS sandbox

`copilot -p` runs headless with `--allow-all-tools` (required — otherwise it blocks on per-tool
permission prompts). Its `sandbox` maps to copilot's tool/path permission flags, which are a
**real-ish but not enforced** boundary:

- **`read-only`** (default) — auto-approves tools to run headless, then **denies** the local `write`
  and `shell` tools (`--deny-tool`). Blocks local file edits and command execution, but it is **not**
  an OS sandbox: other tools (including network/MCP) can still act. Weaker than Codex's `read-only`.
- **`workspace-write`** — writes allowed, but file access stays confined to the workspace (no
  `--allow-all-paths`).
- **`danger-full-access`** — `--allow-all` (tools + all paths + all URLs). Avoid.

For a **hard** read-only boundary, prefer `codex_ask`.

### Cursor — best-effort, agent-enforced

`cursor-agent -p` runs headless with `--trust` (and `--force` when writes are allowed). Its `sandbox`
maps to cursor's mode/force flags — an **agent-enforced**, not OS-level, boundary:

- **`read-only`** (default) — `--mode ask`: the local `write` and `shell` tools are **unavailable**,
  so cursor analyzes and answers but makes no edits (verified: it refuses to write files). Like
  Copilot, this is agent-enforced and **not** an OS sandbox. Weaker than Codex's `read-only`.
- **`workspace-write`** — `--force`: edits and commands allowed, file access rooted at `--workspace`.
- **`danger-full-access`** — `--force --sandbox disabled` (OS sandbox off). Avoid.

For a **hard** read-only boundary, prefer `codex_ask`.

### Grok — real, but only on Linux and macOS

`grok -p` runs headless with `--always-approve` (its headless mode does not auto-approve on its own,
and nothing is there to answer a prompt). Its `sandbox` maps to grok's OS profile **plus** a tool
allowlist:

- **`read-only`** (default) — `--sandbox read-only` + `--tools read_file,list_dir,grep,glob,web_search,web_fetch`
  + `--no-subagents`.
- **`workspace-write`** — `--sandbox workspace`: writes confined to the workspace, `~/.grok`, temp.
- **`danger-full-access`** — `--sandbox off`. Avoid.

The profile is enforced by **Landlock (Linux ≥ 5.13)** and **Seatbelt (macOS)**. On **Windows there is
no mechanism**, and per xAI's docs grok "logs a warning and continues without enforcement" — so on
Windows the only thing standing between `read-only` and your disk is the agent-enforced tool
allowlist. Treat Windows `read-only` as best-effort (Copilot/Cursor tier), not as a jail. The bridge
uses an allowlist rather than a denylist so that a future grok with a new write tool fails safe; note
that MCP meta-tools remain available under an allowlist regardless.

⚠️ This backend is [unverified](backends.md#experimental-backends) — including these sandbox claims, which could
not be exercised, because grok checks **auth before it validates `--sandbox`**.

### opencode — agent-enforced, but the same everywhere

`opencode run` maps its `sandbox` to an `OPENCODE_PERMISSION` policy. It is agent-enforced like
Copilot's and Cursor's, with two properties that make it the strongest of that tier:

- **A rule that would prompt is auto-rejected in headless mode**, so "ask" is effectively "deny" and
  no call can hang on an invisible approval dialog.
- **The policy is merged last**, overriding each agent's own rules — including opencode's `plan`
  agent, which despite the name leaves `bash` allowed.

Modes:

- **`read-only`** (default) — `edit`, `bash`, `task`, `external_directory`, `webfetch`, `websearch`
  denied; read/search tools kept; `.env` files denied.
- **`workspace-write`** — edits and shell inside the run directory; `external_directory` denied.
- **`danger-full-access`** — `--auto`, no policy. Avoid.

Unlike Grok's, none of this depends on the OS — the same policy applies on Windows, macOS and Linux.
Unlike Codex's, none of it is a kernel boundary: it is opencode refusing its own tool calls. A
malformed policy is silently ignored by opencode, so the bridge always serializes it with `json.dumps`.

### Kimi — no boundary at all

`kimi -p` has **no sandbox and no `sandbox` argument**. Print mode auto-executes every tool call with
no approval gate — the same posture as Antigravity, and verified live on 0.29.1 in the sense that `-p`
*rejects* `--auto`/`--yolo` precisely because it is already self-approving. No flag makes it safe.
Assume every `kimi_ask` runs arbitrary code with your privileges.

### Windows: batch-file shims and the prompt

On Windows several of these CLIs install as a `.cmd` shim (npm's `opencode.cmd`, the Cursor
installer's `cursor-agent.CMD`). Windows cannot run a `.cmd` directly — it hands the whole command line
to `cmd.exe`, which re-parses every argument with its own rules: `%var%` and `!var!` expansion, and
`&`, `|`, `<`, `>` as command separators. Python quotes arguments for the C runtime, not for `cmd.exe`,
so a prompt that closes a quote and continues with `& <command>` ran that command on the host —
**before the agent started, so no `sandbox` setting applied**. The prompt can carry text Claude read
from an untrusted file or page, which is what made this exploitable. Versions up to and including
**0.30.2** were affected for the cursor and opencode backends on Windows; it was verified live
against both real shims, and fixed in **0.30.3**:

- **The shims are bypassed.** opencode's npm shim is resolved to the native `opencode.exe` it wraps,
  and cursor is launched as the `node.exe index.js` its own launcher would pick. `cmd.exe` is no
  longer involved, so the prompt reaches the CLI verbatim.
- **Anything still left is refused, not run.** Every spawn goes through one check: if the target is
  a `.cmd`/`.bat` and an argument contains `" % ! ^ & | < >` or a newline, the call fails with an
  error naming the backend's `*_BIN` override instead of running. A test fails the build if any
  module spawns a process without that check.

If a backend reports that refusal, point its `*_BIN` variable at the real executable rather than the
shim.

### What that means for you

- The `workspace` argument is only a *starting context*, **not a security boundary** — Antigravity and
  Kimi can and do act outside it; Codex is bounded by its enforced `sandbox`; Grok by its OS profile
  on Linux/macOS and by a tool allowlist elsewhere; Copilot by its best-effort tool/path permissions;
  Cursor by its agent-enforced mode/force; opencode by a permission policy that denies leaving the
  run directory.
- An Antigravity or Kimi call effectively runs **arbitrary code with your user privileges**. A Copilot
  or Cursor call does too outside its best-effort denials; a Grok call does on Windows outside its
  allowlist; a Codex call does unless you keep it at `read-only`.
- Only invoke these with **trusted prompts on trusted content**. Untrusted input here is the classic
  prompt-injection *lethal trifecta*: private-data access + code execution + network egress.
- For real isolation, run the **whole bridge inside a container or VM**.

The bridge itself does only cross-platform filesystem reads under `~/.gemini/antigravity-cli/`,
`~/.codex/`, `~/.copilot/`, and `~/.cursor/` — no private APIs, no token theft. The risk above is
entirely in what the sub-agents are allowed to do.
