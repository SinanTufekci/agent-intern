"""opencode CLI bridge: run `opencode run` headless and return its answer.

Seventh backend alongside the agy bridge (server.py), Codex (codex_bridge.py),
Copilot (copilot_bridge.py), Cursor (cursor_bridge.py), Grok (grok_bridge.py) and
Kimi (kimi_bridge.py). opencode is SST's open-source terminal coding agent
(`opencode`, https://github.com/sst/opencode, npm `opencode-ai`). Like
codex/copilot/cursor it is stdout-native in headless mode: `opencode run --format
json "<prompt>"` runs one prompt non-interactively, writes NDJSON events to
STDOUT, and exits — so we read the answer from stdout, no transcript scraping.

✅ LIVE-VERIFIED, AND THE ONLY BACKEND THAT NEEDS NO SUBSCRIPTION. Unlike Grok and
Kimi, this bridge HAS completed real round-trips: opencode ships free hosted models
under its own `opencode/*` provider that answer with ZERO credentials configured
(`0 credentials` in `opencode providers list`), so the claims below were observed on
opencode 1.18.29 / Windows rather than inferred. Point `model` at one of the
`opencode/*-free` ids and the bridge works on a machine that has never logged in to
anything. Bring your own key (Anthropic, OpenAI, …) for the good models.

WHY THE FLAGS BELOW ARE NOT GUESSWORK. opencode ships as a single compiled Bun
binary, but the bundle embeds its own JS source, so the `run` command's argv
parser, its event writer and its permission resolution were read off the actual
implementation inside `bin/opencode.exe` — then confirmed by executing them. The
notes marked "read off the bundle" below cite that source.

HEADLESS FLAGS. `opencode run <message...>` takes the prompt POSITIONALLY (unlike
every other backend here, where it is a flag value) and joins the positional array
with spaces — read off the bundle: `[...j.message, ...j["--"]||[]].join(" ")`.
Because `j["--"]` is concatenated too, the bridge passes the prompt after a `--`
separator, so a prompt starting with `-` can never be parsed as a flag.
`--format json` swaps the human transcript for NDJSON events (this is also the
watch-mode stream — one flag serves both, unlike grok's separate
`streaming-json`). `--dir <ws>` roots the run at the workspace, and the caller
sets the process cwd to match.

⚠️ STDIN MUST BE CLOSED. Read off the bundle: `let p = process.stdin.isTTY ?
undefined : await Bun.stdin.text()` — with a non-TTY stdin, opencode READS STDIN TO
EOF and appends it to the prompt. An MCP server's child gets a pipe, not a TTY, so
without `stdin=DEVNULL` opencode would block forever waiting on a stream nobody
closes. Every call here passes DEVNULL.

CONTINUE / RESUME. Each JSON event carries the `sessionID`, so — like codex/cursor
/grok — the bridge captures it on a fresh ask, pins it to the workspace, and
resumes that exact session with `-s <id>`. The restart-proof fallback is
opencode's own `-c/--continue`, which (read off the bundle) picks the most recent
PARENT session for the run directory: `(await client.session.list()).data?.find(s
=> !s.parentID)`, with the client bound to that directory. A stale or unknown id
exits 1 with "Session not found" — verified live — so the failure is loud, never
silent. No session store is ever parsed.

SECURITY — A REAL, PORTABLE PERMISSION GATE (no OS sandbox). Two facts, both read
off the bundle and then confirmed live, make this the strongest non-Codex posture
in the project:
  1. In headless `run`, a permission that would prompt is AUTO-REJECTED, never
     hung: `if (auto) reply("once") else { println("...auto-rejecting");
     reply("reject") }`. So an "ask" rule is a deny in this mode, and the bridge
     can never wedge on an invisible prompt.
  2. `OPENCODE_PERMISSION` (a JSON env var) is merged into the config permission
     set, and the config set is merged LAST into every built-in agent:
     `permission: merge(defaults, agent_specific, fromConfig(config.permission))`.
     So the bridge's policy wins over the agent's own rules — including the
     nominally read-only `plan` agent, which in fact leaves `bash` allowed.
Unlike grok's OS sandbox this is agent-enforced, so it is NOT a hard boundary —
but unlike grok's, it behaves IDENTICALLY on Windows, macOS and Linux. Modes:
  - read-only        deny edit/bash/task/webfetch/websearch/external_directory;
                     read and search tools allowed, `.env` files denied.
  - workspace-write  writes and shell inside the workspace; `external_directory`
                     denied, so escaping the workspace is refused, not asked.
  - danger-full-access  `--auto` (opencode's own word for it: "dangerous!"), no
                     policy at all.
⚠️ FOOTGUN, read off the bundle: invalid JSON in OPENCODE_PERMISSION is SKIPPED
with a debug-level warning — a typo would silently mean NO restrictions. That is
why the policy is built as a dict and serialized with json.dumps, never
hand-formatted.

AUTH. `opencode auth login` (the command is also spelled `opencode providers`)
stores credentials in `~/.local/share/opencode/auth.json`; provider env vars
(ANTHROPIC_API_KEY, OPENAI_API_KEY, …) also work. Neither is REQUIRED: the free
`opencode/*` models answer without any of it. The bridge never touches credentials —
it only shells out to `opencode --version`, `opencode models` and `opencode
providers list` (all non-interactive, all free) for the status view.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Optional

# The opencode executable. npm installs an `opencode.cmd`/`.ps1` shim on Windows
# that CreateProcess can't launch by bare name, so resolve via shutil.which (honors
# PATHEXT, returns the full shim path). Set OPENCODE_BIN to an explicit path to
# override. Mirrors AGY_BIN / CODEX_BIN / COPILOT_BIN / CURSOR_BIN / GROK_BIN /
# KIMI_BIN. Read once at import; the launching process's env wins.
OPENCODE_BIN_ENV = os.environ.get("OPENCODE_BIN", "opencode")


def _resolve_bin() -> str:
    """Full path to the opencode executable (see OPENCODE_BIN_ENV note)."""
    if os.path.sep in OPENCODE_BIN_ENV or os.path.isfile(OPENCODE_BIN_ENV):
        return OPENCODE_BIN_ENV
    return shutil.which(OPENCODE_BIN_ENV) or OPENCODE_BIN_ENV


OPENCODE_BIN = _resolve_bin()

# opencode's data home: auth.json, sessions, logs, plans. It follows the XDG layout
# ON EVERY PLATFORM — read off the bundle as `XDG_DATA_HOME || join(homedir(),
# ".local/share")` then `/opencode`, with no Windows special case, and confirmed live
# there: `opencode providers list` reports "Credentials
# ~\.local\share\opencode\auth.json" rather than an AppData path. We only READ under
# it (for status), never write.
OPENCODE_DATA_HOME = (
    Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "opencode"
)

# Text decoding for opencode's subprocess output: it emits UTF-8, which the Windows
# locale codec (cp1252) can mangle. Decode UTF-8 explicitly and never raise on a
# stray byte. Mirrors cursor_bridge._TEXT.
_TEXT = {"encoding": "utf-8", "errors": "replace"}

# The `sandbox` knob mirrors codex's/copilot's/cursor's/grok's for a uniform
# agent_swarm field, but maps to opencode's permission set (see the module SECURITY
# note). Default read-only for safety parity — callers opt into write access.
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_SANDBOX = "read-only"

# Default per-call budget, and the floor the swarm raises a worker to. 300s rather
# than the other bridges' 180s because opencode's FREE hosted models are
# queue-scheduled and genuinely slow: measured end-to-end at 152, 161, 163, 166,
# 214, 260 and 428 seconds for one-word answers. A 180s budget would fail about
# half of those, so a swarm that kept the shared default would report an opencode
# worker as broken when it was merely slow. Paid models are not affected — they
# just finish early.
DEFAULT_TIMEOUT_S = 300

# Permission policies, keyed by sandbox mode, serialized into OPENCODE_PERMISSION.
# The keys and the three actions come from opencode's own PermissionConfig schema
# (read off the bundle): read, edit, glob, grep, list, bash, task,
# external_directory, todowrite, question, webfetch, websearch, lsp, doom_loop,
# skill — plus arbitrary extra keys — each "ask" | "allow" | "deny", or a
# {pattern: action} map. Remember that in headless run mode "ask" == reject, so
# only allow/deny are used here.
#
# read-only is an explicit DENYLIST of the mutating/outbound tools rather than an
# allowlist, because opencode's set is open-ended (an MCP server adds its own keys)
# and `"*": "deny"` would also switch off the read tools that make the mode useful.
# The one wildcard kept is on `read`, to keep secrets out of the model's context:
# `.env` files are denied outright (opencode's own default merely ASKS, which in
# headless mode already rejects — this makes it explicit and survives a user config
# that loosened it).
_READ_ONLY_PERMISSION = {
    "edit": "deny",
    "bash": "deny",
    "task": "deny",  # a subagent would otherwise get its own, unrestricted turn
    # opencode's own loop guard. Its default is "ask", which headless mode already
    # rejects, so this changes nothing in the default case — it exists so a user
    # config that set it to "allow" cannot turn a fenced run into an unbounded
    # retry loop. Observed live: a weak model told to write under read-only spent
    # the ENTIRE timeout retrying tools it could not have.
    "doom_loop": "deny",
    "external_directory": "deny",
    "webfetch": "deny",
    "websearch": "deny",
    "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"},
    "glob": "allow",
    "grep": "allow",
    "list": "allow",
    "lsp": "allow",
    "todowrite": "allow",
}

# workspace-write keeps opencode's default "allow" for edit/bash (both are already
# rooted at the run directory) and hard-denies the one rule that lets a tool reach
# OUTSIDE it. opencode's default for external_directory is "ask", which headless
# mode already rejects; pinning it to "deny" means the boundary does not move if a
# user config or a future default loosens it.
_WORKSPACE_WRITE_PERMISSION = {
    "doom_loop": "deny",  # same reasoning as read-only's
    "external_directory": "deny",
    "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"},
}

# The count line of `opencode providers list` ("└  0 credentials"). Anchored on the
# NUMBER, not on the word: the same box's header line reads "Credentials
# <path>/auth.json", so a plain substring search matches the wrong line.
_CREDENTIAL_COUNT_RE = re.compile(r"(\d+)\s+credentials?\b", re.IGNORECASE)

# Signal used to take out a POSIX process group in _kill_tree. SIGKILL is the
# intent and always exists where that branch runs; the getattr keeps the constant
# importable on Windows (which has no SIGKILL) so the branch stays unit-testable
# there rather than only on the platforms that execute it.
_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)

# Cached model-id list from `opencode models` (populated once on first validation;
# a transient failure is not cached).
_MODELS_CACHE: Optional[list[str]] = None

# workspace -> session id, pinned after each fresh ask so opencode_continue resumes
# the exact session. Guarded by a lock (MCP tools may run on different threads).
# Lives only for the process; opencode's own `-c` is the restart-proof fallback.
_PINNED: dict[str, str] = {}
_PIN_LOCK = threading.Lock()


def _spawn_kwargs() -> dict:
    """Keep opencode from flashing a console window on Windows; new session elsewhere.

    opencode writes its events to stdout regardless of the controlling terminal.
    Windows uses CREATE_NO_WINDOW so the .cmd shim doesn't flash a console; POSIX
    starts a new session. Mirrors the other bridges.
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {"start_new_session": True}


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the launched process AND everything it spawned.

    ⚠️ This is not belt-and-braces, it is the fix for an observed hang. On Windows
    `opencode` on PATH is npm's `opencode.CMD` shim, so CreateProcess runs cmd.exe
    and the real `opencode.exe` is a GRANDchild. Killing only the direct child
    leaves that grandchild alive holding the write end of our stdout pipe — measured
    live: a 270s timeout took 396s to return, and only because the orphan was killed
    by hand; it would otherwise have blocked forever. (This is also why the blocking
    path below does not use `subprocess.run(timeout=...)`: its Windows branch calls
    communicate() again after killing, which is exactly the read that never ends.)

    `taskkill /T` walks the tree on Windows. POSIX has no shim layer (npm links the
    binary directly, so there is no grandchild by construction) but opencode's own
    `bash` tool can still leave children behind, so the process group created by
    _spawn_kwargs()'s start_new_session is signalled there. Both paths finish with a
    plain kill, which is the only one that can be trusted to have run.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
                **_spawn_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            pass  # fall through to the plain kill below
    else:
        try:
            os.killpg(os.getpgid(proc.pid), _KILL_SIGNAL)
        except (OSError, AttributeError):
            pass  # already reaped, or no process groups here
    try:
        proc.kill()
    except OSError:
        pass


def _permission_policy(sandbox: str) -> Optional[dict]:
    """The OPENCODE_PERMISSION dict for `sandbox`, or None for no policy at all."""
    validate_sandbox(sandbox)
    if sandbox == "read-only":
        return dict(_READ_ONLY_PERMISSION)
    if sandbox == "workspace-write":
        return dict(_WORKSPACE_WRITE_PERMISSION)
    return None  # danger-full-access


def _env(sandbox: Optional[str] = None) -> dict:
    """Process env for opencode: auto-updater off, plus the permission policy.

    A CLI that updates itself mid-session is how this project has been broken
    before with no code change on our side (see the agy stdout saga), and opencode
    ships an auto-updater. `OPENCODE_DISABLE_AUTOUPDATE=1` is its process-scoped off
    switch, read off the bundle's env table.

    `sandbox` selects the OPENCODE_PERMISSION policy; None leaves the caller's
    environment untouched (used by the read-only diagnostics). json.dumps is not
    cosmetic here — see the module note on invalid JSON being silently ignored.
    """
    env = {**os.environ, "OPENCODE_DISABLE_AUTOUPDATE": "1"}
    if sandbox is None:
        return env
    policy = _permission_policy(sandbox)
    if policy is None:
        # danger-full-access: impose nothing. Drop any inherited policy too, so the
        # mode means what it says instead of quietly keeping an outer restriction.
        env.pop("OPENCODE_PERMISSION", None)
    else:
        env["OPENCODE_PERMISSION"] = json.dumps(policy)
    return env


def normalize_workspace(ws: Optional[str]) -> str:
    """Absolute path for `ws`, or the server's cwd when omitted."""
    return os.path.abspath(ws) if ws else os.getcwd()


def validate_sandbox(mode: str) -> str:
    """Return `mode` if valid, else raise ValueError listing the allowed values.

    Client-side on purpose: the sandbox is an env-var policy, not a CLI flag, and
    opencode ignores a policy it cannot parse (module note) — so an unrecognised
    mode must be rejected HERE or it would quietly run wide open.
    """
    if mode not in SANDBOX_MODES:
        raise ValueError(f"invalid sandbox {mode!r}; expected one of: {', '.join(SANDBOX_MODES)}")
    return mode


# ----------------------------------------------------------------- session pinning
def get_pinned(workspace: str) -> Optional[str]:
    """The session id pinned to `workspace` this run, or None."""
    with _PIN_LOCK:
        return _PINNED.get(workspace)


def _pin(workspace: str, session_id: str) -> None:
    with _PIN_LOCK:
        _PINNED[workspace] = session_id


def _resume_flags(workspace: str, continue_conv: bool) -> tuple[Optional[str], bool]:
    """(resume_id, use_continue) for a run against `workspace`.

    Fresh ask -> (None, False). Continue -> the pinned session id if this process
    minted one, else (None, True) so opencode's own "most recent session for this
    directory" lookup handles it. That fallback is what makes continue survive a
    server restart without us reading opencode's session store.
    """
    if not continue_conv:
        return (None, False)
    pinned = get_pinned(workspace)
    return (pinned, pinned is None)


# ----------------------------------------------------------------- conversation history
def read_history(workspace: str, continue_conv: bool) -> list[dict]:
    """Prior turns for the watch view — always [] for opencode (best-effort).

    opencode can export a session (`opencode export <id>`), but that is a second
    subprocess and a second output format to keep in sync for a cosmetic panel, so
    a continued watch window simply opens without visible history. Kept for
    signature parity with the other bridges.
    """
    return []


# ----------------------------------------------------------------- models
def list_models() -> list[str]:
    """Model ids from `opencode models` (cached), or [] if it can't be run.

    Verified live (opencode 1.18.29, ZERO credentials — it still exits 0 and lists
    the free hosted models, which is why this is safe to call for validation):

        opencode/big-pickle
        opencode/nemotron-3.5-lightning-free
        ...

    One `provider/model` id per line and nothing else — so the parse is "keep the
    non-empty lines that contain a slash". Adding credentials lengthens the list.
    """
    global _MODELS_CACHE
    if _MODELS_CACHE is not None:
        return _MODELS_CACHE
    try:
        proc = subprocess.run(
            [OPENCODE_BIN, "models"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            env=_env(),
            **_TEXT,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return []  # transient — don't cache
    ids = [ln.strip() for ln in (proc.stdout or "").splitlines() if "/" in ln.strip()]
    _MODELS_CACHE = ids
    return ids


def validate_model(model: Optional[str]) -> Optional[str]:
    """Return `model` if it's a known opencode model id, else raise ValueError.

    Skips validation (returns as-is) when the model list can't be fetched, mirroring
    the agy/cursor/grok lenient fallback. Worth doing up front because `opencode
    models` costs nothing and answers with no credentials — whereas an unknown id
    otherwise surfaces only once the run is already under way.
    """
    if model is None or not str(model).strip():
        return None
    model = str(model).strip()
    models = list_models()
    if models and model not in models:
        sample = ", ".join(models[:6])
        raise ValueError(
            f"unknown opencode model {model!r}; see `opencode models`. "
            f"Ids look like provider/model, e.g.: {sample}"
        )
    return model


# ----------------------------------------------------------------- running opencode
def build_args(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    resume_id: Optional[str] = None,
    continue_conv: bool = False,
    agent: Optional[str] = None,
) -> list[str]:
    """argv for a headless `opencode run` (fresh, `-s <id>` resume, or `-c` continue).

    `--format json` yields the NDJSON event stream the answer is reconstructed
    from (and that watch mode renders). `--dir` roots the run at the workspace.
    The prompt goes LAST, after `--`, because it is positional here — see the module
    HEADLESS FLAGS note. `--auto` appears only for danger-full-access; the other two
    modes rely on the permission policy in the environment, where "would prompt"
    already means "rejected".

    Resume precedence: an explicit `resume_id` (`-s`) beats `continue_conv` (`-c`).

    `agent` (`--agent`, e.g. opencode's built-in "plan") is a bridge-level knob no
    MCP tool exposes yet: the sandbox modes above are the supported way to fence a
    run, and `plan` in particular is NOT one — it leaves `bash` allowed (see the
    module SECURITY note). It stays here because it is the natural hook for custom
    agents, and it is covered by tests so it can't rot unnoticed.
    """
    validate_sandbox(sandbox)
    args = [OPENCODE_BIN, "run", "--format", "json", "--dir", workspace]
    if resume_id:
        args += ["-s", resume_id]
    elif continue_conv:
        args.append("-c")
    if model:
        args += ["-m", model]
    if agent:
        args += ["--agent", agent]
    if sandbox == "danger-full-access":
        args.append("--auto")
    args += ["--", prompt]
    return args


def _consume_event(ev: dict, state: dict) -> None:
    """Accumulate the answer + session id from one `--format json` event.

    Every event is `{"type": ..., "timestamp": ..., "sessionID": ..., ...}` (read
    off the bundle's writer, confirmed live). Only completed `text` parts carry
    answer text — opencode emits a text event once the part has an end time, so
    there are no partial chunks to de-duplicate. An `error` event is opencode's
    failure envelope; record it so the caller can raise opencode's own message
    instead of "no output".
    """
    sid = ev.get("sessionID")
    if isinstance(sid, str) and sid:
        state["session_id"] = sid
    etype = ev.get("type")
    if etype == "text":
        part = ev.get("part")
        text = part.get("text") if isinstance(part, dict) else None
        if isinstance(text, str) and text.strip():
            state.setdefault("chunks", []).append(text.strip())
    elif etype == "error":
        err = ev.get("error")
        msg = ""
        if isinstance(err, dict):
            data = err.get("data")
            if isinstance(data, dict) and data.get("message"):
                msg = str(data["message"])
            else:
                msg = str(err.get("name") or err)
        elif err:
            msg = str(err)
        if msg.strip():
            state["error"] = msg.strip()


def _finish(state: dict, stderr: str, returncode: Optional[int]) -> str:
    """The final answer from an accumulated event `state`, or raise with a reason."""
    if state.get("error"):
        raise RuntimeError(f"opencode error: {state['error']}")
    if returncode not in (0, None):
        raise RuntimeError(
            f"opencode exited {returncode}\n"
            f"stderr: {(stderr or '')[-1000:]}\n"
            f"session: {state.get('session_id') or '(none)'}"
        )
    answer = "\n\n".join(state.get("chunks", [])).strip()
    if not answer:
        raise RuntimeError(
            f"opencode produced no text in its --format json output. "
            f"stderr: {(stderr or '')[-300:]}"
        )
    return answer


def run_opencode(
    prompt: str,
    workspace: str,
    sandbox: str = DEFAULT_SANDBOX,
    model: Optional[str] = None,
    continue_conv: bool = False,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    pin: bool = True,
    agent: Optional[str] = None,
) -> str:
    """Run `opencode run` (fresh, resume, or continue) and return the final answer.

    On a fresh run the session id comes back on every JSON event and is pinned to
    `workspace` so a later opencode_continue resumes the exact session — pass
    `pin=False` for swarm workers (one-shot, no continue). Signature mirrors
    run_grok so server.py's _run_with_progress can call it unchanged.

    The default timeout is 300s rather than the other bridges' 180s: opencode's FREE
    hosted models are queue-scheduled and were measured taking minutes for a
    one-word answer, and that free path is exactly what a fresh install hits — which
    also means the timeout here is a path users really take, not a theoretical one.
    That is why this shares the streaming implementation instead of calling
    `subprocess.run(timeout=...)`: see _kill_tree for the hang that would cause.
    """
    return _run_impl(prompt, workspace, sandbox, model, continue_conv, timeout_s, None, pin, agent)


def run_opencode_streaming(
    prompt: str,
    workspace: str,
    sandbox: str = DEFAULT_SANDBOX,
    model: Optional[str] = None,
    continue_conv: bool = False,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    on_event=None,
    pin: bool = True,
    agent: Optional[str] = None,
) -> str:
    """Run `opencode run --format json`, stream events, return the answer.

    Same argv and same implementation as run_opencode — opencode's one JSON format
    is already an incremental NDJSON stream, so unlike grok there is no separate
    streaming flag and no second parser to drift. The only difference is that each
    line is handed to `on_event(event_dict)` as it arrives (this is how watch mode
    renders steps live). Completion is driven by the process exiting (with a
    deadline), matching the codex/copilot/cursor/grok path.
    """
    return _run_impl(
        prompt, workspace, sandbox, model, continue_conv, timeout_s, on_event, pin, agent
    )


def _run_impl(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    continue_conv: bool,
    timeout_s: int,
    on_event,
    pin: bool,
    agent: Optional[str],
) -> str:
    """The one runner behind run_opencode and run_opencode_streaming."""
    validate_sandbox(sandbox)
    os.makedirs(workspace, exist_ok=True)
    resume_id, use_continue = _resume_flags(workspace, continue_conv)
    args = build_args(prompt, workspace, sandbox, model, resume_id, use_continue, agent)

    state: dict = {}
    proc = subprocess.Popen(
        args,
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(sandbox),
        **_TEXT,
        **_spawn_kwargs(),
    )
    err_chunks: list[str] = []

    def _pump_stdout() -> None:
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                _consume_event(ev, state)
                if on_event is not None:
                    try:
                        on_event(ev)
                    except Exception:  # noqa: BLE001 — a viewer hiccup must not kill the run
                        pass
        except (ValueError, OSError):
            pass  # pipe closed (e.g. on kill)

    def _pump_stderr() -> None:
        try:
            for line in proc.stderr:
                err_chunks.append(line)
        except (ValueError, OSError):
            pass

    ot = threading.Thread(target=_pump_stdout, daemon=True)
    et = threading.Thread(target=_pump_stderr, daemon=True)
    ot.start()
    et.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s + 30)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)  # the npm shim's grandchild must die too — see _kill_tree
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass  # reaped or not, we are not blocking the caller on it
    # Daemon threads with a bounded join: if a survivor still holds the pipe, we
    # return anyway rather than inheriting its lifetime.
    ot.join(timeout=1)
    et.join(timeout=1)

    if timed_out:
        # Name the usual cause. opencode's free models are queue-scheduled and were
        # measured at 152-428s for trivial answers, so a timeout here is far more
        # often "too slow" than "stuck" — and the caller can fix it with one arg.
        raise RuntimeError(
            f"opencode timed out after {timeout_s + 30}s. opencode's free "
            f"`opencode/*` models are queue-scheduled and routinely take minutes — "
            f"raise timeout_s, or use a configured (paid) model."
        )

    answer = _finish(state, "".join(err_chunks), proc.returncode)
    if not continue_conv and pin and state.get("session_id"):
        _pin(workspace, state["session_id"])
    return answer


# ----------------------------------------------------------------- diagnostics
def opencode_version() -> Optional[str]:
    """`opencode --version` first line (e.g. "1.18.29"), or None if it can't run."""
    try:
        proc = subprocess.run(
            [OPENCODE_BIN, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=20,
            env=_env(),
            **_TEXT,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return text.splitlines()[0] if text else None


def auth_status() -> tuple[bool, str]:
    """(ok, detail) for the credentials row, via `opencode providers list`. Free.

    Verified live on 1.18.29: with nothing configured it exits 0 and prints a box
    whose LAST line is the count, "0 credentials". The count is matched by regex
    rather than by "the line mentioning credentials", because the box's HEADER also
    says "Credentials ~/.local/share/opencode/auth.json" and would otherwise win.
    The row is NOT a hard requirement — opencode's free `opencode/*` models answer
    with zero credentials — so "0 credentials" still reads ok as long as models are
    listed, with a hint about `opencode auth login`. It is a problem only when
    opencode can't run at all.
    """
    try:
        proc = subprocess.run(
            [OPENCODE_BIN, "providers", "list"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            env=_env(),
            **_TEXT,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return (False, "could not run `opencode providers list`")
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if not out:
        return (False, "`opencode providers list` printed nothing")
    match = next(
        (m for m in (_CREDENTIAL_COUNT_RE.search(ln) for ln in out.splitlines()) if m),
        None,
    )
    if match is None:
        # Box layout changed under us: don't guess a number, and don't fail the row
        # either — the free models are what actually decide whether this can answer.
        return (bool(list_models()), "could not read a credential count from the output")
    if match.group(1) == "0":
        return (
            bool(list_models()),
            "0 credentials — free `opencode/*` models only "
            "(run `opencode auth login` for Anthropic/OpenAI/…)",
        )
    return (True, match.group(0).strip())


def status_rows() -> list[tuple[str, bool, str]]:
    """Setup diagnostics as (label, ok, detail) rows. Spends no quota.

    Mirrors the codex/copilot/cursor/grok status_rows shape so server.py renders
    opencode rows with the same formatter.
    """
    rows: list[tuple[str, bool, str]] = []

    ver = opencode_version()
    if ver is None:
        rows.append(
            ("opencode CLI", False, f"not found (set OPENCODE_BIN; tried {OPENCODE_BIN_ENV!r})")
        )
    else:
        rows.append(("opencode CLI", True, ver))

    ok, detail = auth_status()
    rows.append(("credentials", ok, detail))

    models = list_models()
    rows.append(("models", bool(models), ", ".join(models[:4]) if models else "none listed"))

    rows.append(("data dir", OPENCODE_DATA_HOME.exists(), str(OPENCODE_DATA_HOME)))

    with _PIN_LOCK:
        n_pins = len(_PINNED)
    rows.append(("pinned sessions", True, f"{n_pins} workspace(s) pinned this run"))

    return rows
