"""Antigravity CLI (agy) bridge — fastmcp server.

Exposes Antigravity CLI as MCP tools so Claude Code (or any MCP host) can
use it as a sub-agent. Historically agy had a headless print-mode "stdout bug"
(verified broken through 1.0.14): `agy -p` wrote its progress/answer to the
controlling terminal (TTY/console) directly, NOT to its stdout file descriptor
— so a captured-stdout read got nothing, and the bridge read the real response
from agy's own transcript files instead. agy 1.0.15 FIXED this on Windows: `-p`
now writes the clean final answer straight to stdout in a non-TTY subprocess
(verified empirically — stdout carries only the answer, no tool-calling
narration). So _run_agy now PREFERS stdout when present and falls back to
transcript-scraping only when stdout is empty (older agy, non-Windows per the
1.0.15 changelog, or a --sandbox run). As of agy 1.1.8 that stdout is a structured
JSON result object rather than bare text — see the structured-output note below.
The bridge still detaches agy from the
host's controlling terminal when spawning it (see _spawn_kwargs), which prevents
the pre-1.0.15 terminal leak into the host TUI and is harmless on 1.0.15+.
State-file layout and transcript schema re-verified on agy 1.0.15.

Auth: piggybacks on whatever credential store `agy` itself uses on the host
OS (Windows Credential Manager, macOS Keychain, libsecret on Linux). User
must have logged in interactively at least once via the Antigravity IDE or
`agy -i`. Uses the same AI Pro quota. The bridge itself only does cross-
platform filesystem reads under `~/.gemini/antigravity-cli/`.

Model: defaults to agy's settings.json "model" field (gemini-3.8-flash-high as of
agy 1.1.25).
agy 1.0.5 added a --model flag (and a `models` subcommand); through
~1.0.14 switching to a DIFFERENT model in -p HUNG the call (verified on 1.0.5:
the active label returned in seconds, any other hung >60s), so the bridge kept
its distance. Re-verified on 1.0.16 that the hang is FIXED: `agy -p --model
"<label>"` switches the model and returns in seconds (a Claude label answered as
Anthropic Claude, a Gemini label as Gemini). So antigravity_ask/continue and the
antigravity swarm path now take an optional `model`. Unknown labels: through
1.1.1 agy SILENTLY IGNORED an unknown --model, falling back to the settings.json
default with NO error; 1.1.2 changed -p to hard-fail with a non-zero exit that
lists the valid labels (interactive sessions keep the fallback-with-warning).
The bridge still validates a requested label against `agy models` (via
validate_model) and raises on a typo — that now fails fast WITHOUT spending an
agy call, and keeps the guard meaningful on pre-1.1.2 agy where a typo would
otherwise run the wrong model silently. When the label list can't be read (agy
missing, models call failed) validation is skipped and the label passes through
unchecked. `agy models` itself must be
run with stdin closed (it blocks on an interactive terminal otherwise), same as
-p is spawned with DEVNULL stdin.

Model SLUGS (agy 1.1.5, list re-checked live on 1.1.25) — the label format CHANGED
and every old example is now invalid. 1.1.5 introduced "stable, user-facing model
slugs" that the /model picker shows and --model accepts, and `agy models` emits
those slugs (bare through 1.1.10; as `<slug>\t<human label>` from 1.1.11 on — see
_parse_models_output). The live list (re-read on 1.1.25) is:
gemini-3.8-flash-{low,medium,high}, gemini-3.7-flash-{low,medium,high},
gemini-3.6-flash-{low,medium,high}, gemini-3.1-pro-{low,high}, claude-sonnet-4-6,
claude-opus-4-6-thinking, gpt-oss-120b-medium — 1.1.6 ADDED the gemini-3.6-flash
family and moved the settings.json default to it, the gemini-3.7-flash family
arrived by 1.1.16 and took the default in turn, and 1.1.25 added gemini-3.8-flash
and moved the default onto THAT (verified live through this bridge: a call passing
no `model` at all answered "Gemini 3.8 Flash").

Two things about the 3.8 arrival are worth keeping. Its changelog entry scopes it
to "when connecting with a `GEMINI_API_KEY`", but it is in the catalog on the
ordinary AI Pro browser-auth path too — `agy models` lists all three effort
variants there, and `--model gemini-3.8-flash-high` round-tripped clean through
the bridge. And the same release quietly DROPPED the entire gemini-3.5-flash
family, which NO changelog entry mentions (1.1.22 still named 3.5 Flash in a fix,
and it was live on 1.1.20), so every 3.5 example in these docs had silently become
a guaranteed rejection. Neither move needed a code change, because validate_model
reads the live list rather than a baked-in one; what rots is precisely this
paragraph, and a docstring naming a superseded default is how a caller ends up
reasoning about the wrong model. That rot is what DOCUMENTED_AGY_MODELS in
test_server.py exists to catch, and it caught this one from both sides — a
documented slug agy had dropped, and a live family the docs never mentioned.

The old human labels ("Gemini 3.1 Pro (High)", "Claude Sonnet 4.6 (Thinking)") are
GONE from the list, so
validate_model rejects them — verified live on 1.1.5: the old label raised
"unknown agy model ... expected one of: <slugs>" without spending a call, while
the slug form round-tripped clean. Note that agy's settings.json `"model"` field
is NOT that same surface: on this 1.1.25 install it holds the human label
`Gemini 3.8 Flash (High)` and agy honors it (that is the default the no-`model`
call above resolved through). Only `--model` requires a slug, so don't read a
label in that file as a stale config. Nothing in the bridge's
machinery had to change (validation was always format-agnostic, which is exactly
why the whole suite stayed green through this break) — but every docstring and
README example that named an old label was actively steering callers into a
failed first call, so all of them now name slugs. Note the slug bakes the
reasoning effort in for the models that expose variants, which is why there are
three flash entries and no separate effort argument here; 1.1.5 also added an
`--effort low|medium|high` session flag as a second axis, which this bridge does
NOT pass — the slug already pins the combination we want.

Compat (re-verified on agy 1.0.15): state-file paths, last_conversations.json
(still keyed by workspace path), and the transcript schema are unchanged, and a
normally-completing -p run still writes the JSONL transcript this bridge reads —
re-confirmed with a live round-trip. NEW in 1.0.15: `agy -p` also writes the
clean answer to stdout on Windows (the print-mode non-TTY-output fix), so the
bridge now prefers stdout and uses the transcript only as a fallback; the
transcript path stays fully exercised on older agy, non-Windows, and --sandbox
runs. (The other 1.0.15 changes don't touch this bridge: the "MCP connection
timeout → 60 s" is agy acting as an MCP *client* connecting to custom servers —
the opposite direction from this bridge — and the rest are interactive-TUI /
paste-shortcut / permissions-panel fixes.) (Nothing in agy 1.0.13 or 1.0.14 touches
this bridge either: their changes are interactive-TUI / plugin / skill /
browser-task fixes plus permission-rule tweaks — strict-by-default "Always
Approve" matching, a regex: opt-in, relaxed redirection checks — and
permissions still do NOT gate -p. 1.0.14's "MCP configuration path mismatch"
fix concerns agy loading custom MCP servers as a CLIENT, the opposite direction
from this bridge, which drives agy via its CLI. 1.0.13's removed "Resume in the
same project" exit-hint line doesn't matter: the bridge reads the transcript,
never agy's stdout, which it surfaces only on a non-zero exit.) (Nothing in agy
1.0.12 — interactive
--project/--new-project launch flags and the "default project regardless of active
workspace" resolution change, Esc-confirm in comment mode, OSC8 terminal hyperlinks,
reverse diff cycling (shift+n), ctrl+o scrollback fix, Makefile/LaTeX code-block
rendering, the AES-NI/DPI-firewall TLS fix, or the backtab/pgdown key-string
fixes — touches the paths, schema, or the print-mode TTY-leak this bridge depends
on. The new permission-config precedence — per-project files under
~/.gemini/config/projects/ now outrank ~/.gemini/antigravity-cli/settings.json —
is config the bridge never reads; the "model" field still lives in settings.json,
and permissions still do NOT gate -p, so the SECURITY note below stands. The bridge
also never passes --project, relying on cwd=workspace + conversation pinning.) agy
now ALSO dual-writes every
conversation to a SQLite store at ~/.gemini/antigravity-cli/conversations/<id>.db;
the 1.0.4
changelog says SQLite "will be the CLI's conversation format", so JSONL is on its
way out. _read_response handles this: it reads the JSONL transcript when present
and falls back to the SQLite store (_read_response_db) when it isn't — already the
case for --sandbox runs — so the bridge keeps working once JSONL goes away. The
1.0.5 -p metadata fix also stopped agy from writing metadata to the cwd, so
last_conversations.json now updates reliably under cache/.

Execution modes (agy 1.1.0): 1.1.0 added an agent execution-mode system — a
`--mode` launch flag (accept-edits | plan) plus a new interactive default,
request-review, that PAUSES before file writes to show a diff preview. This does
NOT affect the bridge. `-p` is spawned with DEVNULL stdin, and the request-review
approval gate only engages on an INTERACTIVE stdin: given EOF-on-stdin, print mode
still auto-executes every tool call with no prompt (re-verified on 1.1.0 — a
file-writing task completed and wrote its file in ~36 s, exit 0, identically with
and without `--mode accept-edits`). So the bridge kept NOT passing `--mode`; the
`request-review` toolPermission agy has logged since 1.0.5 stays a no-op for -p.
SUPERSEDED IN PART by 1.1.12, which fixed `--mode` being ignored in headless -p
entirely — which is why that 1.1.0 test could not have shown anything else. The
bridge now passes `--mode plan` when a caller opts in, and only then; see the
plan-mode paragraph at the end of the SECURITY note. `accept-edits` stays
unpassed, since --dangerously-skip-permissions already auto-approves everything.
Full compat re-verified on 1.1.0 via the bridge itself: ask + conversation-pinned
continue round-trips return clean over stdout, and base dir /
last_conversations.json / JSONL-primary transcript are all intact (SQLite
dual-write present — 260 .db — but JSONL still written).

Headless permissions (agy 1.1.3) — the one change that DID break this bridge, and
why we now pass --dangerously-skip-permissions. Everything above about print mode
auto-executing tools was true through 1.1.2. 1.1.3 closed exactly that gap: a tool
needing a permission confirmation is no longer auto-approved headlessly, it is
SOFT-DENIED (print mode has no way to prompt), and agy names the allow-rule on
stderr. Verified on 1.1.3: without the flag even a plain "read pyproject.toml and
report the version" is denied — agy exits 0 having done nothing, stdout empty,
stderr "a tool required the \"command\" permission ... so it was auto-denied". That
makes every tool-using bridge call dead, so _agy_base_args now passes
--dangerously-skip-permissions on all six agy argv paths (ask/continue, watch,
image, and the four swarm workers). Verified with the flag: file writes, terminal
commands and workspace reads all run again, exit 0 with the clean answer on stdout
(a bridge round-trip against this repo returned the correct pyproject version).
ORDER MATTERS — see _agy_base_args: `-p` takes the prompt as its VALUE, so the flag
must precede it or it BECOMES the prompt. Note the 1.1.3 gate is NOT a usable
safety knob for this bridge, for the same reason as --sandbox: it denies read-only
file reads and cannot prompt, so "gated" here means a sub-agent that can do
nothing — hence no opt-out knob. That exit-0-with-no-answer shape is also why
_run_agy now folds agy's stderr into the error when the transcript scrape comes up
empty; otherwise agy's actionable notice never reaches the caller.

1.1.4 walked back part of that gate, and it does NOT affect us. Its note reads
"headless (-p) runs now honor persisted settings.json policies, including
permissions, file access, sandbox mode, auto-execution, and artifact review" —
i.e. -p stopped being a blanket soft-deny and instead follows whatever the user's
settings.json says. That would matter if we relied on the default, but
--dangerously-skip-permissions still wins over those persisted policies, so the
flag stays load-bearing and stays exactly where it is. Re-verified live on 1.1.4
against a workspace deliberately ABSENT from settings.json trustedWorkspaces and
with a permissions.allow list naming neither file nor command access: a workspace
file read returned the right contents, and a terminal command and a file write
both executed. Two consequences worth keeping in mind: the flag is now the only
thing standing between a bridge call and the user's own settings.json policy, and
a user who removed it hoping for a safety gate would get whatever their
settings.json happens to say rather than the 1.1.3 deny-everything — neither
changes this module's posture, which already assumes arbitrary code execution.

Rest of 1.1.1–1.1.4 reviewed: nothing else breaks the bridge, and several changes
help it. Benign-and-beneficial: 1.1.1 made `-p` return a non-zero exit + stderr on
a server-side failure (was a silent empty success) and stopped `-p` reading stdin
when the prompt comes from a flag (we already pass DEVNULL, so belt-and-suspenders
against the subprocess hang); 1.1.2 makes a truly headless run with no auth
fail-fast instead of blocking; 1.1.3 fixed conversations breaking after certain
tool calls (corrupted history that blocked all further responses) — a win for the
continue/transcript path; 1.1.4 stopped `/btw` side-questions leaking into the
conversation list as duplicates carrying the PARENT's title, which is the list
_read_last_conv_id picks from, so one way to resume the wrong conversation is now
gone (we never issue /btw ourselves, but the user's interactive sessions share
that store). Not-us: the 1.1.2/1.1.3 "MCP servers hanging / schema
paths / leaked subprocesses" fixes are agy acting as an MCP *client* toward custom
servers — the opposite direction from this bridge, which drives agy's CLI. The rest
(1.1.3 `/codesearch`, no-flickering copy-on-select, compaction markers, startup /
render / keybinding fixes; 1.1.1 artifact-viewer search, nested-subagent display,
`--project` default rename; 1.1.4 stacked leading slash commands, `/diff` scroll
jitter, a custom Enter binding, and clearer eligibility errors) is interactive-TUI
and never on the `-p` path. 1.1.4's `subagent: false` fix (agents declaring it were
still invocable as subagents) rides along on `--agent`, which this bridge does not
pass — it stays on agy's default agent. One
security-tightening note, not a break: 1.1.3 stopped auto-approving out-of-workspace
writes in always-proceed mode; the module's SECURITY posture stays deliberately
conservative regardless (assume arbitrary code), so no claim is loosened on it.

Structured print-mode output (agy 1.1.8) — the first agy change that lets this
bridge DELETE guesswork rather than work around a break, and the reason the
transcript/SQLite scraping described above is now a FALLBACK rather than the primary
read path. 1.1.8 gave `-p` an `--output-format` flag (text | json | stream-json)
plus `--json-schema`. Nothing
broke — re-verified live on 1.1.8 that the pre-existing text path still round-trips
(ask, conversation-pinned continue, and `--model` all returned clean) — but the
`json` format is strictly better than parsing bare text, so _run_agy now asks for
it whenever supports_json_output() says the installed agy is 1.1.8+. Verified live,
the result is one object: {conversation_id, status, response, duration_seconds,
num_turns, usage{...,cache_read_tokens}}, and it composes with every flag this
bridge passes (`--conversation` continue returned num_turns=2 with memory intact,
`--model` round-tripped, `--dangerously-skip-permissions` unaffected).

Two things that buys us. (1) `response` is a contractual field, replacing the
"we verified stdout carries only the answer" assumption the text path rests on.
(2) `conversation_id` is agy telling us EXACTLY which conversation the run used —
previously the bridge could only infer that from last_conversations.json (shared
state agy rewrites for every session, including the user's own interactive TUI work
in the same folder) or from brain-dir mtimes. _run_agy now records that id per
workspace and _build_agy_args prefers it when pinning a continue, falling back to
last_conversations.json and then `-c`. Consequence worth knowing: on 1.1.8+ an
antigravity_continue resumes THIS BRIDGE's last thread in that workspace, where
before it could land on a conversation the user had started interactively in the
same folder since. That is what the pinning was always trying to do; it can only
now be done exactly. The map is process-local, so a restarted server simply falls
back to the old resolution order.

WATCH mode now rides the same 1.1.8 API, via `stream-json` (see _StreamWatch).
Instead of polling the undocumented JSONL transcript on a timer and inferring which
brain dir belongs to this run, the watched runners consume agy's typed NDJSON event
stream — `init` / `step_update` / `result` — straight off stdout. The premise was
verified before writing any of it: the events arrive INCREMENTALLY (a 17 s run
spread 18 events over 12.4 s), and a live watched run through the bridge grew its
step count 0 → 3 → 6 → 7 while agy worked. What the stream gives that the scrape
could not: the real `tool_info.parameters.CommandLine` as a nested object (the
transcript stores tool args JSON-encoded inside a string, which is why
_clean_tool_arg exists), `text_delta` fragments per agent_response step, explicit
ACTIVE→DONE transitions, and `conversation_id` — so a WATCHED run now records its
conversation for later pinning exactly like the plain path, closing an asymmetry
that used to leave watch depending on last_conversations.json. The answer comes
from the terminal `result` event, which is the SAME field the plain path reads, so
watch=True and watch=False can no longer disagree about what the answer is.

Two things still read the transcript on purpose. Continue-mode history seeding
(_read_agy_history) shows PRIOR turns in the viewer, and stream-json describes only
the current run — it is cosmetic and already degrades to [] when unreadable. And
every stream path keeps _resolve_and_read as a fallback for a run that produces no
`result` event (agy died mid-stream, or ignored the flag). Pre-1.1.8 agy keeps the
whole original _WatchFeed path, re-verified live by forcing supports_json_output()
off against a real agy: no --output-format in argv, transcript scrape, right answer.

`--json-schema` remains unadopted: it works (verified — it adds a validated
`structured_output` object alongside the prose `response`) but no bridge tool
currently needs a caller-supplied schema.

ANSWER SHAPE — a deliberate, user-visible change. agy's `response` is the whole
turn: every agent_response step concatenated. The old transcript scrape returned
only the LAST completed PLANNER_RESPONSE. On a single-step ask they are identical
(verified: '0.21.4' both ways), but on a chatty multi-step run they differ — measured
on one run, 297 chars ("I will first count the .py files.\nNext, I will print today's
date.\n…\nCompleted counting .py files (19 found)…") versus 128 for the last step
alone, with the json answer ENDING IN the transcript answer. The full turn is what
the bridge now returns, on every path. Rationale: `result.response` is agy's own
contract for "what this turn produced", and this release's whole direction is to
trust that contract instead of the bridge's reconstruction of it — the last-step
rule was never a product decision, just the best a transcript scrape could do, and
it silently DROPS content whenever the model does its real work and then closes with
a short "Done." The cost is narration noise on multi-step runs; the watch window
exists to show that narration, but dropping content is not recoverable.

Why the version gate rather than "just pass the flag": on 1.1.8 an unrecognised
--output-format VALUE is silently ignored and agy prints plain text (verified —
`--output-format bogus` exited 0 with a normal answer), but a pre-1.1.8 agy has no
such FLAG at all and could fail arg parsing on every call. supports_json_output()
therefore checks the version and answers False when it can't be parsed, and
_parse_json_result returns None on anything that isn't the expected object so a
silently-ignored flag degrades to the text path instead of crashing.

Rest of 1.1.7 reviewed, nothing else touches this bridge: the `-p` fix for sending
a prompt before the account-eligibility check finished is a benign win on our exact
path; the disabled-plugins-still-running-hooks fix, the MCP OAuth relaxation
(Salesforce/Atlassian — agy as an MCP *client*, the opposite direction from this
bridge), the `/btw` first-action crash, and the Windows CJK clipboard-copy fix are
all interactive-TUI or client-side. Same for the rest of 1.1.8: `copyOnSelect` is a
TUI setting, and the compound-command allow-always improvement is an interactive
permission-prompt change (print mode still bypasses that via
--dangerously-skip-permissions).

Machine-readable `agy models` (agy 1.1.11) — the second agy change that DID break
this bridge, and it broke the `model` argument outright rather than degrading it.
1.1.11/1.1.12 made the `models` and `agents` subcommands machine-readable, and their
lines went from a bare slug to a TAB-SEPARATED `<slug>\t<human label>` record (the
change is in no changelog entry; the canary test was green on 1.1.10, issue #3
reports the break on 1.1.11, and it was reproduced here on 1.1.12). The bridge
read each whole line as a slug, so validate_model then rejected EVERY valid model
with an error that listed the very slug it had just refused ("unknown agy model
'gemini-3.6-flash-high'; expected one of: gemini-3.6-flash-high<TAB>Gemini 3.6 Flash
(High), …") — reproduced end-to-end through antigravity_ask before the fix.
_parse_models_output now keeps the first tab field, which reads both formats. Two
notes for the next reader: 1.1.12's changelog advertises `--output-format json` for
these subcommands but the shipped binary has no such flag (`agy models
--output-format json` exits 1 with "flags provided but not defined: -output-format";
`agy models --help` lists only -h/--help), so TSV is the only machine-readable form
there is; and the "Fetching available models..." progress line now goes to stderr,
leaving stdout as the list and nothing else. The rest of the model story is
unchanged: the live slug list is the same eleven entries as on 1.1.6, and
`--model` IS honored in print mode again — 1.1.10 fixed the flags being applied
after model configuration had initialized, which had made `-p --model` silently
fall back to the persisted default. Verified on 1.1.12 the cheap way, without
spending a call: `agy --model gemini-3.1-pro-high -p "/model"` answers
gemini-3.1-pro-high, where a bare `-p "/model"` answers the settings.json default.

Free quota reads (agy 1.1.11/1.1.12) — the first agy change that lets this bridge
ADD a capability for nothing. Print mode now answers read-only slash commands
itself, with no agent turn, no quota spend, and no conversation left behind:
1.1.11 added /usage, /quota, /credits, /model, /effort, /skills, and 1.1.12 added
/permissions, /hooks, /help, /config, /changelog. antigravity_status now runs
`agy -p "/usage"` and reports the remaining share per model family (Gemini vs
Claude-and-GPT, weekly and five-hour), flagging a family at 0% as a problem — the
tool already promised to tell you what will go wrong before you spend a call, and
"you are out of quota" is the most common such answer. supports_print_usage() gates
it at 1.1.11 as a SAFETY gate, not a feature probe: below that the same argv is a
prompt, so a diagnostic advertised as free would quietly spend a call. Note the
probe deliberately omits --disable-slash-commands, the one bridge call that wants
agy's slash handling; see _read_agy_usage.

That same slash handling is why the shield stays. 1.1.11 replaced the silent
fall-through for interactive-only commands with an explicit refusal, so the old
hazard (a prompt starting with "/schedule …" actually running it) is now agy's own
error rather than an action — and that error recommends exactly the flag this
bridge already passes ("pass --disable-slash-commands to send /clear to the model
as literal text", exit 2, verified on 1.1.12). The read-only set is the reason the
shield is still load-bearing: unshielded, an innocent prompt opening with "/model"
or "/usage" gets agy's table instead of an answer. Re-verified end-to-end on 1.1.12
through this bridge — antigravity_ask("/clear Reply with the single word BRIDGE…")
returned BRIDGE.

`--effort` stays unadopted, now with a harder reason than "the slug already pins
it": it is not a universal axis. `agy --model claude-sonnet-4-6 --effort low` exits
with "invalid model selection … --effort is not supported for model
'claude-sonnet-4-6'", so exposing it as a bridge argument would need per-model
validation to avoid handing callers a flag that fails on half the menu, while
buying nothing for the gemini slugs that already bake the level in. (1.1.10 fixed
`--effort` being ignored in `-p`, and a bare `--effort` resolving against the
default model instead of the selected one; both are moot while we don't pass it.)

Rest of 1.1.11/1.1.12 reviewed, nothing else breaks the bridge and several changes
help it. Checked because it looked risky and turned out benign: 1.1.12 stopped
swallowing startup diagnostics into the log file and now surfaces them, including
the `--conversation` not-found warning that the continue path can trigger — they go
to STDERR, so stdout stays a pure JSON result object (verified with a deliberately
bogus `--conversation`: exit 0, clean JSON on stdout, `warning: conversation "…"
not found` on stderr, and agy silently starts a new conversation). Benign wins:
1.1.12 fixed a Windows crash resolving the conversation transcript path in the
trajectory log converter (the artifact the watch path's log_uri points at), made
headless `-p` settle a choice itself where it would otherwise stall on a question
nobody is there to answer (fewer bridge timeouts), raised the per-session
tool-declaration limit (heavier MCP setups), and 1.1.11 made retries honor the
server-supplied delay and stopped an empty credits response reading as a zero
balance ("Out of credits" errors that were not real). Not-us: Vim editing mode,
artifact-viewer wrapping/outline/callouts, terminal hyperlinks, plugin enablement
in config.json, and the tool-call-header summaries are interactive-TUI; 1.1.11's
allowlist fixes (an entry tokenizing to zero command words matching everything;
auto-approval while in request-review mode) are real security fixes on the
permission path that `-p` bypasses anyway via --dangerously-skip-permissions, and
1.1.11's admin-controls and MCP-progress-callback fixes are agy acting as an MCP
*client*, the opposite direction from this bridge.

SECURITY — read this: `agy -p` runs the model as an autonomous agent that
executes its tools (read/write files, run shell commands, reach the network)
with no approval gate. Through 1.1.2 that was unconditional and had NO opt-out:
re-verified empirically on agy 1.0.9 / Windows that print mode ran
out-of-workspace writes even WITHOUT --dangerously-skip-permissions, a no-op for
-p at the time, and agy 1.0.5's permission system (its logs show
toolPermission=request-review) never gated print-mode execution either. agy 1.1.3
added a real headless gate at last — but it soft-denies so broadly (plain file
reads included) that a gated -p can do no useful work, so this bridge opts out of
it with --dangerously-skip-permissions (see the headless-permissions note above).
The flag is load-bearing now rather than decorative, and what it restores is
exactly the posture this note has always described: assume every call runs
arbitrary code with your privileges.

--sandbox is NOT a usable safety knob for this bridge. agy 1.0.6 fixed
--sandbox flag propagation into -p (its 1.0.6 changelog calls this "sandbox
isolation correctly enforced"), and verified here it now DOES block terminal/
shell command execution in print mode. But that "isolation" is partial and
misleadingly named: re-verified on 1.0.9 that under --sandbox the model still
wrote a file OUTSIDE its workspace via the write_to_file tool — so --sandbox
does NOT constrain filesystem writes or network egress, only the terminal.
(agy 1.0.9 hardened the sandbox's command path — stricter exact-match command
checks, .git added to its dangerous-paths list — but none of that closes the
out-of-workspace write_to_file hole.) Worse for us, a --sandbox run that hits
a blocked terminal command writes NO JSONL transcript (only the SQLite .db, as
re-confirmed on 1.0.9), so the bridge would fail to read a response.
Re-verified on 1.1.0: nothing here changed. A sandboxed terminal command still
gets blocked and stalls print mode (it returned "Error: timeout waiting for
response", exit 1, having executed nothing), while write_to_file still runs under
--sandbox and still lands OUTSIDE the declared workspace (exit 0). The new 1.1.0
`--mode accept-edits` and `--sandbox` coexist without error, but neither makes -p
safe. For both reasons the bridge deliberately does NOT pass --sandbox. Of agy's own
knobs, 1.1.3's headless permission gate is the closest thing to safe-and-useful,
and it lands on the useless side of that line (see the headless-permissions note
above).

--mode plan (agy 1.1.12+) IS the exception, and it is opt-in per call rather than
part of the posture above. Verified on 1.1.20 through this bridge, with a control
that makes the claim falsifiable: the same prompt -- run `cmd /c echo SHELLRAN >
<absolute path>` -- EXECUTED and created the file on a normal call, and created
nothing at all under plan=True, which answered with a plan document written into
agy's own brain dir instead. A file read still answered normally, so unlike
--sandbox and unlike the 1.1.3 gate this restricts without disabling. It also
survives --dangerously-skip-permissions, which the bridge keeps passing (dropping
it would soft-deny the very reads plan mode exists to allow).

Do not oversell it. Plan mode constrains agy's AGENT LOOP, so it is enforced the
way Copilot's and Cursor's modes are and not the way codex's seatbelt/landlock
sandbox is: it is a strong default for "look at this repo but don't touch it", not
a boundary to put in front of a hostile prompt. For that, use codex_ask with
sandbox="read-only". And it costs the slash shield: agy refuses the combination
with --disable-slash-commands, printing "--mode plan has no effect while slash
command expansion is disabled" and then running UNRESTRICTED -- the exact
looks-restricted-but-isn't outcome plan mode exists to prevent -- so the bridge
drops that flag and enforces the equivalent client-side in
_guard_plan_mode_prompt.

So `workspace` is only a starting context, NOT a security boundary:
every call effectively runs arbitrary code with your privileges. Only invoke
this bridge with trusted prompts on trusted content (untrusted input here is
the classic prompt-injection "lethal trifecta"). For real isolation, run the
whole bridge inside a container or VM.
"""

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Union

from fastmcp import Context, FastMCP

import codex_bridge
import copilot_bridge
import cursor_bridge
import grok_bridge
import kimi_bridge
import opencode_bridge

# Server-level instructions. The MCP client sends these to its model on connect
# (Claude Code surfaces them as an "MCP Server Instructions" block), so EVERY
# user who installs the bridge gets their host model taught how and WHEN to reach
# for these tools — without ever opening the README. Keep this a tight ROUTING
# guide, not a manual: it costs context tokens in every session, and per-tool
# detail already lives in each tool's own description/annotations. The highest-
# value content here is what a model can't infer from tool schemas alone —
# proactive triggers, which backend to pick, and the workspace footgun.
SERVER_INSTRUCTIONS = """\
This server bridges seven external coding CLIs — Antigravity (Gemini), OpenAI \
Codex, GitHub Copilot, Cursor, opencode (any model, incl. FREE ones), and the \
two EXPERIMENTAL ones, Grok Build (xAI) and Kimi Code (Moonshot) — into your \
session as sub-agents that run on the USER'S OWN quota. Delegating here spends \
that quota instead of your tokens, gets a second model-family opinion, or \
generates images.

Reach for these tools when:
- the user wants an IMAGE — antigravity_image is your only image generator \
(antigravity_image_swarm for several at once).
- a job splits into many independent sub-tasks — agent_swarm runs them in \
parallel, mixing any backends in one call.
- the work is bulk and mechanical (a rename across 30 files, boilerplate, a \
first-pass port) — cheap for a sub-agent, expensive in your context.
- you want a heavier or different-family model's take: a second opinion on a \
risky diff, or a design call where one model's blind spot is the whole risk.

OFFER IT — don't wait to be asked. On a task that fits the triggers above, say \
so in ONE line before you start: name the backend, the reason, and ask. \
("This is 6 independent files — want me to farm it out to Gemini in parallel \
instead of grinding through them here?") The user is paying for these plans \
already; a delegation they never hear about is one they can never take. Then \
respect the answer: ask ONCE per task, never once per turn, and if they decline, \
drop it for the rest of that task. Delegate without asking only for images, or \
where the user has already said to delegate this kind of work.

Don't offer, and don't delegate, when the job is a couple of tool calls, when it \
leans on context only this conversation has, or when the ~10-30s round trip costs \
more than just doing it. A suggestion on every task is nagging, not help.

Pick a backend:
- antigravity_* (Gemini) — fast, cheap tool-calling; the ONLY image model. No \
real sandbox, so trusted prompts only.
- codex_* (OpenAI) — strongest reasoning and real repo edits behind a REAL \
enforced sandbox (read-only by default; pass sandbox="workspace-write" to let \
it edit files).
- copilot_* (GitHub) — agentic coding on a Copilot plan; sandbox is best-effort, \
not an OS boundary.
- cursor_* (Cursor) — agentic coding on a Cursor plan, with a wide model menu \
(GPT/Claude/Grok/Composer via `model`); sandbox is agent-enforced (read-only = \
ask mode), not an OS boundary.
- grok_* (Grok Build, xAI — EXPERIMENTAL, never live-verified) — needs SuperGrok \
/ X Premium+ or XAI_API_KEY. Real OS sandbox, but on LINUX/macOS ONLY: on Windows \
it silently does not enforce, so read-only there rests on a tool allowlist.
- opencode_* (opencode, SST) — the ONLY backend that needs no subscription: its \
free `opencode/*` models answer with zero credentials, so reach for it when the \
user has no other plan, when you want a model family none of the others cover, \
or to spare a paid quota. Add a key for Claude/GPT-class models. Sandbox is \
agent-enforced (a permission policy), not an OS boundary, but it behaves the \
same on every platform. Free models are SLOW — allow minutes, not seconds.
- kimi_* (Kimi Code, Moonshot — EXPERIMENTAL, never live-verified) — Kimi K2 \
family; like antigravity it has NO sandbox and auto-executes tools, so trusted \
prompts only. Needs `kimi login` or an API key.
Both experimental backends are unproven end-to-end: run their *_status first, and \
tell the user plainly if one fails rather than retrying blindly.

Mechanics:
- Pass `workspace` = the relevant project directory. It defaults to the server's \
cwd, so WITHOUT it the sub-agent answers with no repo context — this is the most \
common mistake.
- *_continue resumes the thread rooted at a workspace; swarm workers are \
one-shot (no continue).
- watch=true opens a live browser view of the agent working (identical return \
value).
- *_status checks a backend is installed and logged in, and spends no quota — \
use it if a call reports "not found". Its `bridge version` row also says when a \
NEWER bridge release is out; if it does, tell the user in one line and give the \
upgrade command the row names. The startup notice for this only reaches the \
host's logs, so relaying it is the one way they hear about it.

Security: they all run as autonomous agents. Codex's sandbox is the only hard \
boundary everywhere; grok's is a real one too, but only on Linux/macOS; \
opencode's is agent-enforced, but behaves the same on every platform. Use only \
with trusted prompts on trusted content."""

mcp = FastMCP("agent-intern", instructions=SERVER_INSTRUCTIONS)

# The running bridge's version — the source of truth is THIS file (not the
# installed package metadata, which goes stale on editable installs). Keep in
# sync with pyproject.toml's version. Compared at startup against the latest
# tag on GitHub so a long-lived clone learns when to `git pull`.
__version__ = "0.30.0"

# Logs go to stderr (stdout is the MCP protocol channel). Quiet by default;
# set AGY_BRIDGE_DEBUG=1 for per-call diagnostics. See _configure_logging.
log = logging.getLogger("agy_bridge")

# The agy executable to invoke. Defaults to "agy" (resolved via PATH); set the
# AGY_BIN env var to an explicit path when agy isn't reliably on PATH — e.g. on
# Windows where a new terminal/reboot can drop it:
#   AGY_BIN=%LOCALAPPDATA%\agy\bin\agy.exe
# Read once at import; the launching process's environment wins.
AGY_BIN = os.environ.get("AGY_BIN", "agy")

# GitHub repo polled at startup for a newer release tag. Override AGY_BRIDGE_REPO
# if you run a fork; set AGY_BRIDGE_NO_UPDATE_CHECK=1 to skip the check entirely.
GITHUB_REPO = os.environ.get("AGY_BRIDGE_REPO", "SinanTufekci/agent-intern")

AGY_DATA = Path.home() / ".gemini" / "antigravity-cli"
LAST_CONVERSATIONS = AGY_DATA / "cache" / "last_conversations.json"
BRAIN_DIR = AGY_DATA / "brain"
CONVERSATIONS_DIR = AGY_DATA / "conversations"  # agy 1.0.4+ SQLite store
# agy saves generated images here when not given an explicit absolute save path
SCRATCH_DIR = AGY_DATA / "scratch"

# Serializes agy invocations within this process. Concurrent runs would race
# on last_conversations.json (agy rewrites it on every call), so a second
# request could pick up the first request's conversation id.
_AGY_LOCK = threading.Lock()

# Latest agy version the bridge's state-file assumptions were verified against.
# Newer agy releases may change paths/schemas (the SQLite migration is the known
# risk), so we warn at startup if the installed agy is newer than this.
#
# 1.1.25 re-verification (live, Windows): ask round-tripped through _run_agy with
# the real argv on both the default-model path and `--model gemini-3.8-flash-high`,
# the `--output-format json` object still carries conversation_id/status/response,
# `agy models` still emits <slug>\t<label>, the `/usage` quota table still parses
# (antigravity_status reported both families at 100% without spending a call), and
# the JSONL transcript read path still resolves. The ONLY drift in 1.1.21-1.1.25 is
# the model catalog -- 3.8 in, 3.5 out -- which is data, not plumbing, and is
# handled where it belongs (see the model-slug note above and
# DOCUMENTED_AGY_MODELS).
#
# Two upstream fixes in this range landed directly on shapes this bridge relies on,
# and both moved TOWARD it. 1.1.23 fixed subcommands such as `models` hanging on an
# inherited, unclosed stdin -- exactly the hang list_agy_models has always spawned
# with stdin=DEVNULL to dodge, so that workaround is now belt-and-braces rather
# than load-bearing (it stays: the bridge still supports older agy). And 1.1.24
# fixed headless runs with piped stdout/stderr hanging on EXIT, by setting
# FD_CLOEXEC so a grandchild process can no longer hold the caller's pipes open;
# every call here is `capture_output=True`, so that was a live timeout risk on the
# bridge's own argv shape, absorbed until now only by the subprocess timeout.
#
# Watch out for one false alarm this range introduces: agy left an EMPTY brain dir
# behind (no transcript, just empty .user_uploaded/ and scratch/) at the moment it
# self-updated, and antigravity_status dutifully reported "newest transcript [!!]"
# for it. The read path was fine -- the very next bridge call wrote a real
# transcript and status went green -- so an empty newest-conversation is a stale
# artifact, not a regression.
VERIFIED_AGY_VERSION = (1, 1, 25)

# First agy version whose print mode understands `--output-format json` (1.1.8).
# Below this the flag is unknown to agy's parser, so the bridge must not pass it
# and stays on the plain-text stdout path. See supports_json_output.
JSON_OUTPUT_MIN_VERSION = (1, 1, 8)

# First agy version whose print mode EXPANDS slash commands and skills instead of
# sending them to the model as literal text (1.1.9), and whose parser therefore
# understands the `--disable-slash-commands` opt-out. See
# supports_disable_slash_commands and _agy_base_args.
SLASH_COMMANDS_MIN_VERSION = (1, 1, 9)

# First agy version that answers read-only slash commands in print mode itself,
# without an agent turn, quota spend, or a conversation left behind (1.1.11 added
# /usage, /quota, /credits, /model, /effort, /skills; 1.1.12 added /permissions,
# /hooks, /help, /config, /changelog). This is what makes the quota rows in
# antigravity_status free. See supports_print_usage.
USAGE_PRINT_MIN_VERSION = (1, 1, 11)

# First agy version that HONORS `--mode` in headless print mode (1.1.12). The flag
# parses on older builds and does nothing there -- 1.1.12's changelog says it
# plainly: "--mode being ignored in headless -p runs, where a valid value such as
# accept-edits or plan was never applied and an unrecognized value produced no
# warning at all." That is why plan mode is GATED rather than best-effort. Every
# other gate here degrades to a lesser-but-safe path when it answers False; this
# one cannot, because plan mode is a RESTRICTION. Silently dropping it would hand
# back a fully-empowered run to a caller who asked for a look-but-don't-touch one,
# reporting success either way. See supports_plan_mode.
PLAN_MODE_MIN_VERSION = (1, 1, 12)

# Poll window for the transcript/conversation-id to appear after agy exits.
# agy has already returned 0 by the time we read, so the common case resolves
# on the first attempt; the poll just absorbs filesystem-flush lag.
_RESPONSE_POLL_DEADLINE_S = 5.0
_RESPONSE_POLL_INTERVAL_S = 0.1

# How often the streaming runner re-reads the transcript to emit progress while
# agy is still working. agy flushes the transcript in coarse chunks (verified on
# 1.0.9: it can stay empty for ~15 s then append several entries at once), so
# progress is deliberately coarse — a handful of ticks per run, not token-level.
_PROGRESS_POLL_INTERVAL_S = 0.4

# How often to emit an MCP progress notification while a blocking agy run is in
# flight (see _run_with_progress). agy reports no real percentage, so progress is
# a coarse time bar (elapsed / timeout); ~1 s keeps clients' bars moving without
# spamming notifications.
_PROGRESS_NOTIFY_INTERVAL_S = 1.0


def _parse_agy_version(text: str) -> Optional[tuple[int, int, int]]:
    """Extract a (major, minor, patch) tuple from `agy --version` output."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _compat_warning(version: Optional[tuple[int, int, int]]) -> Optional[str]:
    """Return a warning if the installed agy is newer than we've verified.

    None if the version is unknown, equal to, or older than VERIFIED_AGY_VERSION.
    """
    if version is None or version <= VERIFIED_AGY_VERSION:
        return None
    detected = ".".join(map(str, version))
    verified = ".".join(map(str, VERIFIED_AGY_VERSION))
    return (
        f"agy {detected} is newer than the {verified} this bridge was verified "
        "against. If responses look wrong or empty, agy may have changed its "
        "state-file layout (the SQLite conversation format is the known risk). "
        "Pin a known-good agy version if needed."
    )


def _env_truthy(name: str) -> bool:
    """True if env var `name` is set to a truthy value (1/true/yes/on)."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _debug_enabled() -> bool:
    """True if AGY_BRIDGE_DEBUG is set to a truthy value (1/true/yes/on)."""
    return _env_truthy("AGY_BRIDGE_DEBUG")


def _fetch_latest_release_version() -> Optional[tuple[int, int, int]]:
    """Best-effort: the highest semver tag published on GITHUB_REPO, or None.

    Hits GitHub's public tags API (no auth) with a short timeout. ANY failure —
    offline, DNS, rate-limit, HTTP error, unexpected JSON — returns None so the
    server never blocks or errors on the network at startup.
    """
    url = f"https://api.github.com/repos/{GITHUB_REPO}/tags?per_page=100"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-intern-bridge",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            tags = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    if not isinstance(tags, list):  # e.g. a {"message": "rate limit"} error body
        return None
    versions = [
        v
        for v in (_parse_agy_version(t.get("name", "")) for t in tags if isinstance(t, dict))
        if v is not None
    ]
    return max(versions) if versions else None


def _update_warning(latest: Optional[tuple[int, int, int]]) -> Optional[str]:
    """Return a warning if `latest` is a newer bridge version than this file.

    None if no newer release is known, or if either version can't be parsed.

    Names the uvx upgrade FIRST and the source one second, because that is the
    order the install paths actually rank: the README recommends `uvx
    agent-intern` from PyPI, and this message used to say only "git pull in the
    repo" -- advice with no repo to run it in for the recommended install, and
    advice that disagreed with the upgrade line _bridge_version_status prints in
    chat. Two update notices in one codebase should not hand out different
    commands, and the wrong one should not be the one aimed at most users.
    """
    current = _parse_agy_version(__version__)
    if latest is None or current is None or latest <= current:
        return None
    newest = ".".join(map(str, latest))
    return (
        f"A newer Agent Intern bridge is available: v{newest} "
        f"(you are running v{__version__}). Upgrade with `uvx agent-intern@latest`, "
        "or `git pull` if you installed from source, then restart Claude Code. "
        "Set AGY_BRIDGE_NO_UPDATE_CHECK=1 to silence this."
    )


# Text decoding for every CLI subprocess. The backends emit UTF-8, but bare
# text=True decodes with the LOCALE codepage (locale.getpreferredencoding) — cp1254
# on a Turkish Windows, cp1252 elsewhere — which silently mangles every non-ASCII
# answer: "dosyası" came back as "dosyasÄ±" through antigravity_ask, exactly
# 'dosyası'.encode('utf-8').decode('cp1254'). Decode UTF-8 explicitly and never
# raise on a stray byte. Mirrors cursor_bridge._TEXT, which already got this right.
_TEXT = {"encoding": "utf-8", "errors": "replace"}


def _spawn_kwargs(name: str = "") -> dict:
    """Extra subprocess kwargs that detach agy from the host's controlling terminal.

    Historically (agy ≤1.0.14) `agy -p` wrote its progress/answer to the
    controlling terminal (TTY/console) directly, NOT to its stdout file
    descriptor — which was both why capturing stdout yielded nothing AND why,
    under an interactive terminal, agy's text leaked into the host (e.g. straight
    into Claude Code's TUI prompt input). agy 1.0.15 fixed this on Windows: `-p`
    now writes the clean answer to stdout in a non-TTY subprocess (so _run_agy
    prefers stdout there). Detaching is kept anyway as belt-and-suspenders: it
    still prevents the terminal leak on older agy and on platforms the 1.0.15 fix
    may not cover, and never hurts the stdout path. Windows: CREATE_NO_WINDOW.
    POSIX: a new session (no controlling tty).

    `name` overrides the platform (defaults to os.name) so both branches stay
    unit-testable without globally mutating os.name — which would break pathlib
    (and pytest's own bookkeeping) on non-Windows hosts.
    """
    if (name or os.name) == "nt":
        # CREATE_NO_WINDOW is Windows-only; the literal fallback lets the "nt"
        # branch be exercised on any OS (the value is only ever used on Windows).
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {"start_new_session": True}


def _drain_pipe(stream) -> "tuple[threading.Thread, list]":
    """Continuously read a child's PIPE into a buffer on a daemon thread.

    The watch-mode runners loop on proc.poll() while pumping the transcript, but do
    NOT read the child's stdout/stderr during that loop. On agy 1.0.15+ `agy -p`
    writes its full final answer to stdout; a large answer (or verbose stderr) can
    fill the fixed OS pipe buffer, block agy's write, and hang it forever — the loop
    then runs to the hard deadline and reports a FALSE timeout with a truncated
    transcript answer. Draining each pipe on its own thread keeps the buffer empty so
    the child never blocks (the same thing subprocess.run/communicate does for the
    non-watched paths). Join the thread after the process exits; "".join(chunks) is
    the full captured output.
    """
    chunks: list = []

    def _reader():
        try:
            for line in stream:
                chunks.append(line)
        except (ValueError, OSError):
            pass  # pipe closed (e.g. by proc.kill()) — stop quietly
        finally:
            try:
                stream.close()
            except OSError:
                pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return t, chunks


def _pump_pipe(stream, on_line) -> "tuple[threading.Thread, list]":
    """_drain_pipe, but hand each line to `on_line` as it arrives.

    This is how the watched runners consume agy's stream-json stdout: the events
    must be processed WHILE agy works, but the run loop still has to stay free to
    enforce its hard deadline (a blocking read on stdout could never time out). So
    the parsing happens here on the reader thread and the loop keeps polling.
    Draining is still the other half of the job — an unread pipe fills its OS buffer
    and hangs the child (see _drain_pipe).

    `on_line` must not raise; exceptions are swallowed so a parse bug can never kill
    the reader thread and re-introduce the pipe-buffer hang it exists to prevent.
    """
    chunks: list = []

    def _reader():
        try:
            for line in stream:
                chunks.append(line)
                try:
                    on_line(line)
                except Exception:  # noqa: BLE001 - a bad line must not stop the drain
                    log.debug("stream handler failed on a line", exc_info=True)
        except (ValueError, OSError):
            pass  # pipe closed (e.g. by proc.kill()) — stop quietly
        finally:
            try:
                stream.close()
            except OSError:
                pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return t, chunks


def _get_agy_version() -> Optional[str]:
    """Return `agy --version` output, or None if agy can't be run."""
    try:
        proc = subprocess.run(
            [AGY_BIN, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            **_TEXT,
            timeout=15,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout or "") + (proc.stderr or "")


def _startup_checks() -> None:
    """Warn (once, at startup) about a stale agy or a newer bridge release.

    Both checks are best-effort and non-fatal: the agy check runs `agy --version`
    locally; the update check polls GitHub (skipped via AGY_BRIDGE_NO_UPDATE_CHECK,
    silent on any network failure).
    """
    agy_warning = _compat_warning(_parse_agy_version(_get_agy_version() or ""))
    if agy_warning:
        log.warning(agy_warning)
    if not _env_truthy("AGY_BRIDGE_NO_UPDATE_CHECK"):
        update_warning = _update_warning(_fetch_latest_release_version())
        if update_warning:
            log.warning(update_warning)


def _configure_logging() -> None:
    """Route bridge logs to stderr; DEBUG when AGY_BRIDGE_DEBUG is set."""
    handler = logging.StreamHandler()  # defaults to stderr
    handler.setFormatter(logging.Formatter("[agy-bridge] %(levelname)s: %(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(logging.DEBUG if _debug_enabled() else logging.WARNING)
    log.propagate = False


def _normalize_workspace(ws: Optional[str]) -> str:
    return os.path.abspath(ws) if ws else os.getcwd()


def _read_last_conv_id(workspace: str) -> Optional[str]:
    if not LAST_CONVERSATIONS.exists():
        return None
    try:
        data = json.loads(LAST_CONVERSATIONS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if workspace in data:
        return data[workspace]
    for k, v in data.items():
        if k.lower() == workspace.lower():
            return v
    return None


def _find_newest_conv_after(start_time: float) -> Optional[str]:
    if not BRAIN_DIR.exists():
        return None
    best = None
    best_mtime = start_time - 2
    for child in BRAIN_DIR.iterdir():
        if not child.is_dir():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best = child.name
            best_mtime = mtime
    return best


# --- minimal protobuf wire reader, for agy's SQLite `steps.step_payload` blobs ---
# agy 1.0.4 added a SQLite conversation store and the changelog says it "will be
# the CLI's conversation format". When agy stops writing the JSONL transcript the
# bridge falls back to reading the .db (see _read_response_db). The schema is
# undocumented; these helpers walk the protobuf wire format well enough to pull
# the final answer — verified against the JSONL transcript across 114 local
# conversations (104 byte-identical, 10 a longer superset, 0 wrong).
def _pb_varint(buf: bytes, i: int):
    shift = val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def _pb_fields(buf: bytes) -> list:
    """(field_number, wire_type, value) for each field in a protobuf message.
    value is raw bytes for length-delimited fields and the int for varints; other
    wire types are skipped. Best-effort — stops on malformed input, never raises."""
    out: list = []
    i, n = 0, len(buf)
    while i < n:
        try:
            tag, i = _pb_varint(buf, i)
            field, wt = tag >> 3, tag & 7
            if wt == 0:
                v, i = _pb_varint(buf, i)
                out.append((field, 0, v))
            elif wt == 2:
                ln, i = _pb_varint(buf, i)
                out.append((field, 2, buf[i : i + ln]))
                i += ln
            elif wt == 5:
                i += 4
            elif wt == 1:
                i += 8
            else:
                break
        except IndexError:
            break
    return out


def _pb_bytes(fields: list, num: int) -> list:
    """The length-delimited values of field `num` (bytes / string / sub-message)."""
    return [v for f, wt, v in fields if f == num and wt == 2]


# step_type / status codes in the SQLite `steps` table, reverse-engineered to
# mirror the JSONL transcript's type=PLANNER_RESPONSE / status=DONE filter.
_DB_PLANNER_RESPONSE = 15
_DB_STATUS_DONE = 3


def _read_response_db(conv_id: str) -> Optional[str]:
    """Final planner answer from agy's SQLite store (`conversations/<id>.db`).

    Mirrors _read_response's JSONL logic — the last completed planner-response
    step's text — read from the `steps` table (step_payload protobuf: the
    sub-message at field 20, its string at field 1). Returns None if the .db is
    missing/unreadable or has no such step, so the caller can fall through to a
    clear error. Best-effort: agy's schema is undocumented and may change."""
    db_path = CONVERSATIONS_DIR / f"{conv_id}.db"
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT step_payload FROM steps WHERE step_type=? AND status=? ORDER BY idx",
                (_DB_PLANNER_RESPONSE, _DB_STATUS_DONE),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    answer: Optional[str] = None
    for (payload,) in rows:
        if not payload:
            continue
        for sub in _pb_bytes(_pb_fields(payload), 20):
            for text in _pb_bytes(_pb_fields(sub), 1):
                try:
                    decoded = text.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if decoded.strip():
                    answer = decoded
    return answer


def _read_response(conv_id: str) -> str:
    """Final model answer for a conversation: the last completed planner response.

    Reads agy's JSONL transcript (the fast path) and falls back to its SQLite
    conversation store when the JSONL is missing or empty. That fallback matters
    today (a --sandbox run writes no JSONL, only the .db) and is the migration agy
    has announced — so the bridge keeps working once JSONL goes away entirely."""
    transcript = BRAIN_DIR / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    chunks: list[str] = []
    if transcript.exists():
        for line in transcript.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                entry.get("source") == "MODEL"
                and entry.get("status") == "DONE"
                and entry.get("type") == "PLANNER_RESPONSE"
                and entry.get("content")
            ):
                chunks.append(entry["content"])
    if chunks:
        # Last completed planner response is the final answer (tool steps come earlier).
        return chunks[-1]

    # JSONL absent or empty — fall back to the SQLite (.db) store.
    db_answer = _read_response_db(conv_id)
    if db_answer is not None:
        return db_answer

    db_path = CONVERSATIONS_DIR / f"{conv_id}.db"
    if not transcript.exists():
        raise RuntimeError(
            f"No transcript for conversation {conv_id}: neither the JSONL ({transcript}) "
            f"nor a readable SQLite store ({db_path}) yielded a completed planner response. "
            "If you upgraded agy, its conversation format may have changed in a way the "
            "bridge can't yet parse."
        )
    raise RuntimeError(
        f"No completed MODEL response in transcript {transcript} (and no usable SQLite "
        f"fallback at {db_path}). agy may have failed silently or timed out."
    )


def _transcript_entries(conv_id: str) -> list[dict]:
    """All parsed JSONL entries for a conversation, or [] if no transcript yet.

    Unlike _read_response this is non-raising and returns every entry (not just
    the final answer) — it's the live feed the streaming runner polls for
    progress. Re-reads the whole file each call; transcripts are small (a handful
    of entries per turn), so that's cheap enough for the poll loop.
    """
    transcript = BRAIN_DIR / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not transcript.exists():
        return []
    out: list[dict] = []
    for line in transcript.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _strip_user_request(text: str) -> str:
    """Unwrap agy's <USER_REQUEST>…</USER_REQUEST> envelope around a stored prompt.

    agy records each user turn's content wrapped in a <USER_REQUEST> tag (sometimes
    with only the opening tag). The watch history should show the clean prompt, so
    peel the wrapper; falls back to the raw text if no wrapper is present.
    """
    t = (text or "").strip()
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", t, re.DOTALL)
    if m:
        return m.group(1).strip()
    if t.startswith("<USER_REQUEST>"):
        return t[len("<USER_REQUEST>") :].strip()
    return t


def _read_agy_history(conv_id: str) -> list[dict]:
    """Prior turns of an agy conversation for the watch view: [{role, content}, …].

    Oldest first. Walks the JSONL transcript in order: each USER_INPUT (unwrapped
    from its <USER_REQUEST> envelope) is a user turn, and the LAST completed
    PLANNER_RESPONSE before the next user input is that turn's assistant answer
    (earlier planner steps within a turn are tool-call narration, not the answer).
    Best-effort — returns [] if the transcript is missing/unreadable. Note: agy may
    store a truncated `content` for very long older turns (its own transcript cap),
    so those answers can come back clipped.
    """
    turns: list[dict] = []
    pending: Optional[str] = None
    for e in _transcript_entries(conv_id):
        src, typ = e.get("source"), e.get("type")
        if src == "USER_EXPLICIT" and typ == "USER_INPUT" and e.get("content"):
            if pending is not None:
                turns.append({"role": "assistant", "content": pending})
                pending = None
            turns.append({"role": "user", "content": _strip_user_request(e["content"])})
        elif (
            src == "MODEL"
            and typ == "PLANNER_RESPONSE"
            and e.get("status") == "DONE"
            and e.get("content")
        ):
            pending = e["content"].strip()  # keep the latest; it's the turn's final answer
    if pending is not None:
        turns.append({"role": "assistant", "content": pending})
    return turns


def _clean_tool_arg(value) -> str:
    """Unwrap a tool-call arg. agy stores them JSON-encoded (a quoted/escaped
    string inside the string), so one json.loads turns e.g. CommandLine into the
    real command. Falls back to the raw value if it isn't double-encoded."""
    if not isinstance(value, str):
        return "" if value is None else str(value)
    try:
        decoded = json.loads(value)
        if isinstance(decoded, str):
            return decoded.strip()
    except (json.JSONDecodeError, ValueError):
        pass
    return value.strip()


def _entry_to_watch_lines(entry: dict) -> list[tuple[str, str]]:
    """Richer per-entry breakdown for the watch window: the model's narration,
    the ACTUAL command it runs (from tool_calls), and a command-finished marker.

    Returns a list of (kind, text) where kind is 'narration' | 'command' |
    'result'; the viewer maps kind to colour/symbol. A single planner step can
    yield two lines (its narration + the actual command it runs).
    """
    if entry.get("source") != "MODEL":
        return []
    etype = entry.get("type")
    lines: list[tuple[str, str]] = []
    if etype == "PLANNER_RESPONSE":
        content = entry.get("content")
        if content:
            lines.append(("narration", content.strip().splitlines()[0][:200]))
        for call in entry.get("tool_calls") or []:
            args = (call or {}).get("args") or {}
            cmd = _clean_tool_arg(args.get("CommandLine"))
            if not cmd:
                cmd = _clean_tool_arg(args.get("toolSummary") or args.get("toolAction"))
            if cmd:
                lines.append(("command", cmd[:200]))
    elif etype == "RUN_COMMAND":
        lines.append(("result", "command finished"))
    return lines


# Canonical extension per detected image format. Drives extension-correction:
# agy's image model picks the format itself (JPEG for photos, PNG for flat
# graphics), regardless of the requested filename's extension.
_IMAGE_EXT = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp"}


def _detect_image_format(path: str) -> Optional[str]:
    """Sniff an image format from a file's magic bytes, or None if not an image."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if head[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if head[:4] == b"GIF8":
        return "GIF"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    return None


def _canonical_ext(fmt: str) -> str:
    """Canonical file extension (with dot) for a detected image format."""
    return _IMAGE_EXT[fmt]


def _with_ext(path: str, ext: str) -> str:
    """Return `path` with its extension replaced by `ext` (e.g. '.jpg')."""
    return os.path.splitext(path)[0] + ext


def _resolve_output_path(output_path: Optional[str], workspace: str) -> str:
    """Resolve the absolute target path for a generated image.

    Omitted -> a timestamped default under `workspace`; relative -> joined to
    `workspace`; absolute -> used as-is. The extension may still be corrected
    after generation (agy picks JPEG or PNG itself, regardless of the name).
    """
    if not output_path:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        return os.path.join(workspace, f"agy-image-{stamp}Z.png")
    if os.path.isabs(output_path):
        return os.path.abspath(output_path)
    return os.path.abspath(os.path.join(workspace, output_path))


def _newest_scratch_image_after(start: float) -> Optional[str]:
    """Newest recognized image in agy's scratch dir, modified at/after `start`
    (with a ~2 s buffer to absorb filesystem timestamp lag).

    agy falls back to ~/.gemini/antigravity-cli/scratch/ when not given an
    explicit absolute save path. Returns an absolute path string, or None.
    """
    if not SCRATCH_DIR.exists():
        return None
    best: Optional[str] = None
    best_mtime = start - 2
    for child in SCRATCH_DIR.iterdir():
        if not child.is_file():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime and _detect_image_format(str(child)):
            best = str(child)
            best_mtime = mtime
    return best


def _wrap_image_prompt(prompt: str, target: str) -> str:
    """Wrap a user image prompt with an explicit save path + path-only reply.

    agy honours an explicit absolute path; without one it falls back to its own
    scratch dir. Asking it to reply with only the path gives a reliable hint for
    locating the file.
    """
    base = prompt.rstrip()
    sep = "" if base.endswith(".") else "."
    return (
        f"{base}{sep} Save the generated image to this exact absolute path: "
        f"{target} . After saving, reply with ONLY the absolute file path where "
        f"you actually saved the image, nothing else."
    )


def _finalize_image(target: str, agy_text: Optional[str], start: float) -> tuple[str, str, int]:
    """Locate the generated image, move it to `target` (with its extension
    corrected to the real magic-byte format), and return path + format + size.

    Candidate order: the resolved `target`, then an absolute path agy reported in
    `agy_text`, then the newest image in the scratch dir created at/after `start`.
    Renames to the canonical extension for the real (magic-byte) format, so the
    returned path never lies about its bytes.

    Returns (final_path, format, size_bytes). Raises RuntimeError if no image
    file is found, or if the located file is not a recognized image.
    """
    candidates = [target]
    if agy_text and agy_text.strip():
        # agy may add prose after the path; take the first non-empty line.
        candidates.append(agy_text.strip().splitlines()[0].strip().strip('"'))
    scratch = _newest_scratch_image_after(start)
    if scratch:
        candidates.append(scratch)

    src = next((c for c in candidates if c and os.path.isfile(c)), None)
    if src is None:
        raise RuntimeError(
            f"antigravity_image: no image file found. Looked at target {target!r} and "
            f"scratch dir {SCRATCH_DIR}."
        )

    fmt = _detect_image_format(src)
    if fmt is None:
        raise RuntimeError(
            f"antigravity_image: {src!r} is not a recognized image. agy may have refused "
            "the request or returned text instead of an image."
        )

    final_path = _with_ext(target, _canonical_ext(fmt))
    os.makedirs(os.path.dirname(final_path) or ".", exist_ok=True)
    if os.path.abspath(src) != os.path.abspath(final_path):
        if os.path.exists(final_path):
            os.remove(final_path)
        shutil.move(src, final_path)
    return final_path, fmt, os.path.getsize(final_path)


def _bridge_version_status() -> tuple[str, bool, str]:
    """Status row for the bridge's own version and whether a newer release exists.

    Always reports ok=True — an available update is informational, not a fault, so
    it must not flip the overall status to PROBLEMS FOUND. Honors
    AGY_BRIDGE_NO_UPDATE_CHECK and stays ok (just uninformative) when GitHub is
    unreachable. This is what surfaces the update notice in an MCP client's chat
    (the startup stderr warning only lands in the host's logs).
    """
    label = "bridge version"
    if _env_truthy("AGY_BRIDGE_NO_UPDATE_CHECK"):
        return (label, True, f"v{__version__} (update check disabled)")
    latest = _fetch_latest_release_version()
    if latest is None:
        return (label, True, f"v{__version__} (update check unavailable — offline?)")
    current = _parse_agy_version(__version__)
    if current is not None and latest > current:
        newest = ".".join(map(str, latest))
        return (
            label,
            True,
            f"v{__version__} -> v{newest} available; upgrade: uvx agent-intern@latest",
        )
    return (label, True, f"v{__version__} (latest)")


def _collect_status() -> list[tuple[str, bool, str]]:
    """Gather setup diagnostics as (label, ok, detail) rows.

    Spends no AI Pro quota: runs `agy --version`, asks agy for its own quota table
    (`-p "/usage"`, answered by the CLI without an agent turn on 1.1.11+ — see
    _quota_status_rows), inspects local state files, and (unless
    AGY_BRIDGE_NO_UPDATE_CHECK is set) makes one best-effort GitHub call to report
    whether a newer bridge release exists.
    """
    rows: list[tuple[str, bool, str]] = [_bridge_version_status()]

    version = _parse_agy_version(_get_agy_version() or "")
    if version is None:
        rows.append(("agy CLI", False, "not found on PATH (or --version unparseable)"))
    else:
        vstr = ".".join(map(str, version))
        ok_compat = _compat_warning(version) is None
        detail = f"v{vstr} - " + ("compat OK" if ok_compat else "newer than verified")
        rows.append(("agy CLI", True, detail))
        rows.extend(_quota_status_rows())

    rows.append(("base dir", AGY_DATA.exists(), str(AGY_DATA)))

    if BRAIN_DIR.is_dir():
        n = sum(1 for c in BRAIN_DIR.iterdir() if c.is_dir())
        rows.append(("brain dir", True, f"{n} conversations"))
    else:
        rows.append(("brain dir", False, str(BRAIN_DIR)))

    rows.append(("last_conversations.json", LAST_CONVERSATIONS.exists(), str(LAST_CONVERSATIONS)))

    newest = _find_newest_conv_after(0.0)
    if newest is None:
        rows.append(("newest transcript", True, "no conversations yet"))
    else:
        try:
            _read_response(newest)
            rows.append(("newest transcript", True, "readable"))
        except RuntimeError as e:
            rows.append(("newest transcript", False, str(e)[:80]))

    if CONVERSATIONS_DIR.exists():
        n = sum(1 for _ in CONVERSATIONS_DIR.glob("*.db"))
        rows.append(("SQLite store", True, f"present - {n} .db (JSONL still primary)"))
    else:
        rows.append(("SQLite store", True, "absent"))

    return rows


# Whether the installed agy understands `--output-format json`. Resolved once per
# process (a `agy --version` subprocess) and cached — _run_agy consults it on every
# call, and agy cannot change version underneath a running process.
_AGY_JSON_SUPPORT: Optional[bool] = None
_AGY_JSON_SUPPORT_LOCK = threading.Lock()


def supports_json_output() -> bool:
    """True if this agy has print-mode structured output (`--output-format json`).

    agy 1.1.8 added it. Below that the flag is not in agy's parser, so passing it
    risks an arg-parse failure on every call — hence the version gate rather than
    "just try it". An UNPARSEABLE/missing version answers False: the plain-text
    stdout path works on every agy the bridge has ever supported, so it is the safe
    default. Cached for the process.
    """
    global _AGY_JSON_SUPPORT
    with _AGY_JSON_SUPPORT_LOCK:
        if _AGY_JSON_SUPPORT is None:
            version = _parse_agy_version(_get_agy_version() or "")
            _AGY_JSON_SUPPORT = version is not None and version >= JSON_OUTPUT_MIN_VERSION
        return _AGY_JSON_SUPPORT


# Whether the installed agy understands `--disable-slash-commands`. Resolved once
# per process and cached, exactly like _AGY_JSON_SUPPORT.
_AGY_SLASH_GATE: Optional[bool] = None
_AGY_SLASH_GATE_LOCK = threading.Lock()


def supports_disable_slash_commands() -> bool:
    """True if this agy has the `--disable-slash-commands` opt-out (1.1.9+).

    agy 1.1.9 made print mode expand slash commands and skills, so a prompt whose
    first token names one is EXECUTED as that command instead of reaching the
    model. Below 1.1.9 the flag is not in agy's parser (and the expansion doesn't
    happen), so it must not be passed. An unparseable/missing version answers
    False — never pass a flag we can't confirm exists. Cached for the process.
    """
    global _AGY_SLASH_GATE
    with _AGY_SLASH_GATE_LOCK:
        if _AGY_SLASH_GATE is None:
            version = _parse_agy_version(_get_agy_version() or "")
            _AGY_SLASH_GATE = version is not None and version >= SLASH_COMMANDS_MIN_VERSION
        return _AGY_SLASH_GATE


# Whether the installed agy HONORS `--mode` in print mode. Cached for the process,
# like the gates above.
_AGY_PLAN_GATE: Optional[bool] = None
_AGY_PLAN_GATE_LOCK = threading.Lock()


def supports_plan_mode() -> bool:
    """True if this agy honors `--mode plan` in print mode (1.1.12+).

    An unparseable/missing version answers False, and callers must then REFUSE the
    request rather than run it unrestricted -- see PLAN_MODE_MIN_VERSION and
    _check_plan_mode. Cached for the process.
    """
    global _AGY_PLAN_GATE
    with _AGY_PLAN_GATE_LOCK:
        if _AGY_PLAN_GATE is None:
            version = _parse_agy_version(_get_agy_version() or "")
            _AGY_PLAN_GATE = version is not None and version >= PLAN_MODE_MIN_VERSION
        return _AGY_PLAN_GATE


def _guard_plan_mode_prompt(prompt: str) -> None:
    """Raise if agy's slash expansion would eat `prompt` on a plan-mode run.

    Plan mode and `--disable-slash-commands` are MUTUALLY EXCLUSIVE: agy prints
    "warning: --mode plan has no effect while slash command expansion is disabled"
    and runs with plan mode OFF (verified on 1.1.20 -- the run came back
    unrestricted). So a plan-mode call cannot carry the bridge's usual slash
    shield, and the shield has to move here, client-side.

    The rule mirrors agy's own trigger: expansion fires when the prompt's FIRST
    token names a registered command, and a command is a single segment
    ("/schedule"), where a POSIX path meant literally almost always carries a
    second separator ("/etc/hosts …"). So a leading single-segment /token is
    refused and everything else passes. Refusing is the right failure here: the
    registered set includes side-effecting commands (`/goal` starts an autonomous
    long-running task, `/schedule` creates cron jobs) and bridge prompts routinely
    carry text the caller did not author. A false positive costs a rephrase; a
    false negative runs someone else's command.

    Skipped when AGY_BRIDGE_ALLOW_SLASH_COMMANDS=1 -- the same deliberate opt-in
    _agy_base_args honors for callers who WANT `-p "/my-skill <args>"` to invoke a
    skill.
    """
    if _env_truthy("AGY_BRIDGE_ALLOW_SLASH_COMMANDS"):
        return
    stripped = prompt.strip()
    first = stripped.split(maxsplit=1)[0] if stripped else ""
    if first.startswith("/") and "/" not in first[1:] and "\\" not in first:
        raise ValueError(
            f"plan mode cannot run a prompt starting with {first!r}: agy expands a "
            "leading slash command instead of sending it to the model, and plan mode "
            "is incompatible with the --disable-slash-commands shield that normally "
            "blocks that. Rephrase so the prompt does not begin with a command (or "
            "set AGY_BRIDGE_ALLOW_SLASH_COMMANDS=1 to opt in deliberately)."
        )


def plan_from_sandbox(sandbox: Optional[str]) -> bool:
    """Map agent_swarm's per-task `sandbox` onto agy's plan mode.

    Every other backend in a swarm takes a `sandbox` policy, and Antigravity used
    to be the one that silently IGNORED it — so a task written as
    {"backend": "agy", "sandbox": "read-only"} ran completely unrestricted while
    reading as though it were fenced. Plan mode is the first thing agy has that can
    honour that request, so "read-only" now maps onto it.

    The policies agy cannot honour are refused rather than approximated:

    * "read-only"          -> plan mode. Agent-enforced, not an OS boundary; see
                              _agy_base_args and the module SECURITY note.
    * "danger-full-access" -> no restriction, which is what agy does anyway. Saying
                              so explicitly is the point of the name.
    * None (omitted)       -> no restriction. Antigravity's long-standing swarm
                              default, deliberately left alone: flipping it would
                              turn every existing file-writing swarm task into a
                              plan document.
    * "workspace-write"    -> ValueError. agy has NO write scoping to offer: under
                              its own --sandbox the model still wrote a file
                              OUTSIDE the declared workspace (re-verified on 1.1.0),
                              so accepting this would promise a fence that does not
                              exist. The other backends really do scope writes;
                              quietly treating agy's as equivalent is exactly the
                              confusion this function was added to end.
    """
    if sandbox is None or sandbox == "danger-full-access":
        return False
    if sandbox == "read-only":
        return True
    if sandbox == "workspace-write":
        raise ValueError(
            "antigravity cannot scope writes to a workspace, so sandbox="
            "'workspace-write' would promise a fence agy does not have (under its own "
            "--sandbox it still wrote outside the workspace). Use 'read-only' for plan "
            "mode — agy investigates and writes a plan instead of touching anything — "
            "or 'danger-full-access' to state plainly that this worker is unrestricted."
        )
    raise ValueError(
        f"unknown sandbox {sandbox!r} for antigravity; expected 'read-only' or "
        "'danger-full-access' (or omit it)"
    )


def _check_plan_mode(prompt: str) -> None:
    """Validate a plan-mode request before any quota is spent."""
    if not supports_plan_mode():
        found = _get_agy_version() or "unknown"
        raise ValueError(
            f"plan=True needs agy {'.'.join(map(str, PLAN_MODE_MIN_VERSION))}+ "
            f"(found {found}). Older agy accepts --mode and then ignores it in print "
            "mode, so the run would come back fully empowered while reporting "
            "success -- the one failure a restriction must not have. Upgrade agy, or "
            "call without plan."
        )
    _guard_plan_mode_prompt(prompt)


def _normalize_json_schema(schema: Union[dict, str]) -> str:
    """agy's `--json-schema` argument as text, or raise on something unusable.

    Accepts the object form (what an MCP client naturally sends) and pre-encoded
    JSON text, because a model composing a tool call produces either. agy also
    accepts a PATH to a schema file; the bridge deliberately does not, so a string
    that is not JSON is a typo to report rather than a filename to go hunting for
    — silently treating a malformed schema as a path is how you get an
    unschema'd run that still reports success.
    """
    if isinstance(schema, str):
        text = schema.strip()
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ValueError(
                f"schema must be a JSON object or its JSON text ({exc}). A file path "
                "is not accepted here — pass the schema itself."
            ) from exc
    else:
        parsed = schema
        text = json.dumps(schema)
    if not isinstance(parsed, dict):
        raise ValueError(f"schema must be a JSON object, not {type(parsed).__name__}")
    return text


def _check_schema_support() -> None:
    """Refuse a schema request on an agy that has no `--json-schema`.

    1.1.8 added the flag alongside `--output-format json`, so supports_json_output()
    is exactly the right gate — below it the bridge is on the plain-text stdout
    path, where there is no result object to carry structured_output. Refusing
    beats degrading for the same reason plan mode refuses: the caller is going to
    parse what comes back, and prose that isn't the requested shape fails at their
    end instead of ours.
    """
    if not supports_json_output():
        found = _get_agy_version() or "unknown"
        raise ValueError(
            f"schema needs agy {'.'.join(map(str, JSON_OUTPUT_MIN_VERSION))}+ "
            f"(found {found}), which is where --json-schema and the structured result "
            "object arrived. Upgrade agy, or call without schema."
        )


def _structured_answer(result: dict, conv_id: str) -> str:
    """The validated `structured_output` object of a schema run, as JSON text.

    agy puts the schema-conforming object in its OWN field and leaves `response`
    alone, and the two are not interchangeable: verified on 1.1.20 that `response`
    on a schema run carries the model's raw emission — the same keys plus agy's own
    `toolAction`/`toolSummary`, and in one run a line of prose ahead of the JSON —
    while `structured_output` was exactly the declared fields. So a missing
    structured_output RAISES rather than falling back to `response`; a caller that
    asked for a schema is about to json.loads this.
    """
    structured = result.get("structured_output")
    if structured is None:
        status = result.get("status")
        raise RuntimeError(
            "agy returned no structured_output for a --json-schema run"
            + (f" (status={status})" if status else "")
            + f" in conversation {conv_id or 'unknown'}. The schema may be invalid, or "
            "this agy may not honour the flag."
        )
    return json.dumps(structured, ensure_ascii=False)


# Whether this agy answers `-p "/usage"` itself instead of sending it to a model.
# Cached for the process, like the gates above.
_AGY_USAGE_GATE: Optional[bool] = None
_AGY_USAGE_GATE_LOCK = threading.Lock()


def supports_print_usage() -> bool:
    """True if this agy answers the read-only `/usage` command in print mode (1.1.11+).

    The gate is a SAFETY gate, not a feature probe. On 1.1.11+ `agy -p "/usage"` is
    answered by the CLI itself — no agent turn, no quota, no conversation left
    behind — so it is free for antigravity_status to call. Below 1.1.11 the same
    argv is a PROMPT: pre-1.1.9 agy hands "/usage" to the model as literal text and
    1.1.9/1.1.10 expand it as a command, either of which spends the user's quota to
    answer a diagnostic that promises to spend none. An unparseable/missing version
    answers False, so we probe only where we know it is free.
    """
    global _AGY_USAGE_GATE
    with _AGY_USAGE_GATE_LOCK:
        if _AGY_USAGE_GATE is None:
            version = _parse_agy_version(_get_agy_version() or "")
            _AGY_USAGE_GATE = version is not None and version >= USAGE_PRINT_MIN_VERSION
        return _AGY_USAGE_GATE


def _read_agy_usage() -> Optional[str]:
    """stdout of `agy -p "/usage"`, or None if it can't be read. Spends no quota.

    NOTE the argv deliberately omits BOTH flags _agy_base_args adds:

    * `--disable-slash-commands` would defeat the entire point — it is what makes
      agy treat a leading "/usage" as literal text, so the prompt would reach the
      model, spend quota, and return prose instead of the quota table. This is the
      one bridge call that WANTS agy's slash handling.
    * `--dangerously-skip-permissions` is pointless here: answering /usage runs no
      tools, and a status probe should not carry a permission opt-out.

    Best-effort by contract: any failure (agy missing, non-zero exit, timeout)
    yields None and the caller simply reports the quota as unreadable. We do NOT
    take _AGY_LOCK: this starts no conversation and so cannot race the state files
    (verified on 1.1.12), and taking it would make a status call queue behind a
    long-running ask.
    """
    try:
        proc = subprocess.run(
            [AGY_BIN, "--print-timeout", "20s", "-p", "/usage"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            **_TEXT,
            timeout=25,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _percent_remaining(value: str) -> Optional[float]:
    """ "99%" -> 99.0; None when the field isn't a percentage."""
    try:
        return float(value.strip().rstrip("%"))
    except ValueError:
        return None


def _parse_usage_rows(stdout: str) -> list[tuple[str, bool, str]]:
    """agy's `/usage` table as status rows, one per model family.

    agy 1.1.11+ prints one tab-separated record per limit:

        Gemini Models\tWeekly Limit Remaining\t100%\t2026-08-11T18:50:23Z

    i.e. family, limit name, remaining share, reset time. We group by family in
    first-seen order and keep the remaining share, dropping the reset timestamps —
    they are the least useful column in a one-line status row.

    A family whose remaining share has hit 0% is reported NOT ok: every call
    against it will fail until the window resets, which is exactly the kind of
    thing this tool exists to surface before the user spends a call finding out.
    Unrecognized lines are skipped rather than raising — agy's format may change
    again, and a status probe must never be the thing that breaks.
    """
    families: dict[str, list[tuple[str, str]]] = {}
    for line in stdout.splitlines():
        fields = [f.strip() for f in line.split("\t")]
        if len(fields) < 3 or not fields[0] or not fields[2]:
            continue
        limit = fields[1].removesuffix(" Remaining").removesuffix(" Limit")
        families.setdefault(fields[0], []).append((limit or fields[1], fields[2]))
    rows: list[tuple[str, bool, str]] = []
    for family, limits in families.items():
        detail = ", ".join(f"{name} {value}" for name, value in limits)
        exhausted = any(_percent_remaining(v) == 0.0 for _, v in limits)
        rows.append((f"quota: {family}", not exhausted, detail))
    return rows


def _quota_status_rows() -> list[tuple[str, bool, str]]:
    """Quota rows for _collect_status: agy's own /usage table, or nothing.

    Returns [] on agy older than 1.1.11, which has no free way to report quota (see
    supports_print_usage) — there is nothing to say, and inventing a failing row
    would flip the overall verdict to PROBLEMS FOUND on a perfectly healthy setup.
    Where the command exists but the read fails, we say so in an ok row: the probe
    is a nicety, and its failure is not a broken bridge.
    """
    if not supports_print_usage():
        return []
    out = _read_agy_usage()
    rows = _parse_usage_rows(out) if out else []
    return rows or [("quota", True, 'unreadable (`agy -p "/usage"` failed)')]


def _parse_json_result(stdout: str) -> Optional[dict]:
    """agy's `--output-format json` result object, or None if stdout isn't one.

    The 1.1.8 shape is a single JSON object: {conversation_id, status, response,
    duration_seconds, num_turns, usage} (plus structured_output when --json-schema
    is used). None means "this isn't structured output" — stdout was plain text,
    empty, or malformed — and the caller falls back to treating stdout as the
    answer. That fallback is what keeps the bridge correct if agy ever silently
    ignores the flag: verified on 1.1.8 that an unrecognised --output-format VALUE
    is dropped without error and prints plain text, so a bad/removed format must
    never surface as a crash.

    BANNER TOLERANCE: the object is located rather than assumed to be the whole of
    stdout. agy 1.1.10 started printing a non-blocking advisory banner when the
    same conversation is already open in another CLI instance — precisely the
    shape *_continue and the swarm can produce. A banner prefixed to (or appended
    after) the result would fail a strict startswith("{") test, and the caller's
    fallback would then hand the user a raw JSON blob instead of an answer. We
    scan for the first "{" that decodes to a result object, so leading and
    trailing chatter are both absorbed. Plain text still answers None: the scan
    requires a decodable object carrying the contractual `response` key.
    """
    text = (stdout or "").strip()
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and "response" in obj:
            return obj
        start = text.find("{", start + 1)
    return None


# workspace -> conversation id, recorded from agy's own `--output-format json`
# result. Preferred over last_conversations.json for continue-pinning: it is the
# id of the run THIS bridge just made, whereas last_conversations.json is shared
# state agy rewrites for every session — including the user's own interactive TUI
# work in the same folder. Process-local by design; on a fresh server it is empty
# and pinning falls back to last_conversations.json exactly as before.
_CONV_BY_WORKSPACE: dict[str, str] = {}
_CONV_BY_WORKSPACE_LOCK = threading.Lock()


def _record_conv_id(workspace: str, conv_id: str) -> None:
    """Remember the conversation agy just used for `workspace`."""
    if not conv_id:
        return
    with _CONV_BY_WORKSPACE_LOCK:
        _CONV_BY_WORKSPACE[os.path.normcase(workspace)] = conv_id


def _recorded_conv_id(workspace: str) -> Optional[str]:
    """The conversation this process last ran in `workspace`, if any."""
    with _CONV_BY_WORKSPACE_LOCK:
        return _CONV_BY_WORKSPACE.get(os.path.normcase(workspace))


def _resolve_and_read(pinned_conv: Optional[str], workspace: str, start: float) -> str:
    """Resolve the conversation id for this run and return its final response.

    Resolution order: the pinned id (continue), then the workspace's recorded
    id, then the newest brain dir touched since `start`. Raises if none resolve.
    """
    conv_id = pinned_conv or _read_last_conv_id(workspace) or _find_newest_conv_after(start)
    log.debug("resolved conv_id=%s", conv_id)
    if conv_id is None:
        raise RuntimeError(
            f"No conversation found after agy run (workspace={workspace}). "
            f"Check {LAST_CONVERSATIONS} and {BRAIN_DIR}."
        )
    return _read_response(conv_id)


# Cache of the `agy models` slug list for this process. agy silently ignores an
# unknown --model (falls back to the settings.json default with NO error), so we
# validate a requested slug against this list and fail loudly on a typo — the way
# codex/copilot reject an unknown model. Populated (once) on first validation.
_AGY_MODELS_CACHE: Optional[list[str]] = None
_AGY_MODELS_LOCK = threading.Lock()


def _parse_models_output(stdout: str) -> list[str]:
    """Slugs from `agy models` stdout — the first field of each line.

    agy 1.1.11 made the subcommand machine-readable and its lines are now
    TAB-SEPARATED records, `<slug>\\t<human label>`:

        gemini-3.6-flash-high\tGemini 3.6 Flash (High)

    where 1.1.8 printed the bare slug. The change is undocumented and lands
    between two verified points: the canary test below was green on 1.1.10, and
    issue #3 reports the tab-separated rejection on 1.1.11, where this bridge saw
    it on 1.1.12. Reading the whole line as the slug rejected EVERY valid model,
    silently killing the `model` argument on all three antigravity tools —
    verified live on 1.1.12 through this bridge:
    antigravity_ask(model="gemini-3.6-flash-high") raised "unknown agy model
    'gemini-3.6-flash-high'; expected one of: gemini-3.6-flash-high<TAB>Gemini 3.6
    Flash (High), ...", an error listing the very slug it had just refused. Keeping
    only the first field reads both formats: a bare-slug line has no tab and
    survives unchanged.

    Fields that contain whitespace are dropped: a slug never has a space, so such
    a line is chatter (a progress or status line), not a model. 1.1.12 also moved
    "Fetching available models..." off stdout onto stderr, so on this version the
    guard costs nothing — it is there for the older/newer builds that print it.

    (1.1.12's changelog advertises `--output-format json` for the `models` and
    `agents` subcommands, but the shipped 1.1.12 binary has no such flag: `agy
    models --output-format json` exits 1 with "flags provided but not defined:
    -output-format", and `agy models --help` lists only -h/--help. TSV is the only
    machine-readable form the subcommand actually has, so we parse it.)
    """
    slugs: list[str] = []
    for line in stdout.splitlines():
        slug = line.split("\t", 1)[0].strip()
        if slug and not any(c.isspace() for c in slug):
            slugs.append(slug)
    return slugs


def list_agy_models() -> list[str]:
    """Model slugs reported by `agy models`, cached for the process ([] if unreadable).

    Runs `agy models` with stdin CLOSED — the subcommand otherwise blocks waiting
    on an interactive terminal (the same reason `-p` is spawned with DEVNULL
    stdin). Any failure (agy missing, non-zero exit, timeout) yields [], which
    callers treat as "can't validate" and pass a requested label through unchecked
    rather than wrongly rejecting it.

    Line parsing lives in _parse_models_output, which tracks agy 1.1.12's switch to
    tab-separated `<slug>\\t<label>` records.
    """
    global _AGY_MODELS_CACHE
    with _AGY_MODELS_LOCK:
        if _AGY_MODELS_CACHE is not None:
            return _AGY_MODELS_CACHE
        names: list[str] = []
        try:
            proc = subprocess.run(
                [AGY_BIN, "models"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                **_TEXT,
                timeout=20,
                **_spawn_kwargs(),
            )
            if proc.returncode == 0:
                names = _parse_models_output(proc.stdout or "")
        except (OSError, subprocess.SubprocessError):
            names = []
        _AGY_MODELS_CACHE = names
        return names


def validate_model(model: Optional[str]) -> Optional[str]:
    """Return `model` unchanged, or raise ValueError if it isn't a known agy slug.

    agy accepts --model but SILENTLY falls back to the settings.json default on an
    unknown model (pre-1.1.2), so a typo would quietly run the wrong one. We reject
    it up front, listing the valid slugs. If the list can't be read (agy missing or
    the models call failed), validation is skipped and the value passes through
    — better than blocking a real model just because we couldn't enumerate them.

    Matching is EXACT against `agy models`, so agy 1.1.5's switch from human labels
    ("Gemini 3.1 Pro (High)") to slugs (gemini-3.1-pro-high) is surfaced here as a
    loud rejection listing the new names, not a silent run on the wrong model. The
    same exactness is what turns a model agy RETIRES into a loud failure rather
    than a silent fallback — 1.1.25 dropped the whole gemini-3.5-flash family.
    """
    if not model:
        return model
    known = list_agy_models()
    if known and model not in known:
        raise ValueError(f"unknown agy model {model!r}; expected one of: {', '.join(known)}")
    return model


def _agy_base_args(timeout_s: int, plan: bool = False) -> list[str]:
    """agy's argv prefix: binary, print deadline, and the headless permission opt-out.

    --dangerously-skip-permissions is REQUIRED as of agy 1.1.3, which stopped
    headless `-p` from auto-approving tools: a tool needing permission is now
    soft-denied (print mode can't prompt), agy exits 0 having done nothing, and
    only stderr names the allow-rule. That denies even a plain file read, so
    without this flag every tool-using bridge call is dead — verified on 1.1.3.
    The flag is what agy's own notice recommends; it restores the pre-1.1.3
    behaviour the SECURITY note in the module docstring already describes.

    ORDER MATTERS: it must come BEFORE `-p`. agy's `-p`/`--print` takes the prompt
    as its VALUE, so `-p --dangerously-skip-permissions <task>` parses the flag as
    the prompt and silently drops <task> (verified on 1.1.3 — agy replied with a
    description of the flag). Every caller appends `-p` LAST for this reason.

    --disable-slash-commands is REQUIRED as of agy 1.1.9, which made print mode
    expand slash commands and skills instead of passing them to the model as text.
    A prompt whose FIRST token names a registered command is then executed as that
    command and never reaches the model — verified live on 1.1.10 through this
    bridge: antigravity_ask("/help") returned agy's own help page, not an answer.
    That is not merely wrong output: agy's registered set includes side-effecting
    commands (`/goal` starts an autonomous long-running task, `/schedule` creates
    cron jobs), and bridge prompts routinely carry text the caller did not author,
    so an untrusted string starting with "/schedule ..." would run it. Prompts
    beginning with a POSIX path ("/etc/hosts …") are unaffected — they match no
    command — but that is luck, not a boundary. Version-gated because the flag
    does not exist before 1.1.9 (see supports_disable_slash_commands).

    Set AGY_BRIDGE_ALLOW_SLASH_COMMANDS=1 to keep agy's expansion — the deliberate
    opt-in for callers who WANT `-p "/my-skill <args>"` to invoke a skill.

    `plan` adds agy 1.1.12's `--mode plan`, and it REPLACES the slash shield rather
    than joining it: agy refuses the combination, printing "warning: --mode plan has
    no effect while slash command expansion is disabled" and then running with plan
    mode off. Passing both would therefore produce exactly the outcome plan mode
    exists to prevent — an unrestricted run that looks restricted — so the flags are
    exclusive here and _guard_plan_mode_prompt carries the shield client-side
    instead. Gate on supports_plan_mode() and validate via _check_plan_mode BEFORE
    calling; below 1.1.12 agy parses --mode and ignores it in print mode.

    --dangerously-skip-permissions STAYS in plan mode, which sounds contradictory
    and is not: verified on 1.1.20 that plan survives it. A file write and a shell
    command were both refused and diverted into a plan artifact under agy's own
    brain dir even when the prompt insisted ("do it now", "do not plan it"), while
    a file READ answered normally. Dropping the skip flag would instead reintroduce
    1.1.3's soft-deny and kill the reads plan mode exists to allow. Note what this
    does and does not buy: it is agent-enforced, like Copilot's and Cursor's modes,
    NOT an OS boundary — see the SECURITY note in the module docstring.
    """
    args = [AGY_BIN, "--print-timeout", f"{timeout_s}s", "--dangerously-skip-permissions"]
    if plan:
        args.extend(["--mode", "plan"])
    elif supports_disable_slash_commands() and not _env_truthy("AGY_BRIDGE_ALLOW_SLASH_COMMANDS"):
        args.append("--disable-slash-commands")
    return args


def _build_agy_args(
    prompt: str,
    workspace: str,
    continue_conv: bool,
    timeout_s: int,
    model: Optional[str] = None,
    output_format: Optional[str] = None,
    plan: bool = False,
    schema: Optional[str] = None,
) -> tuple[list[str], Optional[str]]:
    """Build agy's argv and resolve the pinned conversation id for continue mode.

    `model` (when given) becomes agy's `--model <label>` — verified working in
    print mode on 1.0.16; validate it via validate_model before calling this.

    `output_format` adds agy 1.1.8's `--output-format` — "json" for a single result
    object (the plain ask/continue path, see _parse_json_result) or "stream-json"
    for the live NDJSON event stream (the watched runners, see _StreamWatch). It is
    OPT-IN per caller, not folded into _agy_base_args, because the swarm workers
    build their own argv there and have no use for it. Gate it on
    supports_json_output() — the flag does not exist before 1.1.8.

    The base args carry --dangerously-skip-permissions (see _agy_base_args: it is
    load-bearing on agy 1.1.3+, not the no-op it was through 1.1.2). We still do
    NOT pass --sandbox: on 1.0.6+ it blocks only terminal/shell commands, not
    write_to_file/FS or network egress, so it is no real boundary; and a
    sandbox-blocked terminal run writes no JSONL transcript for us to read. No agy
    flag makes print mode safe; see the module docstring's SECURITY note.
    """
    args = _agy_base_args(timeout_s, plan)
    if output_format:
        args.extend(["--output-format", output_format])
    if schema:
        # agy 1.1.8's --json-schema, same release as --output-format json. Pairs with
        # either JSON form: on "json" the validated object lands in the result's
        # structured_output field, on "stream-json" in the terminal result event.
        # Passed as inline text rather than a temp file (agy accepts both) — argv is
        # a list, so there is no quoting to get wrong and no file to clean up.
        args.extend(["--json-schema", schema])
    if model:
        args.extend(["--model", model])
    pinned_conv: Optional[str] = None
    if continue_conv:
        # Pin to the exact conversation rooted at this workspace instead of `-c`
        # ("most recent"), which could resume a conversation started elsewhere in
        # between. Prefer the id agy itself reported for our last run here (exact,
        # and immune to anything else rewriting agy's shared state), then the
        # workspace's entry in last_conversations.json, then -c.
        pinned_conv = _recorded_conv_id(workspace) or _read_last_conv_id(workspace)
        if pinned_conv:
            args.extend(["--conversation", pinned_conv])
        else:
            args.append("-c")
    args.extend(["-p", prompt])
    return args, pinned_conv


def _run_agy(
    prompt: str,
    workspace: str,
    continue_conv: bool,
    timeout_s: int,
    model: Optional[str] = None,
    plan: bool = False,
    schema: Optional[str] = None,
    pin: bool = True,
) -> str:
    """Run one agy print-mode call under _AGY_LOCK and return its answer.

    `pin=False` runs without recording the conversation id for this workspace, so
    the run cannot become the thread a later antigravity_continue resumes. The
    swarm's serialized fallback (swarm._run_text_worker_serialized) passes it for
    the same reason codex workers pass `pin=False`: a swarm is N independent
    one-shot tasks, and letting the last one to finish claim the workspace's
    continue slot would silently redirect the user's next continue.
    """
    os.makedirs(workspace, exist_ok=True)  # agy's cwd must exist (mirrors the swarm)
    use_json = supports_json_output()
    args, pinned_conv = _build_agy_args(
        prompt,
        workspace,
        continue_conv,
        timeout_s,
        model,
        output_format="json" if use_json else None,
        plan=plan,
        schema=schema,
    )

    with _AGY_LOCK:
        start = time.time()
        log.debug(
            "running agy: continue=%s pinned=%s workspace=%s timeout=%ss prompt_chars=%d",
            continue_conv,
            pinned_conv,
            workspace,
            timeout_s,
            len(prompt),
        )
        proc = subprocess.run(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            **_TEXT,
            timeout=timeout_s + 30,
            **_spawn_kwargs(),  # keep agy's TTY writes out of the host terminal
        )
        log.debug("agy exited %s in %.1fs", proc.returncode, time.time() - start)
        if proc.returncode != 0:
            raise RuntimeError(
                f"agy exited {proc.returncode}\n"
                f"stderr: {proc.stderr[-1000:]}\n"
                f"stdout: {proc.stdout[-500:]}"
            )

        # agy 1.0.15 fixed the print-mode stdout bug (Windows): `agy -p` now writes
        # its clean final answer straight to stdout — verified empirically that it
        # carries only the answer, not the tool-calling narration. Prefer stdout
        # when present: it needs no transcript-schema parsing and no flush poll,
        # sidestepping the bridge's biggest fragility (agy's undocumented JSONL/
        # SQLite formats). Older agy — and, per the 1.0.15 changelog, non-Windows
        # platforms — still leave stdout empty; those fall through to the
        # transcript/.db scrape below, unchanged. A --sandbox run likewise writes
        # nothing to stdout, so it too uses the fallback.
        stdout_answer = (proc.stdout or "").strip()

        # agy 1.1.8+: stdout is a structured result object rather than bare text
        # (we asked for it via --output-format json). Parsing it beats trusting the
        # text layout — `response` is a contractual field, and `conversation_id`
        # tells us EXACTLY which conversation this run used instead of inferring it
        # from shared state. Record that id so a later continue pins to this thread.
        # A None result means agy gave us plain text after all (an agy that ignored
        # the flag), which falls through to the text path below unchanged.
        result = _parse_json_result(stdout_answer) if use_json else None
        if schema is not None and result is None:
            raise RuntimeError(
                "agy produced no structured result object for a --json-schema run "
                "(it fell back to plain text). The schema contract cannot be met, and "
                "returning prose to a caller that asked for a shape would fail at their "
                "end instead of here."
            )
        if result is not None:
            conv_id = result.get("conversation_id") or ""
            if conv_id:
                if pin:
                    _record_conv_id(workspace, conv_id)
                pinned_conv = pinned_conv or conv_id
            status = result.get("status")
            if schema is not None:
                # The schema run's contract is structured_output, not `response` —
                # see _structured_answer for why the two are not interchangeable.
                return _structured_answer(result, conv_id)
            answer = (result.get("response") or "").strip()
            log.debug(
                "agy json result: status=%s conv=%s answer_chars=%d",
                status,
                conv_id,
                len(answer),
            )
            if answer:
                # An unexpected status with a real answer is not worth discarding
                # the answer over — agy's status vocabulary may grow — so note it
                # and hand the answer back.
                if status and status != "SUCCESS":
                    log.warning("agy reported status=%s but returned an answer", status)
                return answer
            if status and status != "SUCCESS":
                raise RuntimeError(
                    f"agy reported status={status} with no response "
                    f"(conversation {conv_id or 'unknown'})."
                    + (f"\nstderr: {proc.stderr[-1000:]}" if proc.stderr else "")
                )
            # SUCCESS but an empty response: fall through to the transcript scrape.
        elif stdout_answer:
            log.debug("using agy stdout answer (%d chars)", len(stdout_answer))
            return stdout_answer

        # stdout empty (older agy / non-Windows / --sandbox): read agy's transcript.
        # agy has already exited 0, so the transcript is usually ready at once;
        # poll briefly to absorb filesystem-flush lag instead of a fixed sleep.
        deadline = time.time() + _RESPONSE_POLL_DEADLINE_S
        while True:
            try:
                return _resolve_and_read(pinned_conv, workspace, start)
            except RuntimeError as exc:
                # Retries transient resolution/flush lag. A persistent failure
                # (e.g. the SQLite-migration "transcript not found" from
                # _read_response) is caught here too and surfaces only after the
                # deadline; that small delay is an accepted tradeoff for keeping
                # this loop simple.
                if time.time() >= deadline:
                    # agy exited 0 but wrote neither stdout nor a readable
                    # transcript, so the scrape failure is a symptom, not the
                    # cause — and agy puts the cause on stderr (1.1.3+ soft-denies
                    # a tool print mode can't prompt for and names the allow-rule
                    # needed). Surface that instead of a bare "transcript not
                    # found", which reads as a bridge/schema bug.
                    stderr_tail = (proc.stderr or "").strip()
                    if stderr_tail:
                        raise RuntimeError(
                            f"{exc}\nagy exited 0 but produced no answer — "
                            f"stderr: {stderr_tail[-1000:]}"
                        ) from exc
                    raise
                time.sleep(_RESPONSE_POLL_INTERVAL_S)


async def _run_with_progress(
    run_fn, args: tuple, ctx: "Optional[Context]", timeout_s: int, label: str = "agy"
) -> str:
    """Run a blocking CLI call off the event loop, emitting MCP progress while it works.

    `run_fn(*args)` is the synchronous runner (e.g. _run_agy or
    codex_bridge.run_codex); it executes in a worker thread so the event loop stays
    free to send progress. When `ctx` is None — direct/test calls, or a client that
    sent no progressToken — this is just a threaded call with no notifications.
    Progress is a coarse time bar (elapsed / timeout_s): neither CLI exposes a real
    percentage, so a smooth elapsed fraction is the honest approximation. `label`
    names the backend in the progress message ("agy" or "codex"). Progress
    reporting is best-effort and never fails the run.
    """
    if ctx is None:
        return await asyncio.to_thread(run_fn, *args)

    task = asyncio.ensure_future(asyncio.to_thread(run_fn, *args))
    start = time.monotonic()
    while not task.done():
        await asyncio.sleep(_PROGRESS_NOTIFY_INTERVAL_S)
        elapsed = time.monotonic() - start
        try:
            await ctx.report_progress(
                progress=min(elapsed, float(timeout_s)),
                total=float(timeout_s),
                message=f"{label} running ({int(elapsed)}s)",
            )
        except Exception:  # noqa: BLE001 — progress is cosmetic; never break the run
            pass
    return await task  # re-raises any error from the worker thread


def _existing_conv_names() -> set[str]:
    """Names of brain conversation dirs that exist right now (snapshot)."""
    if not BRAIN_DIR.exists():
        return set()
    return {c.name for c in BRAIN_DIR.iterdir() if c.is_dir()}


def _newest_new_conv(start: float, exclude: set[str]) -> Optional[str]:
    """Newest brain dir touched since `start` whose name is NOT in `exclude`.

    Used to lock streaming onto *this* run's brand-new conversation, ignoring any
    other recently-finished one — without this, agy's initial blind window (the
    transcript can stay empty ~15 s) would resolve to a prior conversation and
    emit its steps as if they were ours.
    """
    if not BRAIN_DIR.exists():
        return None
    best, best_mtime = None, start - 2
    for child in BRAIN_DIR.iterdir():
        if not child.is_dir() or child.name in exclude:
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = child.name, mtime
    return best


# Live "watch" viewer state, served over a localhost HTTP server to a browser page.
# Keyed by a run id so CONCURRENT watched runs (e.g. a codex_ask and a copilot_ask
# at once — they don't share _AGY_LOCK) each get their own window + state instead of
# clobbering one shared one. Sequential runs reuse the "main" slot and its open
# window; a run that starts while "main" is still working gets a fresh id + window.
_MAIN = "main"
_WATCH_RUNS: dict[str, dict] = {}
_WATCH_LOCK = threading.Lock()
_WATCH_SERVER: Optional[tuple] = None  # (httpd, port, thread) singleton
_VIEWER_ALIVE_S = 4.0  # a /events poll within this window means a viewer is still open


def _watch_state(rid, title, start, timeout, backend, prompt, history, last_poll) -> dict:
    return {
        "id": rid,
        "title": title,  # short single-line caption (first prompt line, ≤200 chars)
        "prompt": prompt or title,  # the FULL untruncated prompt, shown in the bubble
        "history": list(history or []),  # prior turns (continue mode); [] for a fresh ask
        "status": "working",  # working | done | error
        "started": start,
        "elapsed": 0.0,
        "timeout": timeout,  # this run's timeout_s, for the time progress bar
        "answer": "",
        "image": "",  # absolute path to a generated image to show, or ""
        "events": [],  # list of {kind, text, t}
        "backend": backend,  # "agy" | "codex" | "copilot" (shown in the header)
        "last_poll": last_poll,  # last /events poll time (0 = never); drives window reuse
    }


_WATCH_IDLE = {
    "status": "idle",
    "started": 0.0,
    "elapsed": 0.0,
    "timeout": 0.0,
    "title": "",
    "prompt": "",
    "history": [],
    "answer": "",
    "image": "",
    "events": [],
    "backend": "agy",
}


def _watch_evict_locked(now: float) -> None:
    """Drop finished, unwatched non-main runs so the map can't grow without bound."""
    stale = [
        r
        for r, s in _WATCH_RUNS.items()
        if r != _MAIN and s["status"] in ("done", "error") and now - s["last_poll"] > 60
    ]
    for rid in stale:
        _WATCH_RUNS.pop(rid, None)


def _watch_begin(
    title: str,
    start: float,
    timeout: float = 0.0,
    backend: str = "agy",
    prompt: str = "",
    history: Optional[list] = None,
) -> str:
    """Start a watched run and return its id. Reuses the "main" slot for the common
    sequential case; a run that begins while "main" is still working gets a fresh id
    (and its own window) so concurrent runs never clobber each other's window."""
    with _WATCH_LOCK:
        _watch_evict_locked(start)
        main = _WATCH_RUNS.get(_MAIN)
        if main is not None and main["status"] == "working":
            rid, last_poll = uuid.uuid4().hex, 0.0
        else:
            # keep the open window's poll time so _open_watch_window can reuse it
            rid, last_poll = _MAIN, (main["last_poll"] if main else 0.0)
        _WATCH_RUNS[rid] = _watch_state(
            rid, title, start, timeout, backend, prompt, history, last_poll
        )
        return rid


def _watch_set_image(rid: str, path: str) -> None:
    with _WATCH_LOCK:
        st = _WATCH_RUNS.get(rid)
        if st is not None:
            st["image"] = path


def _watch_append(rid: str, events: list[dict]) -> None:
    with _WATCH_LOCK:
        st = _WATCH_RUNS.get(rid)
        if st is not None:
            st["events"].extend(events)
            st["elapsed"] = round(time.time() - st["started"], 1)


def _watch_finish(rid: str, status: str, answer: str, elapsed: float) -> None:
    with _WATCH_LOCK:
        st = _WATCH_RUNS.get(rid)
        if st is not None:
            st["status"] = status
            st["answer"] = answer
            st["elapsed"] = round(elapsed, 1)


def _watch_snapshot(rid: str = _MAIN) -> dict:
    with _WATCH_LOCK:
        st = _WATCH_RUNS.get(rid)
        if st is None:
            return dict(_WATCH_IDLE)
        snap = dict(st)
        snap["events"] = list(st["events"])
        return snap


def _watch_mark_poll(rid: str) -> None:
    with _WATCH_LOCK:
        st = _WATCH_RUNS.get(rid)
        if st is not None:
            st["last_poll"] = time.time()


# Per-process secret for the watch viewer. The bridge embeds it in every URL it
# opens; a request without it is refused. Regenerated each run, never persisted.
_WATCH_TOKEN = secrets.token_urlsafe(16)

# Hostnames a watch request may claim. Anything else is a rebinding attempt.
_WATCH_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _watch_authorized(host_header: Optional[str], path: str) -> bool:
    """Whether a watch-server request may be served. Two independent checks.

    The viewer serves the prompts, the answers, and the real commands the agents
    ran. It binds 127.0.0.1 on an ephemeral port, but binding alone is not an
    access boundary, and the server is started lazily and never stopped — one
    watch=true run leaves the port open for the life of the process.

    HOST: the Host header's hostname must be a loopback literal. A browser
    reaches a DNS-rebinding target under the ATTACKER's hostname, so rejecting
    anything that isn't 127.0.0.1/localhost kills that vector outright — it is
    the standard defense, and cheaper than any origin allowlist. A missing Host
    is refused; every browser sends one, and the browser we launch is the only
    client this server has.

    TOKEN: a per-process secret carried as `k` in the query string, compared with
    hmac.compare_digest. The Host check cannot stop another LOCAL process (or
    another user on a shared machine) from simply connecting, since it can send
    whatever Host it likes; the token can, and costs nothing because the bridge
    opens every one of these URLs itself.
    """
    from urllib.parse import parse_qs, urlparse

    host = (host_header or "").strip()
    if not host:
        return False
    if host.startswith("["):  # IPv6 literal, optionally followed by :port
        hostname = host[: host.find("]") + 1]
    else:
        hostname = host.rsplit(":", 1)[0] if ":" in host else host
    if hostname.lower() not in _WATCH_LOCAL_HOSTS:
        return False
    token = parse_qs(urlparse(path).query).get("k", [""])[0]
    return hmac.compare_digest(token, _WATCH_TOKEN)


def _watch_url(port: int, rid: str) -> str:
    """The viewer URL for `rid`, carrying the token the server requires."""
    return f"http://127.0.0.1:{port}/?id={rid}&k={_WATCH_TOKEN}"


def _watch_image_allowed(path: str) -> bool:
    with _WATCH_LOCK:
        return bool(path) and any(s["image"] == path for s in _WATCH_RUNS.values())


class _StreamWatch:
    """Turns agy's `--output-format stream-json` stdout into live watch events.

    The 1.1.8 replacement for _WatchFeed's transcript polling: instead of re-reading
    an undocumented JSONL file on a timer and inferring which conversation is ours,
    we consume the typed NDJSON events agy emits as it works. Verified live that they
    arrive INCREMENTALLY (a 17 s run spread its 18 events over 12.4 s), which is the
    whole premise — a buffered stream would make the viewer useless.

    Event shapes (agy 1.1.8, verified live):
      {"event":"init","conversation_id":…,"init":{cwd,tools,permission_mode}}
      {"event":"step_update","step_update":{conversation_id,step_index,state,
          step_type,tool_name?,text_delta?,tool_info?{name,parameters,output},…}}
      {"event":"result","result":{conversation_id,status,response,usage,…}}

    `state` is ACTIVE then DONE for a given step_index. agent_response text arrives
    as INCREMENTAL `text_delta` fragments that must be concatenated per step (the
    DONE update's delta is usually just the trailing newline), so narration is
    emitted once a step completes rather than per fragment. Tool steps carry
    `tool_info.parameters.CommandLine` as a REAL nested object — unlike the
    transcript, which stores tool args JSON-encoded inside a string and needs
    _clean_tool_arg to unwrap.

    Thread-safe by construction: feed_line runs on the stdout reader thread and only
    touches this instance plus _watch_append (which takes _WATCH_LOCK).
    """

    def __init__(self, rid: str, start: float) -> None:
        self._rid = rid
        self._start = start
        self._text: dict[int, list[str]] = {}  # step_index -> accumulated deltas
        self._emitted: set[int] = set()  # step indices already turned into events
        self.conv_id: Optional[str] = None
        self.result: Optional[dict] = None
        self.saw_event = False  # any well-formed event at all (i.e. agy honoured the flag)

    def _emit(self, kind: str, text: str) -> None:
        if not text:
            return
        _watch_append(
            self._rid, [{"kind": kind, "text": text, "t": round(time.time() - self._start, 1)}]
        )

    def feed_line(self, line: str) -> None:
        """Parse one NDJSON line and append any watch events it implies.

        Never raises: a malformed or unrecognised line is ignored, so an agy that
        changes or drops the format degrades to "no live steps" rather than killing
        the run (the answer still resolves from the result event or the transcript).
        """
        line = (line or "").strip()
        if not line.startswith("{"):
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("event")
        self.saw_event = True
        if event.get("conversation_id") and not self.conv_id:
            self.conv_id = event["conversation_id"]

        if kind == "result":
            result = event.get("result")
            if isinstance(result, dict):
                self.result = result
                if result.get("conversation_id"):
                    self.conv_id = result["conversation_id"]
            return
        if kind != "step_update":
            return

        step = event.get("step_update")
        if not isinstance(step, dict):
            return
        if step.get("conversation_id") and not self.conv_id:
            self.conv_id = step["conversation_id"]
        idx = step.get("step_index")
        stype, state = step.get("step_type"), step.get("state")

        if stype == "agent_response":
            if isinstance(idx, int) and step.get("text_delta"):
                self._text.setdefault(idx, []).append(step["text_delta"])
            if state == "DONE" and isinstance(idx, int) and idx not in self._emitted:
                self._emitted.add(idx)
                full = "".join(self._text.get(idx, [])).strip()
                if full:
                    self._emit("narration", full.splitlines()[0][:200])
        elif stype == "tool":
            info = step.get("tool_info") or {}
            params = info.get("parameters") or {}
            if state == "ACTIVE":
                cmd = params.get("CommandLine") or info.get("name") or step.get("tool_name") or ""
                self._emit("command", str(cmd)[:200])
            elif state == "DONE":
                self._emit("result", "command finished")


class _WatchFeed:
    """Locks onto this run's conversation and turns new transcript entries into
    rich step events (narration / command / result) appended to the shared watch
    state. For a new conversation it locks onto the first brain dir that appears
    after launch and didn't pre-exist, and never switches away from it.

    The pre-1.1.8 fallback. agy 1.1.8+ runs use _StreamWatch instead, which reads
    agy's own typed event stream rather than scraping this undocumented transcript;
    this path stays for older agy (and is why _clean_tool_arg still exists)."""

    def __init__(self, pinned_conv: Optional[str], start: float, rid: str = _MAIN) -> None:
        self._start = start
        self._rid = rid
        self._pre = set() if pinned_conv else _existing_conv_names()
        self._conv = pinned_conv
        self._cursor = len(_transcript_entries(pinned_conv)) if pinned_conv else 0

    @property
    def conv(self) -> Optional[str]:
        return self._conv

    def pump(self) -> None:
        if self._conv is None:
            self._conv = _newest_new_conv(self._start, self._pre)
            if self._conv is None:
                return
            self._cursor = 0
        entries = _transcript_entries(self._conv)
        new_events = []
        for entry in entries[self._cursor :]:
            for kind, text in _entry_to_watch_lines(entry):
                t = round(time.time() - self._start, 1)
                new_events.append({"kind": kind, "text": text, "t": t})
        self._cursor = max(self._cursor, len(entries))
        if new_events:
            _watch_append(self._rid, new_events)


# Self-contained dark-theme page: polls /events and renders steps live, with a
# spinner while working and the final answer card on completion. Resets its view
# when `started` changes, so one browser tab can be reused across runs.
# Terminal-styled page with typewriter step reveal and a Markdown-rendered answer.
# __WIN_W__/__WIN_H__ are substituted per request (see _watch_html).
_WATCH_HTML = """<!doctype html><html lang="en" translate="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="google" content="notranslate">
<title>Agent Intern — watching agy</title>
<style>
:root{
 --bg:#0a0c10;--fg:#d6d6d6;--dim:#6a7480;--green:#3fdf7f;--cyan:#5cd6e6;
 --red:#ff6b6b;--bd:#191c22;--code:#06080b;
 --ubg:#13251c;--ubd:#2a5a41;--uc:#e9f6ee;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg)}
body{
 color:var(--fg);
 font:13px/1.6 ui-monospace,"Cascadia Mono",Consolas,"DejaVu Sans Mono",monospace;
}
::-webkit-scrollbar{width:9px}::-webkit-scrollbar-thumb{background:#23262d;border-radius:6px}
.top{position:sticky;top:0;z-index:3;background:var(--bg)}
header{
 display:flex;align-items:center;gap:8px;
 padding:8px 13px;background:#0d0f14;border-bottom:1px solid var(--bd);
 font-size:12px;color:var(--dim);
}
.name{color:var(--green);font-weight:700;text-shadow:0 0 10px rgba(63,223,127,.4)}
.wlabel{color:var(--dim)}
.pill{margin-left:auto;display:flex;align-items:center;gap:7px;font-variant-numeric:tabular-nums}
.dot{
 width:7px;height:7px;border-radius:50%;background:var(--cyan);
 box-shadow:0 0 9px var(--cyan);animation:pop .45s ease;
}
.dot.err{background:var(--red);box-shadow:0 0 8px var(--red)}
@keyframes pop{0%{transform:scale(.2)}55%{transform:scale(1.5)}100%{transform:scale(1)}}
.spin{
 color:var(--green);display:inline-block;width:9px;text-align:center;
 text-shadow:0 0 8px rgba(63,223,127,.6);
}
#elapsed{color:#556}
.gbar{height:2px;background:#11141a}
.gfill{
 height:100%;width:0;background:linear-gradient(90deg,var(--green),var(--cyan));
 box-shadow:0 0 8px rgba(92,214,230,.5);transition:width .5s linear;
}
/* --- chat conversation --- */
#chat{
 max-width:960px;margin:0 auto;padding:16px 14px 46px;
 display:flex;flex-direction:column;gap:11px;
}
.msg{display:flex;max-width:100%;animation:rise .28s ease both}
@keyframes rise{from{opacity:0;transform:translateY(7px)}}
.msg.user{justify-content:flex-end}
.msg.bot{justify-content:flex-start}
.role{font-size:9px;letter-spacing:1.4px;font-weight:700;opacity:.7;margin:0 3px 3px}
.wrap{display:flex;flex-direction:column;max-width:84%}
.msg.user .wrap{align-items:flex-end}
.bubble{
 position:relative;padding:9px 13px;border-radius:15px;word-break:break-word;
 box-shadow:0 1px 2px rgba(0,0,0,.25);
}
.bubble.user{
 background:var(--ubg);border:1px solid var(--ubd);color:var(--uc);
 border-bottom-right-radius:5px;
}
.bubble.bot{
 background:#0c0e13;border:1px solid var(--bd);border-bottom-left-radius:5px;
}
.btext{white-space:pre-wrap;word-break:break-word}
.bubble.user.clampable .btext{
 max-height:7.4em;overflow:hidden;
 -webkit-mask-image:linear-gradient(180deg,#000 72%,transparent);
}
.bubble.user.expanded .btext{max-height:60vh;overflow:auto;-webkit-mask-image:none}
.exp{
 margin-top:6px;font-size:10.5px;color:var(--cyan);cursor:pointer;
 user-select:none;opacity:.85;
}
.exp:hover{opacity:1}
/* --- live step trace (assistant "thinking") --- */
.trace{
 background:#0b0d12;border:1px solid var(--bd);border-radius:13px;
 border-bottom-left-radius:5px;overflow:hidden;max-width:84%;
}
.trace-head{
 display:flex;align-items:center;gap:8px;padding:7px 12px;cursor:pointer;
 color:var(--dim);font-size:11px;
}
.trace-head:hover{background:#0f1218}
.trace-body{padding:1px 12px 9px;display:flex;flex-direction:column;gap:3px}
.trace.collapsed .trace-body{display:none}
.chev{margin-left:auto;color:var(--green);opacity:.7;transition:transform .2s}
.trace.collapsed .chev{transform:rotate(-90deg)}
.ty{display:inline-flex;gap:3px;align-items:center}
.ty i{width:4px;height:4px;border-radius:50%;background:var(--green);opacity:.4;
 animation:ty 1s infinite}
.ty i:nth-child(2){animation-delay:.16s}.ty i:nth-child(3){animation-delay:.32s}
@keyframes ty{0%,60%,100%{opacity:.35}30%{opacity:1}}
.step{display:flex;gap:8px;align-items:baseline;font-size:11.5px;animation:rise .2s ease both}
.step .sym{width:11px;flex:none}
.step .txt{white-space:pre-wrap;word-break:break-word;color:#c7ccd2}
.step.command .sym{color:var(--green)}.step.command .txt{color:#eaeef2}
.step.narration .sym,.step.narration .txt{color:var(--cyan)}
.step.result .sym,.step.result .txt{color:var(--green);opacity:.55}
/* --- markdown answer card --- */
.md .h{font-weight:700;margin:12px 0 5px;color:#cdd9e5}
.md .h1{font-size:16px;color:#fff}.md .h2{font-size:14px}
.md .h3{font-size:12.5px;color:var(--green)}
.md .p{margin:3px 0;white-space:pre-wrap;word-break:break-word}
.md .li{display:flex;gap:8px;margin:2px 0}
.md .bul{color:var(--green);flex:none;min-width:14px;text-align:right}
.md .lit{white-space:pre-wrap;word-break:break-word}
.md pre.code{
 background:var(--code);border-left:2px solid var(--green);border-radius:4px;
 padding:9px 11px;margin:7px 0;overflow:auto;white-space:pre;color:#e9efe9;
}
.md code{background:#16191f;padding:1px 5px;border-radius:4px;color:#9fe6ad}
.md .lnk{color:var(--cyan);border-bottom:1px dotted #2a6b73}
.md strong{color:#fff}
.md .copy{
 position:absolute;top:7px;right:8px;background:#0e1218;border:1px solid var(--bd);
 color:var(--dim);font:inherit;font-size:10px;padding:2px 8px;border-radius:5px;
 cursor:pointer;opacity:0;transition:opacity .15s,color .15s,border-color .15s;
}
.bubble.bot:hover .copy{opacity:.92}
.md .copy:hover{color:var(--green);border-color:#2a3340}
.shot{max-width:100%;border:1px solid var(--bd);border-radius:12px;display:block;
 animation:rise .3s ease both}
.hint{
 position:fixed;bottom:7px;right:12px;color:#3b414a;font-size:10.5px;
 pointer-events:none;user-select:none;
}
.jump{
 position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#12161d;
 border:1px solid #2a3340;color:var(--cyan);font-size:11.5px;padding:5px 13px;
 border-radius:20px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.5);
 animation:rise .3s;z-index:4;
}
</style></head><body>
<div class="top">
 <header>
  <span class="name">Agent Intern</span><span class="wlabel" id="wlabel">— watching agy</span>
  <span class="pill" id="pill">
   <span class="dot" id="dot" style="display:none"></span>
   <span class="spin" id="spin"></span>
   <span id="status">working</span><span id="elapsed"></span>
  </span>
 </header>
 <div class="gbar"><div class="gfill" id="gfill"></div></div>
</div>
<div id="chat"></div>
<div class="jump" id="jump" style="display:none">↓ jump to latest</div>
<div class="hint">⏎ / esc · close</div>
<script>
try{window.resizeTo(__WIN_W__,__WIN_H__);}catch(e){}
document.addEventListener("keydown",e=>{
 if(e.key==="Enter"||e.key==="Escape"){try{window.close();}catch(_){}}
});
const SYM={narration:"▸",command:"$",result:"✓"};
let started=null,seen=0,finished=false,follow=true,traceEl=null,traceBody=null;
const RID=new URLSearchParams(location.search).get("id")||"main";
const K=encodeURIComponent(new URLSearchParams(location.search).get("k")||"");
const $=id=>document.getElementById(id);
const chat=()=>$("chat");
function toBottom(){window.scrollTo(0,document.body.scrollHeight);}
function maybeBottom(){if(follow)toBottom();}
window.addEventListener("scroll",()=>{
 follow=window.innerHeight+window.scrollY>=document.body.scrollHeight-44;
 $("jump").style.display=follow?"none":"";
});
$("jump").addEventListener("click",()=>{follow=true;$("jump").style.display="none";toBottom();});
function copyText(txt,btn){
 navigator.clipboard.writeText(txt).then(()=>{
  const o=btn.textContent;btn.textContent="copied ✓";setTimeout(()=>btn.textContent=o,1200);
 }).catch(()=>{});
}
const FR="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";let fi=0,spinT=null;
function startSpin(){
 if(spinT)return;
 spinT=setInterval(()=>{$("spin").textContent=FR[fi=(fi+1)%FR.length];},80);
}
function stopSpin(){if(spinT){clearInterval(spinT);spinT=null;}$("spin").textContent="";}
function esc(s){return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function inl(s){
 return s.replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g,"<span class='lnk'>$1</span>")
         .replace(/`([^`]+)`/g,(m,c)=>"<code>"+c+"</code>")
         .replace(/\\*\\*([^*]+)\\*\\*/g,"<strong>$1</strong>");
}
function md(src){
 const lines=esc(src).split("\\n"),out=[];let inC=false,code="";
 for(const ln of lines){
  const f=ln.match(/^```(\\w*)\\s*$/);
  if(f){if(!inC){inC=true;code="";}else{inC=false;
   out.push("<pre class='code'>"+code.replace(/\\n$/,"")+"</pre>");}continue;}
  if(inC){code+=ln+"\\n";continue;}
  const h=ln.match(/^(#{1,6})\\s+(.*)$/);
  if(h){out.push("<div class='h h"+h[1].length+"'>"+inl(h[2])+"</div>");continue;}
  const b=ln.match(/^\\s*[-*]\\s+(.*)$/);
  if(b){out.push("<div class='li'><span class='bul'>•</span>"+
   "<span class='lit'>"+inl(b[1])+"</span></div>");continue;}
  const n=ln.match(/^\\s*(\\d+)\\.\\s+(.*)$/);
  if(n){out.push("<div class='li'><span class='bul'>"+n[1]+".</span>"+
   "<span class='lit'>"+inl(n[2])+"</span></div>");continue;}
  if(ln.trim()==="")continue;
  out.push("<div class='p'>"+inl(ln)+"</div>");
 }
 if(inC)out.push("<pre class='code'>"+code+"</pre>");
 return out.join("");
}
// A user prompt as a right-aligned chat bubble; long ones clamp with an expander.
function userBubble(text,role){
 const m=document.createElement("div");m.className="msg user";
 const wrap=document.createElement("div");wrap.className="wrap";
 if(role){const r=document.createElement("div");r.className="role";
  r.textContent=role;wrap.appendChild(r);}
 const b=document.createElement("div");b.className="bubble user clampable";
 const t=document.createElement("div");t.className="btext";t.textContent=text||"";
 b.appendChild(t);wrap.appendChild(b);m.appendChild(wrap);chat().appendChild(m);
 requestAnimationFrame(()=>{
  if(t.scrollHeight>t.clientHeight+2){
   const x=document.createElement("div");x.className="exp";x.textContent="show more ▾";
   x.onclick=()=>{const e=b.classList.toggle("expanded");
    x.textContent=e?"show less ▴":"show more ▾";maybeBottom();};
   b.appendChild(x);
  }else{b.classList.remove("clampable");}
  maybeBottom();
 });
 return b;
}
// An assistant answer as a left-aligned markdown card (with optional copy button).
function botCard(text,copy,role){
 const m=document.createElement("div");m.className="msg bot";
 const wrap=document.createElement("div");wrap.className="wrap";
 if(role){const r=document.createElement("div");r.className="role";
  r.textContent=role;wrap.appendChild(r);}
 const b=document.createElement("div");b.className="bubble bot md";
 b.innerHTML=md(text||"");
 if(copy){const cp=document.createElement("button");cp.className="copy";cp.textContent="copy";
  cp.onclick=()=>copyText(text,cp);b.appendChild(cp);}
 wrap.appendChild(b);m.appendChild(wrap);chat().appendChild(m);
 return b;
}
// The live "thinking" trace under the current prompt (streams steps; collapsible).
function newTrace(){
 const m=document.createElement("div");m.className="msg bot";
 const tr=document.createElement("div");tr.className="trace";
 tr.innerHTML="<div class='trace-head'><span class='ty'><i></i><i></i><i></i></span>"+
  "<span class='tlabel'>working…</span><span class='chev'>▾</span></div>"+
  "<div class='trace-body'></div>";
 m.appendChild(tr);chat().appendChild(m);
 tr.querySelector(".trace-head").onclick=()=>tr.classList.toggle("collapsed");
 traceEl=tr;traceBody=tr.querySelector(".trace-body");
}
function addStep(e){
 if(!traceBody)return;
 const r=document.createElement("div");r.className="step "+e.kind;
 r.innerHTML="<span class='sym'></span><span class='txt'></span>";
 r.querySelector(".sym").textContent=SYM[e.kind]||"·";
 r.querySelector(".txt").textContent=e.text;
 traceBody.appendChild(r);maybeBottom();
}
function rebuild(s){
 chat().innerHTML="";seen=0;finished=false;follow=true;traceEl=null;traceBody=null;
 $("dot").style.display="none";$("dot").classList.remove("err");
 $("gfill").style.width="0";$("gfill").style.background="";
 $("jump").style.display="none";startSpin();
 (s.history||[]).forEach(t=>{
  if(t.role==="user")userBubble(t.content,"CLAUDE");
  else botCard(t.content,false,(s.backend||"agy").toUpperCase());
 });
 userBubble(s.prompt||s.title||"","CLAUDE");
 newTrace();
}
function finish(s){
 finished=true;stopSpin();$("dot").style.display="";
 $("gfill").style.width="100%";
 if(s.status==="error"){$("dot").classList.add("err");$("gfill").style.background="var(--red)";}
 $("status").textContent=(s.status==="error"?"failed":"done")+" in "+(s.elapsed||0).toFixed(1)+"s";
 $("elapsed").textContent="";
 if(traceEl){
  traceEl.classList.add("collapsed");
  const lbl=traceEl.querySelector(".tlabel");if(lbl)lbl.textContent=seen+" steps ✓";
  const ty=traceEl.querySelector(".ty");if(ty)ty.remove();
 }
 if(s.image){
  const m=document.createElement("div");m.className="msg bot";
  const wrap=document.createElement("div");wrap.className="wrap";
  const im=document.createElement("img");im.className="shot";
  im.onload=maybeBottom;im.src="/image?k="+K+"&p="+encodeURIComponent(s.image);
  wrap.appendChild(im);m.appendChild(wrap);chat().appendChild(m);
 }
 if(s.answer)botCard(s.answer,true,(s.backend||"agy").toUpperCase());
 maybeBottom();
}
async function tick(){
 try{
  const s=await (await fetch("/events?id="+RID+"&k="+K,{cache:"no-store"})).json();
  if(s.started!==started){started=s.started;rebuild(s);}
  const back=s.backend||"agy";
  $("wlabel").textContent="— watching "+back;
  document.title="Agent Intern — "+back;
  if(!finished){
   $("status").textContent="working";
   $("elapsed").textContent=s.elapsed?" · "+s.elapsed.toFixed(1)+"s":"";
   const to=s.timeout||0;const fr=to>0?Math.min((s.elapsed||0)/to,.98):0.05;
   $("gfill").style.width=Math.round(fr*100)+"%";
  }
  for(let i=seen;i<s.events.length;i++)addStep(s.events[i]);
  seen=s.events.length;
  if((s.status==="done"||s.status==="error")&&!finished)finish(s);
 }catch(e){}
 setTimeout(tick,finished?1500:400);
}
tick();
</script></body></html>"""


def _watch_html() -> str:
    """The watch page with the configured window size substituted for resizeTo."""
    w, h = 600, 820
    try:
        parts = [int(x) for x in _WATCH_WINDOW_SIZE.split(",")]
        if len(parts) == 2:
            w, h = parts
    except ValueError:
        pass
    return _WATCH_HTML.replace("__WIN_W__", str(w)).replace("__WIN_H__", str(h))


def _ensure_watch_server() -> int:
    """Lazily start the localhost watch server (once per process); return its port.

    Binds 127.0.0.1 only — the page and events never leave the local machine.
    Started lazily but never stopped, so once any watch=true run happens the port
    stays open for the life of the process; see _watch_authorized for why that
    makes the Host check and the token load-bearing rather than decorative.
    """
    global _WATCH_SERVER
    if _WATCH_SERVER is not None:
        return _WATCH_SERVER[1]

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default stderr request logging
            pass

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (http.server API)
            if not _watch_authorized(self.headers.get("Host"), self.path):
                self.send_response(403)
                self.end_headers()
                return
            if self.path.startswith("/events"):
                from urllib.parse import parse_qs, urlparse

                rid = parse_qs(urlparse(self.path).query).get("id", [_MAIN])[0]
                _watch_mark_poll(rid)
                self._send(json.dumps(_watch_snapshot(rid)).encode("utf-8"), "application/json")
            elif self.path.startswith("/image"):
                from urllib.parse import parse_qs, urlparse

                # `p`, not the bare query string: the token now shares the query,
                # so the path must be a named parameter rather than "everything
                # after the ?".
                path = parse_qs(urlparse(self.path).query).get("p", [""])[0]
                fmt = (
                    _detect_image_format(path)
                    if _watch_image_allowed(path) and os.path.isfile(path)
                    else None
                )
                if fmt:
                    mime = {
                        "JPEG": "image/jpeg",
                        "PNG": "image/png",
                        "GIF": "image/gif",
                        "WEBP": "image/webp",
                    }[fmt]
                    with open(path, "rb") as fh:
                        self._send(fh.read(), mime)
                else:
                    self.send_response(404)
                    self.end_headers()
            else:
                self._send(_watch_html().encode("utf-8"), "text/html; charset=utf-8")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _WATCH_SERVER = (httpd, port, thread)
    log.debug("watch server on http://127.0.0.1:%d", port)
    return port


# Small dedicated viewer window. Override "WIDTH,HEIGHT" via AGY_WATCH_WINDOW_SIZE.
_WATCH_WINDOW_SIZE = os.environ.get("AGY_WATCH_WINDOW_SIZE", "560,760")


def _chromium_app_browsers() -> list[str]:
    """Paths to Chromium-based browsers that support `--app` windowed mode, so the
    viewer can open as a small chromeless window instead of a tab. Best-effort."""
    found: list[str] = []
    if os.name == "nt":
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pfx86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(pfx86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(pfx86, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
        ]
        found += [p for p in candidates if os.path.isfile(p)]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
        found += [p for p in candidates if os.path.isfile(p)]
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "brave-browser",
        "microsoft-edge",
    ):
        path = shutil.which(name)
        if path:
            found.append(path)
    return found


def _watch_viewer_live(rid: str) -> bool:
    """True if a window is currently polling /events for this run's slot (so a new
    run on the SAME slot should reuse it instead of stacking another window)."""
    with _WATCH_LOCK:
        st = _WATCH_RUNS.get(rid)
        return st is not None and (time.time() - st["last_poll"]) < _VIEWER_ALIVE_S


def _open_watch_window(url: str, rid: str = _MAIN) -> None:
    """Open the watch page in a small, dedicated window. Prefers a Chromium browser
    in `--app` mode (a sized, chromeless window — not a tab); falls back to a normal
    new browser window/tab. Best-effort — never raises.

    Reuses an already-open viewer for this run's slot (detected via recent /events
    polls) so repeated SEQUENTIAL watch calls don't pile up windows; a concurrent run
    got its own id upstream, so it opens its own window here. Set AGY_WATCH_ALWAYS_NEW=1
    to force a fresh window every time."""
    if _watch_viewer_live(rid) and not _env_truthy("AGY_WATCH_ALWAYS_NEW"):
        log.debug("watch viewer already open for %s; reusing instead of a new window", rid)
        return
    for exe in _chromium_app_browsers():
        try:
            subprocess.Popen(
                [exe, f"--app={url}", f"--window-size={_WATCH_WINDOW_SIZE}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_spawn_kwargs(),
            )
            return
        except OSError:
            continue
    try:
        webbrowser.open(url, new=1)  # request a new window (clients may still tab)
    except Exception:  # noqa: BLE001 - viewer is best-effort
        pass


def _run_agy_watched(
    prompt: str,
    workspace: str,
    continue_conv: bool,
    timeout_s: int,
    model: Optional[str] = None,
    plan: bool = False,
    schema: Optional[str] = None,
) -> str:
    """Like _run_agy, but open a live browser "watch" view. EXPERIMENTAL.

    agy runs headless (console-detached, no leak); alongside it, the bridge serves
    a small localhost page and opens your browser to it, live-streaming agy's steps
    (narration + the real commands it runs). The return value is identical to
    antigravity_ask. The viewer is best-effort and cross-platform (any browser); if
    it can't open, the run still completes normally.

    On agy 1.1.8+ the steps come from agy's OWN typed event stream
    (--output-format stream-json, parsed by _StreamWatch), so the viewer no longer
    depends on scraping the undocumented JSONL transcript — and the answer comes
    from the stream's terminal `result` event, matching antigravity_ask exactly.
    Older agy keeps the transcript-polling path (_WatchFeed) unchanged.
    """
    os.makedirs(workspace, exist_ok=True)  # agy's cwd must exist (mirrors the swarm)
    use_stream = supports_json_output()
    args, pinned_conv = _build_agy_args(
        prompt,
        workspace,
        continue_conv,
        timeout_s,
        model,
        output_format="stream-json" if use_stream else None,
        plan=plan,
        schema=schema,
    )

    with _AGY_LOCK:
        start = time.time()
        title = prompt.strip().splitlines()[0] if prompt.strip() else ""
        if len(title) > 200:
            title = title[:200].rsplit(" ", 1)[0] + "…"
        # In continue mode, seed the viewer with the prior turns so it reads as one
        # ongoing conversation instead of a blank new window. This still reads the
        # transcript: stream-json describes only the CURRENT run, and prior turns are
        # history. It is cosmetic and already degrades to [] when unreadable.
        history = _read_agy_history(pinned_conv) if (continue_conv and pinned_conv) else []
        rid = _watch_begin(title, start, timeout_s, prompt=prompt, history=history)
        stream = _StreamWatch(rid, start) if use_stream else None
        feed = None if use_stream else _WatchFeed(pinned_conv, start, rid)
        try:
            port = _ensure_watch_server()
            _open_watch_window(_watch_url(port, rid), rid)
        except Exception:  # noqa: BLE001 - the viewer is best-effort, never fatal
            pass

        proc = subprocess.Popen(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_TEXT,
            bufsize=1,  # line-buffered: stream-json events are consumed as they land
            **_spawn_kwargs(),  # agy stays headless; the browser is the viewer
        )
        # Both pipes must be drained regardless of mode, or a big stdout fills the OS
        # pipe buffer and hangs agy into a false timeout (see _drain_pipe). With
        # stream-json the stdout drain doubles as the live event parser (_pump_pipe).
        if stream is not None:
            out_t, _out = _pump_pipe(proc.stdout, stream.feed_line)
        else:
            out_t, _out = _drain_pipe(proc.stdout)
        err_t, err_chunks = _drain_pipe(proc.stderr)
        hard_deadline = start + timeout_s + 30
        while proc.poll() is None:
            if time.time() > hard_deadline:
                proc.kill()
                _watch_finish(rid, "error", "(timed out)", time.time() - start)
                raise RuntimeError(f"agy timed out after {timeout_s + 30}s (watched)")
            if feed is not None:
                feed.pump()
            time.sleep(_PROGRESS_POLL_INTERVAL_S)
        if feed is not None:
            feed.pump()  # drain transcript entries flushed right before exit
        out_t.join(timeout=5)  # let the last events land before reading the result
        err_t.join(timeout=5)
        if proc.returncode != 0:
            _watch_finish(rid, "error", f"(agy exited {proc.returncode})", time.time() - start)
            stderr_tail = "".join(err_chunks)[-1000:]
            raise RuntimeError(f"agy exited {proc.returncode}\nstderr: {stderr_tail}")

        # The stream's terminal `result` event is the answer, and its conversation_id
        # is agy naming its own conversation — record it so a later continue pins to
        # this thread (the same guarantee the non-watched path gained on 1.1.8).
        if stream is not None:
            if stream.conv_id:
                _record_conv_id(workspace, stream.conv_id)
            if stream.result is not None:
                if schema is not None:
                    try:
                        answer = _structured_answer(stream.result, stream.conv_id or "")
                    except RuntimeError:
                        _watch_finish(rid, "error", "(no structured output)", time.time() - start)
                        raise
                    _watch_finish(rid, "done", answer, time.time() - start)
                    return answer
                answer = (stream.result.get("response") or "").strip()
                status = stream.result.get("status")
                if answer:
                    if status and status != "SUCCESS":
                        log.warning("agy reported status=%s but returned an answer", status)
                    _watch_finish(rid, "done", answer, time.time() - start)
                    return answer
                if status and status != "SUCCESS":
                    _watch_finish(rid, "error", f"(agy status {status})", time.time() - start)
                    raise RuntimeError(
                        f"agy reported status={status} with no response "
                        f"(conversation {stream.conv_id or 'unknown'})."
                        + (f"\nstderr: {''.join(err_chunks)[-1000:]}" if err_chunks else "")
                    )
            # No usable result event (agy ignored the flag, or died mid-stream):
            # fall through to the transcript scrape below, exactly as older agy does.

        if schema is not None:
            # The transcript scrape below can only ever produce prose, so a schema
            # run that reaches here has nothing to honour its contract with.
            _watch_finish(rid, "error", "(no structured output)", time.time() - start)
            raise RuntimeError(
                "agy produced no structured result object for a --json-schema run "
                "(it fell back to plain text). The schema contract cannot be met, and "
                "returning prose to a caller that asked for a shape would fail at their "
                "end instead of here."
            )

        resolved_conv = (
            pinned_conv or (stream.conv_id if stream else None) or (feed.conv if feed else None)
        )
        deadline = time.time() + _RESPONSE_POLL_DEADLINE_S
        while True:
            try:
                answer = _resolve_and_read(resolved_conv, workspace, start)
                break
            except RuntimeError as exc:
                if time.time() >= deadline:
                    _watch_finish(rid, "error", "(no answer found)", time.time() - start)
                    # Same exit-0-with-no-answer case as _run_agy: agy's stderr
                    # carries the real cause (e.g. a 1.1.3 permission soft-deny),
                    # and err_chunks is already drained here — fold it in rather
                    # than raising a bare scrape failure that reads as a bridge bug.
                    stderr_tail = "".join(err_chunks).strip()
                    if stderr_tail:
                        raise RuntimeError(
                            f"{exc}\nagy exited 0 but produced no answer — "
                            f"stderr: {stderr_tail[-1000:]}"
                        ) from exc
                    raise
                time.sleep(_RESPONSE_POLL_INTERVAL_S)
        _watch_finish(rid, "done", answer, time.time() - start)
        return answer


def _run_agy_image_watched(
    wrapped_prompt: str, target: str, workspace: str, timeout_s: int, display_prompt: str
) -> str:
    """Generate an image with a live watch window that also displays the result.

    EXPERIMENTAL. Runs agy headless, streams its steps to the Agent Intern window,
    finalises the generated image (extension corrected to the real bytes), shows
    it in the window, and returns the same string as antigravity_image. `display_prompt`
    is the user's original prompt, shown as the window title (not the wrapped
    save-path instructions that actually go to agy).
    """
    os.makedirs(workspace, exist_ok=True)  # agy's cwd must exist (mirrors the swarm)
    use_stream = supports_json_output()
    args, _ = _build_agy_args(
        wrapped_prompt,
        workspace,
        False,
        timeout_s,
        output_format="stream-json" if use_stream else None,
    )

    with _AGY_LOCK:
        start = time.time()
        title = display_prompt.strip().splitlines()[0] if display_prompt.strip() else "image"
        if len(title) > 200:
            title = title[:200].rsplit(" ", 1)[0] + "…"
        rid = _watch_begin(title, start, timeout_s, prompt=display_prompt)
        stream = _StreamWatch(rid, start) if use_stream else None
        feed = None if use_stream else _WatchFeed(None, start, rid)
        try:
            port = _ensure_watch_server()
            _open_watch_window(_watch_url(port, rid), rid)
        except Exception:  # noqa: BLE001 - viewer is best-effort
            pass

        proc = subprocess.Popen(
            args,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_TEXT,
            bufsize=1,  # line-buffered: stream-json events are consumed as they land
            **_spawn_kwargs(),
        )
        # Drain both pipes so a chatty agy can't fill the pipe buffer and hang (see
        # _drain_pipe); with stream-json the stdout drain also parses the live events.
        if stream is not None:
            out_t, _out = _pump_pipe(proc.stdout, stream.feed_line)
        else:
            out_t, _out = _drain_pipe(proc.stdout)
        err_t, _err = _drain_pipe(proc.stderr)
        hard_deadline = start + timeout_s + 30
        while proc.poll() is None:
            if time.time() > hard_deadline:
                proc.kill()
                _watch_finish(rid, "error", "(timed out)", time.time() - start)
                raise RuntimeError(f"agy timed out after {timeout_s + 30}s (image/watch)")
            if feed is not None:
                feed.pump()
            time.sleep(_PROGRESS_POLL_INTERVAL_S)
        if feed is not None:
            feed.pump()
        out_t.join(timeout=5)
        err_t.join(timeout=5)

        # agy's reply names where it saved the image; _finalize_image uses it as a
        # candidate path. Prefer the stream's result, then the transcript. Either may
        # fail even though the image WAS written, so never lose a produced image to a
        # read hiccup (mirrors antigravity_image).
        agy_text = None
        agy_error = None
        if stream is not None and stream.conv_id:
            _record_conv_id(workspace, stream.conv_id)
        if stream is not None and stream.result is not None:
            agy_text = (stream.result.get("response") or "").strip() or None
        if agy_text is None:
            try:
                agy_text = _resolve_and_read(
                    (stream.conv_id if stream else None) or (feed.conv if feed else None),
                    workspace,
                    start,
                )
            except RuntimeError as e:
                agy_error = e

        try:
            final_path, fmt, size = _finalize_image(target, agy_text, start)
        except RuntimeError as fin_err:
            _watch_finish(rid, "error", f"no image produced: {fin_err}", time.time() - start)
            if agy_error is not None:
                raise RuntimeError(f"{fin_err} (agy also failed: {agy_error})") from agy_error
            raise

        _watch_set_image(rid, final_path)
        caption = f"Saved to {final_path}\nformat={fmt} · {size} bytes"
        _watch_finish(rid, "done", caption, time.time() - start)
        return f"{final_path}\nformat={fmt}  size={size} bytes"


@mcp.tool(
    annotations={
        "title": "Ask Antigravity (new conversation)",
        "readOnlyHint": False,  # agy runs unsandboxed: may write files / run commands
        "idempotentHint": False,
        "openWorldHint": True,  # talks to the external Antigravity service
    }
)
async def antigravity_ask(
    prompt: str,
    workspace: Optional[str] = None,
    model: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    plan: bool = False,
    schema: Optional[Union[dict, str]] = None,
    ctx: Optional[Context] = None,
) -> str:
    """Ask Antigravity (agy CLI, Gemini by default) a question in a NEW conversation.

    Uses your existing AI Pro authentication (silent-auth via Windows Credential
    Manager). Returns the model's final response as text. Good for fast
    tool-calling and short tasks; for heavier reasoning pick a bigger `model` or
    use the host model directly.

    Args:
        prompt: Question or instruction for Antigravity.
        workspace: Working directory for the conversation. Defaults to cwd.
                   Choose an existing project dir for context-aware responses.
        model: Optional model slug to run this conversation on (agy's --model),
               e.g. "gemini-3.1-pro-high" or "claude-sonnet-4-6". Omit to use the
               model set in agy's settings.json (gemini-3.8-flash-high as of agy
               1.1.25). Must be one of `agy models` — an unknown slug is
               rejected up front (agy would otherwise silently ignore it and fall
               back to the default). agy 1.1.5 replaced the old human labels
               ("Gemini 3.1 Pro (High)") with these slugs, and the default has
               since moved to the gemini-3.8-flash family; the old form is no
               longer accepted. Note 1.1.25 also DROPPED the gemini-3.5-flash
               family with no changelog entry, so a 3.5 slug you saw in older
               docs is now rejected. See antigravity_status / `agy models` for
               the valid slugs.
        timeout_s: Max seconds to wait for agy to complete. Default 180.
        watch: If true, open a live "watch" view in your browser that streams
               agy's steps (narration + the real commands it runs) as it works.
               agy still runs headless; the same final text is returned. Best-
               effort and cross-platform — if the browser can't open, the run
               completes normally. Default false.
        plan: If true, run agy in PLAN mode (agy 1.1.12+): it investigates and
              writes an implementation plan instead of touching anything. Verified
              on 1.1.20 that a file write and a shell command are both refused and
              diverted into a plan document under agy's own directory — even when
              the prompt insists, and even though the bridge still passes
              --dangerously-skip-permissions — while file READS answer normally.
              Use it to point Antigravity at a repo you don't want it editing.
              Two caveats. It is agent-enforced, not an OS sandbox: it constrains
              agy's agent loop, so treat it as a strong default rather than a
              boundary you'd rely on against a hostile prompt (Codex has the real
              one — see codex_ask's sandbox, and its Windows caveat: as of codex
              0.149.1 a sandboxed run there refuses every command and answers
              anyway). And it is exclusive with the bridge's
              slash-command shield, because agy silently disables plan mode when
              that shield is on; a prompt whose first token is a slash command is
              therefore rejected up front rather than run. Raises on agy older than
              1.1.12, which ignores --mode in print mode, rather than silently
              running your prompt unrestricted. Default false.
        schema: Optional JSON Schema (an object, or its JSON text). When given, agy
                is asked to produce output matching it (agy 1.1.8's --json-schema)
                and this tool returns the VALIDATED OBJECT as JSON text instead of
                prose — json.loads it. What comes back is agy's own
                `structured_output`, which carries exactly the declared fields;
                agy's prose `response` on the same run also picks up its internal
                toolAction/toolSummary keys and can be prefixed with a sentence, so
                the two are NOT interchangeable. If agy produces no structured
                output the call RAISES rather than handing back prose you would
                have to parse anyway. Needs agy 1.1.8+.

                IMPORTANT — write the prompt so the ANSWER is in the turn, and let
                the schema only shape it. agy fills the schema in a finishing pass
                that does not re-reason about the content, so a field the turn never
                established gets guessed from the schema itself. Measured on 1.1.20
                with "this broke my build and wasted my whole afternoon": with
                enum ["positive","negative"] it answered "positive" 3 times out of 4,
                and simply REVERSING the enum to ["negative","positive"] flipped it
                to "negative" 2 out of 2 — it was following field order, not the
                sentence. Adding a `reason` field did not help; the reason came back
                "Completed sentiment classification task." Asking the prompt to state
                the verdict and why, and keeping the same biased enum, was correct
                3 out of 3. So: extraction of what the model has already worked out
                is reliable; a judgment delegated to the schema is not.
    """
    ws = _normalize_workspace(workspace)
    validate_model(model)  # fail fast on a typo (agy would silently ignore it)
    if plan:
        _check_plan_mode(prompt)  # refuses rather than downgrading; see _agy_base_args
    schema_text = None
    if schema is not None:
        _check_schema_support()
        schema_text = _normalize_json_schema(schema)
    if watch:
        return await asyncio.to_thread(
            _run_agy_watched, prompt, ws, False, timeout_s, model, plan, schema_text
        )
    return await _run_with_progress(
        _run_agy, (prompt, ws, False, timeout_s, model, plan, schema_text), ctx, timeout_s
    )


@mcp.tool(
    annotations={
        "title": "Continue Antigravity conversation",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def antigravity_continue(
    prompt: str,
    workspace: Optional[str] = None,
    model: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    plan: bool = False,
    schema: Optional[Union[dict, str]] = None,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the Antigravity conversation rooted at this workspace.

    Resumes the exact conversation id recorded for `workspace` (via agy's
    --conversation flag), not agy's global "most recent", so it stays correct
    even if agy was used elsewhere in between. On agy 1.1.8+ that id is the one
    agy itself reported for this bridge's last run in the workspace, so a
    follow-up resumes THIS thread even if you have since started a separate
    conversation in the same folder from Antigravity's own interface.

    Args:
        prompt: Follow-up message.
        workspace: Working directory used by the prior conversation. Defaults to cwd.
        model: Optional model slug for this turn (agy's --model), e.g.
               "claude-sonnet-4-6". agy's model is per-invocation, not baked into
               the conversation, so a follow-up can run on a different model than
               the original ask — omit to use agy's settings.json default.
               Validated against `agy models`; an unknown slug is rejected (agy
               would silently ignore it).
        timeout_s: Max seconds to wait for agy to complete. Default 180.
        watch: If true, open a live "watch" view in your browser that streams
               agy's steps as it works (same return value, best-effort). Default false.
        plan: If true, run this turn in agy's PLAN mode (1.1.12+) — it investigates
              and writes an implementation plan instead of editing files or running
              commands, while reads still work. Per-invocation like `model`, so a
              follow-up can plan even if the original ask was unrestricted. See
              antigravity_ask's `plan` for what it does and does not guarantee.
              Default false.
        schema: Optional JSON Schema for this turn — returns the validated object as
                JSON text instead of prose. Per-invocation like `model` and `plan`.
                See antigravity_ask's `schema`. Needs agy 1.1.8+.
    """
    ws = _normalize_workspace(workspace)
    validate_model(model)  # fail fast on a typo (agy would silently ignore it)
    if plan:
        _check_plan_mode(prompt)  # refuses rather than downgrading; see _agy_base_args
    schema_text = None
    if schema is not None:
        _check_schema_support()
        schema_text = _normalize_json_schema(schema)
    if watch:
        return await asyncio.to_thread(
            _run_agy_watched, prompt, ws, True, timeout_s, model, plan, schema_text
        )
    return await _run_with_progress(
        _run_agy, (prompt, ws, True, timeout_s, model, plan, schema_text), ctx, timeout_s
    )


@mcp.tool(
    annotations={
        "title": "Generate an image with Antigravity",
        "readOnlyHint": False,  # writes the generated image file to disk
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def antigravity_image(
    prompt: str,
    output_path: Optional[str] = None,
    workspace: Optional[str] = None,
    timeout_s: int = 240,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Generate an image with Antigravity (Gemini image model via agy CLI).

    Drives agy to produce a raster image on your existing AI Pro quota, saves it,
    and returns the absolute file path plus its real format and byte size. The
    host can then read the path to view the image.

    agy picks the image format itself (JPEG for photo-like images, PNG for flat
    graphics), so the returned path's extension is corrected to match the actual
    bytes (a requested out.png may come back as out.jpg). Runs a normal,
    unsandboxed agy session — same privileges/caveats as the other tools (see the
    module SECURITY note).

    Args:
        prompt: Description of the image to generate.
        output_path: Where to save. Absolute, or relative to `workspace`. If
                     omitted, a timestamped name under `workspace` is used.
        workspace: Working directory for the conversation. Defaults to cwd.
        timeout_s: Max seconds to wait for agy to complete. Default 240
                   (image generation is slower than text).
        watch: If true, open the live "watch" window that streams agy's steps and
               shows the finished image inline (same return value, best-effort).
               Default false.
    """
    ws = _normalize_workspace(workspace)
    target = _resolve_output_path(output_path, ws)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    wrapped = _wrap_image_prompt(prompt, target)
    if watch:
        return await asyncio.to_thread(
            _run_agy_image_watched, wrapped, target, ws, timeout_s, prompt
        )

    start = time.time()
    agy_text: Optional[str] = None
    agy_error: Optional[Exception] = None
    try:
        agy_text = await _run_with_progress(
            _run_agy, (wrapped, ws, False, timeout_s), ctx, timeout_s
        )
    except RuntimeError as e:
        # The transcript read may fail even though agy wrote the image. Don't
        # lose a successfully generated file to a transcript hiccup — try to
        # locate it anyway, and only surface this error if nothing was produced.
        agy_error = e

    try:
        final_path, fmt, size = _finalize_image(target, agy_text, start)
    except RuntimeError as fin_err:
        if agy_error is not None:
            raise RuntimeError(f"{fin_err} (agy also failed: {agy_error})") from agy_error
        raise
    return f"{final_path}\nformat={fmt}  size={size} bytes"


def _broadcast_workspaces(workspaces: Optional[list], n: int):
    """Map the MCP `workspaces` arg to swarm's None|str|list contract.

    None -> server cwd for all; a 1-item list -> that dir for all N; an N-item
    list -> one workspace per prompt. (MCP can't pass a bare str for a list field,
    so a 1-item list is the "same dir for everyone" shorthand.)
    """
    if not workspaces:
        return None
    if len(workspaces) == 1:
        return workspaces[0]
    return workspaces


@mcp.tool(
    annotations={
        "title": "Agent swarm (mixed Antigravity + Codex + Copilot + Cursor + …, parallel)",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
def agent_swarm(
    tasks: list[dict],
    max_concurrency: int = 4,
    timeout_s: int = 180,
    watch: bool = False,
) -> str:
    """Run SEVERAL tasks IN PARALLEL across ALL backends in a single swarm.

    Each task is its own worker and names the backend to run on, so one swarm can
    mix Antigravity (Gemini), Codex, Copilot, Cursor, Grok and opencode workers —
    they run truly concurrently (capped at `max_concurrency`) and every answer comes
    back in one labelled block. A worker that fails is reported in place; the others
    still return.

    SECURITY: this launches N unsandboxed agents at once — N times the
    prompt-injection surface of a single call (see the module SECURITY note). Only
    use it with trusted prompts on trusted content.

    Args:
        tasks: One object per parallel worker:
               - backend: "antigravity" (alias "agy"/"gemini"), "codex",
                          "copilot" (alias "gh"/"github"), "cursor", "opencode"
                          (alias "oc" — the one backend that needs no
                          subscription; see opencode_ask), or "grok" (alias
                          "xai"; EXPERIMENTAL — see grok_ask) (required)
               - prompt:  the question or instruction (required)
               - workspace: working dir for that worker (default: server cwd)
               - sandbox: "read-only" (default), "workspace-write", or
                          "danger-full-access". Codex's is an enforced OS sandbox
                          everywhere; Grok's is enforced on Linux/macOS only;
                          Copilot's, Cursor's and opencode's are agent/tool-level,
                          not OS boundaries — see copilot_ask / cursor_ask /
                          grok_ask / opencode_ask.
                          ANTIGRAVITY is the odd one: "read-only" maps to agy's
                          plan mode (it investigates and writes a plan instead of
                          editing files or running commands — see antigravity_ask's
                          `plan`, and note it is agent-enforced, and needs agy
                          1.1.12+), "danger-full-access" states plainly that the
                          worker is unrestricted, and "workspace-write" is REFUSED
                          because agy has no write scoping to offer. Omitting it
                          leaves an Antigravity worker unrestricted — that is the
                          long-standing default, unlike every other backend here,
                          so fence it explicitly if you want it fenced.
               - model:   optional model override for ANY backend — Codex's `-m`,
                          Copilot's/Cursor's `--model`, Grok's/opencode's `-m`
                          (opencode wants "provider/model"), or Antigravity's
                          `--model` (an agy slug like "claude-sonnet-4-6";
                          validated against each backend's model list). Omit for
                          each backend's default.
        max_concurrency: Max workers running at once (default 4). Higher = faster
                         but more quota/rate-limit pressure and more agents at once.
        timeout_s: Per-worker timeout in seconds. Default 180. An opencode
                   worker is given at least 300s regardless — its free models are
                   queue-scheduled and were measured at 152-428s, so the shared
                   default would kill about half of them mid-answer and report a
                   slow worker as a broken one. The budget is only ever raised,
                   never lowered.
        watch: If true, open the live "Agent Swarm" dashboard window (one row per
               worker, with a backend badge; click a row for its full step log).
    """
    import swarm

    results = swarm.swarm_agents(tasks, max_concurrency, timeout_s, watch)
    return swarm.format_agent_results(results)


@mcp.tool(
    annotations={
        "title": "Generate several images in parallel",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
def antigravity_image_swarm(
    prompts: list[str],
    output_paths: Optional[list[str]] = None,
    workspaces: Optional[list[str]] = None,
    max_concurrency: int = 4,
    timeout_s: int = 240,
    watch: bool = False,
) -> str:
    """Generate several images IN PARALLEL with Antigravity (one worker per prompt).

    Like antigravity_image, but runs N image generations concurrently in isolated
    workers (capped at `max_concurrency`). Returns one block listing each image's
    final path/format/size (or its error). Extensions are corrected to the real
    bytes, exactly like antigravity_image. Same unsandboxed privileges/caveats as
    antigravity_swarm.

    Args:
        prompts: One image description per parallel worker.
        output_paths: Where to save each image (aligned to prompts). Omit to write
                      timestamped files in the first workspace (or server cwd).
        workspaces: Working directory per worker (same shorthand as antigravity_swarm).
        max_concurrency: Max workers running at once (default 4).
        timeout_s: Per-worker timeout in seconds. Default 240 (images are slower).
        watch: If true, open the live dashboard; each finished image shows in its
               pane, and clicking a row opens that agent's window beside the dashboard.
    """
    import swarm

    n = len(prompts)
    if output_paths is None:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        base = workspaces[0] if workspaces else os.getcwd()
        output_paths = [os.path.join(base, f"agy-swarm-image-{stamp}-{i}.png") for i in range(n)]
    results = swarm.swarm_image(
        prompts,
        output_paths,
        workspaces=_broadcast_workspaces(workspaces, n),
        max_concurrency=max_concurrency,
        timeout_s=timeout_s,
        watch=watch,
    )
    return swarm.format_image_results(results)


@mcp.tool(
    annotations={
        "title": "agy bridge diagnostics",
        "readOnlyHint": True,  # only reads local state + runs `agy --version`
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def antigravity_status() -> str:
    """Report diagnostics for the agy bridge setup (spends no AI Pro quota).

    Reports the bridge's own version and whether a newer release is available
    (best-effort GitHub check; honors AGY_BRIDGE_NO_UPDATE_CHECK), then checks
    whether agy is on PATH (and its version/compat), how much AI Pro quota is left
    per model family (agy 1.1.11+ answers `/usage` in print mode for free — a
    family at 0% is reported as a problem, since every call against it will fail
    until its window resets), whether agy's state directories exist, whether the
    newest conversation transcript is readable, and whether the SQLite
    conversation store is present. Use this to debug empty or failed responses —
    or to see if the bridge itself is out of date, or if you are simply out of
    quota — before spending quota.
    """
    rows = _collect_status()
    width = max(len(label) for label, _, _ in rows)
    lines = ["agy bridge status"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)


@mcp.tool(
    annotations={
        "title": "Ask Codex (new session)",
        "readOnlyHint": False,  # codex may edit files when sandbox != read-only
        "idempotentHint": False,
        "openWorldHint": True,  # talks to the external OpenAI/Codex service
    }
)
async def codex_ask(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = codex_bridge.DEFAULT_SANDBOX,
    model: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Ask OpenAI Codex (`codex exec`) a question or task in a NEW session.

    Uses your existing Codex login (ChatGPT or API key — see `codex login status`).
    Returns the agent's final message as text, read from codex's
    --output-last-message file (no stdout scraping). Codex is a capable coding
    agent, so this suits heavier reasoning and real code work, not just cheap
    tool-calling. Point `workspace` at a real project dir for context-aware answers.

    Args:
        prompt: Question or instruction for Codex.
        workspace: Working root for the session (`-C`). Defaults to the server cwd.
        sandbox: Filesystem policy — "read-only" (default: reads and answers but
                 writes nothing), "workspace-write" (may edit files under the
                 workspace), or "danger-full-access" (no sandbox — avoid). `codex
                 exec` has no interactive approval gate, so this is the real safety
                 boundary; opt into write access deliberately.

                 WINDOWS CAVEAT (codex 0.149.1): sandboxed runs there currently
                 refuse EVERY command, both policies, down to `pwd` — codex's
                 policy engine cannot classify the `pwsh -Command <...>` wrapper it
                 builds. Shell commands are how codex reads files, so it sees none
                 of the workspace and ANSWERS ANYWAY, from its own knowledge or a
                 web search, with no hint that it read nothing. The bridge appends
                 a visible "[agent-intern] WARNING" to any answer whose run had
                 commands refused: if you see it, treat the answer as unsourced.
        model: Optional model override (`-m`); omit to use codex's configured default.
        timeout_s: Max seconds to wait for codex to complete. Default 180.
        watch: If true, open a live "watch" view in your browser that streams
               codex's steps (reasoning, the commands it runs, file changes) from
               its `--json` event stream. codex still runs headless; the same final
               text is returned. Best-effort — if the browser can't open, the run
               completes normally. Default false.
    """
    ws = codex_bridge.normalize_workspace(workspace)
    codex_bridge.validate_sandbox(sandbox)  # fail fast with a clear message
    if watch:
        return await asyncio.to_thread(
            _run_codex_watched, prompt, ws, sandbox, model, False, timeout_s
        )
    return await _run_with_progress(
        codex_bridge.run_codex,
        (prompt, ws, sandbox, model, False, timeout_s),
        ctx,
        timeout_s,
        label="codex",
    )


@mcp.tool(
    annotations={
        "title": "Continue Codex session",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def codex_continue(
    prompt: str,
    workspace: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the Codex session rooted at this workspace (`codex exec resume`).

    Resumes the exact session id captured from the last codex_ask in this
    workspace, falling back to the newest on-disk session whose recorded cwd
    matches (so it still works after a server restart). The resumed session keeps
    its original sandbox and model — those are chosen when you start it with
    codex_ask.

    Args:
        prompt: Follow-up message for the existing session.
        workspace: Working root used by the prior session. Defaults to the server cwd.
        timeout_s: Max seconds to wait for codex to complete. Default 180.
        watch: If true, open the live "watch" view streaming codex's steps as it
               works (same viewer as codex_ask). Default false.
    """
    ws = codex_bridge.normalize_workspace(workspace)
    if watch:
        return await asyncio.to_thread(
            _run_codex_watched,
            prompt,
            ws,
            codex_bridge.DEFAULT_SANDBOX,
            None,
            True,
            timeout_s,
        )
    return await _run_with_progress(
        codex_bridge.run_codex,
        (prompt, ws, codex_bridge.DEFAULT_SANDBOX, None, True, timeout_s),
        ctx,
        timeout_s,
        label="codex",
    )


@mcp.tool(
    annotations={
        "title": "Codex bridge diagnostics",
        "readOnlyHint": True,  # only runs `codex --version` / `codex login status`
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def codex_status() -> str:
    """Report diagnostics for the Codex bridge setup (spends no quota).

    Reports the bridge's own version and whether a newer release is available
    (best-effort GitHub check; honors AGY_BRIDGE_NO_UPDATE_CHECK) — the same
    update notice antigravity_status shows, so a Codex-only install still surfaces
    it — then checks whether codex is on PATH (and its version), whether you're
    logged in (`codex login status` — no model call, no quota), where codex stores
    its sessions, and how many workspace sessions are pinned this run. Use this to
    debug "codex not found" or auth errors before spending quota.
    """
    rows = [_bridge_version_status()] + codex_bridge.status_rows()
    width = max(len(label) for label, _, _ in rows)
    lines = ["codex bridge status"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)


def _codex_event_to_watch_lines(ev: dict) -> list[tuple[str, str]]:
    """Map one codex --json event to (kind, text) watch lines (kind is
    'narration' | 'command' | 'result'), mirroring _entry_to_watch_lines for agy.
    Returns [] for events with nothing worth showing in the viewer.
    """
    etype = ev.get("type")
    if etype == "item.completed":
        item = ev.get("item") or {}
        itype = item.get("type")
        if itype in ("agent_message", "reasoning"):
            txt = (item.get("text") or item.get("summary") or "").strip()
            return [("narration", txt.splitlines()[0][:200])] if txt else []
        if itype == "command_execution":
            cmd = (item.get("command") or "").strip()
            return [("command", cmd[:200])] if cmd else []
        if itype == "file_change":
            changes = item.get("changes") or item.get("files") or []
            n = len(changes) if isinstance(changes, list) else 0
            return [("result", f"file change ({n} file(s))" if n else "file change")]
        if itype == "mcp_tool_call":
            return [("command", f"mcp: {item.get('tool') or item.get('name') or ''}"[:200])]
        if itype == "web_search":
            return [("command", f"search: {item.get('query') or ''}"[:200])]
        return []
    if etype == "turn.started":
        return [("narration", "thinking…")]
    if etype == "error":
        return [("result", f"error: {ev.get('message') or ''}"[:200])]
    return []


def _run_codex_watched(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    continue_conv: bool,
    timeout_s: int,
) -> str:
    """Like codex_bridge.run_codex, but stream codex's steps to the live watch
    window. EXPERIMENTAL. Reuses the same localhost viewer as the agy watch tools;
    the return value is identical to codex_ask.
    """
    start = time.time()
    title = prompt.strip().splitlines()[0] if prompt.strip() else ""
    if len(title) > 200:
        title = title[:200].rsplit(" ", 1)[0] + "…"
    history = codex_bridge.read_history(workspace, continue_conv)
    rid = _watch_begin(title, start, timeout_s, backend="codex", prompt=prompt, history=history)
    try:
        port = _ensure_watch_server()
        _open_watch_window(_watch_url(port, rid), rid)
    except Exception:  # noqa: BLE001 — the viewer is best-effort, never fatal
        pass

    def on_event(ev: dict) -> None:
        watch_lines = _codex_event_to_watch_lines(ev)
        if watch_lines:
            t = round(time.time() - start, 1)
            _watch_append(rid, [{"kind": k, "text": x, "t": t} for k, x in watch_lines])

    try:
        answer = codex_bridge.run_codex_streaming(
            prompt, workspace, sandbox, model, continue_conv, timeout_s, on_event
        )
    except Exception as e:  # noqa: BLE001 — show the failure in the window, then re-raise
        _watch_finish(rid, "error", f"({e})"[:200], time.time() - start)
        raise
    _watch_finish(rid, "done", answer, time.time() - start)
    return answer


@mcp.tool(
    annotations={
        "title": "Ask GitHub Copilot (new session)",
        "readOnlyHint": False,  # copilot may edit files / run commands per sandbox
        "idempotentHint": False,
        "openWorldHint": True,  # talks to the external GitHub Copilot service
    }
)
async def copilot_ask(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = copilot_bridge.DEFAULT_SANDBOX,
    model: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Ask the GitHub Copilot CLI (`copilot -p`) a question or task in a NEW session.

    Uses your existing Copilot login (OS credential store, or a
    COPILOT_GITHUB_TOKEN/GH_TOKEN/GITHUB_TOKEN env var — see `copilot_status`).
    Returns the agent's final message, read straight from stdout (the CLI's `-s`
    silent mode; no scraping). Copilot is a capable agentic coder — good for real
    code/repo work; point `workspace` at a project dir for context-aware answers.

    Args:
        prompt: Question or instruction for Copilot.
        workspace: Working root for the session (`-C`). Defaults to the server cwd.
        sandbox: Permission policy (maps to copilot's tool/path flags):
                 "read-only" (default — best-effort: denies the local write/shell
                 tools; NOT an OS sandbox, so unlike codex it is not a hard
                 boundary), "workspace-write" (may edit files, confined to the
                 workspace), or "danger-full-access" (--allow-all — avoid).
        model: Optional model override (`--model`). Use "auto" to let Copilot pick.
               Which ids work is ACCOUNT-DEPENDENT and copilot exposes no
               non-interactive list, so the bridge cannot validate this the way the
               agy/cursor tools do — an unavailable id errors immediately with
               copilot's own message, costing a call. Prefer omitting it (your
               account default) or "auto" unless you know your plan's ids.
        timeout_s: Max seconds to wait for copilot to complete. Default 180.
                   (Copilot's reasoning models can be slow; raise this if needed.)
        watch: If true, open a live "watch" view streaming copilot's steps from its
               `--output-format json` event stream. Same final text is returned.
               Best-effort. Default false.
    """
    ws = copilot_bridge.normalize_workspace(workspace)
    copilot_bridge.validate_sandbox(sandbox)  # fail fast with a clear message
    if watch:
        return await asyncio.to_thread(
            _run_copilot_watched, prompt, ws, sandbox, model, False, timeout_s
        )
    return await _run_with_progress(
        copilot_bridge.run_copilot,
        (prompt, ws, sandbox, model, False, timeout_s),
        ctx,
        timeout_s,
        label="copilot",
    )


@mcp.tool(
    annotations={
        "title": "Continue GitHub Copilot session",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def copilot_continue(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = copilot_bridge.DEFAULT_SANDBOX,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the Copilot session rooted at this workspace (resumes its `--session-id`).

    Resumes the exact session id the bridge set on the last copilot_ask in this
    workspace, falling back to the newest on-disk session whose recorded cwd
    matches (so it still works after a server restart). Unlike codex_continue,
    copilot re-applies permission flags on every call, so `sandbox` takes effect
    here too — e.g. analyze read-only with copilot_ask, then continue with
    "workspace-write" to apply the fix.

    Args:
        prompt: Follow-up message for the existing session.
        workspace: Working root used by the prior session. Defaults to the server cwd.
        sandbox: Permission policy for THIS turn (default "read-only"). Same values
                 and caveats as copilot_ask.
        timeout_s: Max seconds to wait for copilot to complete. Default 180.
        watch: If true, open the live "watch" view streaming copilot's steps
               (same viewer as copilot_ask). Default false.
    """
    ws = copilot_bridge.normalize_workspace(workspace)
    copilot_bridge.validate_sandbox(sandbox)
    if watch:
        return await asyncio.to_thread(
            _run_copilot_watched, prompt, ws, sandbox, None, True, timeout_s
        )
    return await _run_with_progress(
        copilot_bridge.run_copilot,
        (prompt, ws, sandbox, None, True, timeout_s),
        ctx,
        timeout_s,
        label="copilot",
    )


@mcp.tool(
    annotations={
        "title": "Copilot bridge diagnostics",
        "readOnlyHint": True,  # only runs `copilot --version` + reads local state
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def copilot_status() -> str:
    """Report diagnostics for the Copilot bridge setup (spends no quota).

    Reports the bridge's own version and whether a newer release is available
    (best-effort GitHub check; honors AGY_BRIDGE_NO_UPDATE_CHECK) — the same
    update notice antigravity_status shows, so a Copilot-only install still
    surfaces it — then checks whether copilot is on PATH (and its version), an auth
    hint (copilot has no `login status` command, so this is best-effort — an env
    token is reported when set, otherwise login via the credential store is
    assumed and unverified), where copilot stores session state, and how many
    workspace sessions are pinned this run. Use this to debug "copilot not found"
    or auth errors before a call.
    """
    rows = [_bridge_version_status()] + copilot_bridge.status_rows()
    width = max(len(label) for label, _, _ in rows)
    lines = ["copilot bridge status"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)


def _copilot_tool_arg(arguments) -> str:
    """A short, human-readable representative argument for a copilot tool call.

    copilot's tool `arguments` is a dict (e.g. {"path": ...} for view,
    {"command": ...} for shell). Return the first informative field's first line.
    """
    if not isinstance(arguments, dict):
        return ""
    for key in ("command", "path", "file", "filePath", "query", "pattern", "url"):
        v = arguments.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().splitlines()[0]
    return ""


def _copilot_event_to_watch_lines(ev: dict) -> list[tuple[str, str]]:
    """Map one copilot --output-format json event to (kind, text) watch lines
    (kind is 'narration' | 'command' | 'result'), mirroring
    _codex_event_to_watch_lines. Returns [] for events not worth showing.
    """
    etype = ev.get("type")
    data = ev.get("data") or {}
    if etype == "assistant.message":
        txt = (data.get("content") or "").strip()
        return [("narration", txt.splitlines()[0][:200])] if txt else []
    if etype == "assistant.turn_start":
        return [("narration", "thinking…")]
    if etype == "tool.execution_start":
        name = data.get("toolName") or data.get("name") or "tool"
        arg = _copilot_tool_arg(data.get("arguments"))
        return [("command", f"{name} {arg}".strip()[:200])]
    if etype == "tool.execution_complete":
        return [("result", "done" if data.get("success") else "tool failed")]
    if etype == "error":
        msg = data.get("message") or ev.get("message") or ""
        return [("result", f"error: {msg}"[:200])]
    return []


def _run_copilot_watched(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    continue_conv: bool,
    timeout_s: int,
) -> str:
    """Like copilot_bridge.run_copilot, but stream copilot's steps to the live watch
    window. EXPERIMENTAL. Reuses the same localhost viewer as the agy/codex watch
    tools; the return value is identical to copilot_ask.
    """
    start = time.time()
    title = prompt.strip().splitlines()[0] if prompt.strip() else ""
    if len(title) > 200:
        title = title[:200].rsplit(" ", 1)[0] + "…"
    history = copilot_bridge.read_history(workspace, continue_conv)
    rid = _watch_begin(title, start, timeout_s, backend="copilot", prompt=prompt, history=history)
    try:
        port = _ensure_watch_server()
        _open_watch_window(_watch_url(port, rid), rid)
    except Exception:  # noqa: BLE001 — the viewer is best-effort, never fatal
        pass

    def on_event(ev: dict) -> None:
        watch_lines = _copilot_event_to_watch_lines(ev)
        if watch_lines:
            t = round(time.time() - start, 1)
            _watch_append(rid, [{"kind": k, "text": x, "t": t} for k, x in watch_lines])

    try:
        answer = copilot_bridge.run_copilot_streaming(
            prompt, workspace, sandbox, model, continue_conv, timeout_s, on_event
        )
    except Exception as e:  # noqa: BLE001 — show the failure in the window, then re-raise
        _watch_finish(rid, "error", f"({e})"[:200], time.time() - start)
        raise
    _watch_finish(rid, "done", answer, time.time() - start)
    return answer


# ============================================================ Cursor tools
@mcp.tool(
    annotations={
        "title": "Ask Cursor (new chat)",
        "readOnlyHint": False,  # cursor may edit files / run commands per sandbox
        "idempotentHint": False,
        "openWorldHint": True,  # talks to the external Cursor service
    }
)
async def cursor_ask(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = cursor_bridge.DEFAULT_SANDBOX,
    model: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Ask the Cursor CLI (`cursor-agent -p`) a question or task in a NEW chat.

    Uses your existing Cursor login (OS credential store, or a CURSOR_API_KEY env
    var — see `cursor_status`). Returns the agent's final message, read straight
    from stdout (no scraping). Cursor is a capable agentic coder with a wide model
    menu (GPT / Claude / Grok / Composer); point `workspace` at a project dir for
    context-aware answers.

    Args:
        prompt: Question or instruction for Cursor.
        workspace: Working root for the chat (`--workspace`). Defaults to the server cwd.
        sandbox: Permission policy (maps to cursor's mode/force flags):
                 "read-only" (default — `--mode ask`: agent-enforced, no file/shell
                 edits; NOT an OS sandbox, so unlike codex it is not a hard
                 boundary), "workspace-write" (may edit files, rooted at the
                 workspace), or "danger-full-access" (OS sandbox off — avoid).
        model: Optional model override (`--model`, e.g. "auto", "gpt-5.2",
               "claude-opus-4-8-high", "composer-2.5"); validated against
               `cursor-agent models` and rejected on a typo. cursor bakes the effort
               and speed axes into the id (…-low/-high/-xhigh/-max, each with a
               -fast twin) and also accepts a bracket form on the family base, e.g.
               "claude-opus-4-8[context=1m,effort=high]". Omit to use your Cursor
               default.
        timeout_s: Max seconds to wait for cursor to complete. Default 180.
        watch: If true, open a live "watch" view streaming cursor's steps from its
               `--output-format stream-json` event stream. Same final text is
               returned. Best-effort. Default false.
    """
    ws = cursor_bridge.normalize_workspace(workspace)
    cursor_bridge.validate_sandbox(sandbox)  # fail fast with a clear message
    cursor_bridge.validate_model(model)  # fail fast on a typo (cursor models)
    if watch:
        return await asyncio.to_thread(
            _run_cursor_watched, prompt, ws, sandbox, model, False, timeout_s
        )
    return await _run_with_progress(
        cursor_bridge.run_cursor,
        (prompt, ws, sandbox, model, False, timeout_s),
        ctx,
        timeout_s,
        label="cursor",
    )


@mcp.tool(
    annotations={
        "title": "Continue Cursor chat",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def cursor_continue(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = cursor_bridge.DEFAULT_SANDBOX,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the Cursor chat rooted at this workspace (resumes its chat id).

    Resumes the exact chat id the bridge minted on the last cursor_ask in this
    workspace, falling back to the newest on-disk chat whose recorded cwd matches
    (so it still works after a server restart). cursor applies permission flags per
    invocation, so `sandbox` takes effect here too — e.g. analyze read-only with
    cursor_ask, then continue with "workspace-write" to apply the fix.

    Args:
        prompt: Follow-up message for the existing chat.
        workspace: Working root used by the prior chat. Defaults to the server cwd.
        sandbox: Permission policy for THIS turn (default "read-only"). Same values
                 and caveats as cursor_ask.
        timeout_s: Max seconds to wait for cursor to complete. Default 180.
        watch: If true, open the live "watch" view streaming cursor's steps
               (same viewer as cursor_ask). Default false.
    """
    ws = cursor_bridge.normalize_workspace(workspace)
    cursor_bridge.validate_sandbox(sandbox)
    if watch:
        return await asyncio.to_thread(
            _run_cursor_watched, prompt, ws, sandbox, None, True, timeout_s
        )
    return await _run_with_progress(
        cursor_bridge.run_cursor,
        (prompt, ws, sandbox, None, True, timeout_s),
        ctx,
        timeout_s,
        label="cursor",
    )


@mcp.tool(
    annotations={
        "title": "Cursor bridge diagnostics",
        "readOnlyHint": True,  # only runs `cursor-agent --version`/`status` + reads local state
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def cursor_status() -> str:
    """Report diagnostics for the Cursor bridge setup (spends no quota).

    Reports the bridge's own version and whether a newer release is available
    (best-effort GitHub check; honors AGY_BRIDGE_NO_UPDATE_CHECK) — the same
    update notice antigravity_status shows, so a Cursor-only install still surfaces
    it — then checks whether cursor-agent is found (and its version), whether
    you're logged in (`cursor-agent status`), and where cursor stores its chats.
    Use this to debug "cursor not found" or auth errors before spending quota.
    """
    rows = [_bridge_version_status()] + cursor_bridge.status_rows()
    width = max(len(label) for label, _, _ in rows)
    lines = ["cursor bridge status"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)


def _cursor_tool_line(tc: dict) -> str:
    """A short, human-readable line for a cursor tool_call.

    cursor's `tool_call` payload has one `<name>ToolCall` key (e.g. shellToolCall,
    readToolCall) holding `args` + a `description`. Prefer a command/path-like
    arg, then the description, then the tool's name.
    """
    if not isinstance(tc, dict):
        return ""
    for key, val in tc.items():
        if not key.endswith("ToolCall") or not isinstance(val, dict):
            continue
        args = val.get("args") or {}
        if isinstance(args, dict):
            for f in ("command", "path", "file", "filePath", "query", "pattern", "url"):
                v = args.get(f)
                if isinstance(v, str) and v.strip():
                    return v.strip().splitlines()[0]
        desc = val.get("description")
        if isinstance(desc, str) and desc.strip():
            return desc.strip().splitlines()[0]
        return key[: -len("ToolCall")]  # e.g. "shell", "read"
    return ""


def _cursor_event_to_watch_lines(ev: dict) -> list[tuple[str, str]]:
    """Map one cursor stream-json event to (kind, text) watch lines
    (kind is 'narration' | 'command' | 'result'), mirroring
    _copilot_event_to_watch_lines. Returns [] for events not worth showing.
    """
    etype = ev.get("type")
    if etype == "assistant":
        content = (ev.get("message") or {}).get("content") or []
        txt = "".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
        return [("narration", txt.splitlines()[0][:200])] if txt else []
    if etype == "tool_call":
        sub = ev.get("subtype")
        if sub == "started":
            line = _cursor_tool_line(ev.get("tool_call") or {})
            return [("command", line[:200])] if line else []
        if sub == "completed":
            return [("result", "done")]
        return []
    if etype == "error":
        msg = ev.get("message") or (ev.get("error") or {}).get("message") or ""
        return [("result", f"error: {msg}"[:200])]
    if etype == "result" and ev.get("is_error"):
        return [("result", "error")]
    return []


def _run_cursor_watched(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    continue_conv: bool,
    timeout_s: int,
) -> str:
    """Like cursor_bridge.run_cursor, but stream cursor's steps to the live watch
    window. EXPERIMENTAL. Reuses the same localhost viewer as the agy/codex/copilot
    watch tools; the return value is identical to cursor_ask.
    """
    start = time.time()
    title = prompt.strip().splitlines()[0] if prompt.strip() else ""
    if len(title) > 200:
        title = title[:200].rsplit(" ", 1)[0] + "…"
    history = cursor_bridge.read_history(workspace, continue_conv)
    rid = _watch_begin(title, start, timeout_s, backend="cursor", prompt=prompt, history=history)
    try:
        port = _ensure_watch_server()
        _open_watch_window(_watch_url(port, rid), rid)
    except Exception:  # noqa: BLE001 — the viewer is best-effort, never fatal
        pass

    def on_event(ev: dict) -> None:
        watch_lines = _cursor_event_to_watch_lines(ev)
        if watch_lines:
            t = round(time.time() - start, 1)
            _watch_append(rid, [{"kind": k, "text": x, "t": t} for k, x in watch_lines])

    try:
        answer = cursor_bridge.run_cursor_streaming(
            prompt, workspace, sandbox, model, continue_conv, timeout_s, on_event
        )
    except Exception as e:  # noqa: BLE001 — show the failure in the window, then re-raise
        _watch_finish(rid, "error", f"({e})"[:200], time.time() - start)
        raise
    _watch_finish(rid, "done", answer, time.time() - start)
    return answer


# ============================================================ Grok tools
# EXPERIMENTAL — see grok_bridge's module docstring. The flag surface below is
# live-verified on grok 1.0.3; the ANSWER path behind grok's auth wall is not.
@mcp.tool(
    annotations={
        "title": "Ask Grok Build (new session) [experimental]",
        "readOnlyHint": False,  # grok may edit files / run commands per sandbox
        "idempotentHint": False,
        "openWorldHint": True,  # talks to the external xAI service
    }
)
async def grok_ask(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = grok_bridge.DEFAULT_SANDBOX,
    model: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Ask Grok Build (`grok -p`) a question or task in a NEW session. EXPERIMENTAL.

    ⚠️ Community-verified only: this bridge has never completed an authenticated
    round-trip, because the author has no Grok subscription. Its flags are verified
    against grok 1.0.3, but the answer path is not. If it misbehaves, say so rather
    than working around it — and please report it.

    Needs a SuperGrok / X Premium+ login (`grok login`) or an XAI_API_KEY env var;
    run `grok_status` first to check. Returns the agent's final message, read from
    grok's `--output-format json` result. Point `workspace` at a project dir for
    context-aware answers.

    Args:
        prompt: Question or instruction for Grok.
        workspace: Working root (`--cwd`). Defaults to the server cwd.
        sandbox: Permission policy (maps to grok's `--sandbox` profile plus a tool
                 allowlist): "read-only" (default — the `read-only` profile, no
                 write/shell tools, no subagents), "workspace-write" (the
                 `workspace` profile: writes land in the workspace, ~/.grok and
                 temp), or "danger-full-access" (profile `off` — avoid).
                 ⚠️ grok's OS sandbox is LINUX/macOS ONLY (Landlock/Seatbelt); on
                 Windows it is silently NOT enforced, so read-only there rests on
                 the agent-enforced tool allowlist alone. For a hard boundary on
                 every platform, use codex.
        model: Optional model override (`-m`, e.g. "grok-4.5"); validated against
               `grok models` and rejected on a typo. Omit to use grok's default.
        timeout_s: Max seconds to wait for grok to complete. Default 180.
        watch: If true, open a live "watch" view streaming grok's steps from its
               `--output-format streaming-json` event stream. Same final text is
               returned. Best-effort. Default false.
    """
    ws = grok_bridge.normalize_workspace(workspace)
    grok_bridge.validate_sandbox(sandbox)  # fail fast (grok checks auth first)
    grok_bridge.validate_model(model)  # fail fast on a typo (grok models)
    if watch:
        return await asyncio.to_thread(
            _run_grok_watched, prompt, ws, sandbox, model, False, timeout_s
        )
    return await _run_with_progress(
        grok_bridge.run_grok,
        (prompt, ws, sandbox, model, False, timeout_s),
        ctx,
        timeout_s,
        label="grok",
    )


@mcp.tool(
    annotations={
        "title": "Continue Grok Build session [experimental]",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def grok_continue(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = grok_bridge.DEFAULT_SANDBOX,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the Grok session rooted at this workspace. EXPERIMENTAL.

    Resumes the exact session id grok returned on the last grok_ask in this
    workspace (`-r <id>`), falling back to grok's own "most recent session for this
    cwd" (`-c`) when that in-memory pin is gone — so it still works after a server
    restart. grok applies permission flags per invocation, so `sandbox` takes effect
    here too: analyze read-only with grok_ask, then continue with "workspace-write"
    to apply the fix. Same experimental caveat and auth requirement as grok_ask.

    Args:
        prompt: Follow-up message for the existing session.
        workspace: Working root used by the prior session. Defaults to the server cwd.
        sandbox: Permission policy for THIS turn (default "read-only"). Same values
                 and platform caveats as grok_ask.
        timeout_s: Max seconds to wait for grok to complete. Default 180.
        watch: If true, open the live "watch" view streaming grok's steps
               (same viewer as grok_ask). Default false.
    """
    ws = grok_bridge.normalize_workspace(workspace)
    grok_bridge.validate_sandbox(sandbox)
    if watch:
        return await asyncio.to_thread(
            _run_grok_watched, prompt, ws, sandbox, None, True, timeout_s
        )
    return await _run_with_progress(
        grok_bridge.run_grok,
        (prompt, ws, sandbox, None, True, timeout_s),
        ctx,
        timeout_s,
        label="grok",
    )


@mcp.tool(
    annotations={
        "title": "Grok bridge diagnostics",
        "readOnlyHint": True,  # only runs `grok --version`/`models` + reads local state
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def grok_status() -> str:
    """Report diagnostics for the Grok Build bridge setup (spends no quota).

    Reports the bridge's own version and any newer release (the same update notice
    antigravity_status shows), then whether `grok` is found (and its version),
    whether you're authenticated, which models it offers, and where grok keeps its
    data. Auth and the model list both come from `grok models`, which answers even
    when logged out — so this is cheap and safe to call first.

    Use this to debug "grok not found" or auth errors before spending quota. This
    backend is EXPERIMENTAL and unverified end-to-end, so a green status here means
    the setup looks right, not that a live answer has ever been confirmed.
    """
    rows = [_bridge_version_status()] + grok_bridge.status_rows()
    width = max(len(label) for label, _, _ in rows)
    lines = ["grok bridge status  (EXPERIMENTAL — community-verified only)"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)


def _grok_tool_line(ev: dict) -> str:
    """A short, human-readable line for a grok `tool_call` event.

    grok's ACP-derived tool_call carries `toolName` plus a `rawInput` object and a
    human `title`. Prefer a command/path-like input field, then the title, then the
    tool name.
    """
    raw = ev.get("rawInput")
    if isinstance(raw, dict):
        for f in ("command", "path", "file_path", "pattern", "query", "url", "target_file"):
            v = raw.get(f)
            if isinstance(v, str) and v.strip():
                return v.strip().splitlines()[0]
    for f in ("title", "toolName"):
        v = ev.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip().splitlines()[0]
    return ""


def _grok_event_to_watch_lines(ev: dict) -> list[tuple[str, str]]:
    """Map one grok streaming-json event to (kind, text) watch lines
    (kind is 'narration' | 'command' | 'result'), mirroring
    _cursor_event_to_watch_lines. Returns [] for events not worth showing.
    """
    etype = ev.get("type")
    if etype in ("text", "thought"):
        data = ev.get("data")
        if isinstance(data, str) and data.strip():
            return [("narration", data.strip().splitlines()[0][:200])]
        return []
    if etype == "tool_call":
        line = _grok_tool_line(ev)
        return [("command", line[:200])] if line else []
    if etype == "tool_call_update":
        if ev.get("status") == "completed":
            return [("result", "done")]
        if ev.get("status") == "failed":
            return [("result", "failed")]
        return []
    if etype == "error":
        return [("result", f"error: {ev.get('message') or ''}"[:200])]
    return []


def _run_grok_watched(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    continue_conv: bool,
    timeout_s: int,
) -> str:
    """Like grok_bridge.run_grok, but stream grok's steps to the live watch window.

    EXPERIMENTAL twice over: the watch viewer is best-effort, and grok's
    streaming-json event shapes are docs-derived (never observed on a real answer).
    Unknown events simply render nothing. Return value is identical to grok_ask.
    """
    start = time.time()
    title = prompt.strip().splitlines()[0] if prompt.strip() else ""
    if len(title) > 200:
        title = title[:200].rsplit(" ", 1)[0] + "…"
    history = grok_bridge.read_history(workspace, continue_conv)
    rid = _watch_begin(title, start, timeout_s, backend="grok", prompt=prompt, history=history)
    try:
        port = _ensure_watch_server()
        _open_watch_window(_watch_url(port, rid), rid)
    except Exception:  # noqa: BLE001 — the viewer is best-effort, never fatal
        pass

    def on_event(ev: dict) -> None:
        watch_lines = _grok_event_to_watch_lines(ev)
        if watch_lines:
            t = round(time.time() - start, 1)
            _watch_append(rid, [{"kind": k, "text": x, "t": t} for k, x in watch_lines])

    try:
        answer = grok_bridge.run_grok_streaming(
            prompt, workspace, sandbox, model, continue_conv, timeout_s, on_event
        )
    except Exception as e:  # noqa: BLE001 — show the failure in the window, then re-raise
        _watch_finish(rid, "error", f"({e})"[:200], time.time() - start)
        raise
    _watch_finish(rid, "done", answer, time.time() - start)
    return answer


# ============================================================ Kimi tools
# EXPERIMENTAL — see kimi_bridge's module docstring. Verified only as far as an
# unauthenticated kimi 0.29.1 allows; deliberately no swarm/watch support until a
# live round-trip confirms kimi's stream-json envelope.
@mcp.tool(
    annotations={
        "title": "Ask Kimi (new session) [experimental]",
        "readOnlyHint": False,  # kimi print mode auto-executes tools (no sandbox)
        "idempotentHint": False,
        "openWorldHint": True,  # talks to the external Moonshot/Kimi service
    }
)
async def kimi_ask(
    prompt: str,
    workspace: Optional[str] = None,
    model: Optional[str] = None,
    timeout_s: int = 180,
    ctx: Optional[Context] = None,
) -> str:
    """Ask Kimi Code (`kimi -p`) a question or task in a NEW session. EXPERIMENTAL.

    ⚠️ Community-verified only — built without a Kimi account, so no authenticated
    round-trip has ever run and the author cannot verify it. It won't answer until
    you authenticate: run `kimi login` (device-code) or put an API key in
    ~/.kimi-code/config.toml, then check `kimi_status`. Returns the agent's final
    message, read straight from stdout. Kimi Code is Moonshot's terminal coding
    agent (Kimi K2 family); point `workspace` at a project dir for context-aware
    answers.

    Kimi print mode has NO sandbox and auto-executes every tool call (like
    antigravity), so run it only with trusted prompts on trusted content.

    Args:
        prompt: Question or instruction for Kimi.
        workspace: Working root (kimi's cwd). Defaults to the server cwd.
        model: Optional model alias (`-m`, from ~/.kimi-code/config.toml); omit to
               use config's default_model. Not validated up front (Kimi has no
               `models` list), so a bad alias surfaces as Kimi's own run-time error.
        timeout_s: Max seconds to wait for kimi to complete. Default 180.
    """
    ws = kimi_bridge.normalize_workspace(workspace)
    model = kimi_bridge.validate_model(model)
    return await _run_with_progress(
        kimi_bridge.run_kimi,
        (prompt, ws, model, False, timeout_s),
        ctx,
        timeout_s,
        label="kimi",
    )


@mcp.tool(
    annotations={
        "title": "Continue Kimi session [experimental]",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def kimi_continue(
    prompt: str,
    workspace: Optional[str] = None,
    timeout_s: int = 180,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the Kimi session rooted at this workspace (`kimi -c`). EXPERIMENTAL.

    Resumes the previous Kimi session for this workspace via `-c/--continue` — Kimi
    scopes sessions per working directory, so there's no id to track. Errors if no
    prior kimi_ask ran in this workspace. Same experimental caveat and auth
    requirement as kimi_ask.

    Args:
        prompt: Follow-up message for the existing session.
        workspace: Working root used by the prior session. Defaults to the server cwd.
        timeout_s: Max seconds to wait for kimi to complete. Default 180.
    """
    ws = kimi_bridge.normalize_workspace(workspace)
    return await _run_with_progress(
        kimi_bridge.run_kimi,
        (prompt, ws, None, True, timeout_s),
        ctx,
        timeout_s,
        label="kimi",
    )


@mcp.tool(
    annotations={
        "title": "Kimi bridge diagnostics",
        "readOnlyHint": True,  # only runs `kimi --version`/`provider list` + reads local state
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def kimi_status() -> str:
    """Report diagnostics for the Kimi bridge setup (spends no quota). EXPERIMENTAL.

    Reports the bridge's own version and any newer release (same update notice
    antigravity_status shows), then checks whether `kimi` is found (and its
    version), whether a provider is configured (`kimi provider list` — the auth
    proxy, since Kimi needs `kimi login` or an API key in config.toml), and where
    Kimi stores its data. This backend is unverified, so expect the auth row to say
    "no providers configured" until you log in.
    """
    rows = [_bridge_version_status()] + kimi_bridge.status_rows()
    width = max(len(label) for label, _, _ in rows)
    lines = ["kimi bridge status  (EXPERIMENTAL — community-verified only)"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)


# ============================================================ opencode tools
# LIVE-VERIFIED, unlike grok/kimi — opencode's free `opencode/*` models answer with
# no credentials at all, so the whole path (answer, resume, permission policy,
# event stream) was exercised end-to-end rather than inferred. See
# opencode_bridge's module docstring.
def _opencode_tool_line(part: dict) -> str:
    """A short, human-readable line for one opencode `tool_use` event part.

    The part is opencode's tool part: `{tool: "<name>", state: {status, input, …}}`,
    whose state schema (read off opencode's bundle) carries `input` on every status,
    a human `title` when completed, and `error` when it failed. Prefer a
    command/path-like input field, then that title, then the bare tool name —
    mirroring _grok_tool_line.
    """
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    tool = part.get("tool") if isinstance(part.get("tool"), str) else ""
    raw = state.get("input")
    if isinstance(raw, dict):
        for f in ("command", "filePath", "file_path", "path", "pattern", "query", "url"):
            v = raw.get(f)
            if isinstance(v, str) and v.strip():
                line = v.strip().splitlines()[0]
                return f"{tool}: {line}" if tool else line
    title = state.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip().splitlines()[0]
    return tool.strip()


def _opencode_event_to_watch_lines(ev: dict) -> list[tuple[str, str]]:
    """Map one opencode `--format json` event to (kind, text) watch lines
    (kind is 'narration' | 'command' | 'result'), mirroring
    _grok_event_to_watch_lines. Returns [] for events not worth showing.
    """
    etype = ev.get("type")
    part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
    if etype in ("text", "reasoning"):
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            return [("narration", text.strip().splitlines()[0][:200])]
        return []
    if etype == "tool_use":
        # opencode emits tool_use only once the call has finished, so a single
        # event carries both what ran and how it went.
        line = _opencode_tool_line(part)
        lines: list[tuple[str, str]] = []
        if line:
            lines.append(("command", line[:200]))
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        if state.get("status") == "error":
            lines.append(("result", f"error: {state.get('error') or ''}"[:200]))
        elif state.get("status") == "completed":
            lines.append(("result", "done"))
        return lines
    if etype == "error":
        err = ev.get("error")
        msg = ""
        if isinstance(err, dict):
            data = err.get("data")
            msg = str(data.get("message")) if isinstance(data, dict) and data.get("message") else ""
            msg = msg or str(err.get("name") or "")
        return [("result", f"error: {msg}"[:200])]
    return []


def _run_opencode_watched(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    continue_conv: bool,
    timeout_s: int,
) -> str:
    """Like opencode_bridge.run_opencode, but stream its steps to the watch window.

    opencode's one `--format json` stream is already incremental, so watch mode
    reuses the very argv the plain path runs — there is no second output format to
    drift (grok has to swap in `streaming-json`). Return value is identical to
    opencode_ask.
    """
    start = time.time()
    title = prompt.strip().splitlines()[0] if prompt.strip() else ""
    if len(title) > 200:
        title = title[:200].rsplit(" ", 1)[0] + "…"
    history = opencode_bridge.read_history(workspace, continue_conv)
    rid = _watch_begin(title, start, timeout_s, backend="opencode", prompt=prompt, history=history)
    try:
        port = _ensure_watch_server()
        _open_watch_window(_watch_url(port, rid), rid)
    except Exception:  # noqa: BLE001 — the viewer is best-effort, never fatal
        pass

    def on_event(ev: dict) -> None:
        watch_lines = _opencode_event_to_watch_lines(ev)
        if watch_lines:
            t = round(time.time() - start, 1)
            _watch_append(rid, [{"kind": k, "text": x, "t": t} for k, x in watch_lines])

    try:
        answer = opencode_bridge.run_opencode_streaming(
            prompt,
            workspace,
            sandbox,
            model,
            continue_conv,
            timeout_s,
            on_event=on_event,
        )
    except Exception as e:  # noqa: BLE001 — show the failure in the window, then re-raise
        _watch_finish(rid, "error", f"({e})"[:200], time.time() - start)
        raise
    _watch_finish(rid, "done", answer, time.time() - start)
    return answer


@mcp.tool(
    annotations={
        "title": "Ask opencode (new session)",
        "readOnlyHint": False,  # opencode may edit files / run commands per sandbox
        "idempotentHint": False,
        "openWorldHint": True,  # talks to an external model provider
    }
)
async def opencode_ask(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = opencode_bridge.DEFAULT_SANDBOX,
    model: Optional[str] = None,
    timeout_s: int = 300,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Ask opencode (`opencode run`) a question or task in a NEW session.

    The one backend here that needs NO subscription: opencode's own free hosted
    models (`opencode/*-free`, see `opencode models`) answer with zero credentials
    configured, so this works on a machine that has never logged in to anything.
    Add a key with `opencode auth login` for Claude/GPT-class models. Returns the
    agent's final message, reconstructed from opencode's `--format json` events.
    Point `workspace` at a project dir for context-aware answers.

    ⚠️ The free models are SLOW (queue-scheduled — a one-word answer has taken
    minutes), which is why timeout_s defaults to 300 here. Prefer a configured
    paid model for anything long, and don't mistake slowness for a hang.

    Args:
        prompt: Question or instruction for opencode.
        workspace: Working root (`--dir`). Defaults to the server cwd.
        sandbox: Permission policy, applied via opencode's OPENCODE_PERMISSION and
                 enforced by the agent on every platform alike: "read-only"
                 (default — no edit/bash/subagent/network tools, `.env` files
                 denied), "workspace-write" (edit and shell inside the workspace;
                 reaching outside it is denied), or "danger-full-access"
                 (opencode's own `--auto`, no policy — avoid).
                 ⚠️ Agent-enforced, NOT an OS boundary: a determined tool call is
                 refused by opencode, not by the kernel. For a hard boundary, use
                 codex.
        model: Optional model id (`-m`, "provider/model" — e.g.
               "opencode/nemotron-3.5-lightning-free"); validated against
               `opencode models` and rejected on a typo. Omit for opencode's
               configured default.
        timeout_s: Max seconds to wait for opencode to complete. Default 300.
        watch: If true, open a live "watch" view streaming opencode's steps from
               the same `--format json` event stream. Same final text is returned.
               Best-effort. Default false.
    """
    ws = opencode_bridge.normalize_workspace(workspace)
    opencode_bridge.validate_sandbox(sandbox)  # fail fast — a bad mode must not run wide open
    model = opencode_bridge.validate_model(model)  # fail fast on a typo (opencode models)
    if watch:
        return await asyncio.to_thread(
            _run_opencode_watched, prompt, ws, sandbox, model, False, timeout_s
        )
    return await _run_with_progress(
        opencode_bridge.run_opencode,
        (prompt, ws, sandbox, model, False, timeout_s),
        ctx,
        timeout_s,
        label="opencode",
    )


@mcp.tool(
    annotations={
        "title": "Continue opencode session",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def opencode_continue(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = opencode_bridge.DEFAULT_SANDBOX,
    timeout_s: int = 300,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the opencode session rooted at this workspace.

    Resumes the exact session id opencode reported on the last opencode_ask in this
    workspace (`-s <id>`), falling back to opencode's own "most recent session for
    this directory" (`-c`) when that in-memory pin is gone — so it still works after
    a server restart. Session scoping is per directory (verified live: the same
    `-c` from a different directory starts a fresh session), so pass the same
    `workspace` you asked in. The permission policy applies per invocation, so
    `sandbox` takes effect here too: analyze read-only with opencode_ask, then
    continue with "workspace-write" to apply the fix.

    Args:
        prompt: Follow-up message for the existing session.
        workspace: Working root used by the prior session. Defaults to the server cwd.
        sandbox: Permission policy for THIS turn (default "read-only"). Same values
                 and caveats as opencode_ask.
        timeout_s: Max seconds to wait for opencode to complete. Default 300.
        watch: If true, open the live "watch" view streaming opencode's steps
               (same viewer as opencode_ask). Default false.
    """
    ws = opencode_bridge.normalize_workspace(workspace)
    opencode_bridge.validate_sandbox(sandbox)
    if watch:
        return await asyncio.to_thread(
            _run_opencode_watched, prompt, ws, sandbox, None, True, timeout_s
        )
    return await _run_with_progress(
        opencode_bridge.run_opencode,
        (prompt, ws, sandbox, None, True, timeout_s),
        ctx,
        timeout_s,
        label="opencode",
    )


@mcp.tool(
    annotations={
        "title": "opencode bridge diagnostics",
        "readOnlyHint": True,  # only runs `opencode --version`/`models`/`providers list`
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def opencode_status() -> str:
    """Report diagnostics for the opencode bridge setup (spends no quota).

    Reports the bridge's own version and any newer release (same update notice
    antigravity_status shows), then checks whether `opencode` is found (and its
    version), how many provider credentials are configured, which model ids are
    available, and where opencode keeps its data. "0 credentials" is NOT a failure
    here — opencode's free `opencode/*` models still answer — so that row stays ok
    as long as models are listed.
    """
    rows = [_bridge_version_status()] + opencode_bridge.status_rows()
    width = max(len(label) for label, _, _ in rows)
    lines = ["opencode bridge status"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)


def main() -> None:
    """Console entry point (also `python server.py`).

    Exposed as the `agent-intern` script so the bridge can be launched with
    `uvx agent-intern` (isolated, always-latest) instead of a hardcoded path.
    """
    _configure_logging()
    _startup_checks()
    mcp.run()


if __name__ == "__main__":
    main()
