"""Cursor CLI bridge: run `cursor-agent -p` headless and return its answer.

Fourth backend alongside the agy bridge (server.py), the Codex bridge
(codex_bridge.py), and the Copilot bridge (copilot_bridge.py). Cursor's agent CLI
(`cursor-agent`, from https://cursor.com/cli) is the well-behaved, stdout-native
kind like codex/copilot: `cursor-agent -p "<prompt>" --output-format text` runs a
prompt non-interactively and writes the clean final answer to STDOUT, then exits.
No transcript-scraping — we read the answer from stdout. Verified against
cursor-agent 2026.07.08 on Windows.

CONTINUE / RESUME. cursor-agent has a `create-chat` command that mints a new chat
and prints its id, plus a `--resume <chatId>` flag that resumes an existing chat.
So — like copilot's self-set `--session-id` — the bridge asks cursor for a fresh
chat id (create-chat), runs the ask with `-p --resume <id>`, and pins that id to
the workspace. A later cursor_continue resumes the exact chat. Deterministic and
race-free. If the in-memory pin is gone (server restarted) we fall back to the
newest on-disk chat whose recorded cwd matches the workspace: cursor stores each
chat under

    ~/.cursor/chats/<md5(workspace_path)>/<chat-id>/

with a `meta.json` recording `cwd:` (and `updatedAtMs`) — the cursor analogue of
copilot's workspace.yaml lookup. The directory name is itself md5(workspace), so
the fast path is O(1); a cwd scan is the cross-version fallback.

HEADLESS FLAGS. `--trust` trusts the workspace without prompting (required for
non-interactive use; otherwise the CLI blocks on a trust prompt). `-p/--print`
selects headless print mode. `--output-format text` returns the clean final
answer on stdout; `stream-json` emits one JSON event per line (for watch mode).

SECURITY. cursor exposes both an execution MODE (agent/ask/plan) and an OS-level
`--sandbox`. The `sandbox` argument maps to cursor flags for a uniform
cross-backend knob; note the strength differences:
  - read-only        `--mode ask`: agent-enforced read-only — the write/shell
                     tools are unavailable, so it analyzes and answers but makes
                     no edits (verified: it refuses to create files). Like
                     copilot's read-only this is agent-enforced, NOT a hard OS
                     boundary; for that, use codex.
  - workspace-write  `--force`: edits and commands allowed; file access stays
                     rooted at `--workspace`.
  - danger-full-access  `--force --sandbox disabled`: everything, OS sandbox off.
                     Avoid.
Even so, only run it with trusted prompts on trusted content.

AUTH. cursor-agent uses your Cursor login (`cursor-agent login`, stored in the OS
credential store) or a CURSOR_API_KEY env var. The bridge never touches the
token; it only reads chat state under ~/.cursor/chats/ (for the continue fallback
and status) and shells out to `cursor-agent status` for the auth hint.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

# The cursor-agent executable. On Windows the installer drops a `cursor-agent.CMD`
# shim (PowerShell -> cursor-agent.ps1); a bare "cursor-agent" name can't be run
# by CreateProcess, so we resolve it via shutil.which (which honors PATHEXT and
# returns the full .CMD path). Set CURSOR_BIN to an explicit path to override —
# e.g. %LOCALAPPDATA%\cursor-agent\cursor-agent.cmd. Mirrors AGY_BIN / CODEX_BIN /
# COPILOT_BIN. Read once at import; the launching process's env wins.
CURSOR_BIN_ENV = os.environ.get("CURSOR_BIN", "cursor-agent")


def _resolve_bin() -> str:
    """Full path to the cursor-agent executable (see CURSOR_BIN_ENV note)."""
    if os.path.sep in CURSOR_BIN_ENV or os.path.isfile(CURSOR_BIN_ENV):
        return CURSOR_BIN_ENV
    return shutil.which(CURSOR_BIN_ENV) or CURSOR_BIN_ENV


CURSOR_BIN = _resolve_bin()

# cursor's state home. Chats live under ~/.cursor/chats/<workspace-hash>/<chat-id>/
# (each chat dir has meta.json + store.db). No documented override, so this is
# fixed to the home dir; we only READ these files (for the restart-proof continue
# fallback and status), never write them.
CURSOR_HOME = Path.home() / ".cursor"
CHATS_DIR = CURSOR_HOME / "chats"

# The `sandbox` knob mirrors codex's/copilot's for a uniform agent_swarm field,
# but maps to cursor's mode/force/sandbox flags (see the module SECURITY note).
# Default read-only for safety parity — callers opt into write access.
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_SANDBOX = "read-only"

# A chat id is a UUID; it names the chat dir and appears in create-chat's output.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Text decoding for cursor's subprocesses: it emits UTF-8 (checkmarks, non-ASCII
# answers), which the Windows locale codec (cp1252) can mangle or choke on. Decode
# UTF-8 explicitly and never raise on a stray byte.
_TEXT = {"encoding": "utf-8", "errors": "replace"}

# Cached model-id list from `cursor-agent models` (populated once on first
# validation; a transient failure is not cached).
_MODELS_CACHE: Optional[list[str]] = None

# workspace -> chat id, pinned after each fresh ask so cursor_continue resumes the
# exact chat rooted at that workspace. Guarded by a lock (MCP tools may run on
# different threads). Lives only for the process; the on-disk meta.json cwd lookup
# (_resume_target_for) is the restart-proof fallback.
_PINNED: dict[str, str] = {}
_PIN_LOCK = threading.Lock()


def _spawn_kwargs() -> dict:
    """Keep cursor from popping a console window on Windows; new session elsewhere.

    Cosmetic + hygiene: cursor writes its answer to stdout regardless of the
    controlling terminal. Windows uses CREATE_NO_WINDOW so the .cmd/PowerShell
    shim doesn't flash a console; POSIX starts a new session.
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
    """cursor mode/permission flags for a `sandbox` value (see module SECURITY note)."""
    if sandbox == "read-only":
        # Ask mode: the write/shell tools are unavailable — analyze & answer only.
        return ["--mode", "ask"]
    if sandbox == "workspace-write":
        # Allow edits + commands headless; file access stays rooted at --workspace.
        return ["--force"]
    if sandbox == "danger-full-access":
        # Everything, OS sandbox off.
        return ["--force", "--sandbox", "disabled"]
    raise ValueError(f"invalid sandbox {sandbox!r}")


# ----------------------------------------------------------------- session pinning
def get_pinned(workspace: str) -> Optional[str]:
    """The chat id pinned to `workspace` this run, or None."""
    with _PIN_LOCK:
        return _PINNED.get(workspace)


def _pin(workspace: str, chat_id: str) -> None:
    with _PIN_LOCK:
        _PINNED[workspace] = chat_id


def create_chat(workspace: str) -> str:
    """Mint a fresh chat via `cursor-agent create-chat` and return its id.

    The chat isn't tied to a workspace on disk until it is first used with
    `-p --resume <id>` from that cwd, so we run create-chat with cwd=workspace for
    good measure and let the subsequent ask create the workspace's chat dir.
    """
    proc = subprocess.run(
        [CURSOR_BIN, "create-chat"],
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        **_TEXT,
        **_spawn_kwargs(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"cursor-agent create-chat failed (rc={proc.returncode}): {(proc.stderr or '')[-300:]}"
        )
    m = _UUID_RE.search(proc.stdout or "")
    if not m:
        raise RuntimeError(
            f"cursor-agent create-chat returned no chat id: {(proc.stdout or '')[:200]!r}"
        )
    return m.group(0)


def _workspace_hash(workspace: str) -> str:
    """md5 of the workspace's absolute (native) path — cursor's chat-dir name."""
    return hashlib.md5(os.path.abspath(workspace).encode("utf-8")).hexdigest()


def _iter_hash_dirs() -> list[Path]:
    """All per-workspace hash dirs under ~/.cursor/chats/, or []."""
    if not CHATS_DIR.exists():
        return []
    return [c for c in CHATS_DIR.iterdir() if c.is_dir()]


def _read_meta(path: Path) -> dict:
    """Parse a chat's meta.json into a dict, or {} on any error."""
    try:
        return json.loads(path.read_text(**_TEXT))
    except (OSError, ValueError):
        return {}


def _newest_chat_matching(hash_dir: Path, target: str, trust_dir: bool) -> Optional[str]:
    """Newest chat id under `hash_dir` whose meta cwd == `target` (normcased).

    `target` is the normcased absolute workspace. In the workspace's own hash dir
    (`trust_dir=True`) a chat with no recorded cwd is trusted, since the dir name
    already encodes the workspace.
    """
    if not hash_dir.is_dir():
        return None
    dated: list[tuple[float, Path]] = []
    for c in hash_dir.iterdir():
        if not c.is_dir() or not _UUID_RE.fullmatch(c.name):
            continue
        try:
            dated.append((c.stat().st_mtime, c))
        except OSError:
            continue
    for _, c in sorted(dated, key=lambda t: t[0], reverse=True):
        cwd = _read_meta(c / "meta.json").get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            if os.path.normcase(os.path.abspath(cwd)) == target:
                return c.name
        elif trust_dir:
            return c.name
    return None


def _resume_target_for(workspace: str) -> Optional[str]:
    """Newest on-disk chat id for `workspace`, or None (restart-proof continue).

    Fast path: the workspace's own md5 hash dir. Fallback: scan every hash dir for
    a meta.json cwd match (covers path-case or cross-version hashing differences).
    """
    target = os.path.normcase(os.path.abspath(workspace))
    hd = CHATS_DIR / _workspace_hash(workspace)
    hit = _newest_chat_matching(hd, target, trust_dir=True)
    if hit:
        return hit
    for d in _iter_hash_dirs():
        if d == hd:
            continue
        hit = _newest_chat_matching(d, target, trust_dir=False)
        if hit:
            return hit
    return None


def _resolve_session(workspace: str, continue_conv: bool) -> str:
    """The chat id to use: a fresh create-chat id, or the id to resume.

    Continue: prefer the in-memory pin, then the newest on-disk chat whose recorded
    cwd matches. Raises if continue is requested but no prior chat exists.
    """
    if not continue_conv:
        return create_chat(workspace)
    sid = get_pinned(workspace) or _resume_target_for(workspace)
    if not sid:
        raise RuntimeError(
            f"No prior cursor chat for workspace {workspace}. "
            "Run cursor_ask first (or check ~/.cursor/chats)."
        )
    return sid


# ----------------------------------------------------------------- conversation history
def read_history(workspace: str, continue_conv: bool) -> list[dict]:
    """Prior turns for the watch view — always [] for cursor (best-effort).

    cursor stores the transcript in an opaque SQLite blob store (store.db `blobs`
    table), not a readable event log, so we don't reconstruct prior turns. A
    continued watch window simply opens without visible history. Kept for
    signature parity with the codex/copilot bridges.
    """
    return []


# ----------------------------------------------------------------- models
def list_models() -> list[str]:
    """Model ids from `cursor-agent models` (cached), or [] if it can't be run.

    Output is a header plus `<id> - <display>` lines; we take the id (first token
    before ' - '). Used to validate a caller's `model` up front.
    """
    global _MODELS_CACHE
    if _MODELS_CACHE is not None:
        return _MODELS_CACHE
    try:
        proc = subprocess.run(
            [CURSOR_BIN, "models"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            **_TEXT,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return []  # transient — don't cache
    ids: list[str] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if " - " not in line:
            continue
        mid = line.split(" - ", 1)[0].strip()
        if mid and " " not in mid:
            ids.append(mid)
    _MODELS_CACHE = ids
    return ids


def validate_model(model: Optional[str]) -> Optional[str]:
    """Return `model` if it's a known cursor model id, else raise ValueError.

    Accepts parameterized ids like `claude-opus-4-8[context=1m]` by checking the
    base id before the bracket. Skips validation (returns as-is) when the model
    list can't be fetched, mirroring agy's lenient fallback.
    """
    if model is None or not str(model).strip():
        return None
    model = str(model).strip()
    base = model.split("[", 1)[0]
    models = list_models()
    if models and base not in models:
        sample = ", ".join(models[:8])
        raise ValueError(
            f"unknown cursor model {model!r}; see `cursor-agent models`. "
            f"Valid ids include: {sample}, ..."
        )
    return model


# ----------------------------------------------------------------- running cursor
def build_args(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    chat_id: str,
    json_stream: bool = False,
) -> list[str]:
    """argv for a headless `cursor-agent -p` run (fresh or resume).

    `--resume <chat_id>` both targets a freshly created chat and resumes an
    existing one, so the same flag serves ask and continue. `--workspace` roots
    file access at the workspace. `json_stream` swaps `text` output for
    `stream-json` (JSONL events on stdout, for watch mode); the final answer is
    reconstructed from the stream in that case. The prompt is positional and goes
    last.
    """
    args = [
        CURSOR_BIN,
        "-p",
        "--output-format",
        "stream-json" if json_stream else "text",
        "--trust",
        "--workspace",
        workspace,
        "--resume",
        chat_id,
    ]
    args += _sandbox_flags(sandbox)
    if model:
        args += ["--model", model]
    args.append(prompt)
    return args


def run_cursor(
    prompt: str,
    workspace: str,
    sandbox: str = DEFAULT_SANDBOX,
    model: Optional[str] = None,
    continue_conv: bool = False,
    timeout_s: int = 180,
    pin: bool = True,
) -> str:
    """Run `cursor-agent -p` (fresh or resume) and return the final answer from stdout.

    On a fresh run the bridge mints the chat id (create-chat) and pins it to
    `workspace` so a later cursor_continue resumes the exact chat — pass
    `pin=False` for swarm workers (one-shot, no continue). Signature mirrors
    run_copilot so server.py's _run_with_progress can call it unchanged.
    """
    validate_sandbox(sandbox)
    os.makedirs(workspace, exist_ok=True)  # cursor's cwd / --workspace must exist
    chat_id = _resolve_session(workspace, continue_conv)
    args = build_args(prompt, workspace, sandbox, model, chat_id)

    proc = subprocess.run(
        args,
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout_s + 30,
        **_TEXT,
        **_spawn_kwargs(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"cursor-agent exited {proc.returncode}\n"
            f"stderr: {(proc.stderr or '')[-1000:]}\n"
            f"stdout: {(proc.stdout or '')[-500:]}"
        )

    answer = (proc.stdout or "").strip()
    if not answer:
        raise RuntimeError(
            f"cursor-agent produced no output on stdout. stderr: {(proc.stderr or '')[-300:]}"
        )

    if not continue_conv and pin:
        _pin(workspace, chat_id)
    return answer


def _answer_from_event(ev: dict, state: dict) -> None:
    """Accumulate the final answer from a cursor stream-json event into `state`.

    In stream mode there's no clean stdout answer to read, so we reconstruct it:
    the terminal `result` event carries the full final message; intermediate
    `assistant` messages are kept as a backup.
    """
    etype = ev.get("type")
    if etype == "result":
        if not ev.get("is_error"):
            r = ev.get("result")
            if isinstance(r, str) and r.strip():
                state["answer"] = r.strip()
    elif etype == "assistant":
        content = (ev.get("message") or {}).get("content") or []
        txt = "".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
        if txt:
            state["assistant"] = txt


def run_cursor_streaming(
    prompt: str,
    workspace: str,
    sandbox: str = DEFAULT_SANDBOX,
    model: Optional[str] = None,
    continue_conv: bool = False,
    timeout_s: int = 180,
    on_event=None,
    pin: bool = True,
) -> str:
    """Run `cursor-agent --output-format stream-json` and stream events, returning the answer.

    Like run_cursor, but launches cursor with stream-json so it emits one JSON
    event per line on stdout, calling `on_event(event_dict)` for each as it arrives
    (this is how watch mode renders steps live). The final answer is reconstructed
    from the stream's `result` event. Completion is driven by the process exiting
    (with a deadline) rather than stdout closing, matching the codex/copilot path.
    """
    validate_sandbox(sandbox)
    os.makedirs(workspace, exist_ok=True)
    chat_id = _resolve_session(workspace, continue_conv)
    args = build_args(prompt, workspace, sandbox, model, chat_id, json_stream=True)

    state: dict = {"answer": "", "assistant": ""}
    proc = subprocess.Popen(
        args,
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **_TEXT,
        **_spawn_kwargs(),
    )
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
    ot.join(timeout=1)
    et.join(timeout=1)

    stderr = "".join(err_chunks)
    if timed_out:
        raise RuntimeError(f"cursor-agent timed out after {timeout_s + 30}s (watched)")
    if proc.returncode not in (0, None):
        raise RuntimeError(
            f"cursor-agent exited {proc.returncode}\nstderr: {(stderr or '')[-1000:]}"
        )

    answer = (state.get("answer") or state.get("assistant") or "").strip()
    if not answer:
        raise RuntimeError(
            f"cursor-agent produced no result in its stream-json output. "
            f"stderr: {(stderr or '')[-300:]}"
        )

    if not continue_conv and pin:
        _pin(workspace, chat_id)
    return answer


# ----------------------------------------------------------------- diagnostics
def cursor_version() -> Optional[str]:
    """`cursor-agent --version` first line, or None if cursor can't be run."""
    try:
        proc = subprocess.run(
            [CURSOR_BIN, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            **_TEXT,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return text.splitlines()[0] if text else None


def auth_status() -> tuple[bool, str]:
    """(ok, detail) for the auth row, via `cursor-agent status`. Spends no quota.

    `cursor-agent status` prints "Logged in as <email>" when authenticated. We
    check for "logged in" case-insensitively so the leading checkmark/glyph
    doesn't matter. ok is False (with a login hint) when not logged in or the
    command can't run.
    """
    try:
        proc = subprocess.run(
            [CURSOR_BIN, "status"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            **_TEXT,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return (False, "could not run `cursor-agent status`")
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    # Strip a leading status glyph (e.g. "✓ ") so the detail reads cleanly.
    first = out.splitlines()[0].lstrip("✓✔✅✗✘ \t").strip() if out else ""
    if "logged in" in out.lower():
        return (True, first or "logged in")
    return (False, first or "not logged in (run `cursor-agent login`)")


def status_rows() -> list[tuple[str, bool, str]]:
    """Setup diagnostics as (label, ok, detail) rows. Spends no quota.

    Mirrors codex_bridge / copilot_bridge status_rows shape so server.py renders
    cursor rows with the same formatter.
    """
    rows: list[tuple[str, bool, str]] = []

    ver = cursor_version()
    if ver is None:
        rows.append(("cursor CLI", False, f"not found (set CURSOR_BIN; tried {CURSOR_BIN_ENV!r})"))
    else:
        rows.append(("cursor CLI", True, ver))

    ok, detail = auth_status()
    rows.append(("cursor auth", ok, detail))

    rows.append(("chats dir", CHATS_DIR.exists(), str(CHATS_DIR)))

    with _PIN_LOCK:
        n_pins = len(_PINNED)
    rows.append(("pinned chats", True, f"{n_pins} workspace(s) pinned this run"))

    return rows
