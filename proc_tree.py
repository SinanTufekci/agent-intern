"""Kill a spawned CLI *and everything it spawned* when its timeout fires.

Every backend this server bridges runs its real work one or two process
generations below the PID we hold: on Windows the thing on `PATH` is an npm
`.CMD` shim, so `CreateProcess` runs `cmd.exe` and the actual binary is a
GRANDchild; on POSIX the CLI's node entrypoint execs a compiled binary, and any
`bash`-style tool the agent itself uses adds another generation. Killing only the
direct child therefore does not stop the work — it orphans it.

Two distinct failure modes follow, and both were observed live rather than
reasoned about:

* **Windows: the call never returns.** The orphan still holds the write end of
  our stdout pipe, and `subprocess.run(timeout=...)` calls `communicate()` a
  second time after killing — re-reading a pipe nobody will ever close. Measured
  in the opencode bridge: a 270 s timeout returned after **396 s**, and only
  because the orphan was killed by hand.
* **POSIX: the deadline silently means nothing.** `subprocess.run` reaps the
  direct child and raises on schedule, so the caller gets an answer — but the
  real worker keeps running, burning the user's quota with no way to ever
  collect a result. Reported from macOS in issue #4: one `codex_ask` left a
  `zsh -c` wrapper, the node `codex` entrypoint and the compiled
  `aarch64-apple-darwin` binary all alive well past `timeout_s`, and all three
  had to be `kill -9`'d by hand.

The opencode bridge fixed this for itself in 0.30.0. Everything here is that
fix, hoisted out so the other seven spawn paths cannot drift back — a `killpg`-
only helper would have left the Windows hang, which is this project's primary
platform and its worse failure mode, completely unaddressed.
"""

from __future__ import annotations

import os
import platform
import re
import signal
import subprocess
from typing import Optional

# SIGKILL is POSIX-only. The getattr keeps this module importable on Windows
# (which has no SIGKILL) so the POSIX branch stays unit-testable there rather
# than only on the platforms that execute it. SIGTERM is never the intent and
# never runs where SIGKILL exists.
KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)

# How long to wait for a tree we have just SIGKILLed to actually be gone. This
# is a bound, not a requirement: if a survivor still holds a pipe we return
# anyway rather than inheriting its lifetime — the whole point is that the
# caller's deadline is real.
REAP_GRACE_S = 15


def no_window_kwargs() -> dict:
    """Keep the `taskkill` helper process from flashing a console on Windows."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill `proc` and every process it spawned. Never raises.

    `taskkill /T` walks the tree on Windows, which is the only thing that reaches
    the npm shim's grandchild. Elsewhere we signal the process group that each
    bridge's `_spawn_kwargs()` created with `start_new_session=True`, which
    catches the CLI's own compiled binary and anything its tools left behind.
    Both paths finish with a plain `proc.kill()`, which is the only one that can
    be trusted to have run at all.

    ⚠️ The POSIX branch assumes `proc` was spawned with `start_new_session=True`.
    A child spawned without it is still in OUR process group, and `killpg` would
    then take out this server rather than the CLI. Every call site passes the
    bridge's `_spawn_kwargs()`; a test guards that they keep doing so.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=REAP_GRACE_S,
                **no_window_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            pass  # fall through to the plain kill below
    else:
        try:
            os.killpg(os.getpgid(proc.pid), KILL_SIGNAL)
        except (OSError, AttributeError):
            pass  # already reaped, or no process groups here
    try:
        proc.kill()
    except OSError:
        pass


# ----------------------------------------------------------------- Windows batch shims
#
# A `.cmd`/`.bat` file cannot be executed directly: `CreateProcess` runs it as
# `cmd.exe /c "<file> <args>"`, so every argument is re-parsed by cmd.exe — with
# `%var%` expansion, `!var!` expansion when the shim enables delayed expansion, and
# `&`/`|`/`<`/`>` as command separators. Python's `list2cmdline` quotes for the
# MSVCRT parser, not for cmd.exe, and cmd.exe does not honour its backslash-escaped
# quotes. So an argument such as `x" & calc & "` closes the quote early and the rest
# RUNS AS A COMMAND — verified live against a shim. The prompt is exactly such an
# argument, and it can carry text Claude read from an untrusted file or web page.
# That escapes every `sandbox` setting: the command runs in the shim's cmd.exe, on
# the host, before the agent (and its sandbox) ever starts.
#
# Correct escaping for cmd.exe is possible only for some inputs (Rust's fix for the
# same bug, CVE-2024-24576, refuses the rest). Two defences instead:
#   1. each bridge resolves its shim to the real executable where it can
#      (`npm_shim_target` below; cursor and muse have their own launch paths), so
#      cmd.exe is never involved;
#   2. `check_args` refuses to hand any metacharacter to a shim that is still left,
#      turning a would-be injection into a clear error. Every spawn goes through
#      `run_captured` / `popen` below, which call it — a test guards that.
BATCH_SUFFIXES = (".cmd", ".bat")
CMD_METACHARS = frozenset('"%!^&|<>\r\n')


def is_batch_file(executable: str) -> bool:
    """True when `executable` is a .cmd/.bat file, which Windows runs via cmd.exe."""
    return str(executable).lower().endswith(BATCH_SUFFIXES)


def check_args(args: list[str], windows: Optional[bool] = None) -> None:
    """Refuse to pass cmd.exe metacharacters to a batch-file shim. Never mutates.

    A no-op off Windows and for real executables. Raises ValueError naming the
    offending characters, so the caller fails loudly instead of running whatever the
    argument smuggled in. See the note above for why escaping is not attempted.
    `windows` defaults to the real platform; tests pass it rather than faking os.name.
    """
    if windows is None:
        windows = os.name == "nt"
    if not windows or not args or not is_batch_file(args[0]):
        return
    for arg in args[1:]:
        bad = sorted(set(str(arg)) & CMD_METACHARS)
        if bad:
            shown = ", ".join(repr(c) for c in bad)
            raise ValueError(
                f"refusing to run {args[0]!r}: it is a batch-file shim, which Windows runs "
                f"through cmd.exe, and an argument contains characters cmd.exe would "
                f"interpret ({shown}) — that is how a prompt injects a shell command. "
                f"Point the backend's *_BIN env var at the real executable instead."
            )


def _shim_launch(shim: str) -> list[str]:
    """The quoted tokens of the line an npm cmd-shim launches its program on.

    npm's shims end in that line, the one that forwards `%*`. A token spelled from
    `%dp0%` (the shim's own directory) comes back as an absolute path; any other
    token, such as `%_prog%`, comes back as written. [] when `shim` isn't a
    readable .cmd/.bat or has no such line.

    Only that line is read, never the rest: a node-script shim also quotes
    `"%dp0%\\node.exe"` in the `IF EXIST` that picks the interpreter, and where
    node.exe sits next to the shim (nvm-windows, the nodejs folder as the global
    prefix) that would read as the program itself.
    """
    if not is_batch_file(shim):
        return []
    try:
        with open(shim, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    launch = next((ln for ln in reversed(lines) if ln.rstrip().endswith("%*")), "")
    base = os.path.dirname(os.path.abspath(shim))
    tokens = []
    for quoted in re.findall(r'"([^"]+)"', launch):
        for prefix in ("%dp0%", "%~dp0"):
            if quoted.lower().startswith(prefix):
                # The shim spells its path with backslashes; "/" joins correctly on
                # every OS, so the resolution is testable off Windows too.
                rel = quoted[len(prefix) :].replace("\\", "/").lstrip("/")
                quoted = os.path.normpath(os.path.join(base, rel))
                break
        tokens.append(quoted)
    return tokens


def npm_shim_target(shim: str) -> Optional[str]:
    """The native .exe an npm cmd-shim launches, or None if it isn't one.

    npm's generated shims end in a line like
        "%dp0%\\node_modules\\opencode-ai\\bin\\opencode.exe"   %*
    where `%dp0%` is the shim's own directory. When the target is a real `.exe`,
    launching it directly behaves identically minus cmd.exe — so the prompt never
    meets cmd's parser. A shim that runs `node script.js` gives None here; see
    npm_shim_script, and each bridge's own knowledge of what its script launches.
    """
    tokens = _shim_launch(shim)
    if tokens and tokens[0].lower().endswith(".exe") and os.path.isfile(tokens[0]):
        return tokens[0]
    return None


def npm_archs() -> list[str]:
    """CPU names in node's `process.arch` spelling, this machine's first.

    npm installs the platform package for the arch of the NODE it ran under, which
    on Windows on Arm may be x64 under emulation, so a caller looking for that
    package should try every name in turn rather than only the native one.
    """
    names = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}
    native = names.get(platform.machine().lower())
    return [native] + [a for a in ("x64", "arm64") if a != native] if native else ["x64", "arm64"]


def npm_shim_script(shim: str) -> Optional[str]:
    """The .js file an npm cmd-shim runs with node, or None if it isn't one.

    Such a shim ends in
        "%_prog%"  "%dp0%\\..\\@github\\copilot\\npm-loader.js" %*
    Where the script only finds and starts a native binary (codex's and copilot's
    do), the bridge can start that binary itself and leave cmd.exe out. Which
    binary, and how to start it, is the bridge's to say, since npm can't.
    """
    tokens = _shim_launch(shim)
    if len(tokens) >= 2 and tokens[0].lower() == "%_prog%":
        script = tokens[1]
        if script.lower().endswith((".js", ".mjs", ".cjs")) and os.path.isfile(script):
            return script
    return None


def popen(args: list[str], **popen_kwargs) -> subprocess.Popen:
    """`subprocess.Popen` behind the batch-shim check. Use this, never Popen directly."""
    check_args(args)
    return subprocess.Popen(args, **popen_kwargs)


def run_captured(
    args: list[str],
    *,
    timeout: float,
    **popen_kwargs,
) -> subprocess.CompletedProcess:
    """`subprocess.run(capture_output=True, timeout=...)` that honours its deadline.

    A drop-in for the blocking bridge calls: stdout and stderr are captured for
    you (do not pass `capture_output`), and the result is a real
    `CompletedProcess`, so `.returncode` / `.stdout` / `.stderr` downstream are
    unchanged.

    The difference is what happens on timeout. `subprocess.TimeoutExpired` is
    raised exactly as `subprocess.run` would raise it — but the process TREE is
    reaped first, so the grandchild can neither outlive the deadline nor hold our
    pipes open. Reading the pipes only *after* the tree is dead is what makes the
    second read terminate; that ordering is the fix, not a detail of it.
    """
    proc = popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **popen_kwargs,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out, err = _drain(proc)
        raise subprocess.TimeoutExpired(args, timeout, output=out, stderr=err) from None
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def _drain(proc: subprocess.Popen) -> tuple[Optional[str], Optional[str]]:
    """Collect whatever the killed tree already wrote, without blocking forever."""
    try:
        return proc.communicate(timeout=REAP_GRACE_S)
    except subprocess.TimeoutExpired:
        # Something is still holding a pipe despite the kill. Report the timeout
        # with no partial output rather than hanging on it — an incomplete
        # diagnostic beats an unbounded wait.
        return None, None
