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
    proc = subprocess.Popen(
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
