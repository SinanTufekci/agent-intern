"""GitHub Copilot CLI bridge: run `copilot -p` headless and return its answer.

Third backend alongside the agy bridge (server.py) and the Codex bridge
(codex_bridge.py). The GitHub Copilot CLI is the well-behaved kind, like codex:
`copilot -p "<prompt>" -s` runs a prompt non-interactively and writes the clean
final answer straight to STDOUT (the `-s/--silent` flag drops the usage stats),
then exits. No transcript-scraping — we read the answer from stdout. Verified
against copilot 1.0.68 on Windows.

CONTINUE / RESUME. copilot's `--session-id <uuid>` flag does double duty: on a
fresh run it SETS the id of the new session; later it RESUMES that exact session.
So — unlike codex, where we scrape the new rollout file to learn the id — the
bridge GENERATES the session id itself (uuid4), passes it on the fresh ask, and
pins it to the workspace. A later copilot_continue resumes that exact id. This is
deterministic and race-free. If the in-memory pin is gone (server restarted) we
fall back to the newest on-disk session whose recorded cwd matches the workspace:
copilot stores each session under

    ~/.copilot/session-state/<session-id>/

with a small `workspace.yaml` recording `id:` and `cwd:` — the copilot analogue
of codex's rollout `session_meta.cwd` lookup.

HEADLESS FLAGS. `--allow-all-tools` is required for non-interactive use (without
it copilot would block on per-tool permission prompts). `--no-ask-user` disables
the ask_user tool so the agent never stalls waiting for a clarifying answer.
`--no-auto-update` keeps a call from triggering a background CLI update.

SECURITY. copilot's boundary is TOOL + PATH based, not codex's enforced OS
sandbox. The `sandbox` argument maps to copilot flags for a uniform cross-backend
knob, but note the difference in strength:
  - read-only        best-effort: all tools auto-approved (required headless) but
                     the local mutating tools (`write`, `shell`) denied — deny
                     takes precedence. NOT an OS sandbox; network/MCP tools can
                     still act. For a HARD read-only boundary, use codex.
  - workspace-write  writes allowed, but file access stays confined to the
                     workspace (no --allow-all-paths) and URLs aren't blanket-open.
  - danger-full-access  --allow-all (tools + all paths + all URLs). Avoid.
Even so, only run it with trusted prompts on trusted content.

AUTH. copilot uses a token from the OS credential store (after `copilot login`)
or, for headless/CI, one of COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN. The
bridge never touches the token; it only reads session-state under ~/.copilot/.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Optional

# The copilot executable. Defaults to "copilot" (resolved via PATH); set
# COPILOT_BIN to an explicit path when copilot isn't reliably on PATH — e.g. on
# Windows the winget install drops it under
#   %LOCALAPPDATA%\Microsoft\WinGet\Packages\GitHub.Copilot_*\copilot.exe
# and a terminal opened before install won't have it on PATH. Mirrors AGY_BIN /
# CODEX_BIN. Read once at import; the launching process's env wins.
COPILOT_BIN = os.environ.get("COPILOT_BIN", "copilot")

# copilot's state home. It stores per-session state under
# ~/.copilot/session-state/<id>/ (each dir has workspace.yaml + events.jsonl).
# copilot has no documented CODEX_HOME-style override, so this is fixed to the
# home dir; we only READ these files (for the restart-proof continue fallback and
# for status), never write them.
COPILOT_HOME = Path.home() / ".copilot"
SESSION_STATE_DIR = COPILOT_HOME / "session-state"

# The `sandbox` knob mirrors codex's for a uniform agent_swarm field, but maps to
# copilot's tool/path permission flags (see the module SECURITY note). Default
# read-only for safety parity with codex — callers opt into write access.
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_SANDBOX = "read-only"

# Env vars copilot checks for a headless auth token, in precedence order. Used
# only to report a helpful auth hint in status_rows (never read for its value).
_TOKEN_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

# copilot ships a builtin GitHub-API MCP server (github-mcp-server). When its HTTP
# endpoint is slow/unreachable it can stall a call up to ~60 s before timing out
# (observed here: 9–78 s of jitter). The bridge is a fast local code/repo
# sub-agent, so by default it passes --disable-builtin-mcps for predictable ~8 s
# latency — this drops ONLY the GitHub-API MCP; all local coding tools (view,
# edit, shell, …) stay. Set COPILOT_GITHUB_MCP=1 to keep the builtin MCP enabled
# (e.g. when you want Copilot's issue/PR/repo tools) and accept the latency risk.
_BUILTIN_GITHUB_MCP = os.environ.get("COPILOT_GITHUB_MCP", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# A session id is a UUID; it names the session-state dir and appears as `id:` in
# that dir's workspace.yaml.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# workspace -> session id, pinned after each fresh ask so copilot_continue resumes
# the exact session rooted at that workspace. Guarded by a lock (MCP tools may run
# on different threads). Lives only for the process; the on-disk workspace.yaml
# cwd lookup (_resume_target_for) is the restart-proof fallback.
_PINNED: dict[str, str] = {}
_PIN_LOCK = threading.Lock()


def _spawn_kwargs() -> dict:
    """Keep copilot from popping a console window on Windows; new session elsewhere.

    Cosmetic + hygiene: copilot writes its answer to stdout regardless of the
    controlling terminal (no agy-style TTY bug). Windows uses CREATE_NO_WINDOW so
    a child console doesn't flash; POSIX starts a new session.
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {"start_new_session": True}


def normalize_workspace(ws: Optional[str]) -> str:
    """Absolute path for `ws`, or the server's cwd when omitted."""
    return os.path.abspath(ws) if ws else os.getcwd()


def validate_sandbox(mode: str) -> str:
    """Return `mode` if valid, else raise ValueError listing the allowed values."""
    if mode not in SANDBOX_MODES:
        raise ValueError(f"invalid sandbox {mode!r}; expected one of: {', '.join(SANDBOX_MODES)}")
    return mode


def _sandbox_flags(sandbox: str) -> list[str]:
    """copilot permission flags for a `sandbox` value (see module SECURITY note).

    All three keep the CLI non-interactive (`--allow-all-tools` is required
    headless); they differ in what they then take away or add back.
    """
    if sandbox == "read-only":
        # Auto-approve everything so it runs headless, then deny the local
        # mutating tools. copilot resolves --deny-tool with precedence over
        # --allow-*. Best-effort (not an OS sandbox); documented as such.
        return ["--allow-all-tools", "--deny-tool=write", "--deny-tool=shell"]
    if sandbox == "workspace-write":
        # Writes allowed; file access still defaults to the workspace only (we do
        # NOT pass --allow-all-paths) and URLs aren't blanket-allowed.
        return ["--allow-all-tools"]
    if sandbox == "danger-full-access":
        # == --allow-all-tools --allow-all-paths --allow-all-urls.
        return ["--allow-all"]
    raise ValueError(f"invalid sandbox {sandbox!r}")


# ----------------------------------------------------------------- session pinning
def get_pinned(workspace: str) -> Optional[str]:
    """The session id pinned to `workspace` this run, or None."""
    with _PIN_LOCK:
        return _PINNED.get(workspace)


def _pin(workspace: str, session_id: str) -> None:
    with _PIN_LOCK:
        _PINNED[workspace] = session_id


def _iter_session_dirs() -> list[Path]:
    """All per-session state dirs under ~/.copilot/session-state/, or []."""
    if not SESSION_STATE_DIR.exists():
        return []
    return [c for c in SESSION_STATE_DIR.iterdir() if c.is_dir()]


def _parse_workspace_yaml(path: Path) -> dict:
    """Parse a session's workspace.yaml into a flat dict, or {} on any error.

    The file is simple `key: value` lines (id, cwd, client_name, name,
    updated_at, ...). Keys contain no colon, so splitting on the first ':' is
    safe even though values do (Windows paths, ISO timestamps). Avoids a YAML
    dependency for these few flat scalars.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict = {}
    for line in text.splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, val = line.split(":", 1)
        out[key.strip()] = val.strip()
    return out


def _resume_target_for(workspace: str) -> Optional[str]:
    """Newest on-disk session id whose recorded cwd matches `workspace`, or None.

    Restart-proof fallback for copilot_continue when the in-memory pin is gone:
    scans session-state dirs newest-first by mtime and returns the id of the first
    whose workspace.yaml `cwd` equals the workspace. Cheap: reads one small yaml
    per dir and stops at the first match.
    """
    target = os.path.normcase(os.path.abspath(workspace))
    dated: list[tuple[float, Path]] = []
    for d in _iter_session_dirs():
        try:
            dated.append((d.stat().st_mtime, d))
        except OSError:
            continue
    for _, d in sorted(dated, key=lambda t: t[0], reverse=True):
        meta = _parse_workspace_yaml(d / "workspace.yaml")
        cwd = meta.get("cwd")
        if isinstance(cwd, str) and os.path.normcase(os.path.abspath(cwd)) == target:
            sid = meta.get("id") or (d.name if _UUID_RE.fullmatch(d.name) else None)
            if sid:
                return sid
    return None


def _resolve_session(workspace: str, continue_conv: bool) -> str:
    """The session id to use: a fresh uuid for a new ask, or the id to resume.

    Fresh: generate a uuid4 and let copilot create the session with it (via
    --session-id). Continue: prefer the in-memory pin, then the newest on-disk
    session whose recorded cwd matches. Raises if continue is requested but no
    prior session exists.
    """
    if not continue_conv:
        return str(uuid.uuid4())
    sid = get_pinned(workspace) or _resume_target_for(workspace)
    if not sid:
        raise RuntimeError(
            f"No prior copilot session for workspace {workspace}. "
            "Run copilot_ask first (or check ~/.copilot/session-state)."
        )
    return sid


# ----------------------------------------------------------------- conversation history
def read_history(workspace: str, continue_conv: bool) -> list[dict]:
    """Prior turns of the copilot session rooted at `workspace`: [{role, content}, …].

    Oldest first, for the watch view's conversation history. Resolves the session
    the same way a resume would (in-memory pin, then newest matching on-disk
    session), then reads its events.jsonl pulling the clean user prompts
    (user.message.data.content) and assistant answers (assistant.message.data.content).
    Returns [] for a fresh ask, an unresolved session, a session without an
    events.jsonl (some copilot builds don't write one), or any read error.
    """
    if not continue_conv:
        return []
    sid = get_pinned(workspace) or _resume_target_for(workspace)
    if not sid:
        return []
    events = SESSION_STATE_DIR / sid / "events.jsonl"
    if not events.exists():
        return []
    turns: list[dict] = []
    try:
        text = events.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        etype = e.get("type")
        data = e.get("data") or {}
        content = (data.get("content") or "").strip()
        if not content:
            continue
        if etype == "user.message":
            turns.append({"role": "user", "content": content})
        elif etype == "assistant.message":
            turns.append({"role": "assistant", "content": content})
    return turns


# ----------------------------------------------------------------- running copilot
def build_args(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    session_id: str,
    json_stream: bool = False,
) -> list[str]:
    """argv for a headless `copilot -p` run (fresh or resume).

    `--session-id` both sets the id on a fresh session and resumes an existing
    one, so the same flag serves ask and continue. `-C` roots file access at the
    workspace. `json_stream` swaps `-s` (clean text on stdout) for
    `--output-format json` (JSONL events on stdout, for watch mode); the final
    answer is reconstructed from the stream in that case.
    """
    args = [
        COPILOT_BIN,
        "--session-id",
        session_id,
        "-C",
        workspace,
        "--no-ask-user",
        "--no-color",
        "--no-auto-update",
    ]
    if not _BUILTIN_GITHUB_MCP:
        args.append("--disable-builtin-mcps")  # skip the flaky GitHub-API MCP (see note)
    args += _sandbox_flags(sandbox)
    if model:
        args += ["--model", model]
    if json_stream:
        args += ["--output-format", "json"]
    else:
        args += ["-s"]  # silent: only the agent's final answer on stdout
    args += ["-p", prompt]
    return args


def run_copilot(
    prompt: str,
    workspace: str,
    sandbox: str = DEFAULT_SANDBOX,
    model: Optional[str] = None,
    continue_conv: bool = False,
    timeout_s: int = 180,
    pin: bool = True,
) -> str:
    """Run `copilot -p` (fresh or resume) and return the final answer from stdout.

    On a fresh run the bridge generates the session id and pins it to `workspace`
    so a later copilot_continue resumes the exact session — pass `pin=False` for
    swarm workers (one-shot, no continue). Signature is positional-friendly so it
    can be handed to server.py's _run_with_progress unchanged.
    """
    validate_sandbox(sandbox)
    os.makedirs(workspace, exist_ok=True)  # copilot's cwd (-C) must exist
    session_id = _resolve_session(workspace, continue_conv)
    args = build_args(prompt, workspace, sandbox, model, session_id)

    proc = subprocess.run(
        args,
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout_s + 30,
        **_spawn_kwargs(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"copilot exited {proc.returncode}\n"
            f"stderr: {(proc.stderr or '')[-1000:]}\n"
            f"stdout: {(proc.stdout or '')[-500:]}"
        )

    answer = (proc.stdout or "").strip()
    if not answer:
        raise RuntimeError(
            f"copilot produced no output on stdout. stderr: {(proc.stderr or '')[-300:]}"
        )

    if not continue_conv and pin:
        _pin(workspace, session_id)
    return answer


def _answer_from_event(ev: dict, state: dict) -> None:
    """Accumulate the final answer from a copilot --json event into `state`.

    In json mode there's no clean stdout answer to read, so we reconstruct it from
    the stream: keep the content of the latest completed assistant message (the
    final turn's message is the answer), with streamed deltas as a backup.
    """
    etype = ev.get("type")
    data = ev.get("data") or {}
    if etype == "assistant.message":
        content = (data.get("content") or "").strip()
        if content:
            state["answer"] = content
            state["delta"] = ""
    elif etype == "assistant.message_start":
        state["delta"] = ""
    elif etype == "assistant.message_delta":
        state["delta"] = state.get("delta", "") + (data.get("deltaContent") or "")


def run_copilot_streaming(
    prompt: str,
    workspace: str,
    sandbox: str = DEFAULT_SANDBOX,
    model: Optional[str] = None,
    continue_conv: bool = False,
    timeout_s: int = 180,
    on_event=None,
    pin: bool = True,
) -> str:
    """Run `copilot --output-format json` and stream events live, returning the answer.

    Like run_copilot, but launches copilot with --output-format json so it emits
    one JSON event per line on stdout, calling `on_event(event_dict)` for each as
    it arrives (this is how watch mode renders steps live). The final answer is
    reconstructed from the stream's assistant messages. Completion is driven by the
    process exiting (with a deadline) rather than stdout closing, because the CLI can
    leave a child holding the stdout pipe open after the turn finishes.
    """
    validate_sandbox(sandbox)
    os.makedirs(workspace, exist_ok=True)  # copilot's cwd (-C) must exist
    session_id = _resolve_session(workspace, continue_conv)
    args = build_args(prompt, workspace, sandbox, model, session_id, json_stream=True)

    state: dict = {"answer": "", "delta": ""}
    proc = subprocess.Popen(
        args,
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **_spawn_kwargs(),
    )
    # Drive completion off PROCESS EXIT, not stdout EOF: like codex, the CLI can
    # leave a child holding the stdout pipe open after the turn finishes, so a plain
    # `for line in proc.stdout` could block past completion. Read stdout + stderr on
    # daemon threads and wait on the process itself (with a deadline).
    err_chunks: list[str] = []

    def _pump_stdout() -> None:
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                _answer_from_event(ev, state)
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
        proc.kill()
        proc.wait()
    ot.join(timeout=2)
    et.join(timeout=2)

    stderr = "".join(err_chunks)
    if timed_out:
        raise RuntimeError(f"copilot timed out after {timeout_s + 30}s (watched)")
    if proc.returncode not in (0, None):
        raise RuntimeError(f"copilot exited {proc.returncode}\nstderr: {(stderr or '')[-1000:]}")

    answer = (state.get("answer") or state.get("delta") or "").strip()
    if not answer:
        raise RuntimeError(
            f"copilot produced no assistant message in its json stream. "
            f"stderr: {(stderr or '')[-300:]}"
        )

    if not continue_conv and pin:
        _pin(workspace, session_id)
    return answer


# ----------------------------------------------------------------- diagnostics
def copilot_version() -> Optional[str]:
    """`copilot --version` first line, or None if copilot can't be run."""
    try:
        proc = subprocess.run(
            [COPILOT_BIN, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return text.splitlines()[0] if text else None


def auth_hint() -> tuple[bool, str]:
    """(ok, detail) for the auth row. Spends no quota — and does NOT verify login.

    copilot has no `login status` command, and its token lives in the OS
    credential store (unreadable cross-platform) unless supplied via env. So we
    report the env-token hint when present, otherwise note login is assumed and
    unverified. ok stays True (we can't prove a fault without spending quota); a
    real auth problem surfaces as an error on the first ask.
    """
    for var in _TOKEN_ENV_VARS:
        if os.environ.get(var):
            return (True, f"env token set ({var})")
    return (True, "via `copilot login` credential store; not verified (spends no quota)")


def status_rows() -> list[tuple[str, bool, str]]:
    """Setup diagnostics as (label, ok, detail) rows. Spends no quota.

    Mirrors codex_bridge.status_rows / server._collect_status shape so server.py
    renders copilot rows with the same formatter.
    """
    rows: list[tuple[str, bool, str]] = []

    ver = copilot_version()
    if ver is None:
        rows.append(
            ("copilot CLI", False, f"not found on PATH (set COPILOT_BIN; tried {COPILOT_BIN!r})")
        )
    else:
        rows.append(("copilot CLI", True, ver))

    ok, detail = auth_hint()
    rows.append(("copilot auth", ok, detail))

    rows.append(("session-state dir", SESSION_STATE_DIR.exists(), str(SESSION_STATE_DIR)))

    with _PIN_LOCK:
        n_pins = len(_PINNED)
    rows.append(("pinned sessions", True, f"{n_pins} workspace(s) pinned this run"))

    return rows
