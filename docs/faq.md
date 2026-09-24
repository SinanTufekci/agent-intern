# FAQ

<sub>[← back to the README](../README.md) · [all docs](README.md)</sub>

## FAQ

<details>
<summary><b>Is this against Google's / OpenAI's / GitHub's / Cursor's Terms of Service?</b></summary>

It runs the **official `agy`, `codex`, `copilot`, and `cursor-agent` CLIs under your own logins** — no
private APIs, no token theft, no quota abuse. It just bridges what the CLIs already do. That said, your
AI Pro / Antigravity, OpenAI / Codex, GitHub Copilot, and Cursor ToS apply, and you're responsible for
staying within them.
</details>

<details>
<summary><b>Do I need all eight CLIs?</b></summary>

No. Each backend is independent — install only the CLI(s) you want. The tools for a missing backend
report "not found" via their `*_status` tool (`antigravity_status` / `codex_status` /
`copilot_status` / `cursor_status` / `opencode_status` / `grok_status` / `kimi_status` /
`muse_status`) and never
crash the server. If you hold no subscriptions at all, opencode is the one that still answers.
</details>

<details>
<summary><b>Claude never suggests delegating — can I make it offer on its own?</b></summary>

It does now, by default. The server ships an `instructions` block that Claude Code loads with the
tool list, and as of **0.29.0** that block tells it to **offer delegation in one line before
starting** a task that fits — bulk mechanical work, a job that splits into independent parallel
subtasks, or a second opinion on a risky diff — and to ask rather than just spend your quota.
It's also told to ask **once per task, not once per turn**, and to drop it if you decline, because a
suggestion on every task is nagging rather than help.

MCP instructions are guidance, though, not a rule the harness enforces — so if you want it *reliably*
proactive, put it where Claude Code treats it as an instruction. Add this to your project or global
`CLAUDE.md`:

```markdown
## Delegating to sub-agents

The `intern` MCP server bridges Antigravity/Gemini, Codex, Copilot and Cursor as sub-agents
running on my own subscriptions. Before starting bulk mechanical work (a rename across many
files, boilerplate, a first-pass port), or anything that splits into independent parallel
subtasks, propose delegating it in one line and wait for my answer — e.g. "this is 6
independent files, shall I farm it out to Gemini in parallel?". Pass `workspace` = the repo
root. Don't propose it for small tasks or work that needs our conversation's context.
```

Turn it the other way — "never delegate without me asking first" — and that works too: the same
file, the opposite sentence.
</details>

<details>
<summary><b>When should I use Antigravity vs Codex vs Copilot vs Cursor vs Grok vs Kimi?</b></summary>

Use **Antigravity** for fast, cheap tool-calling, quick answers, and **image generation** (it's the
only backend with an image model) — and it now lets you **pick the model** too (agy's `--model`). Use
**Codex** for heavier reasoning, real code/repo work, or when you want a **real, enforced
`workspace-write` sandbox**. Use **Copilot** for agentic coding on your GitHub Copilot plan, or as a
second coding opinion alongside Codex — noting its sandbox is **best-effort**, not enforced. Use
**Cursor** for agentic coding on a Cursor plan, or when you want the **widest model menu** —
GPT, Claude, Grok, and Composer, all via `model` — noting its sandbox is **agent-enforced**, like
Copilot's.

**Grok** and **Kimi** are [experimental and unverified](backends.md#experimental-backends) — reach for them to
help verify them, or if they're the subscription you actually have. Grok is the more capable of the
two here: real OS sandbox (Linux/macOS), watch mode, and swarm support. Kimi has no sandbox and no
swarm/watch yet.

All of them let you choose a `model` (except Kimi, which can't validate one); in a swarm
you can mix five of the six. See [The backends at a glance](backends.md#the-backends-at-a-glance).
</details>

<details>
<summary><b>Will it break when agy updates?</b></summary>

Less likely now. As of **agy 1.0.15** the bridge prefers agy's **stdout** on the happy path (1.0.15
fixed the print-mode stdout bug on Windows — `-p` now writes the clean answer there), which removes
its dependence on agy's **undocumented transcript schema** for normal runs. It still falls back to
reading the JSONL transcript, or the SQLite `.db` agy dual-writes, when stdout is empty (older agy,
non-Windows, or `--sandbox` runs) — so a schema change would only bite that fallback path. Re-verified
working on **1.0.15** (stdout answer clean under tool use; transcript/`.db` fallback intact; live ask
round-trip + `antigravity_status` diagnostics pass). Still, if you rely on the fallback, pin a
known-good `agy` version.
</details>

<details>
<summary><b>Which model does Antigravity use — can I pick it?</b></summary>

Yes. Pass `model` to `antigravity_ask`/`antigravity_continue` (or per task in `agent_swarm`) — it maps
to agy's `--model`, taking any slug from `agy models` (e.g. `"gemini-3.1-pro-high"`,
`"claude-sonnet-4-6"`). Omit it to use the `"model"` field in agy's `settings.json`, which
defaults to **`gemini-3.8-flash-high`** — speed-optimized for cheap tool-calling.

**agy 1.1.5 renamed every model**, replacing the old human labels (`"Gemini 3.1 Pro (High)"`) with
stable slugs (`gemini-3.1-pro-high`) — the old form is no longer accepted, so pass slugs. **The
default has since moved three times**: 1.1.6 added the `gemini-3.6-flash` family and took it, the
`gemini-3.7-flash` family arrived by 1.1.16 and took it in turn, and **1.1.25 added
`gemini-3.8-flash` and moved the default onto that** (verified through the bridge: a call passing no
`model` answers as Gemini 3.8 Flash). The same release also **dropped the whole `gemini-3.5-flash`
family, which no changelog entry mentions** — a 3.5 slug from older docs is now rejected up front.
The full list, re-checked live on 1.2.10 (unchanged since 1.1.25):
`gemini-3.8-flash-low|medium|high`, `gemini-3.7-flash-low|medium|high`,
`gemini-3.6-flash-low|medium|high`, `gemini-3.1-pro-low|high`,
`claude-sonnet-4-6`, `claude-opus-4-6-thinking`, `gpt-oss-120b-medium`. Note the slug bakes in the
reasoning effort, which is why the flash and pro models appear once per level. agy self-updates in
the background, so treat any list written down here — this one included — as a snapshot; `agy models`
and `antigravity_status` are the live answer.

agy 1.0.5 added `--model`, but through ~1.0.14 switching to a different model in `-p` **hung** the
call, so earlier bridge versions stayed single-model. **Re-verified on agy 1.0.16 that the hang is
fixed** — a Claude model answers as Anthropic Claude, a Gemini model as Gemini, each in seconds. One
caveat the bridge handles for you: agy **silently ignores an unknown model** (it falls back to the
default with no error), so the bridge validates your slug against `agy models` and rejects a typo up
front. (That validation is also what agy 1.1.11 broke, by making `agy models` print
`<slug>\t<human label>` instead of a bare slug — see [Status & caveats](status.md#status--caveats). Fixed as
of bridge 0.24.0; on 0.23.x with agy 1.1.11+, pass no `model` at all.)
</details>

<details>
<summary><b>Can it generate images?</b></summary>

**Yes — that's the `antigravity_image` tool**, on the Antigravity backend. agy's print mode generates
real images on your AI Pro quota; `antigravity_image` drives it, saves the file to a path you choose
(or a timestamped default in your workspace), fixes the extension to match the real bytes (agy picks
JPEG or PNG itself), and returns the path. Verified on **agy 1.0.9 / Windows**. Codex has no image
model — it's a coding agent.
</details>

<details>
<summary><b>Does it cost extra money?</b></summary>

No. It uses the **same quota you already pay for** — AI Pro for Antigravity, your Codex plan for
Codex, your GitHub Copilot plan for Copilot, your Cursor plan for Cursor. The smoke test spends a
negligible amount.
</details>

<details>
<summary><b>Does it stream responses?</b></summary>

The final answer is request/response — the CLIs return it all at once, so the tools return when the
agent finishes (each call typically takes 10–30 s; Copilot's reasoning models can run longer). If you
want to *watch* the agent work as it goes,
pass **`watch=true`** to any single-prompt tool: it opens the **Agent Intern** browser window and
live-streams the agent's steps — see [Watch mode](watch-and-swarm.md#watch-mode). It's coarse (a handful of steps, not
token-by-token), and the returned value is identical to the non-watch call.
</details>

<details>
<summary><b>Can I run several calls at once?</b></summary>

The **single-agent** tools are **serialized** inside the server: agy rewrites `last_conversations.json`
on every call, so concurrent runs sharing one state dir would race and could return the wrong
conversation. A `threading.Lock` makes extra requests queue rather than race. (On agy 1.1.8+ the
bridge also records the `conversation_id` agy reports for each run and prefers it when pinning a
continue, so that resolution no longer depends on the shared file — but the lock stays, since agy's
state dir is still shared and a fresh server process starts with nothing recorded.)

For real parallelism use **[`agent_swarm`](watch-and-swarm.md#swarm)** — each agy worker runs in its own isolated state
dir (and Codex/Copilot/Cursor workers need none), so they don't race and the lock isn't needed (~2.8×
at 3 workers). That's the supported way to run many calls at once, across any backend.

The isolated state dir is a redirected `HOME`, and on **macOS** that also hides agy's stored
credentials (the login keychain is resolved through `$HOME`), so isolated workers there fall back to
running **serialized** in your real HOME — correct, but without the speedup. Windows is unaffected
(Credential Manager is HOME-independent). See the [swarm auth note](status.md#status--caveats).
</details>
