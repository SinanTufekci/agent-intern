"""Muse Code CLI bridge: run `muse exec` headless and return its answer.

Eighth backend. Muse Code is Meta's terminal coding agent (`muse`,
https://dev.meta.ai/docs/muse-code), powered by the Muse Spark models. Its headless
mode, `muse exec --json`, runs one prompt to completion and writes JSONL events to
stdout; the answer is the `text` of the single `run_terminal` event.

⚠️ EXPERIMENTAL — THE REAL MODEL HAS NEVER ANSWERED THROUGH THIS BRIDGE. Muse needs
a paid plan or a Meta Model API key and the bridge author has neither. It is,
though, the best-verified of the experimental backends, because muse ships a
built-in `--provider echo` that runs the WHOLE exec pipeline with no credentials.

VERIFIED LIVE on Muse Code 1.3.0 (1.3.0-R3401.1) / Windows, via the echo provider:
  - the JSONL envelope: one object per line, `payload.kind` discriminates, the
    session id is `stream.id`, and a run ends in exactly one
    `{"kind":"run_terminal","terminal":"completed","text":...,"reason":null}`;
  - `--session-id <uuid>` both names a new session and resumes an existing one
    (the sequence numbers continue); an id muse hasn't seen starts a fresh one;
  - resuming a session from a DIFFERENT workspace exits 1 with no events and a
    stderr message naming both workspaces — so pins are keyed by workspace;
  - `muse export --last` (run in the workspace) returns the most recent session
    there as JSON, `sessions[0].session_id` — the restart-proof continue fallback;
    with no session it exits 1 ("no retained sessions found for this workspace");
  - every argv this bridge builds, in all three sandbox modes, parses and runs;
    an unknown flag or effort exits 2 BEFORE the auth check (unlike grok), and
    `--model` is accepted with the meta provider (it rejects it for echo);
  - the real provider logged out exits 1 with NO events and the stderr line
    "missing meta credentials: run `muse login` or set META_API_KEY, or save
    credentials at ~/.config/muse/auth.json";
  - four concurrent runs complete without lock contention (swarm-safe).
UNVERIFIED: a real Muse Spark answer, the event kinds a real tool call emits (the
echo provider calls no tools, so watch mode renders them generically), and what
the Windows OS sandbox does in workspace-write mode on a machine where it has not
been set up (see SECURITY).

WINDOWS LAUNCH. The installer puts a `muse.cmd` shim in %LOCALAPPDATA%\\Programs\\muse,
which runs a PowerShell 5.1 launcher, which runs `muse-bin-<version>.exe` named by
the `.muse-version` file next to it. The bridge runs that binary DIRECTLY:
  - a `.cmd` hands its arguments to cmd.exe, which re-parses them — the command
    injection proc_tree's batch-shim note describes;
  - Windows PowerShell 5.1 started from a PowerShell 7 environment inherits a
    PSModulePath under which `Get-FileHash` does not resolve, so the launcher's
    install/update path fails there (verified: missing with the inherited path,
    present with a clean one);
  - it is ~400 ms faster per call.
The cost is that calls through the bridge never trigger the launcher's background
self-update; running `muse` yourself does. On macOS/Linux the launcher is a bash
script that forwards its arguments intact, so it is used as-is.

THE PROMPT always travels in a file (`--prompt-file`), never in argv: no argument
the bridge passes ever contains user text, no command-line length limit applies,
and a prompt starting with "-" can't be mistaken for a flag (muse refuses those
positionally).

SECURITY. Approval and an OS sandbox are ON by default in muse; headless runs
can't answer approval prompts, so every mode disables approval and the containment
comes from what is switched off:
  - read-only        --disable-write --disable-shell --disable-web-tools
  - workspace-write  shell runs inside muse's OS sandbox (network proxy-only)
  - danger-full-access  --yolo: no approval, no sandbox. Avoid.
read-only needs no OS sandbox at all — nothing that could write or run is left on
— so it holds on every platform. That is why it switches the shell off rather than
trusting the sandbox: on Windows the sandbox needs a one-time elevated setup
(`muse sandbox windows check` reported `status=setup_required` on a fresh
install), and what an unready sandbox does to a headless shell call is unverified.
MCP servers configured in muse's own settings stay available in every mode.
`--no-foreign-personal-context` is always passed: by default muse imports Claude
Code's personal skills and rules, which would include this very bridge's plugin.
`--trust-workspace` is always passed, so the repo's AGENTS.md/CLAUDE.md load like
they do for the other backends.

AUTH. `muse login` (browser device code), or META_API_KEY (which takes priority).
Credentials are cached in ~/.config/muse/auth.json. The bridge never reads them —
the status view only checks that the file or the env var exists.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Optional

import proc_tree

# The muse executable. Set MUSE_BIN to an explicit path to override (the real
# binary, or on POSIX the launcher). Mirrors AGY_BIN / GROK_BIN. Read once at import.
MUSE_BIN_ENV = os.environ.get("MUSE_BIN", "muse")

# Testing knob: `echo` runs muse's full exec pipeline offline, with no account.
# Everything the module docstring calls "verified live" was verified this way.
MUSE_PROVIDER = os.environ.get("MUSE_PROVIDER", "").strip() or None

# Muse's data home (sessions, model catalog). Muse follows the XDG layout on every
# platform — verified on Windows, where it is ~/.local/share/muse, not AppData.
MUSE_DATA_HOME = (
    Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "muse"
)

# Where `muse login` caches credentials (the launcher's own resolution order).
MUSE_AUTH_PATH = Path(
    os.environ.get("MUSE_AUTH_PATH")
    or Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "muse" / "auth.json"
)

# Windows installer target; the launcher records the active binary's version here.
_WIN_INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / (
    "Programs/muse"
)
# The launcher's own validation pattern for `.muse-version` (e.g. 1.3.0-R3401.1).
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+-R[0-9]+(\.[0-9]+)?$")


def _windows_binary(install_dir: Path) -> Optional[str]:
    """`muse-bin-<version>.exe` that the launcher in `install_dir` would run, or None."""
    try:
        version = (install_dir / ".muse-version").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not _VERSION_RE.match(version):
        return None
    binary = install_dir / f"muse-bin-{version}.exe"
    return str(binary) if binary.is_file() else None


def _resolve_bin(windows: Optional[bool] = None) -> str:
    """Full path to the program to run for `muse` (see the WINDOWS LAUNCH note).

    `windows` defaults to the real platform; tests pass it explicitly.
    """
    if windows is None:
        windows = os.name == "nt"
    explicit = os.path.sep in MUSE_BIN_ENV or os.path.isfile(MUSE_BIN_ENV)
    found = MUSE_BIN_ENV if explicit else shutil.which(MUSE_BIN_ENV)
    if windows:
        # The shim on PATH, or the installer's default dir when the server started
        # before the installer's PATH edit (which only reaches new processes).
        dirs = [Path(found).parent] if found and proc_tree.is_batch_file(found) else []
        if not found:
            dirs.append(_WIN_INSTALL_DIR)
        for d in dirs:
            binary = _windows_binary(d)
            if binary:
                return binary
    if found:
        return found
    # POSIX installer default; its PATH edit has the same reach problem.
    candidate = Path.home() / ".local" / "bin" / MUSE_BIN_ENV
    return str(candidate) if candidate.is_file() else MUSE_BIN_ENV


MUSE_BIN = _resolve_bin()

SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_SANDBOX = "read-only"

# Text decoding for muse's output: UTF-8, never raising on a stray byte.
_TEXT = {"encoding": "utf-8", "errors": "replace"}

# Informational stderr lines muse prints on every run; not worth surfacing in errors.
_NOISE_PREFIXES = (
    "muse: workspace root:",
    "muse: workspace trust:",
    "muse: Agent delegation:",
)

# workspace -> session id, pinned after each fresh ask so muse_continue resumes the
# exact session. In-memory only; `muse export --last` is the restart-proof fallback.
_PINNED: dict[str, str] = {}
_PIN_LOCK = threading.Lock()


def _spawn_kwargs() -> dict:
    """No console window on Windows; a new session (process group) elsewhere."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {"start_new_session": True}


def _env() -> dict:
    """Process env for muse, minus a PSModulePath inherited from PowerShell 7.

    Only matters when muse runs through its Windows launcher (MUSE_BIN pointed at
    the shim): Windows PowerShell 5.1 with PowerShell 7's module path cannot load
    `Get-FileHash`, and the launcher's self-update then fails. Dropping the variable
    lets 5.1 rebuild its own default. Harmless everywhere else.
    """
    env = dict(os.environ)
    if os.name == "nt":
        env.pop("PSModulePath", None)
    return env


def normalize_workspace(ws: Optional[str]) -> str:
    """Absolute path for `ws`, or the server's cwd when omitted."""
    return os.path.abspath(ws) if ws else os.getcwd()


def validate_sandbox(mode: str) -> str:
    """Return `mode` if valid, else raise ValueError listing the allowed values."""
    if mode not in SANDBOX_MODES:
        raise ValueError(f"invalid sandbox {mode!r}; expected one of: {', '.join(SANDBOX_MODES)}")
    return mode


def _sandbox_flags(sandbox: str) -> list[str]:
    """muse safety flags for a `sandbox` value (see the module SECURITY note)."""
    if sandbox == "read-only":
        return ["--disable-approval", "--disable-write", "--disable-shell", "--disable-web-tools"]
    if sandbox == "workspace-write":
        return ["--disable-approval"]
    if sandbox == "danger-full-access":
        return ["--yolo"]
    raise ValueError(f"invalid sandbox {sandbox!r}")


# ----------------------------------------------------------------- session pinning
def get_pinned(workspace: str) -> Optional[str]:
    """The session id pinned to `workspace` this run, or None."""
    with _PIN_LOCK:
        return _PINNED.get(workspace)


def _pin(workspace: str, session_id: str) -> None:
    with _PIN_LOCK:
        _PINNED[workspace] = session_id


def last_session(workspace: str) -> Optional[str]:
    """The most recent muse session in `workspace`, via `muse export --last`.

    Muse's own on-disk store is a binary journal plus derived views whose layout
    already changed once (a third-party integration's `workspaceRoot` regex matches
    nothing on 1.3.0), so the documented export command is used instead of reading
    it. Returns None when muse has no session there (it exits 1) or can't be run.
    """
    fd, out = tempfile.mkstemp(prefix="muse-export-", suffix=".json")
    os.close(fd)
    try:
        proc = proc_tree.run_captured(
            [MUSE_BIN, "export", "--last", "--out", out],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            timeout=60,
            env=_env(),
            **_TEXT,
            **_spawn_kwargs(),
        )
        if proc.returncode != 0:
            return None
        with open(out, encoding="utf-8", errors="replace") as f:
            doc = json.load(f)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    sessions = doc.get("sessions") if isinstance(doc, dict) else None
    if isinstance(sessions, list) and sessions and isinstance(sessions[0], dict):
        sid = sessions[0].get("session_id")
        if isinstance(sid, str) and sid:
            return sid
    return None


def _session_for(workspace: str, continue_conv: bool, pin: bool) -> Optional[str]:
    """The `--session-id` to pass: fresh, pinned, or recovered — or None.

    Fresh ask: a new uuid we mint (so we know it without parsing events), or None for
    swarm workers (pin=False) so muse mints one. Continue: the pin, else the
    workspace's most recent session per `muse export --last`, else an error — a
    silent fresh session would answer the follow-up with none of the context the
    caller asked to continue.
    """
    if not continue_conv:
        return str(uuid.uuid4()) if pin else None
    sid = get_pinned(workspace) or last_session(workspace)
    if not sid:
        raise RuntimeError(
            "no Muse session to continue in this workspace — start one with muse_ask "
            f"(workspace: {workspace})"
        )
    return sid


# ----------------------------------------------------------------- models
def list_models() -> list[str]:
    """Model ids from muse's cached catalog, or [] if there is none yet.

    Muse has no `models` command; it caches catalog documents as
    `<data>/model-catalog/*.json` with `rows[].model_id` (the shape a third-party
    integration relies on). None existed on a machine that never logged in, so the
    list is informational and never used to reject a model.
    """
    ids: list[str] = []
    for path in sorted((MUSE_DATA_HOME / "model-catalog").glob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                rows = json.load(f).get("rows") or []
        except (OSError, ValueError, AttributeError):
            continue
        for row in rows:
            mid = row.get("model_id") if isinstance(row, dict) else None
            if isinstance(mid, str) and mid not in ids:
                ids.append(mid)
    return ids


def validate_model(model: Optional[str]) -> Optional[str]:
    """Return a cleaned `model`, or None. Lenient: muse accepts any model id.

    Verified behaviour (changelog 1.1.1, and live: the flag parses): unknown ids run
    with assumed metadata rather than failing, and there is no list to check against.
    """
    if model is None or not str(model).strip():
        return None
    return str(model).strip()


# ----------------------------------------------------------------- running muse
def build_args(
    prompt_file: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    session_id: Optional[str],
) -> list[str]:
    """argv for one headless `muse exec --json` run. No element carries user text."""
    args = [
        MUSE_BIN,
        "exec",
        "--json",
        "--workspace",
        workspace,
        "--worktree",
        "off",
        "--user-input-auto-resolve",  # never block on a question nobody can answer
        "--no-foreign-personal-context",  # don't import Claude Code's own skills/rules
        "--trust-workspace",  # load the repo's AGENTS.md/CLAUDE.md, like the others
    ]
    if MUSE_PROVIDER:
        args += ["--provider", MUSE_PROVIDER]
    if session_id:
        args += ["--session-id", session_id]
    if model and MUSE_PROVIDER in (None, "meta"):  # echo rejects --model
        args += ["--model", model]
    args += _sandbox_flags(sandbox)
    args += ["--prompt-file", prompt_file]
    return args


def _write_prompt(prompt: str) -> str:
    fd, path = tempfile.mkstemp(prefix="muse-prompt-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(prompt)
    return path


def _answer_from_event(ev: dict, state: dict) -> None:
    """Fold one JSONL event into `state`: the answer deltas and the terminal record."""
    payload = ev.get("payload")
    if not isinstance(payload, dict):
        return
    kind = payload.get("kind")
    if kind == "run_output_delta":
        text = payload.get("text")
        if isinstance(text, str):
            state["deltas"] = state.get("deltas", "") + text
    elif kind == "run_terminal":
        state["terminal"] = payload


def _events(stdout: str) -> list[dict]:
    out = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _stderr_tail(stderr: str, limit: int = 600) -> str:
    lines = [
        ln
        for ln in (stderr or "").splitlines()
        if ln.strip() and not ln.strip().startswith(_NOISE_PREFIXES)
    ]
    return "\n".join(lines)[-limit:]


def _finish(state: dict, returncode: Optional[int], stderr: str) -> str:
    """The answer from folded events, or a RuntimeError saying why there isn't one."""
    term = state.get("terminal")
    if term is None:
        detail = _stderr_tail(stderr) or "nothing on stderr"
        if returncode not in (0, None):
            raise RuntimeError(f"muse exited {returncode} without a result: {detail}")
        raise RuntimeError(f"muse produced no run_terminal event: {detail}")
    outcome = term.get("terminal")
    if outcome != "completed":
        reason = term.get("reason") or _stderr_tail(stderr) or "no reason given"
        raise RuntimeError(f"muse run {outcome}: {reason}")
    answer = (term.get("text") or state.get("deltas") or "").strip()
    if not answer:
        raise RuntimeError("muse completed with an empty answer")
    return answer


def run_muse(
    prompt: str,
    workspace: str,
    sandbox: str = DEFAULT_SANDBOX,
    model: Optional[str] = None,
    continue_conv: bool = False,
    timeout_s: int = 180,
    pin: bool = True,
) -> str:
    """Run `muse exec` (fresh or continued) and return the final answer.

    Signature mirrors run_grok so server.py's _run_with_progress and the swarm can
    call it unchanged. `pin=False` for swarm workers (one-shot, never continued).
    """
    validate_sandbox(sandbox)
    os.makedirs(workspace, exist_ok=True)
    session_id = _session_for(workspace, continue_conv, pin)
    prompt_file = _write_prompt(prompt)
    try:
        proc = proc_tree.run_captured(
            build_args(prompt_file, workspace, sandbox, model, session_id),
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            timeout=timeout_s + 30,
            env=_env(),
            **_TEXT,
            **_spawn_kwargs(),
        )
    finally:
        _remove(prompt_file)
    state: dict = {}
    for ev in _events(proc.stdout):
        _answer_from_event(ev, state)
    answer = _finish(state, proc.returncode, proc.stderr or "")
    if pin and session_id:
        _pin(workspace, session_id)
    return answer


def run_muse_streaming(
    prompt: str,
    workspace: str,
    sandbox: str = DEFAULT_SANDBOX,
    model: Optional[str] = None,
    continue_conv: bool = False,
    timeout_s: int = 180,
    on_event=None,
    pin: bool = True,
) -> str:
    """Like run_muse, but call `on_event(event_dict)` for each JSONL event as it arrives."""
    validate_sandbox(sandbox)
    os.makedirs(workspace, exist_ok=True)
    session_id = _session_for(workspace, continue_conv, pin)
    prompt_file = _write_prompt(prompt)
    state: dict = {}
    err_chunks: list[str] = []
    try:
        proc = proc_tree.popen(
            build_args(prompt_file, workspace, sandbox, model, session_id),
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_env(),
            **_TEXT,
            **_spawn_kwargs(),
        )

        def _pump_stdout() -> None:
            try:
                for line in proc.stdout:
                    for ev in _events(line):
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
            proc_tree.kill_tree(proc)
            try:
                proc.wait(timeout=proc_tree.REAP_GRACE_S)
            except subprocess.TimeoutExpired:
                pass
        ot.join(timeout=1)
        et.join(timeout=1)
    finally:
        _remove(prompt_file)
    if timed_out:
        raise RuntimeError(f"muse timed out after {timeout_s + 30}s (watched)")
    answer = _finish(state, proc.returncode, "".join(err_chunks))
    if pin and session_id:
        _pin(workspace, session_id)
    return answer


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def read_history(workspace: str, continue_conv: bool) -> list[dict]:
    """Prior turns for the watch view — always [] (muse's session store is binary)."""
    return []


# ----------------------------------------------------------------- watch rendering
def watch_mapper():
    """A stateful event -> [(kind, text)] mapper for one watched run.

    Muse reports work as tasks: `task_lifecycle` events with an inner `event.kind`
    of proposed/started/completed/failed, where only `proposed` names the task
    (`task_kind`). The mapper remembers which tasks it showed so a later failure is
    reported for those and not for muse's internal bookkeeping (reminders, the model
    call itself). Tool-call task kinds were never observed (the echo provider calls
    no tools), so they are shown by name, unparsed. Unknown events render nothing.
    """
    shown: set[str] = set()

    def lines(ev: dict) -> list[tuple[str, str]]:
        payload = ev.get("payload")
        if not isinstance(payload, dict):
            return []
        kind = payload.get("kind")
        if kind == "run_model_configured":
            mid = payload.get("model_id")
            return [("narration", f"model: {mid}")] if isinstance(mid, str) else []
        if kind == "task_lifecycle":
            inner = payload.get("event")
            if not isinstance(inner, dict):
                return []
            phase = inner.get("kind")
            tid = inner.get("task_id") or payload.get("task_id")
            if phase == "proposed":
                task_kind = inner.get("task_kind")
                if not isinstance(task_kind, str) or task_kind.startswith(("reminder.", "model.")):
                    return []
                shown.add(tid)
                return [("command", task_kind[:200])]
            if tid in shown and phase in ("completed", "failed"):
                if phase == "completed":
                    return [("result", "done")]
                return [("result", f"failed: {inner.get('reason') or ''}"[:200])]
            return []
        if kind == "run_terminal" and payload.get("terminal") != "completed":
            reason = payload.get("reason") or ""
            return [("result", f"{payload.get('terminal')}: {reason}"[:200])]
        return []

    return lines


# ----------------------------------------------------------------- diagnostics
def muse_version() -> Optional[str]:
    """`muse --version` (e.g. "Muse Code 1.3.0 (1.3.0-R3401.1)"), or None."""
    try:
        proc = proc_tree.run_captured(
            [MUSE_BIN, "--version"],
            stdin=subprocess.DEVNULL,
            timeout=20,
            env=_env(),
            **_TEXT,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return text.splitlines()[0] if proc.returncode == 0 and text else None


def auth_status() -> tuple[bool, str]:
    """(ok, detail) for the auth row. Spends no quota — and can't prove validity.

    Muse has no free auth probe: the only call that checks credentials is a real
    run. So this reports what muse would use — META_API_KEY first (muse gives it
    priority), then the credentials file `muse login` writes.
    """
    if os.environ.get("META_API_KEY"):
        return (True, "META_API_KEY is set (takes priority over a login)")
    if MUSE_AUTH_PATH.is_file():
        return (True, f"logged in (credentials at {MUSE_AUTH_PATH})")
    return (False, "not logged in (run `muse login`, or set META_API_KEY)")


def windows_sandbox_status() -> tuple[bool, str]:
    """(ok, detail) from `muse sandbox windows check`. Local only, no quota.

    Never a failure row: an unprepared sandbox doesn't affect read-only (the
    default), which switches the shell off instead of relying on it. It is shown so
    a workspace-write user knows the one-time setup exists.
    """
    try:
        proc = proc_tree.run_captured(
            [MUSE_BIN, "sandbox", "windows", "check"],
            stdin=subprocess.DEVNULL,
            timeout=30,
            env=_env(),
            **_TEXT,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return (True, "unknown (could not run `muse sandbox windows check`)")
    fields = dict(
        ln.split("=", 1) for ln in (proc.stdout or "").splitlines() if "=" in ln and " " not in ln
    )
    status = fields.get("status", "unknown")
    if status == "ready":
        return (True, "ready")
    return (
        True,
        f"{status} — only matters for workspace-write, whose shell calls are unverified "
        "until `muse sandbox windows setup` has run; read-only never needs it",
    )


def status_rows() -> list[tuple[str, bool, str]]:
    """Setup diagnostics as (label, ok, detail) rows. Spends no quota."""
    rows: list[tuple[str, bool, str]] = []
    ver = muse_version()
    if ver is None:
        rows.append(("muse CLI", False, f"not found (set MUSE_BIN; tried {MUSE_BIN_ENV!r})"))
        return rows
    rows.append(("muse CLI", True, f"{ver} — {MUSE_BIN}"))
    ok, detail = auth_status()
    rows.append(("muse auth", ok, detail))
    models = list_models()
    rows.append(
        (
            "models",
            True,
            ", ".join(models[:6]) if models else "no catalog cached yet (muse accepts any id)",
        )
    )
    if os.name == "nt":
        ok, detail = windows_sandbox_status()
        rows.append(("windows sandbox", ok, detail))
    rows.append(("data dir", MUSE_DATA_HOME.exists(), str(MUSE_DATA_HOME)))
    with _PIN_LOCK:
        n_pins = len(_PINNED)
    rows.append(("pinned sessions", True, f"{n_pins} workspace(s) pinned this run"))
    return rows
