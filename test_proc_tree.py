"""Offline unit tests for proc_tree.py — the shared timeout/process-tree kill.

Three layers, because this module exists to fix a bug that a green suite happily
shipped for seven of the eight bridges:

1. Unit tests for `kill_tree` on both platform branches (both run everywhere —
   `os.name` is monkeypatched, so CI does not need three OSes to cover them).
2. `run_captured`'s contract, including the ORDERING that is the actual fix:
   the pipes are read only after the tree is dead.
3. A real end-to-end reproduction of issue #4's failure shape — a live process
   that spawns a live grandchild outliving the deadline — plus two source-level
   guards so the other bridges cannot drift back to the broken shape.

    pytest test_proc_tree.py
"""

import ast
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import proc_tree

REPO = Path(__file__).parent


class _Proc:
    """Popen stand-in that records what was done to it."""

    returncode = 0

    def __init__(self, pid=4242, communicate=None):
        self.pid = pid
        self.killed = False
        self._communicate = communicate

    def kill(self):
        self.killed = True

    def communicate(self, timeout=None):
        if self._communicate is None:
            return ("", "")
        return self._communicate(self, timeout)


# --------------------------------------------------------------- kill_tree
def test_kill_tree_walks_the_tree_with_taskkill_on_windows(monkeypatch):
    """Windows has no process group here — /T is the only thing reaching the shim's child."""
    calls = []
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(subprocess, "run", lambda args, **k: calls.append(args))
    proc = _Proc(pid=4242)

    proc_tree.kill_tree(proc)

    assert calls[0][:4] == ["taskkill", "/F", "/T", "/PID"]
    assert calls[0][4] == "4242"
    assert proc.killed  # the plain kill always runs too


def test_kill_tree_signals_the_whole_process_group_on_posix(monkeypatch):
    """killpg, NOT kill: the compiled binary is a separate PID in the same group."""
    signalled = []
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(
        os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)), raising=False
    )
    proc = _Proc(pid=4242)

    proc_tree.kill_tree(proc)

    assert signalled == [(4242, proc_tree.KILL_SIGNAL)]
    assert proc.killed


def test_kill_tree_survives_an_already_reaped_process(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(
        os, "getpgid", lambda pid: (_ for _ in ()).throw(OSError("gone")), raising=False
    )

    class _Gone(_Proc):
        def kill(self):
            raise OSError("no such process")

    proc_tree.kill_tree(_Gone())  # must not raise


def test_kill_tree_survives_taskkill_being_unavailable(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no taskkill"))
    )
    proc = _Proc()

    proc_tree.kill_tree(proc)

    assert proc.killed  # still falls through to the one kill we can trust


# ------------------------------------------------------------ run_captured
def test_run_captured_returns_a_completed_process(monkeypatch):
    def _popen(args, **kwargs):
        p = _Proc(communicate=lambda p, t: ("out", "err"))
        p.returncode = 3
        return p

    monkeypatch.setattr(subprocess, "Popen", _popen)

    got = proc_tree.run_captured(["cli", "--version"], timeout=5)

    assert isinstance(got, subprocess.CompletedProcess)
    assert (got.returncode, got.stdout, got.stderr) == (3, "out", "err")
    assert got.args == ["cli", "--version"]


def test_run_captured_forwards_popen_kwargs_but_pipes_the_streams(monkeypatch):
    seen = {}

    def _popen(args, **kwargs):
        seen.update(kwargs)
        return _Proc(communicate=lambda p, t: ("", ""))

    monkeypatch.setattr(subprocess, "Popen", _popen)

    proc_tree.run_captured(["cli"], timeout=5, cwd="/ws", start_new_session=True)

    assert seen["cwd"] == "/ws"
    assert seen["start_new_session"] is True  # the group killpg later needs
    assert seen["stdout"] is subprocess.PIPE and seen["stderr"] is subprocess.PIPE


def test_run_captured_kills_the_tree_and_reraises_on_timeout(monkeypatch):
    killed = []

    def _hang(p, timeout):
        if not killed:
            raise subprocess.TimeoutExpired("cli", timeout or 0)
        return ("partial", "")

    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: _Proc(communicate=_hang))
    monkeypatch.setattr(proc_tree, "kill_tree", lambda p: killed.append(p))

    with pytest.raises(subprocess.TimeoutExpired) as exc:
        proc_tree.run_captured(["cli"], timeout=1)

    assert len(killed) == 1
    assert exc.value.output == "partial"  # whatever the tree already wrote survives


def test_run_captured_reads_the_pipes_only_after_the_tree_is_dead(monkeypatch):
    """The ordering IS the fix. Reading first is the read that never returns."""
    order = []

    def _communicate(p, timeout):
        order.append("read")
        if order.count("read") == 1:
            raise subprocess.TimeoutExpired("cli", timeout or 0)
        return ("", "")

    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: _Proc(communicate=_communicate))
    monkeypatch.setattr(proc_tree, "kill_tree", lambda p: order.append("kill"))

    with pytest.raises(subprocess.TimeoutExpired):
        proc_tree.run_captured(["cli"], timeout=1)

    assert order == ["read", "kill", "read"]


def test_run_captured_does_not_hang_if_a_survivor_still_holds_a_pipe(monkeypatch):
    """Belt and braces: an unkillable holder costs us REAP_GRACE_S, not forever."""

    def _always_times_out(p, timeout):
        raise subprocess.TimeoutExpired("cli", timeout or 0)

    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: _Proc(communicate=_always_times_out))
    monkeypatch.setattr(proc_tree, "kill_tree", lambda p: None)

    with pytest.raises(subprocess.TimeoutExpired) as exc:
        proc_tree.run_captured(["cli"], timeout=1)

    assert exc.value.output is None  # reported without partial output rather than blocking


# ------------------------------------------- the real thing (live processes)
# A parent that leaves a grandchild behind and then refuses to exit — the exact
# shape reported in issue #4 (shell wrapper -> CLI entrypoint -> real binary) and
# the one measured in 0.30.0 on Windows (npm .CMD shim -> opencode.exe).
_LINGERING_PARENT = "\n".join(
    [
        "import subprocess, sys, time",
        "marker, python = sys.argv[1], sys.executable",
        'child = \'import time,sys; time.sleep(4); open(sys.argv[1], "w").write("alive")\'',
        "subprocess.Popen([python, '-c', child, marker])",
        "time.sleep(120)  # outlives any deadline the test sets",
        "",
    ]
)


def _spawn_kwargs() -> dict:
    """What every bridge passes, so killpg has a group to signal."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {"start_new_session": True}


def test_timeout_actually_reaps_a_live_grandchild(tmp_path):
    """The regression, end to end: the orphan must be dead, not merely disowned.

    Before this fix the grandchild survived the deadline and went on to write the
    marker — on POSIX silently burning the user's quota, on Windows also holding
    our stdout pipe open so the call never returned at all.
    """
    parent = tmp_path / "lingering_parent.py"
    parent.write_text(_LINGERING_PARENT, encoding="utf-8")
    marker = tmp_path / "grandchild_survived.txt"

    started = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        proc_tree.run_captured(
            [sys.executable, str(parent), str(marker)],
            timeout=1.5,
            stdin=subprocess.DEVNULL,
            **_spawn_kwargs(),
        )
    elapsed = time.time() - started
    assert elapsed < 20, f"the deadline must be honoured, took {elapsed:.1f}s"

    # Outlast the grandchild's own sleep: if it is still alive it writes the marker.
    time.sleep(5)
    assert not marker.exists(), "the grandchild outlived the timeout — the tree was not reaped"


# ------------------------------------------------------------------- guards
def _cli_modules() -> list[Path]:
    """Every module in the repo root that may spawn a bridged CLI."""
    return [
        p
        for p in sorted(REPO.glob("*.py"))
        if not p.name.startswith("test_") and p.name != "proc_tree.py"
    ]


def test_no_module_calls_subprocess_run_with_a_timeout():
    """`subprocess.run(timeout=...)` kills one PID. Every CLI path must use run_captured.

    This is the guard, not the fix: issue #4 existed because the 0.30.0 fix was
    applied to the one bridge that found the bug and nowhere else. A new bridge
    that reaches for subprocess.run now fails the build.
    """
    offenders = []
    for path in _cli_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "run"):
                continue
            if not (isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"):
                continue
            if any(kw.arg == "timeout" for kw in node.keywords):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "use proc_tree.run_captured instead of subprocess.run(timeout=...): " + ", ".join(offenders)
    )


def test_every_run_captured_call_passes_spawn_kwargs():
    """Not style — a safety property, and the sharpest edge on this whole change.

    `kill_tree`'s POSIX branch signals `os.getpgid(proc.pid)`. A child spawned
    WITHOUT `start_new_session=True` is still in OUR process group, so that
    killpg would take out the MCP server itself (and, in the suite, the pytest
    process running this file) instead of the CLI. Every call site must therefore
    carry the bridge's `_spawn_kwargs()`.
    """
    offenders = []
    for path in _cli_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "run_captured"):
                continue
            starred = [ast.unparse(k.value) for k in node.keywords if k.arg is None]
            if not any("_spawn_kwargs" in s for s in starred):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "run_captured needs **_spawn_kwargs() or killpg will signal our own group: "
        + ", ".join(offenders)
    )


def test_no_module_kills_a_bare_process_on_timeout():
    """A `proc.kill()` anywhere in a bridge is the single-PID bug by another name.

    The streaming and watch paths call kill directly rather than going through
    run_captured, so the AST guard above cannot see them; this one can. proc_tree
    is excluded because its own final `proc.kill()` is the trusted fallback.
    """
    offenders = []
    for path in _cli_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "kill":
                if isinstance(fn.value, ast.Name) and fn.value.id == "proc":
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, "call proc_tree.kill_tree(proc) so the grandchild dies too: " + ", ".join(
        offenders
    )
