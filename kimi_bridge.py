"""Kimi Code CLI bridge: run `kimi -p` headless and return its answer.

Fifth backend alongside the agy bridge (server.py), Codex (codex_bridge.py),
Copilot (copilot_bridge.py), and Cursor (cursor_bridge.py). Kimi Code is
Moonshot AI's terminal coding agent (`kimi`, https://github.com/MoonshotAI/kimi-code,
npm `@moonshot-ai/kimi-code`). Like codex/copilot/cursor it is stdout-native in
print mode: `kimi -p "<prompt>" --output-format text` runs one prompt
non-interactively and writes the clean final answer to STDOUT, then exits — so we
read the answer from stdout, no transcript scraping.

⚠️ EXPERIMENTAL — NOT LIVE-VERIFIED. Unlike the other four backends, this bridge
has NOT been exercised against an authenticated Kimi — the bridge author has no Kimi
account and cannot verify it, so it stays COMMUNITY-VERIFIED until a user with Kimi
access confirms a live round-trip. What IS verified live on kimi 0.29.1 / Windows
(even unauthenticated):
the flag surface (`kimi --help`), that `-p` REJECTS `--auto`/`--yolo` ("Cannot
combine --prompt with ..." — print mode is already self-approving, so we pass
NEITHER), the unauthenticated failure mode (exit 1, stderr "failed to run prompt:
No model configured..."), and the base dir layout `~/.kimi-code/` (config.toml,
device_id, logs/, updates/). What is DOCS-DERIVED and still UNVERIFIED: the
happy-path stdout answer, `-c/--continue` resume behaviour, and the on-disk
`sessions/` format (never created without a successful model run). Do not treat
this backend as "verified" until a live `kimi -p` round-trip is confirmed.

CONTINUE / RESUME. Kimi scopes sessions per working directory and exposes
`-c/--continue` ("Continue the previous session for the working directory"). So —
unlike codex/cursor, which capture and pin a session id — the bridge just re-runs
with cwd=workspace and `-c`; Kimi's own cwd scoping does the pinning. No session-id
capture, no on-disk state reading (whose format we couldn't verify). If no prior
session exists for the workspace, Kimi errors and we surface that. `-S/--session
<id>` exists too but is deliberately unused here for the same "unverified state
format" reason.

HEADLESS FLAGS. `-p/--prompt <prompt>` selects print mode (the prompt is the flag
VALUE, not positional). `--output-format text` returns the clean final answer on
stdout (`stream-json` exists, for a future watch mode). NOTHING else is needed:
print mode auto-approves tool calls on its own, and there is no sandbox flag — see
SECURITY.

SECURITY. Kimi print mode has NO sandbox and auto-executes every tool call with no
approval gate (this is why `-p` refuses `--auto`/`--yolo` — they'd be redundant).
That is the same posture as agy's print mode: no agy/kimi flag makes it safe. Only
run it with trusted prompts on trusted content.

AUTH. Kimi authenticates via `kimi login` (device-code OAuth) or an API key placed
in `~/.kimi-code/config.toml` (it does NOT read a bare env var). The bridge never
touches credentials; it only shells out to `kimi --version` and `kimi provider
list` (both non-interactive, no quota) for the status view. `KIMI_CODE_HOME`
relocates the whole data dir — the hook a future swarm path would use for HOME-style
isolation, mirroring the agy swarm trick.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import proc_tree

# The kimi executable. npm installs a `kimi.ps1`/`kimi.cmd` shim on Windows that
# CreateProcess can't launch by bare name, so resolve via shutil.which (honors
# PATHEXT, returns the full shim path). Set KIMI_BIN to an explicit path to
# override. Mirrors AGY_BIN / CODEX_BIN / COPILOT_BIN / CURSOR_BIN. Read once at
# import; the launching process's env wins.
KIMI_BIN_ENV = os.environ.get("KIMI_BIN", "kimi")


def _resolve_bin() -> str:
    """Full path to the kimi executable (see KIMI_BIN_ENV note)."""
    if os.path.sep in KIMI_BIN_ENV or os.path.isfile(KIMI_BIN_ENV):
        return KIMI_BIN_ENV
    return shutil.which(KIMI_BIN_ENV) or KIMI_BIN_ENV


KIMI_BIN = _resolve_bin()

# Kimi's data home. Sessions live under <home>/sessions/, config in
# <home>/config.toml. Relocatable via KIMI_CODE_HOME (verified: kimi honors it);
# default ~/.kimi-code. We only READ under it (for status), never write.
KIMI_CODE_HOME = Path(os.environ.get("KIMI_CODE_HOME") or (Path.home() / ".kimi-code"))

# Text decoding for kimi's subprocess output: it emits UTF-8, which the Windows
# locale codec (cp1252) can mangle. Decode UTF-8 explicitly and never raise on a
# stray byte. Mirrors cursor_bridge._TEXT.
_TEXT = {"encoding": "utf-8", "errors": "replace"}


def _spawn_kwargs() -> dict:
    """Keep kimi from flashing a console window on Windows; new session elsewhere.

    Kimi writes its answer to stdout regardless of the controlling terminal.
    Windows uses CREATE_NO_WINDOW so the .cmd/PowerShell shim doesn't flash a
    console; POSIX starts a new session. Mirrors the other bridges.
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {"start_new_session": True}


def normalize_workspace(ws: Optional[str]) -> str:
    """Absolute path for `ws`, or the server's cwd when omitted."""
    return os.path.abspath(ws) if ws else os.getcwd()


def validate_model(model: Optional[str]) -> Optional[str]:
    """Return `model` stripped, or None — Kimi model aliases aren't enumerable.

    Kimi has no `models` subcommand; model aliases are user-defined in
    config.toml under `[models."<alias>"]` and resolved by `kimi -m <alias>`. There
    is no reliable list to validate against (and none at all before login), so this
    is a lenient pass-through — mirroring agy/cursor's behaviour when their model
    list can't be read. An unknown alias surfaces as Kimi's own error at run time.
    """
    if model is None or not str(model).strip():
        return None
    return str(model).strip()


def build_args(
    prompt: str,
    workspace: str,
    model: Optional[str],
    continue_conv: bool,
) -> list[str]:
    """argv for a headless `kimi -p` run (fresh or `-c` continue).

    `-p <prompt>` takes the prompt as its VALUE (not positional). `--output-format
    text` yields the clean final answer on stdout. `-c/--continue` resumes the
    previous session for the working directory (cwd = workspace, set by the caller).
    We pass NEITHER `--auto` nor `--yolo`: verified on 0.29.1 that `-p` rejects both
    ("Cannot combine --prompt with ...") because print mode already auto-approves.
    `workspace` is not a flag here — Kimi roots on the process cwd, which the caller
    sets to the workspace.
    """
    args = [KIMI_BIN]
    if continue_conv:
        args.append("-c")
    if model:
        args += ["-m", model]
    args += ["-p", prompt, "--output-format", "text"]
    return args


def run_kimi(
    prompt: str,
    workspace: str,
    model: Optional[str] = None,
    continue_conv: bool = False,
    timeout_s: int = 180,
) -> str:
    """Run `kimi -p` (fresh or `-c` continue) and return the final answer from stdout.

    Continue mode relies on Kimi's per-cwd session scoping (`-c`), so there is no id
    to pin or capture — the signature has no `pin` arg (unlike codex/cursor). Shaped
    positional-friendly so server.py's _run_with_progress(run_fn, args, ...) can call
    it unchanged, matching the agy/text worker's (prompt, ws, model, continue,
    timeout) shape.
    """
    os.makedirs(workspace, exist_ok=True)  # kimi's cwd must exist
    args = build_args(prompt, workspace, model, continue_conv)

    proc = proc_tree.run_captured(
        args,
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=timeout_s + 30,
        **_TEXT,
        **_spawn_kwargs(),
    )
    if proc.returncode != 0:
        # stderr carries Kimi's own message, e.g. "No model configured" (not logged
        # in) or a "no previous session" continue error — surface it verbatim.
        raise RuntimeError(
            f"kimi exited {proc.returncode}\n"
            f"stderr: {(proc.stderr or '')[-1000:]}\n"
            f"stdout: {(proc.stdout or '')[-500:]}"
        )

    answer = (proc.stdout or "").strip()
    if not answer:
        raise RuntimeError(
            f"kimi produced no output on stdout. stderr: {(proc.stderr or '')[-300:]}"
        )
    return answer


# ----------------------------------------------------------------- diagnostics
def kimi_version() -> Optional[str]:
    """`kimi --version` first line (e.g. "0.29.1"), or None if kimi can't be run."""
    try:
        proc = proc_tree.run_captured(
            [KIMI_BIN, "--version"],
            stdin=subprocess.DEVNULL,
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
    """(ok, detail) for the auth row, via `kimi provider list`. Spends no quota.

    Verified on 0.29.1: unauthenticated, `kimi provider list` exits 0 and prints
    "No providers configured."; after `kimi login` it lists the managed Kimi
    provider. So a configured provider is our proxy for "authenticated". ok is False
    (with a login hint) when no provider is configured or the command can't run.
    """
    try:
        proc = proc_tree.run_captured(
            [KIMI_BIN, "provider", "list"],
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=20,
            **_TEXT,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return (False, "could not run `kimi provider list`")
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if not out or "no providers configured" in out.lower():
        return (False, "no providers configured (run `kimi login` or set config.toml)")
    return (True, out.splitlines()[0].strip() or "provider configured")


def status_rows() -> list[tuple[str, bool, str]]:
    """Setup diagnostics as (label, ok, detail) rows. Spends no quota.

    Mirrors the codex/copilot/cursor status_rows shape so server.py renders Kimi
    rows with the same formatter.
    """
    rows: list[tuple[str, bool, str]] = []

    ver = kimi_version()
    if ver is None:
        rows.append(("kimi CLI", False, f"not found (set KIMI_BIN; tried {KIMI_BIN_ENV!r})"))
    else:
        rows.append(("kimi CLI", True, ver))

    ok, detail = auth_status()
    rows.append(("kimi auth", ok, detail))

    rows.append(("data dir", KIMI_CODE_HOME.exists(), str(KIMI_CODE_HOME)))

    return rows
