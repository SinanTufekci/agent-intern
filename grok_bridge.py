"""Grok Build CLI bridge: run `grok -p` headless and return its answer.

Fifth backend alongside the agy bridge (server.py), Codex (codex_bridge.py),
Copilot (copilot_bridge.py), and Cursor (cursor_bridge.py). Grok Build is xAI's
terminal coding agent (`grok`, https://github.com/xai-org/grok-build, installed
by https://x.ai/cli/install.sh). Like codex/copilot/cursor it is stdout-native in
headless mode: `grok -p "<prompt>" --output-format json` runs one prompt
non-interactively, writes a single JSON object to STDOUT, and exits — so we read
the answer from stdout, no transcript scraping.

⚠️ EXPERIMENTAL — NO AUTHENTICATED ROUND-TRIP HAS EVER RUN. Grok Build needs a
SuperGrok or X Premium+ subscription (or an xAI API key) and the bridge author has
neither, so this backend stays COMMUNITY-VERIFIED until a user with Grok access
confirms a live run. Unlike the other backends, though, its flag surface is not
guesswork: grok-build is OPEN SOURCE, so every flag below was read off the actual
clap definitions in `crates/codegen/xai-grok-pager/src/app/cli.rs`, then confirmed
against a live `grok --help`.

VERIFIED LIVE on grok 1.0.3 / Windows, EVEN UNAUTHENTICATED:
  - the full flag surface (`grok --help`), including every flag this bridge passes;
  - `grok models` exits 0 logged out, prints "You are not authenticated." and still
    lists the catalogue — so it doubles as the auth probe AND the model list, and
    spends no quota (this is why status/validation are cheap here);
  - the DEFAULT MODEL IS `grok-4.5`, not the `grok-build` that xAI's own headless
    docs use in their examples — the docs had already rotted at 1.0.3;
  - unauthenticated `grok -p` exits 1 and emits `{"type":"error","message":"Not
    signed in. ..."}` on stdout for both `json` and `streaming-json` (and a human
    copy on stderr). No browser is opened and it does not hang;
  - AUTH IS CHECKED BEFORE model/sandbox/session validation, so a bad `--sandbox`
    profile or `-m` id is NOT rejected up front while logged out. That is why this
    bridge validates the sandbox client-side (validate_sandbox) instead of trusting
    grok to reject it;
  - `GROK_HOME` really relocates the entire data dir (a fresh home is created);
  - concurrent `grok -p` runs do not deadlock on the `~/.grok` lock files;
  - the on-disk layout: config.toml, auth.json, sessions/, logs/, bin/.
DOCS-DERIVED and still UNVERIFIED: the happy-path answer itself — the `json`
envelope's `text`/`sessionId` fields, `-r` actually restoring context, and the
`streaming-json` event stream that watch mode renders. Everything the auth wall
sits in front of is unproven. Do not call this backend "verified" until a live
round-trip confirms it.

CONTINUE / RESUME. Grok's `--output-format json` returns the `sessionId` it just
used, so — like codex/cursor — the bridge captures that id and pins it to the
workspace, then resumes the exact session with `-r <id>`. The restart-proof
fallback is nicer than cursor's: grok's own `-c/--continue` means "the most recent
session for this cwd", so when the in-memory pin is gone we just pass `-c` and let
grok do the lookup. No SQLite session store is ever read (it is opaque, and its
format is one more thing we could not verify). Note `-s/--session-id` is NOT a
resume flag on 1.0.3 — it creates a new session with a caller-chosen UUID and
errors if that UUID is taken — so the bridge never passes it.

HEADLESS FLAGS. `-p/--single <prompt>` takes the prompt as its VALUE (not
positional) and selects headless mode. `--output-format json` yields the single
JSON result object (`streaming-json` yields NDJSON events, for watch mode).
`--cwd` roots the run at the workspace. Update checks are suppressed with the
`GROK_DISABLE_AUTOUPDATER=1` ENV VAR rather than the `--no-auto-update` flag:
that flag is `hide = true` in grok's clap definition and absent from `--help`, and
betting every call on an undocumented flag is how a silent CLI update breaks the
whole bridge — an unknown env var is merely ignored.

SECURITY. Grok exposes a real OS sandbox (`--sandbox <profile>`) AND an approval
gate. Headless does NOT auto-approve on its own, so every mode passes
`--always-approve` (alias of the `--yolo` in xAI's CI examples); the actual
containment comes from the profile plus a tool allowlist:
  - read-only        `--sandbox read-only` + `--tools` allowlist + `--no-subagents`
  - workspace-write  `--sandbox workspace`: writes land in cwd, ~/.grok and temp
  - danger-full-access  `--sandbox off`: everything. Avoid.
⚠️ THE OS SANDBOX IS LINUX + macOS ONLY (Landlock / Seatbelt). xAI's own docs say
that where it cannot be applied grok "logs a warning and continues WITHOUT
enforcement" — i.e. on WINDOWS `--sandbox read-only` is silently not enforced.
That is exactly why read-only does not lean on the profile alone: the `--tools`
allowlist below is agent-enforced and therefore holds on every platform. It is
still weaker than codex's hard boundary. MCP meta-tools stay available under an
allowlist (grok's documented behaviour), so a configured MCP server can still
write even in read-only. Only run it with trusted prompts on trusted content.

AUTH. `grok login` (browser OAuth), `grok login --device-code` (headless), or the
`XAI_API_KEY` env var; credentials cache in `$GROK_HOME/auth.json`. The bridge
never touches credentials — it only shells out to `grok --version` and
`grok models` (both non-interactive, no quota) for the status view.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

import proc_tree

# The grok executable. Set GROK_BIN to an explicit path to override. Mirrors
# AGY_BIN / CODEX_BIN / COPILOT_BIN / CURSOR_BIN. Read once at import; the
# launching process's env wins.
GROK_BIN_ENV = os.environ.get("GROK_BIN", "grok")

# Grok's data home: config.toml, auth.json, sessions/, logs/, bin/. Relocatable
# via GROK_HOME (verified live: grok creates and uses a fresh home). We only READ
# under it (for status), never write.
GROK_HOME = Path(os.environ.get("GROK_HOME") or (Path.home() / ".grok"))
SESSIONS_DIR = GROK_HOME / "sessions"

# The installer drops the binary here and appends it to the *user* PATH. A PATH
# edit only reaches processes started afterwards, so an MCP server that was
# already running when the user installed grok would never find it on PATH. Fall
# back to the known install dir before giving up.
_INSTALL_BIN_DIR = GROK_HOME / "bin"


def _resolve_bin() -> str:
    """Full path to the grok executable (see GROK_BIN_ENV note)."""
    if os.path.sep in GROK_BIN_ENV or os.path.isfile(GROK_BIN_ENV):
        return GROK_BIN_ENV
    found = shutil.which(GROK_BIN_ENV)
    if found:
        return found
    # PATH miss — try the installer's own directory (see _INSTALL_BIN_DIR).
    for name in (f"{GROK_BIN_ENV}.exe", GROK_BIN_ENV):
        candidate = _INSTALL_BIN_DIR / name
        if candidate.is_file():
            return str(candidate)
    return GROK_BIN_ENV


GROK_BIN = _resolve_bin()

# The `sandbox` knob mirrors codex's/copilot's/cursor's for a uniform agent_swarm
# field, but maps to grok's profile + tool allowlist (see the module SECURITY
# note). Default read-only for safety parity — callers opt into write access.
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_SANDBOX = "read-only"

# Built-in tools left enabled in read-only mode. Grok takes an ALLOWLIST
# (`--tools`) as well as a denylist (`--disallowed-tools`); the allowlist is the
# right primitive for a security knob because it FAILS SAFE — if a future grok
# adds a new write tool, a denylist silently leaks it while an allowlist keeps it
# out. Names are grok's internal tool ids (the shell tool is `run_terminal_cmd`,
# not `bash`), read off the tool implementations in xai-grok-tools. The write side
# deliberately left out: bash, run_terminal_cmd, search_replace, write, task.
READ_ONLY_TOOLS = ("read_file", "list_dir", "grep", "glob", "web_search", "web_fetch")

# A grok session id is a UUID (`-s` requires one, and the json result echoes it).
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Text decoding for grok's subprocess output: it emits UTF-8, which the Windows
# locale codec (cp1252) can mangle. Decode UTF-8 explicitly and never raise on a
# stray byte. Mirrors cursor_bridge._TEXT.
_TEXT = {"encoding": "utf-8", "errors": "replace"}

# Cached model-id list from `grok models` (populated once on first validation; a
# transient failure is not cached).
_MODELS_CACHE: Optional[list[str]] = None

# workspace -> session id, pinned after each fresh ask so grok_continue resumes
# the exact session. Guarded by a lock (MCP tools may run on different threads).
# Lives only for the process; grok's own `-c` is the restart-proof fallback.
_PINNED: dict[str, str] = {}
_PIN_LOCK = threading.Lock()


def _spawn_kwargs() -> dict:
    """Keep grok from flashing a console window on Windows; new session elsewhere.

    Grok writes its answer to stdout regardless of the controlling terminal.
    Windows uses CREATE_NO_WINDOW so no console flashes; POSIX starts a new
    session. Mirrors the other bridges.
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {"start_new_session": True}


def _env() -> dict:
    """Process env for grok, with the background auto-updater disabled.

    A CLI that updates itself mid-session is how this project has been broken
    before with no code change on our side, and grok ships an auto-updater that
    runs on launch. `GROK_DISABLE_AUTOUPDATER=1` is the documented process-scoped
    off switch; see the module note on why this is an env var and not the hidden
    `--no-auto-update` flag.
    """
    return {**os.environ, "GROK_DISABLE_AUTOUPDATER": "1"}


def normalize_workspace(ws: Optional[str]) -> str:
    """Absolute path for `ws`, or the server's cwd when omitted."""
    return os.path.abspath(ws) if ws else os.getcwd()


def validate_sandbox(mode: str) -> str:
    """Return `mode` if valid, else raise ValueError listing the allowed values.

    Client-side on purpose: verified live that grok checks AUTH BEFORE it validates
    `--sandbox`, so a typo'd profile would come back as "Not signed in" (or, once
    authenticated, only after a round-trip) instead of a useful message.
    """
    if mode not in SANDBOX_MODES:
        raise ValueError(f"invalid sandbox {mode!r}; expected one of: {', '.join(SANDBOX_MODES)}")
    return mode


def _sandbox_flags(sandbox: str) -> list[str]:
    """grok profile/approval flags for a `sandbox` value (see module SECURITY note).

    `--always-approve` is passed in every mode because grok's headless mode does
    NOT auto-approve tool calls on its own (unlike agy/kimi print mode) and there
    is no human to answer a prompt; containment comes from the profile and the
    read-only tool allowlist, not from the approval gate.
    """
    if sandbox == "read-only":
        # OS profile (Linux/macOS only) PLUS an agent-enforced allowlist that holds
        # on every platform, plus no subagents (a subagent could otherwise write).
        return [
            "--sandbox",
            "read-only",
            "--tools",
            ",".join(READ_ONLY_TOOLS),
            "--no-subagents",
            "--always-approve",
        ]
    if sandbox == "workspace-write":
        # Writes land in cwd + ~/.grok + temp dirs; reads are unrestricted.
        return ["--sandbox", "workspace", "--always-approve"]
    if sandbox == "danger-full-access":
        # Everything, OS sandbox explicitly off.
        return ["--sandbox", "off", "--always-approve"]
    raise ValueError(f"invalid sandbox {sandbox!r}")


# ----------------------------------------------------------------- session pinning
def get_pinned(workspace: str) -> Optional[str]:
    """The session id pinned to `workspace` this run, or None."""
    with _PIN_LOCK:
        return _PINNED.get(workspace)


def _pin(workspace: str, session_id: str) -> None:
    with _PIN_LOCK:
        _PINNED[workspace] = session_id


# ----------------------------------------------------------------- conversation history
def read_history(workspace: str, continue_conv: bool) -> list[dict]:
    """Prior turns for the watch view — always [] for grok (best-effort).

    Grok stores transcripts as SQLite under $GROK_HOME/sessions/, not a readable
    event log, so we don't reconstruct prior turns; a continued watch window simply
    opens without visible history. Kept for signature parity with the other bridges.
    """
    return []


# ----------------------------------------------------------------- models
def list_models() -> list[str]:
    """Model ids from `grok models` (cached), or [] if it can't be run.

    Verified live (grok 1.0.3, logged OUT — it still prints the catalogue and exits
    0, which is why this is safe to call for validation):

        You are not authenticated.

        Default model: grok-4.5

        Available models:
          * grok-4.5 (default)

    So: take the lines after "Available models:", strip the "* " bullet, and keep
    the first token (dropping a trailing "(default)" marker).
    """
    global _MODELS_CACHE
    if _MODELS_CACHE is not None:
        return _MODELS_CACHE
    try:
        proc = proc_tree.run_captured(
            [GROK_BIN, "models"],
            stdin=subprocess.DEVNULL,
            timeout=20,
            env=_env(),
            **_TEXT,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return []  # transient — don't cache
    ids: list[str] = []
    in_list = False
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if line.lower().startswith("available models"):
            in_list = True
            continue
        if not in_list:
            continue
        if not line:
            continue
        mid = line.lstrip("*-• \t").split()[0].strip()
        if mid:
            ids.append(mid)
    _MODELS_CACHE = ids
    return ids


def validate_model(model: Optional[str]) -> Optional[str]:
    """Return `model` if it's a known grok model id, else raise ValueError.

    Skips validation (returns as-is) when the model list can't be fetched, mirroring
    the agy/cursor lenient fallback. Worth doing up front here because `grok models`
    costs nothing and, verified live, still answers while logged out — whereas grok
    itself checks auth BEFORE it would ever reject a bad `-m`, so an unvalidated typo
    surfaces as a confusing "Not signed in" rather than "unknown model".
    """
    if model is None or not str(model).strip():
        return None
    model = str(model).strip()
    models = list_models()
    if models and model not in models:
        sample = ", ".join(models[:8])
        raise ValueError(
            f"unknown grok model {model!r}; see `grok models`. Valid ids include: {sample}"
        )
    return model


# ----------------------------------------------------------------- running grok
def build_args(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    resume_id: Optional[str] = None,
    continue_conv: bool = False,
    json_stream: bool = False,
) -> list[str]:
    """argv for a headless `grok -p` run (fresh, `-r <id>` resume, or `-c` continue).

    `-p <prompt>` takes the prompt as its VALUE (not positional). `--cwd` roots the
    run at the workspace (the caller also sets the process cwd). `json_stream` swaps
    the single `json` result object for `streaming-json` NDJSON events (watch mode);
    the answer is reconstructed from the stream in that case.

    Resume precedence: an explicit `resume_id` (`-r`) beats `continue_conv` (`-c`),
    and grok itself rejects the two together. `-s/--session-id` is never passed —
    on 1.0.3 it means "create a NEW session with this UUID", not "resume".
    """
    args = [
        GROK_BIN,
        "-p",
        prompt,
        "--cwd",
        workspace,
        "--output-format",
        "streaming-json" if json_stream else "json",
    ]
    if resume_id:
        args += ["-r", resume_id]
    elif continue_conv:
        args.append("-c")
    if model:
        args += ["-m", model]
    args += _sandbox_flags(sandbox)
    return args


def _resume_flags(workspace: str, continue_conv: bool) -> tuple[Optional[str], bool]:
    """(resume_id, use_continue) for a run against `workspace`.

    Fresh ask -> (None, False). Continue -> the pinned session id if this process
    minted one, else (None, True) so grok's own "most recent session for this cwd"
    lookup handles it. That fallback is what makes continue survive a server
    restart without us parsing grok's opaque SQLite session store.
    """
    if not continue_conv:
        return (None, False)
    pinned = get_pinned(workspace)
    return (pinned, pinned is None)


def _parse_result(stdout: str, stderr: str) -> tuple[str, Optional[str]]:
    """(answer, session_id) from `--output-format json` stdout.

    Grok documents a single JSON object, but tolerate stray lines around it by
    scanning for the last parseable object. A `{"type":"error"}` payload is grok's
    own failure envelope — verified live for the unauthenticated case — and carries
    the only useful message, so surface it as the error rather than "no output".
    """
    candidates: list[dict] = []
    text = (stdout or "").strip()
    if text:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                candidates.append(obj)
        except ValueError:
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    candidates.append(obj)
    if not candidates:
        raise RuntimeError(
            f"grok produced no JSON result on stdout. "
            f"stdout: {text[:300]!r} stderr: {(stderr or '')[-300:]}"
        )
    result = candidates[-1]
    if result.get("type") == "error":
        raise RuntimeError(f"grok error: {result.get('message') or result}")
    answer = (result.get("text") or "").strip()
    session_id = result.get("sessionId")
    if not answer:
        raise RuntimeError(
            f"grok returned an empty answer (stopReason={result.get('stopReason')!r}). "
            f"stderr: {(stderr or '')[-300:]}"
        )
    return (answer, session_id if isinstance(session_id, str) else None)


def run_grok(
    prompt: str,
    workspace: str,
    sandbox: str = DEFAULT_SANDBOX,
    model: Optional[str] = None,
    continue_conv: bool = False,
    timeout_s: int = 180,
    pin: bool = True,
) -> str:
    """Run `grok -p` (fresh, resume, or continue) and return the final answer.

    On a fresh run the session id comes back in the JSON result and is pinned to
    `workspace` so a later grok_continue resumes the exact session — pass
    `pin=False` for swarm workers (one-shot, no continue). Signature mirrors
    run_cursor so server.py's _run_with_progress can call it unchanged.
    """
    validate_sandbox(sandbox)
    os.makedirs(workspace, exist_ok=True)  # grok's cwd must exist
    resume_id, use_continue = _resume_flags(workspace, continue_conv)
    args = build_args(prompt, workspace, sandbox, model, resume_id, use_continue)

    proc = proc_tree.run_captured(
        args,
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        timeout=timeout_s + 30,
        env=_env(),
        **_TEXT,
        **_spawn_kwargs(),
    )
    if proc.returncode != 0:
        # Grok still writes its {"type":"error"} envelope on a non-zero exit
        # (verified live for "Not signed in"), and that message beats a bare exit
        # code — so prefer it, and fall back to the raw streams.
        try:
            _parse_result(proc.stdout or "", proc.stderr or "")
        except RuntimeError as exc:
            raise RuntimeError(f"grok exited {proc.returncode}: {exc}") from None
        raise RuntimeError(
            f"grok exited {proc.returncode}\n"
            f"stderr: {(proc.stderr or '')[-1000:]}\n"
            f"stdout: {(proc.stdout or '')[-500:]}"
        )

    answer, session_id = _parse_result(proc.stdout or "", proc.stderr or "")
    if not continue_conv and pin and session_id:
        _pin(workspace, session_id)
    return answer


def _answer_from_event(ev: dict, state: dict) -> None:
    """Accumulate the final answer + session id from a grok streaming-json event.

    In stream mode there's no single result object to read, so we reconstruct:
    `text` events carry the answer in chunks, and the terminal `end` event carries
    the session id. An `error` event is grok's failure envelope — record it so the
    caller can raise with grok's own message instead of "no output".
    """
    etype = ev.get("type")
    if etype == "text":
        chunk = ev.get("data")
        if isinstance(chunk, str):
            state["text"] = state.get("text", "") + chunk
    elif etype == "end":
        sid = ev.get("sessionId")
        if isinstance(sid, str):
            state["session_id"] = sid
    elif etype == "error":
        msg = ev.get("message")
        if isinstance(msg, str) and msg.strip():
            state["error"] = msg.strip()


def run_grok_streaming(
    prompt: str,
    workspace: str,
    sandbox: str = DEFAULT_SANDBOX,
    model: Optional[str] = None,
    continue_conv: bool = False,
    timeout_s: int = 180,
    on_event=None,
    pin: bool = True,
) -> str:
    """Run `grok --output-format streaming-json`, stream events, return the answer.

    Like run_grok, but grok emits one JSON event per line on stdout and we call
    `on_event(event_dict)` for each as it arrives (this is how watch mode renders
    steps live). Completion is driven by the process exiting (with a deadline),
    matching the codex/copilot/cursor path.
    """
    validate_sandbox(sandbox)
    os.makedirs(workspace, exist_ok=True)
    resume_id, use_continue = _resume_flags(workspace, continue_conv)
    args = build_args(prompt, workspace, sandbox, model, resume_id, use_continue, json_stream=True)

    state: dict = {}
    proc = subprocess.Popen(
        args,
        cwd=workspace,
        stdin=subprocess.DEVNULL,
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
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
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
        proc_tree.kill_tree(proc)  # the grandchild must die too — see proc_tree
        try:
            proc.wait(timeout=proc_tree.REAP_GRACE_S)
        except subprocess.TimeoutExpired:
            pass  # reaped or not, the caller's deadline stays real
    ot.join(timeout=1)
    et.join(timeout=1)

    stderr = "".join(err_chunks)
    if timed_out:
        raise RuntimeError(f"grok timed out after {timeout_s + 30}s (watched)")
    if state.get("error"):
        raise RuntimeError(f"grok error: {state['error']}")
    if proc.returncode not in (0, None):
        raise RuntimeError(f"grok exited {proc.returncode}\nstderr: {(stderr or '')[-1000:]}")

    answer = (state.get("text") or "").strip()
    if not answer:
        raise RuntimeError(
            f"grok produced no text in its streaming-json output. stderr: {(stderr or '')[-300:]}"
        )

    if not continue_conv and pin and state.get("session_id"):
        _pin(workspace, state["session_id"])
    return answer


# ----------------------------------------------------------------- diagnostics
def grok_version() -> Optional[str]:
    """`grok --version` first line (e.g. "grok 1.0.3 (1a29d5bc12)"), or None."""
    try:
        proc = proc_tree.run_captured(
            [GROK_BIN, "--version"],
            stdin=subprocess.DEVNULL,
            timeout=15,
            env=_env(),
            **_TEXT,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return text.splitlines()[0] if text else None


def auth_status() -> tuple[bool, str]:
    """(ok, detail) for the auth row, via `grok models`. Spends no quota.

    Verified live on 1.0.3: `grok models` exits 0 either way, but prints the line
    "You are not authenticated." when logged out. Grok has no `status`/`whoami`
    subcommand, so that string is the auth signal — and it comes free with the
    model list. XAI_API_KEY is reported separately because it authenticates grok
    without any local credential file.
    """
    try:
        proc = proc_tree.run_captured(
            [GROK_BIN, "models"],
            stdin=subprocess.DEVNULL,
            timeout=20,
            env=_env(),
            **_TEXT,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return (False, "could not run `grok models`")
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if not out:
        return (False, "`grok models` printed nothing")
    if "not authenticated" in out.lower():
        if os.environ.get("XAI_API_KEY"):
            return (True, "XAI_API_KEY is set (grok reports no cached login)")
        return (False, "not authenticated (run `grok login`, or set XAI_API_KEY)")
    default = next(
        (ln.strip() for ln in out.splitlines() if ln.lower().startswith("default model:")),
        "",
    )
    return (True, default or "authenticated")


def status_rows() -> list[tuple[str, bool, str]]:
    """Setup diagnostics as (label, ok, detail) rows. Spends no quota.

    Mirrors the codex/copilot/cursor status_rows shape so server.py renders grok
    rows with the same formatter.
    """
    rows: list[tuple[str, bool, str]] = []

    ver = grok_version()
    if ver is None:
        rows.append(("grok CLI", False, f"not found (set GROK_BIN; tried {GROK_BIN_ENV!r})"))
    else:
        rows.append(("grok CLI", True, ver))

    ok, detail = auth_status()
    rows.append(("grok auth", ok, detail))

    models = list_models()
    rows.append(("models", bool(models), ", ".join(models[:6]) if models else "none listed"))

    rows.append(("data dir", GROK_HOME.exists(), str(GROK_HOME)))

    with _PIN_LOCK:
        n_pins = len(_PINNED)
    rows.append(("pinned sessions", True, f"{n_pins} workspace(s) pinned this run"))

    return rows
