"""opencode CLI bridge: run `opencode run` headless and return its answer.

Seventh backend alongside the agy bridge (server.py), Codex (codex_bridge.py),
Copilot (copilot_bridge.py), Cursor (cursor_bridge.py), Grok (grok_bridge.py) and
Kimi (kimi_bridge.py). opencode is SST's open-source terminal coding agent
(`opencode`, https://github.com/sst/opencode, npm `@opencode/cli` for 2.x,
`opencode-ai` for 1.x). Like
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
  - read-only        deny edit/task/webfetch/websearch/external_directory, and
                     "ask" (= reject, see 1.) for bash; read and search tools
                     allowed, `.env` files denied.
  - workspace-write  writes and shell inside the workspace; `external_directory`
                     denied, so escaping the workspace is refused, not asked.
  - danger-full-access  `--auto` (opencode's own word for it: "dangerous!"), no
                     policy at all.
OPENCODE 2.x changes the mechanism, not the modes: the policy goes in
OPENCODE_CONFIG_CONTENT as an ordered rule list and every run passes --standalone
(see _V2_READ_ONLY_RULES for why both are required, and what workspace-write no
longer confines there: the shell). A major the bridge has no verified policy for
is refused outright (check_supported_version), never run unfenced.
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
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

import proc_tree

# The opencode executable. npm installs an `opencode.cmd`/`.ps1` shim on Windows
# that CreateProcess can't launch by bare name, so resolve via shutil.which (honors
# PATHEXT, returns the full shim path). Set OPENCODE_BIN to an explicit path to
# override. Mirrors AGY_BIN / CODEX_BIN / COPILOT_BIN / CURSOR_BIN / GROK_BIN /
# KIMI_BIN. Read once at import; the launching process's env wins.
OPENCODE_BIN_ENV = os.environ.get("OPENCODE_BIN", "opencode")


def _resolve_bin() -> str:
    """Full path to the opencode executable (see OPENCODE_BIN_ENV note).

    On Windows the npm install is an `opencode.cmd` shim, and a shim hands its
    arguments to cmd.exe — which re-parses them, so a prompt containing `"` and `&`
    would run a shell command on the host (see proc_tree's batch-shim note). The npm
    shim only forwards to `node_modules/opencode-ai/bin/opencode.exe` (2.x:
    `node_modules/@opencode/cli/bin/opencode.exe`, verified on 2.2.5), a native
    binary, so launch that directly: same program, no cmd.exe. If the shim can't be
    resolved, proc_tree.check_args refuses a dangerous prompt instead of running it.
    """
    if os.path.sep in OPENCODE_BIN_ENV or os.path.isfile(OPENCODE_BIN_ENV):
        found = OPENCODE_BIN_ENV
    else:
        found = shutil.which(OPENCODE_BIN_ENV) or OPENCODE_BIN_ENV
    return proc_tree.npm_shim_target(found) or found


OPENCODE_BIN = _resolve_bin()

# Whether the user chose the binary (OPENCODE_BIN in the environment). A chosen
# path is never swapped for another one behind their back.
_BIN_EXPLICIT = bool(os.environ.get("OPENCODE_BIN"))


def _refresh_bin() -> str:
    """OPENCODE_BIN, re-resolved if the file it names is gone.

    The resolved path is the native binary INSIDE an npm package, so an upgrade
    that changes the package moves it: the owner's 1.x -> 2.x upgrade
    (`opencode-ai` -> `@opencode/cli`) deleted node_modules/opencode-ai/bin/
    opencode.exe while the server was running, and every later call failed to
    start. So when an absolute OPENCODE_BIN no longer exists, resolve once more
    through PATH. Not when the user set OPENCODE_BIN: their path stays, and
    unsupported_version_reason says it is gone. A bare name ("opencode", nothing
    found on PATH) is left alone; there is nothing to re-resolve from."""
    global OPENCODE_BIN
    current = OPENCODE_BIN
    if _BIN_EXPLICIT or not os.path.isabs(current) or os.path.exists(current):
        return current
    fresh = _resolve_bin()
    if fresh != current:
        logging.getLogger("agy_bridge").warning(
            "opencode: %s is gone (upgraded?); using %s", current, fresh
        )
        OPENCODE_BIN = fresh
    return OPENCODE_BIN


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

# Default per-call budget, and the floor the swarm raises a worker to. A CEILING,
# not an expected duration: on opencode 1.19.5 six free models answered a short
# prompt in 2.5-6.5 s (live). It stays at 300s rather than the other bridges' 180s
# because the free tier has been queue-scheduled before (1.18.x: 152-428 s for
# one-word answers), and a run that finishes early never notices a larger budget.
DEFAULT_TIMEOUT_S = 300

# Permission policies, keyed by sandbox mode, serialized into OPENCODE_PERMISSION.
# The keys and the three actions come from opencode's own PermissionConfig schema
# (read off the bundle): read, edit, glob, grep, list, bash, task,
# external_directory, todowrite, question, webfetch, websearch, lsp, doom_loop,
# skill — plus arbitrary extra keys — each "ask" | "allow" | "deny", or a
# {pattern: action} map. Remember that in headless run mode "ask" == reject, so
# "ask" fences a tool as firmly as "deny" does; read-only's bash is the one rule
# that has to use it (see there).
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
    # "ask", not "deny": a denied bash is left out of the request, and since early
    # October 2026 opencode's free tier refuses a request without it ("OpenCode's
    # free tier can only be used from within OpenCode"), on every CLI version, so
    # read-only failed every free-model call. Headless run auto-rejects an "ask",
    # verified live on 1.18.29 and 1.19.0: the model's bash call came back
    # "rejected permission to use this specific tool call" and wrote nothing.
    "bash": "ask",
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

# Re-exported so this module's own tests keep naming it. The definition (and the
# note on why it is a getattr) lives in proc_tree.
_KILL_SIGNAL = proc_tree.KILL_SIGNAL

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

    This bridge found the bug (Windows npm `.CMD` shim -> the real `opencode.exe`
    is a grandchild; a 270 s timeout measured returning after 396 s), so the
    implementation used to live here. It is now shared: issue #4 showed the other
    seven spawn paths had the identical defect, and one copy is the only way that
    stays fixed. The reasoning, both failure modes and the measurements are in
    proc_tree's module docstring; this name stays so the tests that monkeypatch it
    keep working.
    """
    proc_tree.kill_tree(proc)


# opencode 2.x (`@opencode/cli`, the same `opencode` command) changed how the fence
# is set, and the 1.x way fails OPEN on it. Verified live on 2.2.5 / Windows with
# free models:
#   - OPENCODE_PERMISSION is not read at all: with the 1.x read-only policy in it, a
#     shell write went through.
#   - The policy goes in OPENCODE_CONFIG_CONTENT as {"permissions": [rules]}, an
#     ORDERED array of {"action", "resource", "effect"} where the LAST matching rule
#     wins. Actions were renamed: shell (was bash), subagent (task), edit (also
#     covers write/patch), plus execute (Code Mode); doom_loop and lsp are gone.
#   - A run normally connects to a shared background service that ignores the
#     run's own env (OPENCODE_CONFIG_CONTENT with a read-only policy: the shell
#     write still went through). `--standalone` runs a private server that does
#     apply it, and leaves no service running on the user's machine. So every 2.x
#     run passes --standalone (build_args).
#   - "ask" is still auto-rejected headless. The free tier no longer needs shell
#     offered, but read-only keeps "ask" for it, the combination verified.
# .env files are kept out of reads, as on 1.x. Verified on 2.2.5 through this bridge:
# the read tool is refused .env and .env.local and allowed .env.example and other
# files, in both modes. In workspace-write the SHELL (allowed, unconfined) can still
# `cat` a .env: a .env.local leaked once that way, so only read-only protects them.
_V2_ENV_RULES = [
    {"action": "read", "resource": "*.env", "effect": "deny"},
    {"action": "read", "resource": "*.env.*", "effect": "deny"},
    {"action": "read", "resource": "*.env.example", "effect": "allow"},
]
# Verified: a shell write and an edit-tool write were both refused, no file created.
_V2_READ_ONLY_RULES = [
    {"action": "shell", "resource": "*", "effect": "ask"},
    {"action": "edit", "resource": "*", "effect": "deny"},
    {"action": "subagent", "resource": "*", "effect": "deny"},
    {"action": "external_directory", "resource": "*", "effect": "deny"},
    {"action": "webfetch", "resource": "*", "effect": "deny"},
    {"action": "websearch", "resource": "*", "effect": "deny"},
    # Code Mode. Without this a model called `execute` a dozen times (and still
    # wrote nothing); denying it is cheap insurance.
    {"action": "execute", "resource": "*", "effect": "deny"},
    *_V2_ENV_RULES,
]
# workspace-write on 2.x. The SHELL is NOT confined (verified live: on 2.x
# external_directory deny did not stop a shell redirect into the parent directory),
# so the mode is documented as unconfined and these rules only keep the EDIT tool
# in the workspace. Per opencode 2.2.5's source (core/src/file-access.ts,
# core/src/util/wildcard.ts), an edit inside the run directory is matched as a
# RELATIVE path with forward slashes ("sub/dir/f.txt"), one outside as "../..." or
# an absolute path, and `*` crosses "/", case-insensitively on Windows. So: allow
# edits, then deny the parent-relative and absolute forms. An earlier rule set
# allowed only "<absolute workspace>/*", which an inside edit never matches: it
# denied the edit tool everywhere, and the "inside write" seen live was the shell.
# Verified live on 2.2.5 (2026-11-09, free model, the write tool doing every write,
# shell denied for the test): sub/dir/nested.txt inside was written; ../x, an
# absolute C:/... path and a backslashed C:\... path outside were all refused, by
# the external_directory deny ("Permission denied: external_directory") before the
# edit denies were reached, so those stand as a second layer. A .env inside CAN be
# written: the env rules cover reads only. With the real rules (shell allowed) the
# write tool wrote a file in a subdirectory.
_V2_WORKSPACE_WRITE_RULES = [
    {"action": "shell", "resource": "*", "effect": "allow"},
    {"action": "edit", "resource": "*", "effect": "allow"},
    {"action": "edit", "resource": "../*", "effect": "deny"},
    {"action": "edit", "resource": "?:/*", "effect": "deny"},  # Windows drive paths
    {"action": "edit", "resource": "/*", "effect": "deny"},  # POSIX and UNC (//host/...)
    {"action": "external_directory", "resource": "*", "effect": "deny"},
    *_V2_ENV_RULES,
]

# The permission policies per opencode MAJOR version: a dict for 1.x's
# OPENCODE_PERMISSION, a rule list for 2.x's OPENCODE_CONFIG_CONTENT. A major
# missing here is REFUSED (check_supported_version) rather than run with a fence
# nobody has verified; supporting one is adding its entry, once verified live.
_PERMISSION_POLICIES: dict[int, dict[str, object]] = {
    1: {"read-only": _READ_ONLY_PERMISSION, "workspace-write": _WORKSPACE_WRITE_PERMISSION},
    2: {"read-only": _V2_READ_ONLY_RULES, "workspace-write": _V2_WORKSPACE_WRITE_RULES},
}

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

# `opencode --version`, cached per BINARY rather than per process: the owner upgraded
# opencode 1.x -> 2.x mid-session, and a process-long cache would have kept building
# 1.x argv (`--dir`, which 2.x rejects) or, on a future 2 -> 3, kept applying a
# fence nobody verified. The key is the binary's path, mtime and size (one stat per
# call), so an upgrade in place re-reads the version. A failed read, or a binary
# that can't be stat'ed, is not cached.
_VERSION_CACHE: Optional[tuple[tuple, str]] = None  # (binary key, version text)
_VERSION_LOCK = threading.Lock()
# A PINNED major that skips the read entirely (tests set it). None = read it.
_MAJOR_CACHE: Optional[int] = None


def _binary_key() -> Optional[tuple]:
    """(path, mtime_ns, size) of OPENCODE_BIN, or None if it can't be stat'ed.
    Re-resolves a binary an upgrade moved first (see _refresh_bin)."""
    path = _refresh_bin()
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (path, st.st_mtime_ns, st.st_size)


def parse_major(version_text: Optional[str]) -> Optional[int]:
    """The major of the first X.Y.Z in `version_text` ("opencode v2.2.5" -> 2)."""
    m = _VERSION_RE.search(version_text or "")
    return int(m.group(1)) if m else None


def opencode_major() -> Optional[int]:
    """The installed opencode's major version, or None if it can't be read.

    Re-read whenever the binary changes (see _VERSION_CACHE), unless pinned."""
    if _MAJOR_CACHE is not None:
        return _MAJOR_CACHE
    return parse_major(opencode_version())


def unsupported_version_reason(major: Optional[int]) -> Optional[str]:
    """Why the bridge won't run this opencode major, or None if it will."""
    if major is None and _BIN_EXPLICIT and not os.path.exists(OPENCODE_BIN):
        if os.path.isabs(OPENCODE_BIN) or os.path.sep in OPENCODE_BIN:
            return (
                f"OPENCODE_BIN points at {OPENCODE_BIN}, which does not exist (moved by an "
                "upgrade?). Update OPENCODE_BIN to the new path, or unset it to let the "
                "bridge find opencode on PATH."
            )
    if major is None:
        return (
            "could not read opencode's version (`opencode --version`), so the bridge "
            "cannot tell which permission settings fence it, and will not run it "
            "unfenced. Check that opencode is installed, or set OPENCODE_BIN."
        )
    if major not in _PERMISSION_POLICIES:
        known = ", ".join(f"{m}.x" for m in sorted(_PERMISSION_POLICIES))
        return (
            f"opencode {major}.x is not supported yet: its permission settings are "
            "unverified, so the bridge's sandbox fence might not apply, and it will not "
            f"run it unfenced. Supported: {known} (`npm i -g @opencode/cli` for 2.x)."
        )
    return None


def _diagnostic_prefix() -> list[str]:
    """Global flags for opencode's own subcommands (`models`, `auth list`).

    On 2.x those START the shared background service and leave it running on the
    user's machine (verified on 2.2.5: `opencode service status` went from stopped
    to http://127.0.0.1:28894 after either). The global `--standalone`, placed
    BEFORE the subcommand, avoids it; after the subcommand (`models --standalone`)
    it did not. `--version` starts nothing. 1.x has no service and no such flag."""
    return ["--standalone"] if (opencode_major() or 1) >= 2 else []


def check_supported_version() -> int:
    """The installed opencode's major, or raise RuntimeError if the bridge won't run it."""
    major = opencode_major()
    reason = unsupported_version_reason(major)
    if reason:
        raise RuntimeError(reason)
    return major


def _permission_policy(sandbox: str, major: int = 1):
    """The policy for `sandbox` on opencode `major`: 1.x's OPENCODE_PERMISSION dict,
    2.x's ordered rule list, or None for no policy at all (danger-full-access). A
    copy: callers may change it freely. No rule names the workspace: on 2.x an edit
    inside the run directory is matched as a relative path (see
    _V2_WORKSPACE_WRITE_RULES)."""
    validate_sandbox(sandbox)
    if sandbox == "danger-full-access":
        return None
    policy = _PERMISSION_POLICIES[major][sandbox]
    if isinstance(policy, dict):
        return dict(policy)
    return [dict(rule) for rule in policy]


def _config_content_with(inherited: Optional[str], rules: Optional[list]) -> Optional[str]:
    """OPENCODE_CONFIG_CONTENT for a 2.x run: the caller's own inline config, if it
    is a JSON object, with its permission keys replaced by `rules` (or removed, for
    no policy). None means "unset the variable"."""
    try:
        data = json.loads(inherited) if inherited else {}
    except ValueError:
        data = None
    if not isinstance(data, dict):
        if inherited and inherited.strip():
            # opencode also takes JSONC; strict JSON can't read comments or trailing
            # commas, and guessing at them could change what the user meant. Say so
            # rather than drop their settings silently.
            logging.getLogger("agy_bridge").warning(
                "opencode: OPENCODE_CONFIG_CONTENT is not a plain JSON object (JSONC?); "
                "it was replaced by the sandbox policy for this run, so its other "
                "settings do not apply to it"
            )
        data = {}
    data.pop("permission", None)  # the deprecated 1.x-style object key
    data.pop("permissions", None)
    if rules is not None:
        data["permissions"] = rules
    return json.dumps(data) if data else None


def _env(sandbox: Optional[str] = None, major: int = 1) -> dict:
    """Process env for opencode: auto-updater off, plus the permission policy.

    A CLI that updates itself mid-session is how this project has been broken
    before with no code change on our side (see the agy stdout saga), and opencode
    shipped an auto-updater. `OPENCODE_DISABLE_AUTOUPDATE=1` is its process-scoped off
    switch, read off the bundle's env table. 1.19.0 stopped installing updates on
    its own and the variable now only silences the update check; it stays set for
    the versions before.

    `sandbox` selects the OPENCODE_PERMISSION policy; None leaves the caller's
    environment untouched (used by the read-only diagnostics). json.dumps is not
    cosmetic here — see the module note on invalid JSON being silently ignored.

    On 2.x the policy goes in OPENCODE_CONFIG_CONTENT instead (see
    _V2_READ_ONLY_RULES), merged into any inline config the caller already set, and
    with its permission keys replaced, so an outer policy cannot loosen or tighten
    the mode. danger-full-access removes them and imposes nothing.
    """
    env = {**os.environ, "OPENCODE_DISABLE_AUTOUPDATE": "1"}
    if sandbox is None:
        return env
    policy = _permission_policy(sandbox, major)
    if major >= 2:
        content = _config_content_with(env.get("OPENCODE_CONFIG_CONTENT"), policy)
        if content is None:
            env.pop("OPENCODE_CONFIG_CONTENT", None)
        else:
            env["OPENCODE_CONFIG_CONTENT"] = content
        return env
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
        proc = proc_tree.run_captured(
            [_refresh_bin(), *_diagnostic_prefix(), "models"],
            starts_agent=False,  # a probe, not an agent run
            stdin=subprocess.DEVNULL,
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
    major: int = 1,
) -> list[str]:
    """argv for a headless `opencode run` (fresh, `-s <id>` resume, or `-c` continue).

    opencode 2.x (`major` >= 2) dropped `--dir` ("Unrecognized flag: --dir", exit 1,
    verified on 2.2.5): the run directory is the process cwd, which the caller
    already sets to the workspace. It gets `--standalone` instead, which is what
    makes opencode apply the run's own permission policy (see _V2_READ_ONLY_RULES).
    Verified on 2.2.5: `-s`, `-c`, `-m` and `--` behave as on 1.x, `-s`/`-c`
    included under --standalone. `--auto` and `--agent` are untested there.

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
    args = [OPENCODE_BIN, "run", "--format", "json"]
    args += ["--standalone"] if major >= 2 else ["--dir", workspace]
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
    elif etype == "tool_use":
        # A call the permission policy auto-rejected comes back as a finished tool
        # part in state "error" whose message says so (headless run never prompts).
        # Counted so an empty answer can name the fence instead of "no text".
        part = ev.get("part")
        tstate = part.get("state") if isinstance(part, dict) else None
        if isinstance(tstate, dict) and tstate.get("status") == "error":
            if _looks_rejected(str(tstate.get("error") or "")):
                state["rejected"] = state.get("rejected", 0) + 1
    elif etype == "error":
        err = ev.get("error")
        msg = ""
        if isinstance(err, dict):
            data = err.get("data")
            if isinstance(data, dict) and data.get("message"):
                msg = str(data["message"])
            elif err.get("message"):
                # 2.x: {"type": "provider.no-route", "message": "Model unavailable: ..."}
                msg = str(err["message"])
            else:
                msg = str(err.get("name") or err)
        elif err:
            msg = str(err)
        if msg.strip():
            state["error"] = msg.strip()


# How a call the policy fenced shows up: the tool part's error ("rejected
# permission to use this specific tool call" on 1.18.29 and 1.19.0; "This
# non-interactive run cannot ask the user for permission, so the request was
# rejected." on 2.2.5, both live) and run's own stderr line when it auto-rejects a
# prompt ("! permission requested: shell (...); auto-rejecting" on both majors).
# Matched loosely: the wording is not a contract.
_REJECTED_MARKERS = ("rejected permission", "auto-rejecting", "cannot ask the user for permission")


def _looks_rejected(text: str) -> bool:
    """True if `text` reads like opencode auto-rejecting a fenced permission."""
    low = (text or "").lower()
    return any(m in low for m in _REJECTED_MARKERS)


def _finish(
    state: dict, stderr: str, returncode: Optional[int], sandbox: Optional[str] = None
) -> str:
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
    if not answer and (state.get("rejected") or _looks_rejected(stderr)):
        n = state.get("rejected") or 0
        calls = f"{n} tool call(s)" if n else "a tool call"
        mode = f"the sandbox={sandbox!r}" if sandbox else "the sandbox"
        raise RuntimeError(
            f"opencode produced no answer: {calls} it attempted were blocked by "
            f"{mode} permission policy (headless runs auto-reject anything that would "
            "prompt), and the model stopped without replying. Ask for something that "
            "does not need those tools, or pick a sandbox that allows them "
            "(workspace-write lets it edit files and run shell commands in the "
            f"workspace). stderr: {(stderr or '')[-300:]}"
        )
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

    The default timeout is a 300s ceiling rather than the other bridges' 180s (see
    DEFAULT_TIMEOUT_S): the free models answer in seconds today, but have been
    queue-scheduled to minutes before, and that free path is what a fresh install
    hits. A run that outlives the budget is killed as a tree, which is why this
    shares the streaming implementation instead of calling
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
    major = check_supported_version()  # before anything runs: fail closed
    os.makedirs(workspace, exist_ok=True)
    resume_id, use_continue = _resume_flags(workspace, continue_conv)
    args = build_args(prompt, workspace, sandbox, model, resume_id, use_continue, agent, major)

    state: dict = {}
    proc = proc_tree.popen(
        args,
        cwd=workspace,  # also the run directory on 2.x, which has no --dir
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(sandbox, major),
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
        # Free models normally answer in seconds, so a timeout means a long task, a
        # busy free tier or a stuck run; the caller can rule out the first two.
        raise RuntimeError(
            f"opencode timed out after {timeout_s + 30}s. For a long task raise "
            f"timeout_s; if a short prompt timed out, the free `opencode/*` tier may "
            f"be congested: retry later, or use another model."
        )

    answer = _finish(state, "".join(err_chunks), proc.returncode, sandbox)
    if not continue_conv and pin and state.get("session_id"):
        _pin(workspace, state["session_id"])
    return answer


# ----------------------------------------------------------------- diagnostics
def opencode_version() -> Optional[str]:
    """`opencode --version` first line (e.g. "1.18.29", "opencode v2.2.5"), or None
    if it can't run. Cached per binary (see _VERSION_CACHE), so status_rows and the
    version gate share one read."""
    global _VERSION_CACHE
    key = _binary_key()
    with _VERSION_LOCK:
        if key is not None and _VERSION_CACHE is not None and _VERSION_CACHE[0] == key:
            return _VERSION_CACHE[1]
    text = _read_version()
    if text is not None and key is not None:
        with _VERSION_LOCK:
            _VERSION_CACHE = (key, text)
    return text


def _read_version() -> Optional[str]:
    """Run `opencode --version` (uncached)."""
    try:
        proc = proc_tree.run_captured(
            [OPENCODE_BIN, "--version"],
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

    2.x has no `providers list` (it prints usage and "Unexpected positional
    argument"); `opencode auth list` replaces it, see _auth_status_v2.
    """
    if (opencode_major() or 1) >= 2:
        return _auth_status_v2()
    try:
        proc = proc_tree.run_captured(
            [OPENCODE_BIN, "providers", "list"],
            starts_agent=False,  # a probe, not an agent run
            stdin=subprocess.DEVNULL,
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


def _auth_status_v2() -> tuple[bool, str]:
    """The credentials row on opencode 2.x, via `opencode auth list`. Free.

    Verified on 2.2.5 with nothing configured: it prints "No authenticated
    integrations". Its output with a key configured is unverified, so anything else
    is reported as-is (first line) rather than parsed into a count. As on 1.x, no
    credentials is fine while free models are listed."""
    try:
        proc = proc_tree.run_captured(
            [OPENCODE_BIN, *_diagnostic_prefix(), "auth", "list"],
            starts_agent=False,  # a probe, not an agent run
            stdin=subprocess.DEVNULL,
            timeout=30,
            env=_env(),
            **_TEXT,
            **_spawn_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return (False, "could not run `opencode auth list`")
    out = _strip_ansi((proc.stdout or "") + (proc.stderr or "")).strip()
    if not out:
        return (False, "`opencode auth list` printed nothing")
    if "no authenticated" in out.lower():
        return (
            bool(list_models()),
            "none — free `opencode/*` models only "
            "(run `opencode auth login` for Anthropic/OpenAI/…)",
        )
    return (True, out.splitlines()[0].strip()[:120])


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """`text` without ANSI escape sequences (opencode colours some of its output)."""
    return _ANSI_RE.sub("", text or "")


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
        reason = unsupported_version_reason(parse_major(ver))
        rows.append(("opencode CLI", reason is None, f"{ver} — {reason}" if reason else ver))

    ok, detail = auth_status()
    rows.append(("credentials", ok, detail))

    models = list_models()
    rows.append(("models", bool(models), ", ".join(models[:4]) if models else "none listed"))

    rows.append(("data dir", OPENCODE_DATA_HOME.exists(), str(OPENCODE_DATA_HOME)))

    with _PIN_LOCK:
        n_pins = len(_PINNED)
    rows.append(("pinned sessions", True, f"{n_pins} workspace(s) pinned this run"))

    return rows
