# Watch mode & swarm

<sub>[← back to the README](../README.md) · [all docs](README.md)</sub>

The live **Agent Intern** window, and `agent_swarm` for running many agents in parallel.

<a id="watch-mode"></a>

## 👁️ Watch mode — Agent Intern (experimental)

Pass **`watch=true`** to **any single-prompt tool** — `antigravity_ask`, `antigravity_continue`,
`antigravity_image`, `codex_ask`, `codex_continue`, `copilot_ask`, `copilot_continue`, `cursor_ask`,
`cursor_continue`, `opencode_ask`, `opencode_continue`, `grok_ask`, `grok_continue`, `muse_ask`, or
`muse_continue` — to **watch
the agent work live in a little chat-style browser window** called **Agent Intern**. The agent
still runs headless; alongside it the bridge serves a tiny page on `127.0.0.1` and opens it in a
small, chromeless app window that renders the exchange as a **conversation**: your prompt shows as a
chat bubble, the agent's live steps stream in as a collapsible **timeline** — its planner narration,
the **real commands** it runs (`$`), and how each one ended (✓ / ✗), read live (from agy's
`--output-format stream-json` on 1.1.8+ — its transcript on older agy — or codex's / copilot's JSON
event stream, cursor's / grok's streaming-json, or opencode's `--format json` events, which are the
very same stream its non-watched calls read) — and the final
answer arrives as a Markdown card (and, for
`antigravity_image` with `watch=true`, the generated image shown inline). A **`*_continue`** run
opens with the **prior turns of the conversation shown as history**, so it reads as one ongoing
thread rather than a blank new window. (A watched `cursor_continue` is the exception — Cursor stores
its transcript in an opaque SQLite blob, so its window opens without visible prior-turn history; a
watched `grok_continue` or `opencode_continue` opens without history for the same reason.)

<div align="center">
<table>
<tr>
<td width="50%" align="center"><b>text ask / continue (agy, codex, copilot, <i>or</i> cursor)</b></td>
<td width="50%" align="center"><b><code>antigravity_image</code> — image inline</b></td>
</tr>
<tr>
<td><img src="../assets/watch-ask.gif" width="100%" alt="Agent Intern window for a text ask: Claude's prompt as a chat bubble, the agent's live steps as a timeline (its narration, and each real command it runs ticked off with its duration), then the answer as a Markdown card"></td>
<td><img src="../assets/watch-image.gif" width="100%" alt="Agent Intern window generating an image: the prompt bubble, the live step timeline, then the finished image shown inline"></td>
</tr>
</table>
<sub>Real captures — the agent runs headless while the <b>Agent Intern</b> window renders the exchange as a chat conversation: Claude's prompt as a bubble, the live steps as a timeline (narration, and each <code>$</code> command ticked off ✓ with its duration), then the final Markdown answer or inline image.</sub>
</div>

- **Cross-platform & best-effort.** Prefers a Chromium browser (`--app` mode) for the
  windowed look; falls back to a normal browser window. If nothing can open, the run
  still completes and returns normally.
- **Window size.** Set **`AGY_WATCH_WINDOW_SIZE`** (e.g. `AGY_WATCH_WINDOW_SIZE=480,700`)
  to resize the window; default is `560,760`. Press **Esc** (or Enter) in the window to
  close it.
- **One window, reused — but concurrent runs stay separate.** Repeated *sequential*
  watch calls **reuse the already-open window** instead of stacking a new one (the open
  page resets itself for the new run; the swarm dashboard rebuilds for the new fan-out).
  A run that starts while another watched run is **still working** gets its **own
  window** instead — so two concurrent single-worker runs (e.g. a `codex_ask` and a
  `copilot_ask` at once) each stream into their own view and never clobber each other.
  If you closed the window, the next run opens a fresh one. Set **`AGY_WATCH_ALWAYS_NEW=1`**
  to force a new window every time.
- **Access control.** The viewer is an HTTP server, and it serves your prompts, the answers, and
  the real commands the agents ran — so it binds `127.0.0.1` on an ephemeral port **and** requires
  two things on every request: a **loopback `Host` header** (which is what makes DNS rebinding
  fail — a rebound page arrives under the attacker's hostname) and a **per-process token** carried
  in the URL, which stops another local process or another user on a shared machine from simply
  connecting. The bridge puts the token in every URL it opens, so none of this is visible in
  normal use. Worth knowing because the server starts lazily but is never stopped: one
  `watch=true` run leaves the port listening for the life of the MCP server.
- **Chat layout & history.** Prompts render as chat bubbles labelled **Claude**, since the MCP
  client writes them. Long ones clamp to a few lines behind a **Show more / Show less** toggle.
  The agent's side carries its backend's logo, which also heads the window, and its answer
  arrives as a Markdown card. A
  **`*_continue`** run seeds the window with
  the conversation's **prior turns**, read from each backend's own session store (agy's
  transcript, codex's rollout, copilot's `events.jsonl`; Cursor's store is opaque, so a watched
  `cursor_continue` opens without visible history). The swarm's per-worker detail
  window uses the same chat design for its one task.
- **Steps you can read at a glance.** The steps form a timeline, each with its time since the
  start. A command shows a pulsing node and a spinner while it runs, then a **✓ with how long it
  took**, or a red **✗ with the error underneath**. That replaces the separate "command finished"
  line each command used to add. (Codex reports a command only once it has finished, so its
  commands never show as running.) A failed run leaves its timeline open and marks its answer card red.
- **Progress, keyboard & copy.** The status chip in the header ticks live, next to the time budget
  (`Working · 14.1s / 3m00s`). The progress bar under it turns **amber past 75%** of the budget.
  The window title leads with the state (● working, ✓ done, ✗ failed), so you can read it from
  the taskbar. If the bridge goes away mid-run, the window says **Disconnected** and stops
  pretending to work. Answers render as Markdown, including tables, block quotes, nested lists
  and fenced code with a language label. Code blocks have their own **copy** button, and so does
  the answer. A "jump to latest" badge appears if you scroll up.
- **Swarm dashboard.** One card per worker. The header counts workers by state (running,
  queued, done, failed), its overall bar splits into done and failed, and its clock stops when the
  last worker finishes. Each card shows the backend's logo, the prompt, a status chip with a live
  clock, the latest step, a step count and a time bar that turns amber near the budget. An opencode
  card is measured against opencode's raised budget. Use **↑/↓** to select a worker and **↵** to
  open its detail window.
- **Coarse, not token-level.** The backends flush their step stream in chunks, so you
  get a handful of live steps, not character streaming. The returned value is identical
  to the non-watch call. Nothing is sent anywhere but your own machine.

<a id="swarm"></a>

## 🐝 Swarm — run agents in parallel

`agent_swarm` fans a list of **tasks** out to workers that run **truly
concurrently** (capped at `max_concurrency`, default 4), then returns every
worker's result in one block. Each task names its own `backend`, so a **single
swarm can mix Antigravity (Gemini), Codex, Copilot, Cursor, opencode, Grok and Muse**
workers — hand the reasoning-heavy jobs to Codex, Copilot, or Cursor, the quick ones to
Gemini, and the ones you'd rather not spend a paid quota on to opencode, all at once. Good for independent sub-tasks: summarise N files, ask the same question
about N repos, fix N bugs. (`antigravity_image_swarm` stays separate — it
generates N images, and only agy has an image model.)

```
agent_swarm(tasks=[
  {"backend": "antigravity", "prompt": "Summarise src/auth.py in 2 bullets."},
  {"backend": "codex", "prompt": "Find and fix the failing test in tests/",
   "sandbox": "workspace-write", "workspace": "./repo"},
  {"backend": "copilot", "prompt": "Explain what src/api.py exposes.",
   "sandbox": "read-only", "workspace": "./repo"},
  {"backend": "cursor", "prompt": "Draft a docstring for src/utils.py.",
   "model": "auto", "workspace": "./repo"},
  {"backend": "grok", "prompt": "List the public exports of src/index.ts.",
   "sandbox": "read-only", "model": "grok-4.5", "workspace": "./repo"},
  {"backend": "opencode", "prompt": "What does src/config.ts read from the env?",
   "sandbox": "read-only", "model": "opencode/nemotron-3.5-lightning-free",
   "workspace": "./repo"},
])
```

<div align="center">
<img src="../assets/watch-swarm.gif" width="62%" alt="Agent Swarm dashboard: one card per worker with its backend's logo, prompt, a status chip with a live clock, its latest step and a time bar, under counters for running, queued, done and failed workers">
<br>
<sub><code>agent_swarm(..., watch=true)</code> — one card per worker, with its backend's logo; the counters and the done/failed bar move as workers finish. Click a card (or <b>↑/↓</b> then <b>↵</b>) to pop that agent into its own window.</sub>
</div>

**How it stays correct under concurrency.** The single-agent agy tools serialize
through a lock because agy rewrites `last_conversations.json` on every call, so
concurrent runs sharing one state dir would race. The swarm sidesteps this: each
**agy** worker runs with its **own isolated `HOME`/`USERPROFILE`**, so agy's
`brain/`, `cache/`, and `last_conversations.json` never collide — no lock needed.
Auth still works because agy reads it from the **OS credential store**, not from
`~/.gemini` (verified on agy 1.0.9). **Codex**, **Copilot**, **Cursor**, **Grok** and **opencode**
workers need no such isolation — each is a fresh one-shot (`codex exec` with its own `-o` file;
`copilot -p` with its own self-set session id; `cursor-agent -p` with its own minted chat id;
`opencode run` with a fresh session per run, and never pinned). Each worker's `cwd` is its real `workspace`,
so file access is unchanged. Measured ~**2.8× speedup at 3 agy workers** (the AI Pro
backend does not serialize per-account); higher `max_concurrency` trades
quota/rate-limit pressure for wall-clock.

- **Per-task fields** — `backend` (`antigravity`/`codex`/`copilot`/`cursor`/`opencode`/`grok`/`muse`,
  with `oc` as an alias for opencode and `meta` for muse) and `prompt` are required; `workspace` defaults to the server cwd;
  `sandbox` and `model` apply to **Codex, Copilot, Cursor, opencode and Grok** (on Antigravity
  `sandbox: "read-only"` means plan mode). Swarm workers are
  **one-shot** — there is no `*_continue` for a swarm worker's session.
- **Per-backend timeout floor** — `timeout_s` is shared by every worker and
  defaults to 180 s, which is right for Codex/Copilot/Cursor and wrong for
  opencode, whose free models were measured at **152–428 s**. An opencode worker is
  therefore given **at least 300 s** whatever you pass; the budget is only raised,
  never lowered, and a paid model simply finishes early.
- **Error isolation** — a worker that fails is reported in place; the others still
  return.
- **`watch=true`** — opens a thin live **Agent Swarm** dashboard (one card per
  worker, with its **backend's logo**, repo, prompt, and latest step). **Click a card**
  to pop that agent into its own window streaming its full step log.

> [!WARNING]
> A swarm launches **N unsandboxed agents at once** — N× the prompt-injection
> "lethal trifecta" surface of a single call (see [Security](security.md#security)). Only use
> it with **trusted prompts on trusted content**. Codex workers honor their
> enforced `sandbox`; Copilot, Cursor and opencode workers honor their best-effort `sandbox`;
> Antigravity workers have no real boundary.
