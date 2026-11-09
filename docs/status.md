# Status & caveats

<sub>[← back to the README](../README.md) · [all docs](README.md)</sub>

Version-by-version compatibility notes: what changed upstream, what the bridge does about it, and what's still open.

## Status & caveats

- ✅ **Re-verified on agy 1.5.3 (two releases from 1.5.1). No break.** Run through the `feat/0.36`
  branch's code on Windows, 2026-11-09, on `gemini-3.8-flash-high`: ask, continue in the same
  conversation, one image (a 545 KB PNG), a two-worker antigravity swarm, and the cut-short-turn error
  (a 6 s `timeout_s` raises *"agy hit its 6s timeout before finishing…"*). Changelog entries that touch
  the bridge's path, read and not re-tested one by one: **1.5.2** makes `--dangerously-skip-permissions`
  block commands on a system denylist (every other command still runs) and makes a headless `-p` run
  with it deny commands or file access outside the workspace that a Gemini Enterprise administrator
  requires to be reviewed; `agy -p` also stopped making a background request for a conversation title.
  **1.5.3** retries agent turns that failed with `http2: timeout awaiting response headers` after 60 s,
  and teaches the file tools UTF-16 and legacy code-page files (PowerShell scripts with non-ASCII text
  are now written with a byte order mark). Not re-checked: watch mode, `antigravity_image_swarm`, plan
  mode, `schema`. `VERIFIED_AGY_VERSION` becomes `(1, 5, 3)`.
- ✅ **The 0.36 guidance, checked in all five hosts (2026-11-09).** With the 0.36 server (instructions
  3,407 characters, no tool description over 528) each host was asked, with no tool call, how many tools
  it had, what the last 25 characters of the instructions were, and which planted `MARK-…` strings it
  could see; then to call `opencode_status` and `claude_status`.
  - **Claude Code 2.1.319:** 29 tools (the `claude_*` three are hidden inside it). The instructions
    arrive whole, with no `[truncated]`. The status call worked.
  - **Copilot CLI 1.0.105** (with `--allow-all-mcp-server-instructions`) and **Cursor CLI 2026.11.05**
    (with `--approve-mcps`; `--force` for the calls): 32 tools, instructions whole, both status calls
    worked. **agy 1.5.3:** 32 tools listed by name only, no instructions. Its headless run denies an MCP
    call unless `settings.json` has an allow-rule; `{"permissions": {"allow": ["mcp(*)"]}}` was enough
    (narrower targets were not tried alone). **opencode 2.2.5:** the model counts no separate tools,
    because they sit behind Code Mode (`await tools["agent-intern"].claude_status()` answered), and said
    it had no instructions for the third time on the free model.
  - **Which rules file each host loads** (one marker planted in each convention in a scratch
    workspace): Claude Code reads `CLAUDE.md` only. agy reads `AGENTS.md` and `GEMINI.md`. Copilot reads
    `.github/copilot-instructions.md`, `AGENTS.md`, `CLAUDE.md` and `GEMINI.md`. Cursor reads
    `.cursor/rules`, `CLAUDE.md` and `AGENTS.md`. opencode reads `AGENTS.md` only. So `AGENTS.md` is the
    one file every host but Claude Code loads. A `CLAUDE.md` that imports `AGENTS.md`, and rules files
    in the home directory, were not tried.
  - Per-run flags, a scratch workspace and (for agy) a throwaway home directory were used throughout:
    the real agy MCP config's hash did not change, and no Copilot or Cursor user-level MCP file exists.
- ⚠️ **How each host hands this server's guidance to its model.** Measured on 2026-11-09 with a scratch
  stdio server whose instructions and descriptions are built from 100-character slices that start
  with a marker (`<00100>`, `<00200>`, …); the model reports the highest marker it can see.
  - **Claude Code 2.1.319** forwards the server `instructions` and the tool descriptions but cuts
    **each at 4,096 characters**, ending them with *"… [truncated]"* (a 6,000-character description
    was cut at about 4,095). Parameter descriptions are not cut (9,000 characters arrived whole).
    `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH=20000` lifted both caps. Reports of an older 2,048 limit
    exist ([claudeissues.com #41593](https://claudeissues.com/issue/41593-mcp-tool-descriptions-truncated-at-2kb-breaks-code-execution-tools));
    that was not tested. The server's own 4,857-character instructions lost everything after character
    4,096: the end of the `workspace` bullet, the `*_continue` and `watch` lines, the `*_status`
    bridge-version relay and the Security paragraph. In `-p` the tool definitions are also deferred
    (the model sees only the name) unless `ENABLE_TOOL_SEARCH=false`.
  - **Copilot CLI 1.0.105** forwards instructions only with `--allow-all-mcp-server-instructions`
    (12,000 characters arrived whole); tool and parameter descriptions arrive whole.
  - **Cursor CLI 2026.11.05** forwards instructions (12,000 whole, with `--approve-mcps`). It does not
    preload tool or parameter descriptions: asked cold, the model saw none; it fetched them whole
    through its `GetDynamicTools` call.
  - **opencode 2.2.5:** the source forwards them as an `<mcp_instructions>` block per server, untouched
    (`packages/core/src/mcp/instructions.ts`, `client.ts`), but two live runs on a free model said it
    had none, and the probe runs on that model timed out, so no cap was measured.
  - **agy 1.5.3 forwards no instructions, and lists an MCP tool by name only.** The model's tool list
    showed `probe_tool (MCP server: agent-probe — lazy-loaded)` with no description or schema. What a
    lazy fetch returns could not be seen: a headless run denies it without an allow-rule.
  - The server's own numbers at that date: instructions 4,857 characters, no tool description over
    858, and three parameter descriptions over 1,000 (`agent_swarm` tasks 2,053, `antigravity_ask`
    schema 1,574, `codex_ask` 1,219).
- 🐛 **opencode 2.x: the 1.x bridge fails on every call, and its read-only fence would have been open.**
  The owner's global install moved to opencode 2.2.5 (`@opencode/cli`; 1.19.x stays the 1.x line).
  Found live on Windows with free models; [full table and policies →](backends.md#opencode-2).
  - **`--dir` is gone.** `opencode run --dir <workspace> …` exits 1 with *"Unrecognized flag: --dir"*.
    The directory is now the process's working directory.
  - **`OPENCODE_PERMISSION` is not read.** With the bridge's read-only policy set that way, a shell
    write succeeded and the file was created. The policy now goes in `OPENCODE_CONFIG_CONTENT` as an
    ordered `permissions` array of `{action, resource, effect}` rules (`bash` became `shell`, `task`
    became `subagent`).
  - **A run applies none of that unless it is `--standalone`.** By default 2.x connects to a shared
    background service that ignores the run's environment: the same new-format read-only policy let a
    shell write through in default mode and fenced it with `--standalone`.
  - **Workspace-write confines the edit tool, not the shell.** `external_directory` denied did not
    stop a shell redirect into the parent directory, in a plain directory and in a git repository,
    and a shell can read a `.env` file the read rules refuse. The edit tool is another matter. My
    first attempt used an allow rule with the absolute workspace path and I reported that it worked:
    that was wrong. opencode matches an edit inside the run's directory against a location-relative
    path (`sub/a.txt`), so the rule never matched, and the file I saw written was most likely the
    shell's (it was allowed, and I had not recorded which tool wrote). The same slip probably lay
    behind my note that the edit tool wrote outside the workspace under an `external_directory` deny. Rules built from the source
    (`edit` allow, then deny `../*`, `?:/*` and `/*`) were run live on 2026-11-09 with the shell
    denied: a nested edit inside was written, and a `../` path, an absolute path and a backslashed
    path outside were all refused by the `external_directory` deny, with the `edit` denies as a second
    layer. [backends.md](backends.md#opencode-2) has the details and the source lines.
  - Unchanged: the `--format json` events, `-s`, `-c`, `-m` and `--`. A fenced call's error text
    changed (*"This non-interactive run cannot ask the user for permission, so the request was
    rejected"*), `opencode providers list` is gone (`opencode auth list`), and `--version` prints
    `opencode v2.2.5`. Free models answered in 1.4–14 s.
  - **The bridge** now passes `--standalone` and no `--dir` on 2.x, sets the policy through
    `OPENCODE_CONFIG_CONTENT`, and refuses any major other than 1 or 2 instead of running it unfenced.
    Through it on 2.2.5: a read-only ask answered, a shell write and an edit write were both refused,
    `-s` resumed a session, and the read tool was refused `.env` and `.env.local`.
  - **opencode as a host** for this server (2.2.5, free model, 2026-11-09): it loads and lists all 29
    tools, `opencode_status` ran with no approval flag, and the tools are reached through opencode's
    Code Mode as `await tools["agent-intern"].opencode_status()`, not as separate tools. The model
    said it had no `OFFER IT` instructions (one run). Details in
    [backends.md](backends.md#opencode-2).
  - Not checked on 2.x: `--auto`, `--agent`, `--fork`, a configured provider, macOS and Linux. The
    free tier answered `provider.free-limit` to every model for about 13 hours after the first
    round of probes, so the edit and host checks ran once it lifted, on 2026-11-09.
- ✅ **Copilot CLI and Cursor CLI load this server as a host.** Checked on 2026-11-05 on Windows with
  this branch's server (all 29 tools listed; `opencode_status` called, no quota spent; default
  models). **Copilot CLI 1.0.105:** `copilot mcp add <name> -- <command>` registers it (the per-run
  `--additional-mcp-config` takes the same JSON); `-p` called the tool with no approval flag, but the
  model only saw the `OFFER IT` server instructions with `--allow-all-mcp-server-instructions`.
  **Cursor CLI 2026.11.05:** a project `.cursor/mcp.json` registers it; a headless `-p` run needs
  **`--approve-mcps --force`** (without them every call came back *"User rejected MCP"*, and
  `--approve-mcps` alone was not enough), and the instructions reached the model. Two more hosts
  are recorded in this file: opencode 2.x in its entry above (the tools sit behind Code Mode and the
  instructions were not seen) and agy in the agy 1.5.1 entry below. In both runs here the user's own
  MCP config was left as it was ([opencode details](backends.md#opencode-2)).
- ✅ **Re-verified on agy 1.5.1, copilot 1.0.105, cursor-agent 2026.11.05 and opencode 1.19.5. One
  change touches the bridge: an expired `--print-timeout` now exits 3.** Run against this branch's code
  on Windows, 2026-11-05, on gemini-3.8-flash-high / gemini-4-argon-high and free opencode models only.
  - **agy 1.5.1** (two releases from 1.4.12). Held: ask, continue in the same conversation, one image
    (a valid PNG), a two-worker antigravity swarm, `agy models`, `/usage` and `/model`. The change is
    **1.5.0's**: a `--print-timeout` that cuts a turn short used to exit 0 with status `SUCCESS` (since
    1.2.0). It now **exits 3**, prints `AGY_ERROR: {"status":"DEADLINE_EXCEEDED","reason":"print_timeout",…}`
    and `[agy] print timeout after Ns…` on stderr, and writes a JSON result with status `ERROR` and
    `finished_reason` `print_timeout`. The bridge treated any non-zero exit as a plain failure before
    its timeout handling ran, so the "raise `timeout_s`" message and the conversation pin were lost
    (seen through `antigravity_ask`: *"agy exited 3 …"*). The timeout is now recognised first, and a
    6 s timeout through the branch's `_run_agy` raises *"agy hit its 6s timeout before finishing…"*. The
    partial text was empty in both runs, so the partial-answer path was not exercised again.
    Also: `-p` without `--print-timeout` now waits for background tasks instead of stopping at a
    hidden 30 minutes (the bridge always passes the flag). `agy models` still lists 13 slugs, all with
    an effort suffix, and **agy accepts a base name without one**: `--model gemini-4-argon` and
    `--model gemini-3.8-flash` both answered (a Claude base name was not tried). The default is still
    **Gemini 4 Argon (Medium)**, read with `/model` in a clean home directory; this machine's
    `settings.json` pins High. Not re-checked on 1.5.1: watch mode, `antigravity_image_swarm`, plan
    mode, `schema`. `agy acp`, `--remote-agent` and `/projects` are off the bridge's path.
  - **agy as a host.** Reported by the live Antigravity check, not rerun here: the agy CLI loads MCP
    servers from `~/.gemini/config/mcp_config.json` (add one with `agy mcp add`), not from
    `~/.gemini/antigravity/`; a headless run needs allow-rules to call them; the server's instructions
    do not reach the model; the Antigravity IDE is untested.
  - **copilot 1.0.105** (eight releases from 1.0.97). Every flag the bridge passes still parses.
    Ask, continue in the same session and a read-only run that was asked to write a file (it refused,
    nothing was created) all passed. 1.0.98 added Gemini 4 Argon models, 1.0.100 `--trust-folder` and
    `--disable-client-telemetry`, and there is a `copilot sandbox` subcommand; none is adopted.
  - **cursor-agent 2026.11.05** (from 2026.10.14). Ask, continue in the same chat and a refused
    read-only write passed. cursor.com's changelog lists nothing for the CLI in October or November.
  - **opencode 1.19.5** (from 1.19.0; 1.19.2 keeps 1.x installs on the 1.x line). Ask and continue in
    the same session answered; the read-only fence held (a shell write was auto-rejected, no file written) and
    workspace-write wrote. 1.19.5 runs a tool named `bash` as `shell` with the same permissions,
    which changed nothing here. A fenced run whose model then says nothing raises *"opencode produced
    no text"* rather than the refusal.
  - **Free models are fast now.** Six calls through the bridge, read-only: the default 4.8 s,
    `nemotron-3.5-lightning-free` 3.8 s, `mimo-v2.6-flash-free` 6.5 s, `deepseek-v4.1-flash-free` 3.1 s,
    `glm-5.3-flash-free` 3.4 s and `beam-free` 2.5 s, all answering. The free list is nine ids now;
    `mimo-v2.5-free` is gone. Once, after about 25 quick calls, the free tier answered
    `provider.free-limit` (HTTP 429), and later calls went through. This settles the 0.33.0 follow-up:
    the "slow, allow minutes" guidance described September and early October.
- ✅ **codex 0.166.0: an ask answered, and the project's `AGENTS.md` still reaches it. The rest was not
  re-checked, and `workspace-write` on Windows has a problem.** The README's verified version stays
  0.162.1, because the Codex quota ran out partway (it resets 2026-11-22). The machine's only codex
  was a standalone 0.153.4, below the version last verified; `codex update` brought it to 0.166.0.
  - **`AGENTS.md` (the 0.33.0 follow-up).** 0.150.0 stopped loading project `AGENTS.md` in "untrusted"
    projects. With the bridge's flags (`exec -s read-only -C <dir> --skip-git-repo-check`), in a
    directory outside any git repository and with no trust entry, the model returned a token that
    exists only in that directory's `AGENTS.md`, and the session's rollout shows the file injected.
    Not tried: a directory explicitly marked `trust_level = "untrusted"`, and a git repository.
  - **`-s workspace-write` ran as read-only.** On this Windows machine `codex exec -s workspace-write`
    prints `sandbox: read-only` in its header, and any `-c windows.sandbox=…` makes it
    `workspace-write`. `codex sandbox` itself writes inside a workspace and is refused outside it
    ([details](backends.md#codex-bridge)). Whether the old "every command refused" caveat has the same
    cause is a hypothesis; the test that settles it needs a model call and is written down there.
  - The changelogs from 0.163.0 to 0.166.0 hold nothing else on the bridge's path: resuming restores
    the session's permissions, `codex exec` stopped repeating a diff, GPT-6.1 Terra joined the model
    catalog. `exec resume`, `-o`, `--json` and `--skip-git-repo-check` still parse.
- ✅ **Re-verified on agy 1.4.12. No break, but agy's default model moved.** Fourteen releases from
  1.3.5. These held, live on Windows: ask on the default model, continue in the same conversation,
  an image, a two-worker swarm, `--model gemini-4-argon-high`, and `/usage` and `/changelog`, which
  the CLI still answers itself without starting a conversation. Two changelog entries touched the
  bridge's path:
  - **1.4.7:** headless runs no longer "render the transcript for display". The JSONL transcript is
    still written: the swarm workers, which read nothing else, answered.
  - **1.4.11:** the json and stream-json results gained `finished_reason`, present only when a turn
    ended early: `print_timeout`, `interrupted` or `error`. An expired `--print-timeout` still exits
    0 with status `SUCCESS` and the same stderr line, so the bridge's check already held; it now
    reads the field too, and treats `interrupted` and `error` as a cut-short answer, raising with
    the partial text attached rather than returning it as whole. Only `print_timeout` was seen
    live; the other two follow the changelog.

  The catalog moved again, with no changelog entry until 1.4.3 fixed it for `GEMINI_API_KEY`
  users: a **`gemini-4-argon`** family (low, medium, high) arrived, and with no model in
  `settings.json` agy 1.4.12 picks **Gemini 4 Argon (Medium)**, checked in a clean home directory.
  `/usage` gained a matching `Gemini 4 Argon` family.
- 🐛 **A quota failure didn't say which model family was out
  ([#7](https://github.com/SinanTufekci/agent-intern/issues/7)). Fixed.** agy counts quota per
  family (Gemini 4 Argon, the other Gemini models, Claude and GPT), but a call that hit one came
  back as a bare `agy exited 1 … Individual quota reached`. A calling agent read that as "agy is out
  of quota" and moved the work onto its own tokens, when another family would have answered. Now
  a failed run whose output reads like a quota or rate-limit error gets agy's own `/usage` table
  appended (free to read), names the family the call drew on, and suggests a model from a family
  that still has quota. This covers `antigravity_ask`/`_continue` with and without `watch`, and
  antigravity swarm workers; image workers used to report such a failure as "no image file
  produced" and now pass agy's error on. Checked against the live `/usage` table and model list:
  every one of the 13 models maps to its family. A real exhausted family was not available to test
  against, so the 0% case was checked by editing the live table.
- 🐛 **`antigravity_status` reported `newest transcript [!!]` while everything worked
  ([#8](https://github.com/SinanTufekci/agent-intern/issues/8)). Fixed.** The row read only the
  single newest conversation, which is often unfinished: empty after a self-update, still running
  in another call or swarm worker, or a failed turn with no answer. It now reports the newest
  conversation that has a readable answer and says how many newer ones it skipped. It fails only
  when none of the ten newest is readable and at least one of those is neither empty nor touched
  in the last five minutes. Run against the same live brain dir, the old check reported `[!!]`
  for the newest conversation, a turn with no answer, and the new one reports
  `readable (skipped 2 newer …)`.
- 🐛 **opencode's free tier refused every `read-only` call. Fixed.** Since early October 2026 the
  free tier has rejected any request that lacks the `bash` tool, with *"OpenCode's free tier can
  only be used from within OpenCode."* `read-only`, the default, denied `bash`, and a denied tool is
  left out of the request, so every keyless `opencode_ask` failed in about 3 s. The check is on
  the server, so 1.18.29 is hit as well as 1.19.0. `read-only` now sets `bash` to *ask*, which a
  headless run rejects. Verified live on both versions: told to write a file through `bash`, the
  model got *"rejected permission to use this specific tool call"* and nothing was written. Free
  models also got much faster: 4–6 s for a short answer on 1.19.0, down from 152–260 s.
- ✅ **Re-verified on codex 0.162.1, copilot 1.0.97 and opencode 1.19.0.** Each answered an ask, a
  continue in the same session, and a read-only file read, with the worktree untouched.
  - **codex:** the Windows read-only caveat still holds on 0.162.1. Three commands were refused and
    the bridge's warning fired. Only `read-only` was re-checked. The upstream issues are still
    open. One bridge bug turned up: from 0.149.1 on, codex records turns as `item_completed`
    items, so the watch view of `codex_continue` had shown no earlier turns. Fixed.
  - **copilot:** 1.0.85 moved argument parsing to a Rust grammar, and every flag the bridge passes
    still parses. Under `read-only` a file write was refused.
  - **opencode:** besides the fix above, 1.19.0 stopped installing updates on its own.
- ✅ **Re-verified on cursor-agent 2026.08.11 and 2026.10.14, and `cursor_status` now catches an
  expired login.** On both, ask, continue in the same chat and a read-only file read all answered.
  Under `read-only` (ask mode) a file write and a shell command were both refused, and nothing was
  written. Getting there
  took a fresh login: the saved one had expired, but `cursor-agent status` still printed *"Login
  successful!"* while every `-p` run failed with *"Authentication required"*, and
  `cursor_status` reported that as ready. It now flags it. The first call after logging in again
  timed out in `create-chat`, which the bridge gives 30 s; afterwards it took 0.4 s every time.
- 🐛 **On Windows, an npm-installed codex or copilot couldn't take most prompts. Fixed.** npm
  installs both as a `.cmd` shim that runs a node script. Launched by bare name, the shim isn't
  found at all, because Windows looks only for a `.exe`. Pointed at by `CODEX_BIN` or
  `COPILOT_BIN`, it runs through `cmd.exe`, so the
  [shim check](security.md#windows-batch-file-shims-and-the-prompt) refused any prompt containing
  `" % ! ^ & | < >`, which covers nearly every diff. Both scripts only find and start a native
  binary, so the bridge now starts that binary itself:
  - codex: `@openai/codex-win32-<arch>\vendor\<triple>\bin\codex.exe`, with the two update-hint
    variables `codex.js` sets.
  - copilot: `@github/copilot-win32-<arch>\copilot.exe`.

  Verified on codex 0.162.1 and copilot 1.0.97, with the shim named in `*_BIN` and with bare names
  and the shims first on `PATH`: a prompt containing `"`, `&` and `%` answered through both. The
  standalone codex installer and the winget copilot are native `.exe`s and launch as before.
- ✅ **Re-verified on agy 1.3.5. No code change was needed, but the model catalog moved again.**
  Thirteen releases from 1.2.10, and nothing on the bridge's path broke. These held: ask on the
  default model, continue in the same conversation, a swarm worker, and the `/usage` table. Two
  changelog entries looked like risks and turned out not to be:
  - **1.3.2:** `--dangerously-skip-permissions` stopped approving plans in `/plan` mode. `plan=true`
    still answers headlessly (58 s), writes its plan and leaves the worktree untouched.
  - **1.2.14:** `--json-schema` now refuses a schema whose root isn't `"type": "object"`. agy
    exits 1 before any model call, with a one-line reason the bridge passes on.

  The catalog is the drift. **No changelog entry mentions it**:
  - Gone: `gemini-3.7-flash`, `gemini-3.6-flash`, `gemini-3.1-pro`, `claude-sonnet-4-6` and
    `claude-opus-4-6-thinking`.
  - New: `claude-opus-5-5` and `claude-sonnet-5-5`, each with low, medium and high variants.
  - The default stays Gemini 3.8 Flash (High).

  The docs' examples now name live slugs. `claude-sonnet-5-5-high` passes the bridge's check and
  reaches agy, but its answer is unverified: the test account's Claude quota was spent.
- 🐛 **agy 1.2.0+ passed a timed-out answer off as a finished one — fixed, and re-verified on agy
  1.2.10.** 1.2.0 changed what an expired `--print-timeout` does: instead of failing, agy returns
  whatever it has written so far, exits 0 and reports `"status":"SUCCESS"`. The only sign is one
  stderr line (*"print timeout after 20s with turn in progress; returning partial output"*). The
  bridge passes `timeout_s` as that flag and trusted the exit code, so a long task that ran out of
  time came back as an ordinary answer that simply stops. Reproduced through the bridge before the
  fix: 5629 characters ending *"…when an artisan"*, returned as the answer. Now a cut turn is an
  error again, as it was before 1.2.0, and the partial text comes along in the message, labelled as
  cut off, rather than being thrown away. Verified live on `antigravity_ask`, watch mode and a swarm
  worker. The fix doesn't suggest `antigravity_continue` to finish the answer: the conversation
  resumes, but the history agy keeps for the cut turn isn't the text it printed (asked for its last
  words, it quoted a sentence from well before where the output stopped).

  Everything else held across the eleven releases from 1.1.25: ask on the default model and on an
  explicit `--model`, continue pinning the same conversation, the `--output-format json` object, the
  transcript read, `agy models` (unchanged catalog) and the `/usage` table.
- 🆓 **opencode is the first backend verified end-to-end without a subscription.** Its free hosted
  models answer with `0 credentials`, so the answer path, the session-id resume, the per-directory
  `-c` scoping and the failure envelopes were all *observed* on opencode 1.18.29 rather than taken
  from docs — and the flags that couldn't be observed were read off opencode's own bundled source
  instead of guessed. Two caveats worth knowing before you reach for it: the free models are **slow**
  (152–260 s for a one-word answer, hence the 300 s default timeout), and its `sandbox` is
  **agent-enforced** — real, portable across OSes, and still not the kernel boundary Codex gives you.
  [Full detail →](backends.md#opencode-bridge)
- 🐛 **A headless timeout could hang the call on Windows — found while building the opencode bridge,
  and fixed there.** `opencode` on `PATH` is npm's `opencode.CMD` shim, so the real `opencode.exe` is
  a *grandchild*: `subprocess.run(timeout=...)` kills the shim, then its Windows branch re-reads a
  pipe the surviving grandchild still holds. Measured: a **270 s timeout returned after 396 s**, and
  only because the orphan was killed by hand. The opencode bridge now runs its blocking path through
  `Popen` and kills the whole tree (`taskkill /T`); re-measured, a 630 s timeout returns at **630.1 s**
  with no orphan left behind. The other npm-shim backends share the shape of this bug and have not
  been re-measured — worth knowing if you ever see a `*_ask` outlive its own timeout.
- 🧪 **Grok Build and Kimi Code ship UNVERIFIED — help wanted.** Two new backends, neither ever
  exercised against an authenticated account, because I have neither subscription. This is a
  deliberate trade: shipping them unverified is the only way anyone *can* verify them, and the parts
  that historically rot — flag surfaces, model ids, error shapes — are pinned down live against
  grok 1.0.3 and kimi 0.29.1. Grok's are unusually solid, since its CLI is
  [open source](https://github.com/xai-org/grok-build): every argv the bridge builds was read off the
  real clap definitions and confirmed to parse. What's unproven is everything behind each CLI's auth
  wall — the answer itself, resume, and Grok's streaming events. That verification gap already caught
  one live docs error (xAI documents `grok-build` as the model; the real default is `grok-4.5`), which
  is a fair warning about what else the docs may be wrong about. **If you have either subscription,
  [one issue from the verification template](https://github.com/SinanTufekci/agent-intern/issues/new?template=backend_verification.yml)
  closes this gap.** [Full detail →](backends.md#experimental-backends)
- 🐛 **`agent_swarm` antigravity workers died with "authentication timed out" on macOS — fixed**
  ([#2](https://github.com/SinanTufekci/agent-intern/issues/2)). Each swarm worker gets an isolated
  `HOME` so agy's per-process state can't collide. The module shipped asserting that auth survives
  it because agy reads credentials from the OS credential store — **true on Windows** (Credential
  Manager is keyed to the user session; re-verified on 1.1.12 that `agy models` inside a fake HOME
  returns the full list) and **false on macOS**, where the login keychain is resolved through
  `$HOME`. So every antigravity worker started a fresh OAuth flow and died at agy's 60 s
  `authentication timed out`, while `antigravity_ask` — which never touches HOME — kept working.
  Isolation is now conditional, decided two ways:
  - **Proactively**, by probing once per process: `agy -p "/usage"` inside a throwaway isolated
    HOME. On agy 1.1.11+ that's free (the CLI answers it, no agent turn, no quota), but the quota
    table comes from your account, so it can't be answered without working credentials. Skipped on
    Windows, where it can't fail.
  - **Reactively**, because a probe is a proxy and this one can't be tested on the platform it
    exists for: any worker that fails with an authentication signature flips the process to
    serialized mode and **retries itself there**, so you get an answer even if the probe was wrong
    or unavailable.

  In serialized mode the antigravity workers run in your real HOME behind the same lock the
  single-agent tools use — correct everywhere, at the cost of the parallelism (other backends stay
  parallel; a watched worker shows a note instead of live steps, since the step feed reads the
  isolated transcript). `AGY_BRIDGE_NO_HOME_ISOLATION=1` forces it without a probe. Diagnosed by
  inspection from the report; **not reproduced on a Mac** — if you're on macOS, please confirm on #2.
- 🐛 **agy 1.1.11 killed the `model` argument — fixed** ([#3](https://github.com/SinanTufekci/agent-intern/issues/3)).
  agy made `agy models` machine-readable,
  turning each line from a bare slug into a **tab-separated `<slug>\t<human label>` record**
  (`gemini-3.6-flash-high\tGemini 3.6 Flash (High)`). The bridge read the whole line as the slug, so
  its up-front validation rejected **every valid model** with an error that listed the very slug it
  had just refused: `unknown agy model 'gemini-3.6-flash-high'; expected one of:
  gemini-3.6-flash-high<TAB>Gemini 3.6 Flash (High), …`. Reproduced end-to-end through
  `antigravity_ask`; `model` was unusable on all three antigravity tools (omitting it still worked —
  the default model path never touched this code). The parser now keeps the first tab field, which
  reads both formats, and drops any field containing whitespace (a slug never has a space, so such a
  line is chatter). Note 1.1.12's changelog advertises `--output-format json` for the `models` and
  `agents` subcommands, but **the shipped binary has no such flag** (`agy models --output-format
  json` → `flags provided but not defined: -output-format`), so TSV is what there is to parse. The
  live slug list is unchanged from 1.1.6, and this suite stayed green through the break because
  every model test mocked the old format — the new tests pin **both** formats down. The change is
  in no changelog entry: the guard test was green on 1.1.10, #3 reports the break on **1.1.11**,
  and it was reproduced here on 1.1.12.
- ✨ **`antigravity_status` now reports your remaining AI Pro quota — for free.** agy 1.1.11/1.1.12
  answer read-only slash commands **in print mode itself**: no agent turn, no quota spend, no
  conversation left behind (1.1.11: `/usage`, `/quota`, `/credits`, `/model`, `/effort`, `/skills`;
  1.1.12: `/permissions`, `/hooks`, `/help`, `/config`, `/changelog`). The status tool runs
  `agy -p "/usage"` and adds a row per model family — `quota: Gemini Models [ok] Weekly 100%, Five
  Hour 100%` — flagging a family at **0%** as a problem, since every call against it fails until its
  window resets. Version-gated at 1.1.11 as a *safety* gate: on older agy the same argv is a prompt,
  so a diagnostic advertised as free would quietly spend a call. The probe is the one bridge call
  that deliberately **omits** `--disable-slash-commands` (a regression test asserts it), and it
  degrades to nothing on older agy rather than reporting a false problem.
- 🐛 **codex's Windows sandbox refuses every command — and codex answers anyway.** Re-verifying the
  bridge against **codex 0.149.1** (from 0.144.1) turned up the worst failure shape there is: one
  that reports success. Under **both** `read-only` and `workspace-write`, every command codex tried
  came back `rejected: blocked by policy` — down to `pwd` — because the policy engine can't classify
  the `pwsh -Command <...>` wrapper codex itself builds. Shell commands are how codex reads files, so
  it saw nothing of the workspace. It did not say so. Asked for the version in a local
  `pyproject.toml` declaring `0.27.0`, it ran a **web search** and answered **`1.2.0`**, a version
  from an unrelated GitHub repository; a second run said `0.1.0`. With the sandbox off, the same
  prompt answered `0.27.0`. Exit 0 and a full `-o` file every time. Not local: there is no exec
  policy in this machine's `config.toml`, and it is open upstream
  ([#40060](https://github.com/openai/codex/issues/40060),
  [#38886](https://github.com/openai/codex/issues/38886)).

  The bridge can't fix a CLI that reports success, but it no longer passes the result off as sound:
  any answer whose run had commands refused now comes back with a visible `[agent-intern] WARNING`
  naming the count and the policy. It **appends rather than raises** on purpose — under `read-only` a
  model that tries to write is *supposed* to be blocked, and that run's answer is perfectly good;
  what was unacceptable was the silence.

- 🐛 **`codex_continue` was broken outside a git repo — fixed.** `codex exec resume` enforces the
  trusted-directory check just like a fresh run, and the bridge passed `--skip-git-repo-check` only
  on fresh runs, on the reasoning that resume inherits the session's recorded cwd and sandbox. So a
  continue in a plain directory died with *"Not inside a trusted directory and
  --skip-git-repo-check was not specified"* — while the fresh ask that created that very session had
  just succeeded. It only bit outside a git repo, which is why a green hermetic suite and everyday
  use inside a project never saw it; the test that covered this argv actively asserted the flag was
  absent. `codex exec resume --help` lists it, so the fix is the supported one. Re-verified live: ask
  then continue in a non-git workspace now both answer.

- ✅ **Re-verified copilot on 1.0.80 (from 1.0.69) — eleven releases, nothing to change.** The one
  entry that could have reached this bridge was 1.0.71's *"reject malformed `--allow-tool` and
  `--deny-tool` patterns with an error message"*, since read-only mode **is** a pair of `--deny-tool`
  patterns. Re-verified live on 1.0.80: they still parse, a workspace file read answers, and a write
  is still refused (*"Blocked: I can't write files in this environment"*, no file created). 1.0.79's
  BREAKING rename of the sandbox setting `allowDevToolCaches` → `allowDevToolAccess` is a config key
  this bridge never reads.

- ✅ **`agent_swarm` honours `sandbox` on Antigravity workers — it used to ignore it.** Every other
  backend took a per-task `sandbox` policy; Antigravity dropped the key on the floor, so
  `{"backend": "agy", "sandbox": "read-only"}` was a task that *looked* fenced and ran with nothing
  holding it back. Plan mode is the first thing agy has that can honour the request, so `"read-only"`
  now maps onto it, `"danger-full-access"` names the unrestricted posture out loud, and
  `"workspace-write"` **raises** rather than implying a scoping agy cannot do.

  Verified live on 1.1.20 in the **parallel isolated-HOME path** — the one that builds its own argv
  rather than going through `_run_agy`, so it had to be threaded explicitly — with both workers in a
  single swarm running the same `cmd /c echo RAN > <absolute path>`: the `read-only` worker created
  nothing and returned a plan document, the unfenced one created its file. The version gate and the
  slash-command guard run at task-normalization time, so a bad plan request fails the whole swarm up
  front rather than N calls in.

  Two deliberate omissions. **An omitted `sandbox` still leaves an agy worker unrestricted**, unlike
  every other backend: flipping that default would silently turn existing file-writing swarm tasks
  into plan documents. And **`schema` is not wired into the swarm at all** — both Antigravity swarm
  paths read the answer from the isolated *transcript*, where `structured_output` does not exist, so
  it would mean moving the most load-bearing read path in the swarm onto JSON stdout. The
  structured-output caveat below is the other half of that reasoning: a schema that follows field
  order rather than content, run across N parallel workers, is N confidently wrong answers.

- ⚠️ **`schema` returns structured output — and agy fills it in a pass that doesn't re-read the
  question.** Passing a JSON Schema to `antigravity_ask` / `antigravity_continue` maps to agy 1.1.8's
  `--json-schema`, and the tool returns agy's `structured_output` — exactly the declared fields, as
  JSON text you can `json.loads`. Note the prose `response` on the same run is **not** the same
  thing: it carries the model's raw emission, agy's internal `toolAction` / `toolSummary` keys, and
  sometimes a sentence of prose ahead of the JSON. A run that yields no structured output **raises**
  rather than handing you prose to parse.

  The caveat is worth more than the feature. agy populates the schema in a **finishing pass that does
  not reason about the content again**, so any field the turn never actually established gets guessed
  from the shape of the schema. Measured on 1.1.20, classifying *"this broke my build and wasted my
  whole afternoon"*:

  | prompt | schema | result |
  |---|---|---|
  | plain classify request | `enum: ["positive","negative"]` | **positive** — 3 runs out of 4 |
  | plain classify request | same enum, order **reversed** | **negative** — 2 of 2 |
  | same, plus a `reason` field | `reason` first, then the enum | reason came back *"Completed sentiment classification task."* |
  | "state the verdict and why, then report it" | the **original, biased** enum | **negative** — 3 of 3 |

  It was following field order, not the sentence. So use `schema` to **shape an answer the turn has
  already worked out** — extraction, formatting, pulling fields out of something the model just read
  — and do not delegate the judgment itself to it. Ask for the reasoning in the prompt; the schema is
  the envelope, not the thinker.

- ✅ **Re-verified on agy 1.1.21–1.1.25 — the only drift was the model catalog, and it moved in
  *both* directions.** Five releases, re-checked live on Windows. Plumbing all held: `antigravity_ask`
  round-tripped through the real argv on both the default-model path and an explicit `--model`, the
  `--output-format json` object still carries `conversation_id` / `status` / `response`, `agy models`
  still emits `<slug>\t<label>`, the `/usage` quota table still parses, and the JSONL transcript read
  path still resolves. What changed is data, not code:
  - **`gemini-3.8-flash` arrived and took the default** (`gemini-3.8-flash-{low,medium,high}`). Its
    changelog entry scopes it to "when connecting with a `GEMINI_API_KEY`", but it is in the catalog
    on ordinary AI Pro browser auth too, and `--model gemini-3.8-flash-high` round-tripped clean.
  - **The whole `gemini-3.5-flash` family was dropped, and no changelog entry says so.** 1.1.22 was
    still naming 3.5 Flash in a fix, so this is the same class of silent catalog change as the
    1.1.11 TSV break — every 3.5 example in these docs had quietly become a guaranteed rejection.

  Nothing broke, because validation reads the live list rather than a baked-in one; the two guard
  tests added after the last drift are what fired, one per direction. Two upstream fixes also landed
  on shapes this bridge depends on, both moving toward it: **1.1.23** fixed subcommands such as
  `models` hanging on an inherited, unclosed stdin — the exact hang `list_agy_models` has always
  spawned with a closed stdin to dodge — and **1.1.24** fixed headless runs with piped stdout/stderr
  hanging *on exit*, which is every call this bridge makes. One false alarm worth knowing: agy leaves
  an **empty conversation directory** behind when it self-updates, and `antigravity_status` reports it
  as `newest transcript [!!]`. The read path is fine — the next real call writes a transcript and the
  row goes green. `VERIFIED_AGY_VERSION` → `(1, 1, 25)`.
- ✅ **Re-verified on agy 1.1.13–1.1.20 — no code change needed, and two upstream fixes moved
  *toward* the bridge.** Eight releases of drift, re-checked live on Windows: `antigravity_ask` and
  `antigravity_continue` both round-tripped through the real argv (the continue pinned its
  conversation and recalled a codeword), agy's `--output-format json` object still carries
  `conversation_id` / `status` / `response`, `agy models` still emits `<slug>\t<label>`, the `/usage`
  quota table still parses into per-family rows, and all three version gates
  (`supports_json_output`, `supports_disable_slash_commands`, `supports_print_usage`) resolve true.
  The two changes that touch this bridge both make it *more* correct:
  - **1.1.18 made a valueless `-p` and a stray trailing argument hard errors.** That is precisely the
    mis-parse the bridge is built to avoid — `--print --sandbox 'do the task'` used to run with the
    prompt `--sandbox` and the sandbox silently off. The bridge already passes the prompt as `-p`'s
    value and already appends `-p` last, so nothing changed here except that agy now enforces the
    rule instead of failing quietly. Re-verified with a prompt whose first character is a dash.
  - **Print-mode exit codes got honest, in both directions.** 1.1.18 made a dropped agent stream exit
    non-zero rather than reporting a clean success with an empty response; 1.1.20 stopped treating
    benign tool errors and permission denials as fatal. The bridge raises on any non-zero exit, so
    both edits sharpen a signal it was already trusting.

  Also worth knowing, though nothing here had to move: **1.1.13 fixed two long-standing hazards in
  exactly the code path this bridge falls back to** — transcript corruption when a background message
  appended while context compaction was rewriting it (which left JSON that no longer parsed) and
  unbounded growth of the on-disk conversation database. That fallback is the bridge's most fragile
  read path, and it is now sturdier upstream. **1.1.14 and 1.1.20 also shifted agy's default
  permission posture** (workspace reads auto-granted under the default review mode; access outside
  the workspace narrowed to read-only) — `--dangerously-skip-permissions` stays load-bearing, so the
  [Security](security.md#security) note is unchanged.

  **Docs-only drift, now fixed:** agy grew the **`gemini-3.7-flash`** family and moved the default
  onto it, so an untouched install runs `gemini-3.7-flash-high`, not the `gemini-3.6-flash-high` every
  doc here named (verified in a throwaway HOME with no `settings.json` at all). Nothing broke —
  validation reads the live list — but the guard test only ever noticed a model agy *dropped*, which
  is why a whole new family and a moved default sailed through a green suite. A second guard now
  fails when agy offers a family the docs don't mention. `VERIFIED_AGY_VERSION` → `(1, 1, 20)`.
  Not adopted, and not exercised beyond confirming agy still lists them: `--json-schema`,
  1.1.15's `--input-format stream-json` (one process, many turns), `--mode accept-edits|plan`, and
  1.1.16's `agy mcp` subcommands. Nothing here needs them today.
- ✅ **Re-verified the rest of agy 1.1.11/1.1.12 — nothing else broke.** The **slash shield still
  holds**: 1.1.11 replaced the silent fall-through for interactive-only commands with an explicit
  refusal that recommends the exact flag this bridge already passes (`agy -p "/clear"` → exit 2,
  *"pass --disable-slash-commands to send /clear to the model as literal text"*), and
  `antigravity_ask("/clear Reply with the single word BRIDGE…")` returned `BRIDGE` end-to-end. The
  read-only set is why the shield stays load-bearing — unshielded, a prompt opening with `/model`
  would get agy's table instead of an answer. 1.1.12 also stopped swallowing startup diagnostics
  (including the `--conversation` not-found warning the continue path can trigger): they go to
  **stderr**, so stdout stays a pure JSON result object — verified with a deliberately bogus
  `--conversation` (exit 0, clean JSON, warning on stderr). Benign wins: a **Windows** crash
  resolving the conversation transcript path is fixed (the artifact watch mode's `log_uri` points
  at), headless `-p` now settles a choice itself instead of stalling on a question nobody can answer
  (fewer timeouts), and 1.1.11 made retries honor the server's retry delay and stopped an empty
  credits response reading as "Out of credits". `VERIFIED_AGY_VERSION` → `(1, 1, 12)`. `--effort`
  stays unadopted, now with a harder reason: it isn't universal — `--model claude-sonnet-4-6
  --effort low` fails with *"--effort is not supported for model"*, while the gemini slugs already
  bake the level in. Everything else (Vim editing mode, artifact-viewer polish, plugin enablement,
  admin controls and MCP progress callbacks — agy as an MCP *client*) is off the bridge's path.
- 🛡️ **agy 1.1.9 broke print mode for any prompt starting with a slash — fixed by
  `--disable-slash-commands`.** 1.1.9 made `-p` **expand slash commands and skills** instead of
  sending them to the model as text, so a prompt whose first token names a registered command is
  *executed as that command and never reaches the model*. Verified live on 1.1.10 through this
  bridge: `antigravity_ask("/help")` came back with agy's own help page, not an answer. That is not
  just wrong output — agy's registered set includes **side-effecting** commands (`/goal` starts an
  autonomous long-running task, `/schedule` creates cron jobs), and bridge prompts routinely carry
  text the caller did not author, so an untrusted string beginning `/schedule …` would have run it.
  Every agy argv path now passes `--disable-slash-commands` (one change in `_agy_base_args` covers
  ask, continue, both watched runners, and both swarm workers). Version-gated: the flag doesn't
  exist before 1.1.9, and neither does the expansion. Prompts starting with a POSIX path
  (`/etc/hosts …`) were never affected — they match no command — but that was luck, not a boundary.
  Set **`AGY_BRIDGE_ALLOW_SLASH_COMMANDS=1`** to keep the expansion if you *want*
  `-p "/my-skill <args>"` to invoke a skill.
- 🐛 **Non-ASCII answers were being mangled on Windows — fixed.** Every backend emits UTF-8, but the
  bridge spawned them with bare `text=True`, which decodes using the **locale** codepage
  (`locale.getpreferredencoding()` — cp1254 on a Turkish Windows, cp1252 elsewhere). Any non-ASCII
  answer came back corrupted: `dosyası` arrived as `dosyasÄ±`, exactly
  `'dosyası'.encode('utf-8').decode('cp1254')`. Every bridge plus the swarm workers now decode
  UTF-8 explicitly with `errors="replace"`, the pattern `cursor_bridge.py` already used. A
  regression test asserts no subprocess call reintroduces bare `text=True`. ASCII-only answers were
  never affected, which is why this survived so long.
- ✅ **Re-verified on agy 1.1.9 and 1.1.10.** Beyond the slash-command break above: 1.1.10 fixed
  `--model`/`--effort` being **silently ignored in headless `-p`** (they were applied after model
  configuration had already initialized, so the run fell back to the persisted/default model). The
  bridge validates and passes `--model` on every call, so on **1.1.8–1.1.9 the `model` argument was
  a no-op** even though a typo was still correctly rejected — if you pinned a model in that window,
  you were served the default. Re-confirmed working on 1.1.10 through the bridge
  (`model="claude-sonnet-4-6"` → a Claude answer, not Gemini). No code change was needed for it.
  1.1.10 also added a non-blocking advisory banner when the same conversation is open in another CLI
  instance — the shape `antigravity_continue` and the swarm can produce — so `_parse_json_result`
  now **locates** the result object instead of requiring stdout to *start* with `{`; leading and
  trailing chatter are both absorbed rather than degrading into a raw JSON blob in your answer.
  `VERIFIED_AGY_VERSION` → `(1, 1, 10)`. Nothing else in 1.1.9/1.1.10 reaches the bridge — the rest
  is interactive-TUI, hooks, auth, and MCP-*client* work.
- ✅ **Verified on agy 1.1.7 and 1.1.8 — nothing broke, and 1.1.8 made the bridge sturdier.** 1.1.8
  gave print mode an `--output-format` flag (`text` | `json` | `stream-json`). The existing text path
  was confirmed live on 1.1.8 first (ask, pinned continue, and `--model` all clean), then the bridge
  switched its plain ask/continue calls to `--output-format json`, because reading a contractual
  `response` field beats trusting the layout of bare text. The real prize is the `conversation_id`
  agy returns with it: the bridge records it and **pins a later `antigravity_continue` to exactly the
  conversation it last ran in that workspace**, instead of inferring it from `last_conversations.json`
  — shared state agy rewrites for *every* session, including your own interactive TUI work in the same
  folder. Practical difference: `antigravity_continue` now resumes *the bridge's own* thread, where
  before it could land on a conversation you'd since started in the Antigravity TUI. Older agy is
  unaffected — the flag is version-gated (pre-1.1.8 has no such flag), and any non-JSON stdout falls
  back to the previous text path, so a silently-ignored flag degrades instead of crashing.
  `VERIFIED_AGY_VERSION` → `(1, 1, 8)`. Not adopted: `--json-schema` (works; nothing here needs it).
  Nothing else in 1.1.7/1.1.8 reaches the bridge — the rest is interactive-TUI, plugin-hook, and
  MCP-*client* work.
- ✅ **[Watch mode](watch-and-swarm.md#watch-mode) reads agy's live event stream instead of scraping its transcript.**
  On agy 1.1.8+ the watched runners request `--output-format stream-json` and consume agy's typed
  `init` / `step_update` / `result` events straight off stdout. Verified before the rewrite that they
  arrive **incrementally** (a 17 s run spread its 18 events over 12.4 s), and confirmed live that a
  watched run's step count grows while agy works. The stream carries the real command as a nested
  object (the transcript stored tool args JSON-encoded *inside a string*), streaming text fragments,
  and a `conversation_id` — so a watched run now pins later continues just like a plain one. This
  retires the timer-based transcript polling, which matters beyond tidiness: agy has announced JSONL
  is being replaced by SQLite, and watch was the last path that would have broken when it goes.
  Pre-1.1.8 agy keeps the original transcript path, re-verified live.
- ⚠️ **Behavior change: multi-step answers now include the model's narration.** agy's `response` is
  the whole turn; the old transcript scrape returned only the last planner response. Identical for a
  single-step ask, different for a chatty multi-step one (one measured run: 297 chars vs 128, the
  full answer *ending in* the old one). The full turn is now returned on every path — `response` is
  agy's own contract for what the turn produced, and the old last-step rule silently dropped content
  whenever the model did the work and then closed with a short "Done."
- ✅ **Re-verified on agy 1.1.6 — no code change needed.** 1.1.6 added the `gemini-3.6-flash` family
  to `agy models` and moved the `settings.json` default to **Gemini 3.6 Flash (High)**; the default
  path and `--model gemini-3.6-flash-high` both round-tripped clean, and the JSONL + SQLite read paths
  still match agy's unchanged conversation schema. Its one bridge-adjacent fix — print mode now
  surfacing the real conversation-creation error instead of a misleading "no active conversation" —
  only improves the diagnostic the bridge already reads on failure. Everything else (Markdown custom
  agents, `/copy` and `/codesearch` polish, background-task hardening) is interactive-TUI or
  client-side work that doesn't reach the bridge. Docs-only: the model list and default examples now
  name the 1.1.6 slugs, and the guard test advertises `gemini-3.6-flash-high` against the live list.
- ⚠️ **Verified on agy 1.1.5 — it renamed every model, so old `model` values now fail.** 1.1.5
  replaced agy's human-readable model labels with stable slugs, and `agy models` reports only those:
  `"Gemini 3.1 Pro (High)"` is now `gemini-3.1-pro-high`, and the Claude entries are
  `claude-sonnet-4-6` and `claude-opus-4-6-thinking` (the mapping is not 1:1 — check
  `agy models`, or `antigravity_status`, for the current list). Since the bridge validates `model` against
  `agy models`, an old label is **rejected up front** with the valid list — you lose the call, not
  your money, and never silently run on the wrong model. Pass slugs and you're fine. Nothing in the
  bridge's machinery needed changing (validation was always format-agnostic — which is exactly why
  the entire test suite stayed green while every *documented example* went stale), so this release is
  docs plus one new test that checks the models we advertise against the live `agy models` list.
  Everything else in 1.1.5 is interactive-TUI, MCP-client, or background-task work that doesn't reach
  the bridge; its new `--effort` flag is a second axis we don't pass, because the slug already pins
  the effort variant.
- ✅ **Verified on agy 1.1.4** — no code change was needed. 1.1.4 relaxed the 1.1.3 headless gate so
  that `-p` now **honors your persisted `settings.json` policies** (permissions, file access, sandbox
  mode, auto-execution, artifact review) instead of blanket-denying. `--dangerously-skip-permissions`
  still overrides those policies, so the flag stays load-bearing and stays exactly where it is —
  re-verified live against a workspace deliberately **absent** from `trustedWorkspaces`, with a
  `permissions.allow` list naming neither file nor command access: a workspace file read returned the
  right contents, and a terminal command and a file write both executed. Worth knowing: that flag is
  now the only thing between a bridge call and your own `settings.json` policy, and dropping it would
  get you whatever that file says rather than 1.1.3's deny-everything. 1.1.4 also stopped `/btw`
  side-questions from leaking into the conversation list as duplicates carrying the *parent's* title —
  that list is what conversation pinning reads, so one way to resume the wrong thread is gone.
- ✅ **Verified on agy 1.1.3** — base dir, `last_conversations.json` (still keyed by workspace path),
  the `brain/.../transcript.jsonl` path, the transcript schema, and the `-p`/`-c`/`--print-timeout`
  flags are all unchanged; a live `antigravity_ask` + conversation-pinned `antigravity_continue`
  round-trip returns clean over stdout and `antigravity_status` diagnostics pass. **1.1.3 broke and
  the bridge fixed** the one thing that mattered: headless `-p` no longer auto-approves tool calls,
  it **soft-denies** them (print mode cannot prompt), so without a flag even "read `pyproject.toml`
  and report the version" returned nothing — exit 0, empty stdout, the reason only on stderr. The
  bridge now passes `--dangerously-skip-permissions` on every agy path, which restores file writes,
  terminal commands and workspace reads (a live bridge round-trip reads this repo's real version
  again). The flag **must precede `-p`**, whose *value* is the prompt — otherwise the flag *becomes*
  the prompt and the task is silently dropped. **1.1.2** also made an unresolvable `--model` hard-fail
  in `-p` instead of silently falling back to the settings.json default (the bridge's `validate_model`
  still rejects a typo up front, without spending a call). **1.1.0's** execution-mode system
  (`--mode`, `request-review`) remains a no-op for the bridge: `-p` is spawned with DEVNULL stdin, so
  that interactive gate never engages. `--sandbox` behavior is likewise unchanged (blocks the
  terminal, not file writes). The print-mode stdout path (fixed on **1.0.15**, Windows) still
  applies; the transcript stays the fallback.
- ✅ **Verified on codex-cli 0.144.1** — `codex exec`, `-o/--output-last-message`,
  `codex exec resume`, the `--json` event stream, and the `~/.codex/sessions/.../rollout-*.jsonl`
  layout the continue path reads are all in place; a live `codex_ask` round-trip + `codex_status`
  pass. (Bumped from the 0.141.0 baseline: flags, session layout and the round-trip all re-verified
  unchanged.)
- ✅ **Verified on copilot 1.0.69** — `copilot -p -s` (clean stdout answer), `--session-id`
  set-then-resume, `--model`, `--output-format json` (watch stream), and the
  `~/.copilot/session-state/<id>/workspace.yaml` layout the continue fallback reads are all in place;
  live `copilot_ask` / `copilot_continue` round-trips + a mixed `agent_swarm` pass. (Bumped from
  1.0.68: 1.0.69 adds a `--resume` convenience flag the bridge doesn't need; `--session-id` still
  both *sets* a fresh id and resumes it — re-verified live, ACK then codeword recall.)
- ✅ **Verified on cursor-agent 2026.07.23** — `cursor-agent -p --output-format text --trust` (clean
  stdout answer), `create-chat` + `-p --resume <id>`, `--model` (validated against `cursor-agent
  models`), `--output-format stream-json` (watch stream), and the
  `~/.cursor/chats/<md5(workspace)>/<chat-id>/meta.json` layout the continue fallback reads are all in
  place; live `cursor_ask` / `cursor_continue` round-trips + a mixed `agent_swarm` pass.

  **The deferred live round-trip has since been done, on that same 2026.07.23.** It had been skipped
  the first time because the Cursor account was at its usage limit, leaving the run path confirmed
  only by structure. End-to-end through the bridge now: `cursor_ask` read a workspace file and
  answered from it, `cursor_continue` resumed the pinned chat and recalled it, and a `read-only` run
  refused to write — *"I'm in Ask mode … I can't create or write files"*, no file created — so
  cursor's agent-enforced mode holds in practice and not just in `--help`. Every model id the docs
  name still validates against the live list, which has grown from 193 ids to 204 with no CLI
  release: the catalogue moves on its own, so `cursor-agent models` stays the only current answer.
- 🖥️ **Console-detach** — before 1.0.15 agy `-p` wrote its answer to the *controlling terminal*,
  not stdout; under a TUI that text leaked into the host's prompt (seen on 1.0.9). 1.0.15 fixed this
  on Windows (stdout now carries the answer), but the bridge still spawns agy detached
  (`CREATE_NO_WINDOW` / a new POSIX session), which prevents the leak on older/other platforms and is
  harmless on 1.0.15+.
- 💾 **SQLite migration — handled** — agy still dual-writes a `.db` per conversation; on the fallback
  path, when the JSONL transcript is absent (already true for `--sandbox` runs, and the announced
  future default) `_read_response` falls back to reading the `.db`, verified to match across 100+
  conversations. See the [FAQ](faq.md#faq).
- 🐛 **agy stdout bug — fixed on 1.0.15** — `-p` now prints the clean answer to stdout in a non-TTY
  subprocess (Windows), so the bridge prefers stdout and only scrapes the transcript when stdout is
  empty (older agy, non-Windows, or `--sandbox`). (Codex and Copilot never had this problem — both
  are stdout-native.)
- 👁️ **Watch mode is experimental** — pass `watch=true` to any single-prompt tool to open the
  **Agent Intern** window and watch the agent work live (coarse steps; image shown inline).
  Best-effort and cross-platform; see [Watch mode](watch-and-swarm.md#watch-mode).
- 🔒 **Sandbox** — agy's `--sandbox` blocks only shell commands, so it's no boundary and the bridge
  never passes it. **Codex's `sandbox` is real and enforced** — use it; default `read-only`.
  **Copilot's `sandbox` is best-effort** (tool/path denials, not an OS sandbox); default `read-only`.
  **Cursor's `sandbox` is agent-enforced** (mode/force; read-only = `--mode ask` makes write/shell
  unavailable, not an OS sandbox); default `read-only`. See [Security](security.md#security).
