"""Claude Code CLI bridge: run `claude -p` headless and return its answer.

Lets a host that is NOT Claude Code (Antigravity, Codex, Copilot, Cursor, opencode,
...) delegate to Claude Code as a sub-agent on the user's own Claude plan or API
key. When the host IS Claude Code the tools are left out (see
server._claude_hidden_in_claude_code): delegating to yourself only spends the same
quota twice.

HEADLESS SHAPE (claude 2.1.319, from `claude --help` and the headless docs):
  claude -p --output-format stream-json --verbose --disable-slash-commands
         --strict-mcp-config <sandbox flags> [--model M]
         (--session-id <uuid> | --resume <id> | --continue)
with the PROMPT ON STDIN. `-p` reads stdin, which keeps the prompt off the argv:
no Windows command-line limit, no dash-leading prompt parsed as a flag, nothing for
cmd.exe to re-parse. stream-json is used for every run, watched or not, so there is
one parser; its last line is a `result` event carrying the answer, session_id,
is_error and permission_denials.

  - --disable-slash-commands: in -p a prompt that starts with "/name" runs that
    skill or command instead of reaching the model, the same hazard agy 1.1.9 had.
  - --strict-mcp-config with no --mcp-config: the child loads NO MCP servers. Without
    it, it would load the user's, which can include agent-intern itself, and a
    sub-agent able to call claude_ask again is a recursion.
  - --session-id: a fresh ask names its own session up front (a uuid4), so the pin
    is known before the run and parallel swarm workers cannot race for it.
    claude_continue resumes it with --resume, which finds a session by id from any
    directory; after a server restart it falls back to --continue (the newest
    session in the workspace).
  - NOT --bare: it skips hooks, plugins and CLAUDE.md, but it also ignores OAuth
    logins and takes only ANTHROPIC_API_KEY, which would lock out every user on a
    Claude subscription.

SANDBOX (all agent-enforced; none is an OS boundary for file edits on any OS):
  read-only        --restricted (no Bash/PowerShell/code tools, no WebFetch, user
                   and project settings ignored so a permissions.allow rule cannot
                   loosen it, file tools confined to the working directory) +
                   --permission-mode dontAsk + --permission-prompts none (anything
                   that would prompt is denied) + Edit/Write/NotebookEdit disallowed.
  workspace-write  --restricted + acceptEdits: file edits auto-approved and confined
                   to the workspace, and NO shell (so it cannot run tests or builds).
  danger-full-access  --dangerously-skip-permissions, no restriction; the user's
                   own settings apply.
Claude Code's own sandbox covers Bash/PowerShell only, never the file tools, and
does not run on native Windows, so it is not used here.

NESTING. Claude Code exports its session to child processes (CLAUDECODE, the
session id, a cross-session MESSAGING socket and token, ...). A child `claude`
started from a server Claude Code launched would inherit them and could act on
the host session's channel, so _env scrubs them. The shared AGENT_INTERN_DEPTH
guard (proc_tree) stops runaway nesting across every backend.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Optional

import proc_tree

# The claude executable. The official installer drops a NATIVE claude.exe in
# ~/.local/bin (verified on 2.1.319); the npm package installs a claude.cmd shim,
# which is resolved to its target the way the other bridges do. CLAUDE_BIN
# overrides. Read once at import; the launching process's env wins.
CLAUDE_BIN_ENV = os.environ.get("CLAUDE_BIN", "claude")


def _resolve_bin() -> str:
    """Full path to the claude executable (see CLAUDE_BIN_ENV)."""
    if os.path.sep in CLAUDE_BIN_ENV or os.path.isfile(CLAUDE_BIN_ENV):
        found = CLAUDE_BIN_ENV
    else:
        found = shutil.which(CLAUDE_BIN_ENV) or CLAUDE_BIN_ENV
    return proc_tree.npm_shim_target(found) or found


CLAUDE_BIN = _resolve_bin()

# Claude Code's config home (projects/, sessions). CLAUDE_CONFIG_DIR relocates it.
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_HOME / "projects"

SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_SANDBOX = "read-only"

# The oldest claude this bridge runs: --permission-prompts arrived in 2.1.259 (an
# older claude rejects it as an unknown option, so a run there would fail anyway,
# but with a less useful message). Verified live on VERIFIED_VERSION.
MIN_VERSION = (2, 1, 259)
VERIFIED_VERSION = (2, 1, 319)
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

# What Claude Code exports to its child processes that a nested `claude` must not
# inherit: the host session's identity and its cross-session messaging channel.
# Auth and provider settings (ANTHROPIC_*, CLAUDE_CONFIG_DIR, CLAUDE_CODE_USE_*)
# are kept. Seen in a Claude Code 2.1.319 session's environment.
_SCRUBBED_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_PID",
    "AI_AGENT",
)

# What a failed run says when the plan's limit is the cause.
_USAGE_LIMIT_MARKERS = ("usage limit", "limit reached", "rate limit", "rate_limit", "quota")

_TEXT = {"encoding": "utf-8", "errors": "replace"}

# workspace -> session id, pinned after each fresh ask so claude_continue resumes
# the exact session. Lives only for the process; --continue is the fallback.
_PINNED: dict[str, str] = {}
_PIN_LOCK = threading.Lock()

# `claude --version` per binary (path, mtime, size), like opencode_bridge's.
_VERSION_CACHE: Optional[tuple[tuple, str]] = None
_VERSION_LOCK = threading.Lock()


def _spawn_kwargs() -> dict:
    """No console window on Windows; a new session elsewhere."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {"start_new_session": True}


def _env() -> dict:
    """The child's env: the host session's variables scrubbed, background work off.

    CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 keeps `-p` from waiting (up to 10 minutes
    by default) for background commands or subagents after the turn has ended."""
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
    env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
    return env


def normalize_workspace(ws: Optional[str]) -> str:
    """Absolute path for `ws`, or the server's cwd when omitted."""
    return os.path.abspath(ws) if ws else os.getcwd()


def validate_sandbox(mode: str) -> str:
    """Return `mode` if valid, else raise ValueError listing the allowed values."""
    if mode not in SANDBOX_MODES:
        raise ValueError(f"invalid sandbox {mode!r}; expected one of: {', '.join(SANDBOX_MODES)}")
    return mode


def validate_model(model: Optional[str]) -> Optional[str]:
    """`model` unchanged. claude has no free command that lists its models, and it
    takes aliases (haiku, sonnet, opus, fable) as well as full ids, so an unknown
    one is left for claude itself to reject."""
    return model or None


def _sandbox_flags(sandbox: str) -> list[str]:
    """claude permission flags for a `sandbox` value (see the module SANDBOX note).

    The `=` form keeps a variadic option from swallowing the next argument."""
    if sandbox == "read-only":
        return [
            "--restricted",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--disallowedTools=Edit,Write,NotebookEdit",
        ]
    if sandbox == "workspace-write":
        return [
            "--restricted",
            "--permission-mode",
            "acceptEdits",
            "--permission-prompts",
            "none",
        ]
    if sandbox == "danger-full-access":
        return ["--dangerously-skip-permissions"]
    raise ValueError(f"invalid sandbox {sandbox!r}")


# ----------------------------------------------------------------- session pinning
def get_pinned(workspace: str) -> Optional[str]:
    """The session id pinned to `workspace` this run, or None."""
    with _PIN_LOCK:
        return _PINNED.get(workspace)


def _pin(workspace: str, session_id: str) -> None:
    with _PIN_LOCK:
        _PINNED[workspace] = session_id


def read_history(workspace: str, continue_conv: bool) -> list[dict]:
    """Prior turns for the watch view. Not read in v1: the window starts empty."""
    return []


# ----------------------------------------------------------------- argv
def build_args(
    sandbox: str,
    model: Optional[str] = None,
    session_id: Optional[str] = None,
    resume_id: Optional[str] = None,
    continue_conv: bool = False,
) -> list[str]:
    """argv for a headless `claude -p`. The prompt is NOT here: it goes on stdin.

    Exactly one of `session_id` (a fresh run's own id), `resume_id` (continue a
    pinned session) or `continue_conv` (the newest session in the cwd) is used."""
    validate_sandbox(sandbox)
    args = [
        CLAUDE_BIN,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",  # stream-json requires it in -p
        "--disable-slash-commands",
        "--strict-mcp-config",
        *_sandbox_flags(sandbox),
    ]
    if model:
        args += ["--model", model]
    if resume_id:
        args += ["--resume", resume_id]
    elif continue_conv:
        args.append("--continue")
    elif session_id:
        args += ["--session-id", session_id]
    return args


# ----------------------------------------------------------------- events
def _consume_event(ev: dict, state: dict) -> None:
    """Accumulate the answer and session id from one stream-json event."""
    sid = ev.get("session_id")
    if isinstance(sid, str) and sid:
        state["session_id"] = sid
    etype = ev.get("type")
    if etype == "assistant":
        content = (ev.get("message") or {}).get("content") or []
        txt = "".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
        if txt:
            state["assistant"] = txt
    elif etype == "result":
        state["result"] = ev


def _looks_like_usage_limit(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _USAGE_LIMIT_MARKERS)


def _finish(state: dict, stderr: str, returncode: Optional[int], sandbox: str) -> str:
    """The answer from an accumulated `state`, or raise with a reason."""
    result = state.get("result") or {}
    text = result.get("result") if isinstance(result.get("result"), str) else ""
    if result.get("is_error") or returncode not in (0, None):
        msg = (text or "").strip() or (stderr or "").strip()[-1000:] or f"exit {returncode}"
        note = ""
        if _looks_like_usage_limit(f"{msg}\n{stderr}"):
            note = (
                "\nThis reads like your Claude plan's usage limit: it resets on Claude's "
                "schedule, so retry later or delegate this task to another backend."
            )
        # An unknown --model comes back is_error with subtype "success" (2.1.319), so
        # only a subtype that is not "success" is worth naming.
        subtype = result.get("subtype")
        kind = subtype if subtype and subtype != "success" else "error"
        raise RuntimeError(f"claude failed ({kind}): {msg}{note}")
    answer = (text or state.get("assistant") or "").strip()
    if answer:
        return answer
    denials = result.get("permission_denials") or []
    if denials:
        tools = sorted(
            {str(d.get("tool_name") or "?") for d in denials if isinstance(d, dict)} or {"?"}
        )
        raise RuntimeError(
            f"claude produced no answer: {len(denials)} tool call(s) ({', '.join(tools)}) were "
            f"denied by the sandbox={sandbox!r} policy, and the model stopped without "
            "replying. Ask for something those tools are not needed for, or pick a sandbox "
            "that allows them."
        )
    raise RuntimeError(
        f"claude produced no result in its stream-json output. stderr: {(stderr or '')[-300:]}"
    )


# ----------------------------------------------------------------- runs
def _write_prompt(stdin, prompt: str) -> None:
    """Write `prompt` to the child's stdin byte-for-byte, then close it.

    The pipe is text mode (for stdout's sake), and a text-mode writer on Windows
    turns every "\\n" into "\\r\\n", which would alter code blocks, diffs and heredocs
    in what the model sees. So the UTF-8 bytes go to the underlying binary buffer.
    A stdin with no .buffer (a test stand-in) gets the text as-is."""
    raw = getattr(stdin, "buffer", None)
    if raw is not None:
        raw.write(prompt.encode("utf-8"))
        raw.flush()
    else:
        stdin.write(prompt)
    stdin.close()


def run_claude(
    prompt: str,
    workspace: str,
    sandbox: str = DEFAULT_SANDBOX,
    model: Optional[str] = None,
    continue_conv: bool = False,
    timeout_s: int = 180,
    pin: bool = True,
) -> str:
    """Run `claude -p` (fresh or continue) and return the final answer.

    `pin=False` for swarm workers (one-shot, no continue). Signature mirrors
    run_cursor so server._run_with_progress can call it unchanged."""
    return _run_impl(prompt, workspace, sandbox, model, continue_conv, timeout_s, None, pin)


def run_claude_streaming(
    prompt: str,
    workspace: str,
    sandbox: str = DEFAULT_SANDBOX,
    model: Optional[str] = None,
    continue_conv: bool = False,
    timeout_s: int = 180,
    on_event=None,
    pin: bool = True,
) -> str:
    """Like run_claude, handing each stream-json event to `on_event` as it arrives."""
    return _run_impl(prompt, workspace, sandbox, model, continue_conv, timeout_s, on_event, pin)


def _run_impl(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    continue_conv: bool,
    timeout_s: int,
    on_event,
    pin: bool,
) -> str:
    """The one runner behind run_claude and run_claude_streaming."""
    validate_sandbox(sandbox)
    check_supported_version()
    os.makedirs(workspace, exist_ok=True)
    resume_id = get_pinned(workspace) if continue_conv else None
    session_id = None if continue_conv else str(uuid.uuid4())
    args = build_args(
        sandbox,
        model,
        session_id=session_id,
        resume_id=resume_id,
        continue_conv=continue_conv and resume_id is None,
    )

    state: dict = {}
    proc = proc_tree.popen(
        args,
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
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
    # The prompt goes in only once both pipes are being drained, so a chatty child
    # can never block on a full stdout while we block on its stdin.
    try:
        _write_prompt(proc.stdin, prompt)
    except (OSError, ValueError):
        pass  # the child died early; its exit code and stderr say why
    timed_out = False
    try:
        proc.wait(timeout=timeout_s + 30)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc_tree.kill_tree(proc)
        try:
            proc.wait(timeout=proc_tree.REAP_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
    ot.join(timeout=1)
    et.join(timeout=1)

    if timed_out:
        raise RuntimeError(f"claude timed out after {timeout_s + 30}s. Raise timeout_s.")
    answer = _finish(state, "".join(err_chunks), proc.returncode, sandbox)
    if not continue_conv and pin:
        _pin(workspace, state.get("session_id") or session_id)
    return answer


# ----------------------------------------------------------------- version gate
def parse_version(text: Optional[str]) -> Optional[tuple[int, int, int]]:
    """(major, minor, patch) from `claude --version` ("2.1.319 (Claude Code)")."""
    m = _VERSION_RE.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _binary_key() -> Optional[tuple]:
    try:
        st = os.stat(CLAUDE_BIN)
    except (OSError, ValueError):
        return None
    return (CLAUDE_BIN, st.st_mtime_ns, st.st_size)


def claude_version() -> Optional[str]:
    """`claude --version` first line, or None if claude can't run. Cached per binary,
    so an upgrade in place is re-read."""
    global _VERSION_CACHE
    key = _binary_key()
    with _VERSION_LOCK:
        if key is not None and _VERSION_CACHE is not None and _VERSION_CACHE[0] == key:
            return _VERSION_CACHE[1]
    try:
        proc = proc_tree.run_captured(
            [CLAUDE_BIN, "--version"],
            starts_agent=False,  # a probe, not an agent run
            stdin=subprocess.DEVNULL,
            timeout=20,
            env=_env(),
            **_TEXT,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    first = text.splitlines()[0] if text else None
    if first is not None and key is not None:
        with _VERSION_LOCK:
            _VERSION_CACHE = (key, first)
    return first


def unsupported_version_reason(version: Optional[tuple[int, int, int]]) -> Optional[str]:
    """Why the bridge won't run this claude, or None if it will."""
    if version is None:
        return (
            "could not read claude's version (`claude --version`). Check that Claude Code "
            "is installed, or set CLAUDE_BIN."
        )
    if version < MIN_VERSION:
        have = ".".join(map(str, version))
        need = ".".join(map(str, MIN_VERSION))
        return (
            f"claude {have} is too old for this bridge (needs {need}+ for "
            "--permission-prompts, which the sandbox relies on). Run `claude update`."
        )
    return None


def check_supported_version() -> None:
    """Raise RuntimeError if the bridge won't run the installed claude."""
    reason = unsupported_version_reason(parse_version(claude_version()))
    if reason:
        raise RuntimeError(reason)


# ----------------------------------------------------------------- diagnostics
def auth_status() -> tuple[bool, str]:
    """(ok, detail) for the auth row, via `claude auth status` (JSON). Free.

    The JSON names the account (email, orgId, orgName); the row never shows those,
    only how it is signed in. An ANTHROPIC_API_KEY counts as signed in."""
    try:
        proc = proc_tree.run_captured(
            [CLAUDE_BIN, "auth", "status"],
            starts_agent=False,  # a probe, not an agent run
            stdin=subprocess.DEVNULL,
            timeout=20,
            env=_env(),
            **_TEXT,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return (False, "could not run `claude auth status`")
    try:
        data = json.loads(proc.stdout or "")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        if os.environ.get("ANTHROPIC_API_KEY"):
            return (True, "ANTHROPIC_API_KEY is set")
        return (False, "could not read `claude auth status` (run `claude auth login`)")
    if not data.get("loggedIn"):
        if os.environ.get("ANTHROPIC_API_KEY"):
            return (True, "ANTHROPIC_API_KEY is set (no saved login)")
        return (False, "not logged in (run `claude auth login`, or set ANTHROPIC_API_KEY)")
    parts = [str(data.get("authMethod") or "logged in")]
    if data.get("subscriptionType"):
        parts.append(f"plan: {data['subscriptionType']}")
    if data.get("loginExpiresAt"):
        parts.append(f"login expires {str(data['loginExpiresAt'])[:10]}")
    return (True, ", ".join(parts))


def status_rows() -> list[tuple[str, bool, str]]:
    """Setup diagnostics as (label, ok, detail) rows. Spends no quota."""
    rows: list[tuple[str, bool, str]] = []
    ver = claude_version()
    if ver is None:
        rows.append(("claude CLI", False, f"not found (set CLAUDE_BIN; tried {CLAUDE_BIN_ENV!r})"))
    else:
        reason = unsupported_version_reason(parse_version(ver))
        rows.append(("claude CLI", reason is None, f"{ver} — {reason}" if reason else ver))
    ok, detail = auth_status()
    rows.append(("claude auth", ok, detail))
    rows.append(("projects dir", PROJECTS_DIR.exists(), str(PROJECTS_DIR)))
    with _PIN_LOCK:
        n_pins = len(_PINNED)
    rows.append(("pinned sessions", True, f"{n_pins} workspace(s) pinned this run"))
    return rows
