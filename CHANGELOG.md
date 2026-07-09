# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

GitHub Release notes are auto-generated from commits on each `vX.Y.Z` tag
(see `.github/workflows/release.yml`); this file is the curated, human-facing
summary.

## [Unreleased]

## [0.19.5] - 2026-07-09

### Changed

- **Docs brought in line with the chat redesign.** The watch-mode README section still named the
  expand/collapse toggle by its old Turkish label ("daha fazla / daha az" → "show more / show less"),
  the GIF table header omitted Copilot ("agy or codex" → "agy, codex, or copilot"), and the GIF
  alt-text/caption described the old streaming view — reworded to the chat-conversation framing
  (a **CLAUDE** prompt bubble → collapsible step trace → Markdown answer card tagged by backend).
  Also refreshed `swarm_watch.py`'s module docstring, which still called the single-worker viewer a
  "singleton (`_WATCH_STATE`)" and mentioned a "typewriter" — neither true after the id-keyed
  multi-window state (`_WATCH_RUNS`) and the chat redesign.

## [0.19.4] - 2026-07-09

### Changed

- **Re-captured the watch GIFs with the English UI.** `assets/watch-ask.gif` and
  `assets/watch-image.gif` were regenerated after the 0.19.3 string fix, so the on-screen labels in
  the GIFs ("working…", "N steps", "show more", "close") now match the app instead of the earlier
  Turkish captures. Frames inspected again for privacy before commit — no paths/usernames leaked.

## [0.19.3] - 2026-07-09

### Changed

- **Watch window UI text is now all English.** A handful of labels in the chat viewers (single-worker
  and swarm per-worker) had slipped into Turkish during the redesign — they now read "working…",
  "show more" / "show less", "N steps", "jump to latest", "close", and "queued" (from "çalışıyor…",
  "daha fazla" / "daha az", "N adım", "en alta in", "kapat", "sırada").

## [0.19.2] - 2026-07-09

### Changed

- **Refreshed the watch-mode GIFs for the chat redesign.** `assets/watch-ask.gif` and
  `assets/watch-image.gif` now show the current chat UI (a **CLAUDE** prompt bubble → a live
  collapsible step trace → a Markdown answer card / inline image) instead of the prior terminal-style
  view. The capture tool (`tools/capture_watch_gif.py`) was made safe for a public asset: the `ask`
  capture uses a path-free prompt (a knowledge question + `git --version`) so agy touches no files
  and no absolute paths or usernames appear on screen, and the `image` capture saves under a neutral
  `C:\Users\Public` dir so the "Saved to …" caption carries no personal username. (The swarm
  dashboard GIF is unchanged — the chat redesign only touched the per-worker detail window, which
  that GIF doesn't capture.)

### Changed

- **Codex/Copilot watch finishes ~2s sooner after the answer.** The streaming runners join their
  stdout/stderr reader threads after the process exits; when a lingering child holds the pipe open
  those joins hit their timeout, and two 2s joins made the window sit on "working" ~4s past the
  answer. Trimmed each grace to 1s (~2s total), keeping the "done" transition snappy. The answer is
  unaffected — it comes from codex's `-o` file / copilot's stream, not the trailing pipe.

### Tests

- **Regression guard for the 0.18.1 streaming-exit fix.** New unit tests in `test_codex.py` /
  `test_copilot.py` launch a fake CLI that emits its JSON events, spawns a child that keeps the
  stdout pipe open, then exits — asserting `run_codex_streaming` / `run_copilot_streaming` return
  promptly (on process exit) with the right answer instead of blocking on the lingering child. No
  codex/copilot quota (the fake is a local Python script).

## [0.19.0] - 2026-07-09

### Added

- **Concurrent single-worker watch runs no longer clobber each other's window.** The single-worker
  viewer's state was a process-wide singleton, so two watched runs that overlapped (e.g. a
  `codex_ask` and a `copilot_ask` at the same time — they don't share the agy lock) wrote into one
  shared window and garbled it. The viewer state is now keyed by a per-run id (`_WATCH_RUNS`): the
  common **sequential** case still reuses the `"main"` slot and its already-open window, but a run
  that begins while another is **still working** is given its own id and its **own window**. The
  browser page reads its id from the URL (`/?id=…`) and polls `/events?id=…`; `/image` validates the
  requested path against all live runs. Finished, unwatched runs are evicted so the map can't grow
  without bound. Verified: two concurrent runs render into two isolated windows (each shows only its
  own prompt/answer/backend); a unit test asserts the second run gets a distinct id and independent
  state.

## [0.18.1] - 2026-07-08

### Fixed

- **Codex/Copilot watch mode no longer hangs until timeout after the answer is ready.** The streaming
  runners (`run_codex_streaming` / `run_copilot_streaming`) ended their live loop on **stdout EOF**
  (`for line in proc.stdout`), but codex (and copilot — both node CLIs) can leave a child process
  holding the stdout pipe open after the turn completes, so the loop blocked past completion and the
  watched run only returned when the watchdog killed it at `timeout+30s` — the window showed the
  answer but kept spinning. Completion is now driven by **process exit** (`proc.wait(timeout=…)`) with
  stdout/stderr read on daemon threads, so the run finishes as soon as the CLI exits regardless of a
  lingering child (verified live: a codex watched ask returned in ~4.6s instead of timing out). This
  also drains stderr concurrently, closing the same pipe-buffer deadlock class fixed for agy in 0.17.2.
- **Fresh `workspace` dirs no longer crash the single-worker tools with `WinError 267`.** `antigravity_ask`
  / `antigravity_continue` / the image tool and `codex_*` / `copilot_*` set the child's cwd to
  `workspace` but didn't create it, so a not-yet-existing workspace failed with a cryptic "directory
  name is invalid". Each runner now `os.makedirs(workspace, exist_ok=True)` before spawning, matching
  what the swarm workers already did.

## [0.18.0] - 2026-07-08

### Added

- **Watch mode is now a chat conversation.** Both the single-worker viewer (`antigravity_ask` /
  `antigravity_continue` / the image tool / `codex_*` / `copilot_*` with `watch=true`) and the swarm's
  per-worker detail window were redesigned as a chat UI: the prompt shows as a **chat bubble** (long
  ones clamp with a **daha fazla / daha az** expand-collapse toggle), the agent's live steps stream in
  a collapsible "thinking" trace, and the answer arrives as a Markdown card tagged with the backend.
  Prompt bubbles are labelled **CLAUDE** (the MCP client authors the prompt), answers by backend
  (**AGY** / **CODEX** / **COPILOT**).
- **`*_continue` watch shows the conversation history.** A continued run now opens seeded with the
  **prior turns** of the conversation instead of a blank new window, so it reads as one ongoing
  thread. History is reconstructed from each backend's own session store — agy's JSONL transcript
  (user turns unwrapped from their `<USER_REQUEST>` envelope; the final planner response per turn),
  codex's rollout (`event_msg` `user_message` / `agent_message`), and copilot's `events.jsonl`
  (`user.message` / `assistant.message`). Best-effort: a fresh ask, an unresolved session, or an
  unreadable store yields no history and the run proceeds normally. (agy caps very long older turns in
  its transcript, so their tails can be clipped; the live/last turn is complete.)

## [0.17.2] - 2026-07-08

### Fixed

- **Watch mode no longer deadlocks on long answers (false timeouts + truncated output).** All four
  *watched* runners (`_run_agy_watched`, `_run_agy_image_watched`, and the swarm's
  `_run_text_worker_watched` / `_run_image_worker_watched`) opened agy with `stdout`/`stderr` as
  pipes but never drained them while polling — the classic `Popen` deadlock. Since agy 1.0.15+ writes
  its full final answer to stdout, a large answer filled the fixed OS pipe buffer, blocked agy's
  write, and hung it until the hard deadline fired: a **spurious timeout with a half-written
  transcript answer**. Each pipe is now drained on a background thread (a new `_drain_pipe` helper),
  matching what `subprocess.run` already does for the non-watched paths. Verified with a real 3 MB
  child: the old loop never exits (deadlock) while the drained loop finishes in 0.2 s with the full
  output captured. (The non-watched and Codex/Copilot streaming paths were never affected.)
- **Swarm detail window shows the whole prompt when expanded.** In the swarm watch dashboard's
  per-worker log window, a long prompt expanded past the bottom of the window with no scrollbar, so
  its tail was unreachable. The expanded prompt panel is now capped at `46vh` and scrolls internally,
  mirroring the single-worker viewer in `server.py`.

## [0.17.1] - 2026-07-08

### Changed

- **Verified against agy 1.1.0** (`VERIFIED_AGY_VERSION`), silencing the spurious "newer than
  verified" startup warning that fired for every 1.1.0 user. Full bridge round-trip re-confirmed live:
  `antigravity_ask` + conversation-pinned `antigravity_continue` return clean over stdout, and the
  state-file layout (`last_conversations.json`, JSONL-primary transcript, SQLite dual-write) is intact.
- **agy 1.1.0's new agent execution modes do not affect the bridge.** 1.1.0 added a `--mode` launch
  flag (`accept-edits` | `plan`) and a new interactive **request-review** default that pauses before
  file writes for a diff preview. Because the bridge spawns `-p` with **DEVNULL stdin**, the
  request-review approval gate never engages (it needs an interactive stdin) — print mode still
  auto-executes every tool call. Verified: a file-writing task completed in ~36 s (exit 0),
  identically with and without `--mode accept-edits`, so the bridge keeps passing neither `--mode`
  nor `--sandbox`.
- **Sandbox behavior unchanged on 1.1.0.** Re-verified that `--sandbox` still blocks terminal
  commands (a sandboxed terminal run timed out with no response, exit 1) but still does **not** gate
  `write_to_file` (a sandboxed write succeeded outside the workspace, exit 0). `--mode` and
  `--sandbox` coexist without error; neither makes `-p` safe. The SECURITY note stands.

## [0.17.0] - 2026-07-03

### Added

- **Model selection for Antigravity.** `antigravity_ask`, `antigravity_continue`, and the
  `antigravity` swarm backend now take an optional `model` — agy's `--model` (e.g.
  `"Gemini 3.1 Pro (High)"`, `"Claude Sonnet 4.6 (Thinking)"`). Through ~1.0.14 switching model in
  `-p` **hung** the call, so the bridge stayed single-model; **re-verified on agy 1.0.16 that the
  hang is fixed** — a Claude label answers as Anthropic Claude, a Gemini label as Gemini, each
  returning in seconds. Omit `model` to use agy's `settings.json` default (Gemini 3.5 Flash (High)).
  All three backends now expose the same `model` knob.
- **`agy models` validation.** agy **silently ignores** an unknown `--model` label (falls back to
  the `settings.json` default with no error), so a typo would quietly run the wrong model. The bridge
  now validates a requested label against `agy models` (`validate_model`) and rejects an unknown one
  up front — matching codex/copilot's fail-fast. If the label list can't be read (agy missing, models
  call failed), validation is skipped and the label passes through unchecked.

### Changed

- **Verified against agy 1.0.16** (`VERIFIED_AGY_VERSION`). State-file paths, transcript schema, and
  the 1.0.15 stdout path re-confirmed with a live round-trip; the `--model` print-mode fix is the
  notable change this release builds on.

## [0.16.0] - 2026-07-02

### Added

- **Third backend: the GitHub Copilot CLI (`copilot`).** Three new MCP tools —
  `copilot_ask`, `copilot_continue`, `copilot_status` — plus a `copilot` backend for `agent_swarm`
  (aliases `gh`/`github`) and full watch-mode support. Like Codex it's stdout-native: `copilot -p
  "…" -s` writes the clean answer straight to stdout (no scraping). Highlights, verified live against
  **copilot 1.0.68**:
  - **Deterministic, race-free continue.** `copilot`'s `--session-id <uuid>` both *sets* a new
    session's id and *resumes* an existing one, so the bridge generates the id itself, pins it to the
    workspace, and resumes that exact session — no rollout-scraping. Restart-proof fallback reads the
    newest `~/.copilot/session-state/<id>/workspace.yaml` whose `cwd` matches the workspace.
  - **Model selection** via `--model` (a first-class knob, like Codex's `-m`).
  - **`sandbox` knob** mapped to copilot's tool/path permissions for a uniform cross-backend field:
    `read-only` (default — best-effort: denies the local `write`/`shell` tools; **not** an OS
    sandbox, so unlike Codex it isn't a hard boundary), `workspace-write` (writes confined to the
    workspace), `danger-full-access` (`--allow-all`).
  - **Fast, predictable latency.** Runs headless with `--allow-all-tools --no-ask-user
    --no-auto-update`, and disables copilot's builtin GitHub-API MCP by default
    (`--disable-builtin-mcps`) because its flaky HTTP connect could stall a call up to ~60 s. Set
    `COPILOT_GITHUB_MCP=1` to keep it. `COPILOT_BIN` overrides the executable path (mirrors
    `AGY_BIN`/`CODEX_BIN`), useful since the winget install may be off a stale `PATH`.

### Changed

- **Verified against agy 1.0.15 — and now prefers agy's stdout.** agy 1.0.15 fixes the long-standing
  print-mode stdout bug on Windows: `agy -p` now writes its clean final answer straight to stdout in
  a non-TTY subprocess (verified empirically — stdout carries only the answer, no tool-calling
  narration). `_run_agy` now **prefers stdout when present** and falls back to the transcript/`.db`
  scrape only when it's empty (older agy, non-Windows per the changelog, or `--sandbox` runs). This
  removes the bridge's dependence on agy's *undocumented transcript schema* on the happy path — its
  single biggest fragility — and drops the flush-poll latency. Fully backward-compatible: the
  transcript path stays exercised everywhere stdout is empty. Bumped `VERIFIED_AGY_VERSION` to
  `(1, 0, 15)`. The other 1.0.15 changes don't touch this bridge (the "MCP connection timeout → 60 s"
  is agy acting as an MCP *client*, the opposite direction; the rest are interactive-TUI / paste /
  permissions-panel fixes).

## [0.15.3] - 2026-06-30

### Changed

- **Verified against agy 1.0.14.** Bumped `VERIFIED_AGY_VERSION` to `(1, 0, 14)`, silencing the
  startup compat warning and the status tool's "newer than verified" row. Confirmed with a live
  round-trip (`antigravity_status` green on every row; ask round-trip returns clean text): state-file
  paths, `last_conversations.json` (still keyed by workspace path), and the transcript schema (JSONL
  still primary, SQLite fallback intact) are unchanged across 1.0.13 **and** 1.0.14. Both releases are
  interactive-TUI / plugin / skill / browser-task work plus permission-rule tweaks — clipboard image
  paste in tmux, the lifted `/goal` max limit, subagent "always proceeds" artifact approval, the
  plugin-import directory-copy fix, TUI layout/viewport/rewind fixes, skill slash-prefix rendering,
  the prompt-editor undo/redo fix, the restored browser prompt sections, and the permission changes
  (strict-by-default "Always Approve" matching, a `regex:` opt-in, relaxed redirection checks) — none
  of which the headless `agy -p` path uses. Two items looked worth a closer check and both come up
  clean: 1.0.14's "MCP configuration path mismatch" fix is about agy loading custom MCP servers *as a
  client* (the opposite direction from this bridge, which drives agy via its CLI), and 1.0.13's
  removed "Resume in the same project" exit-hint line never mattered because the bridge reads the
  transcript, not agy's stdout (stdout is surfaced only on a non-zero exit). Permissions still do not
  gate `-p`, so the security posture is unchanged.

## [0.15.2] - 2026-06-25

### Changed

- **Verified against agy 1.0.12.** Bumped `VERIFIED_AGY_VERSION` to `(1, 0, 12)`, silencing the
  startup compat warning and the status tool's "newer than verified" row. Confirmed with a live
  round-trip (`antigravity_status` green on every row, ask round-trip returns clean text): state-file
  paths, `last_conversations.json` (still keyed by workspace path), and the transcript schema (JSONL
  still primary, SQLite fallback intact) are unchanged. 1.0.12 is interactive-TUI / rendering /
  keybinding / network-layer work — `--project`/`--new-project` flags and the "default project
  regardless of active workspace" resolution change, Esc-confirm in comment mode, OSC8 hyperlinks,
  reverse diff cycling, ctrl+o scrollback, Makefile/LaTeX code-block rendering, the AES-NI/DPI TLS
  fix, and backtab/pgdown key-string fixes — none of which the headless `agy -p` path uses. The new
  permission-config precedence (per-project files under `~/.gemini/config/projects/` now outrank
  `~/.gemini/antigravity-cli/settings.json`) is config the bridge never reads; the `model` field
  still lives in `settings.json`, and permissions still do not gate `-p`, so the security posture is
  unchanged. The bridge passes no `--project` flag — it relies on `cwd=workspace` plus conversation
  pinning, both verified intact.

## [0.15.1] - 2026-06-24

### Changed

- **Verified against agy 1.0.11.** Bumped `VERIFIED_AGY_VERSION` to `(1, 0, 11)`, silencing the
  startup compat warning and the status tool's "newer than verified" row. 1.0.11 is entirely
  interactive-TUI / keybindings / additive-env-var work (ctrl+c/ctrl+d handling, `/resume`, the
  ctrl+g AltScreen tool-confirmation view, keybinding validation and lazy `keybindings.json`
  creation, `USE_ADC` ADC auth, `AGY_CLI_CMD_OUTPUT_PERCENTAGE`, command-output/ANSI/VCS-tree
  rendering) — none of which the headless `agy -p` path uses. Its one auth change (return an empty
  config when unsigned in) touches agy's own config read; the bridge never reads agy's config, only
  state-file paths and the transcript. State-file layout, `last_conversations.json`, and the
  transcript schema (JSONL + SQLite fallback) are unchanged — re-confirmed via the status tool.

## [0.15.0] - 2026-06-23

### Added

- **SQLite (`.db`) transcript fallback.** When agy's JSONL transcript is missing or empty,
  `_read_response` now reads the answer from agy's SQLite conversation store
  (`conversations/<id>.db`) instead of failing — walking the `steps` table's protobuf `step_payload`
  (the planner response is the sub-message at field 20, its text at field 1) for the last completed
  planner step. This already covers `--sandbox` runs (which write no JSONL) and future-proofs the
  bridge against agy's announced switch to SQLite as the default conversation format. The reader was
  reverse-engineered and verified to match the JSONL answer across 115 local conversations (104
  byte-identical, 11 a superset, **0 wrong**).

## [0.14.1] - 2026-06-23

### Added

- **Unified `agent_swarm`** — one swarm tool that runs a heterogeneous list of tasks across **both**
  backends at once. Each task names its `backend` (`antigravity` or `codex`) plus a `prompt` (and,
  for Codex, `sandbox`/`model`); workers run concurrently in one pool and, with `watch=true`, share
  one **Agent Swarm** dashboard that now shows a per-worker backend badge.

### Changed

- **BREAKING** (despite the patch bump): removed `antigravity_swarm` and `codex_swarm` — use
  `agent_swarm` instead, passing `tasks=[{"backend": "antigravity"|"codex", "prompt": ...}, ...]`.
  `antigravity_image_swarm` is unchanged (Codex has no image model). Tool count 10 → 9.

### Fixed

- CI now also runs `test_codex.py` (it previously ran only `test_server.py` + `test_swarm.py`),
  which surfaced two latent POSIX bugs: a cwd test recursed infinitely (a monkeypatched `os.getcwd`
  called `os.path.abspath`, which re-enters `getcwd` when the path isn't absolute off-Windows), and
  `codex_bridge.format_swarm_results` used `os.path.basename` (wrong for backslash paths off-Windows).
  The recursion is fixed; the basename bug is moot — `swarm_codex` / `format_swarm_results` /
  `_broadcast_workspaces` became dead code when `agent_swarm` replaced the per-backend swarms, so they
  were removed.

## [0.14.0] - 2026-06-23

### Changed

- **BREAKING:** folded Codex's live watch view into a `watch=true` flag on `codex_ask` and
  `codex_continue` (and **removed the separate `codex_ask_watch` tool**), matching how the
  Antigravity tools have worked since v0.11.0. `codex_continue` gains watch mode (it had none
  before). Codex tool count drops 5 → 4; total 11 → 10. Update any client that called
  `codex_ask_watch` to pass `watch=true` to `codex_ask` instead.

## [0.13.0] - 2026-06-23

### Added

- **OpenAI Codex bridge** — drive `codex exec` as a sub-agent alongside Antigravity. Five new tools
  (in a new `codex_bridge.py` module): `codex_ask`, `codex_continue`, `codex_ask_watch`,
  `codex_swarm`, `codex_status`. Unlike `agy -p`, `codex exec` writes its final message to a file the
  bridge requests (`-o/--output-last-message`), so answers are read cleanly with **no
  transcript-scraping**; continue resumes the exact session via codex's own rollout files
  (`codex exec resume <id>`, with a cwd-matched on-disk fallback after a restart). Codex's
  `-s/--sandbox` is a **real** boundary (default `read-only`) and model selection (`-m`) works — both
  exposed as tool parameters. Verified on **codex-cli 0.141.0**.

### Changed

- **Renamed `antigravity-intern` → `agent-intern`** to reflect that the bridge now drives multiple
  agent CLIs (Antigravity + Codex), not just Antigravity. The MCP server name — and therefore the
  tool prefix — becomes `mcp__agent-intern__*`, so **update your client config**. The live viewer is
  now "Agent Intern" / "Agent Swarm". Backend-specific names are unchanged on purpose: the
  `antigravity_*` tools, the `AGY_BIN` / `AGY_BRIDGE_*` env vars, and "Antigravity" itself all keep
  their names.
- README documents both backends; the header animation now shows two Codex agents (cloud + `>_`)
  alongside two Antigravity agents.

## [0.12.1] - 2026-06-19

### Changed

- Re-verified state-file paths, `last_conversations.json`, and the `-p` JSONL transcript
  schema against **agy 1.0.10** (live `antigravity_status` + ask round-trip), and bumped
  `VERIFIED_AGY_VERSION` so the startup compat check no longer warns on 1.0.10. No functional
  change: nothing in agy 1.0.10 — bash-mode stdout escaping, PowerShell default shell, the new
  alert message type, the permission/`settings.json` fixes, or rundll32 browser sign-in — touches
  the paths, schema, or print-mode TTY-leak the bridge depends on.

## [0.12.0] - 2026-06-18

### Added

- Watch panels overhaul (aesthetics + animation + usability), terminal look kept:
  per-panel time progress bars (elapsed / timeout); the swarm dashboard also gains an
  overall done/total bar, per-row time bars, and keyboard navigation (↑/↓ select, ↵
  open); the worker detail window gains the Markdown + typewriter rendering and a copy
  button from the single-worker viewer; a "jump to latest" follow affordance; and
  status-glow / completion-pop animations.

### Changed

- Watch windows are now **reused** across repeated runs instead of stacking a new
  browser window each time watch mode is used — the bridge detects an already-open
  viewer (via its `/events` polling) and lets it pick up the new run (the page resets
  itself; the swarm dashboard rebuilds for the new fan-out). Set
  `AGY_WATCH_ALWAYS_NEW=1` to force a fresh window per run.

## [0.11.0] - 2026-06-18

### Changed

- **BREAKING:** folded the live "watch" view into the single-prompt tools as a
  `watch` flag instead of separate tools. `antigravity_ask`, `antigravity_continue`
  and `antigravity_image` now take **`watch=true`** to open the Antigravity Intern
  browser window — matching `antigravity_swarm`'s existing `watch` flag. This also
  means **`antigravity_continue` gains watch mode** (it had none before). Tool count
  drops from eight to six.
- Swarm dashboard now shows the **full prompt**: dashboard rows wrap to 3 lines
  (were single-line, ellipsis-clipped), and each worker's detail window shows the
  complete, untruncated prompt in an **expandable** PROMPT pane (click to expand /
  collapse). The truncated row caption is unchanged.

### Removed

- **BREAKING:** `antigravity_ask_watch` and `antigravity_image_watch` — superseded
  by `watch=true` on `antigravity_ask` / `antigravity_image`.

## [0.10.4] - 2026-06-18

### Changed

- Docs: refreshed the `bridge version` example in the README so the illustrative
  versions don't imply a stale release is current.

## [0.10.3] - 2026-06-18

### Changed

- Docs: document the in-chat update notice in the README — both surfaces (the
  `bridge version` row in `antigravity_status`, visible in the client's chat,
  and the startup stderr warning in host logs). Refreshes the PyPI
  long-description.

## [0.10.2] - 2026-06-18

### Added

- `antigravity_status` now reports the bridge's own version and whether a newer
  release is available (e.g. `v0.10.1 -> v0.10.2 available; upgrade: uvx
  antigravity-intern@latest`). This surfaces the update notice **in the MCP
  client's chat** — the startup stderr warning only reaches the host's logs.
  Best-effort GitHub check; honors `AGY_BRIDGE_NO_UPDATE_CHECK` and never flips
  the overall status to PROBLEMS FOUND (an available update is informational).

## [0.10.1] - 2026-06-18

### Changed

- Docs: reworked install guidance now that the package is live on PyPI.
  `uvx antigravity-intern` is the recommended install, with **opt-in** updates —
  uvx caches and does not auto-upgrade, so the bridge only runs a release you
  chose to install (it runs unsandboxed code, so this is deliberate). Upgrade
  with `uvx antigravity-intern@latest`; `@latest` in the config opts into
  hands-off auto-updates. Corrected an earlier inaccurate "latest on every
  launch" claim, and swapped the GitHub-release badge for a PyPI version badge.

## [0.10.0] - 2026-06-17

### Added

- **PyPI packaging + `uvx` install.** `antigravity-intern` is now an installable
  package with an `antigravity-intern` console entry point, so it can be launched
  with `uvx antigravity-intern` (isolated) instead of a hardcoded path to
  `server.py`.
- **MCP tool annotations.** All eight tools now carry MCP annotations
  (`readOnlyHint` / `idempotentHint` / `openWorldHint` / `title`) so clients can
  reason about which tools are safe — `antigravity_status` is read-only and
  idempotent; the agy-invoking tools are flagged open-world and non-read-only.
- **Native MCP progress notifications.** `antigravity_ask`, `antigravity_continue`
  and `antigravity_image` now emit MCP progress (a coarse elapsed/timeout bar)
  while agy works, for clients that send a progress token. The browser "watch"
  tools are unchanged.
- **CI** (`.github/workflows/ci.yml`): ruff + offline tests on Windows / macOS /
  Linux across Python 3.10–3.13.
- **Release automation**: tagging `vX.Y.Z` cuts a GitHub Release with generated
  notes (`release.yml`) and publishes to PyPI via Trusted Publishing
  (`publish.yml`).

## [0.9.0] - 2026-06-17

### Added

- **Startup update check.** The server polls the GitHub tags API once at launch
  and logs a one-line warning if a newer release is tagged than the running
  `__version__`. Best-effort: silent when offline/rate-limited, never blocks
  startup. Opt out with `AGY_BRIDGE_NO_UPDATE_CHECK=1`; point at a fork with
  `AGY_BRIDGE_REPO`.

## [0.8.0] - 2026-06-17

### Added

- `antigravity_swarm` and `antigravity_image_swarm`: run several agy workers in
  parallel, each in an isolated state dir, with error isolation.
- Live browser "watch" mode (`antigravity_ask_watch`, `antigravity_image_watch`)
  that streams agy's steps and shows generated images inline.

### Changed

- **BREAKING:** rebranded to "Antigravity Intern"; tools renamed `agy_*` →
  `antigravity_*`.

### Removed

- **BREAKING:** `antigravity_ask_stream` (superseded by watch mode).

[Unreleased]: https://github.com/SinanTufekci/agent-intern/compare/v0.15.0...HEAD
[0.15.0]: https://github.com/SinanTufekci/agent-intern/compare/v0.14.1...v0.15.0
[0.14.1]: https://github.com/SinanTufekci/agent-intern/compare/v0.14.0...v0.14.1
[0.14.0]: https://github.com/SinanTufekci/agent-intern/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/SinanTufekci/agent-intern/compare/v0.12.1...v0.13.0
[0.12.1]: https://github.com/SinanTufekci/agent-intern/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/SinanTufekci/agent-intern/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/SinanTufekci/agent-intern/compare/v0.10.4...v0.11.0
[0.10.4]: https://github.com/SinanTufekci/agent-intern/compare/v0.10.3...v0.10.4
[0.10.3]: https://github.com/SinanTufekci/agent-intern/compare/v0.10.2...v0.10.3
[0.10.2]: https://github.com/SinanTufekci/agent-intern/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/SinanTufekci/agent-intern/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/SinanTufekci/agent-intern/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/SinanTufekci/agent-intern/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/SinanTufekci/agent-intern/releases/tag/v0.8.0
