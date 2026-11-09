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
import io
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import proc_tree
from conftest import HiddenEnv, fake_os_name, hide_env

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
        seen.update(hide_env(kwargs))
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


# ------------------------------------------------------------------- batch-file shims
#
# A .cmd/.bat target is run through cmd.exe, which re-parses every argument — so a
# prompt that closes a quote and adds `& <command>` runs that command on the host,
# before the agent or its sandbox starts. These pin both defences: the refusal in
# check_args, and resolving shims to the real executable.

INJECTION = 'summarise this" & echo INJECTED_MARKER & rem "'


def test_check_args_refuses_metacharacters_for_a_batch_shim():
    for bad in (INJECTION, "100%", "wow!", "a|b", "a > b", "a ^ b", "line1\nline2"):
        with pytest.raises(ValueError, match="batch-file shim"):
            proc_tree.check_args([r"C:\tools\agent.CMD", "-p", bad], windows=True)


def test_check_args_allows_plain_arguments_and_real_executables():
    proc_tree.check_args([r"C:\tools\agent.cmd", "--version", r"C:\work\repo"], windows=True)
    proc_tree.check_args([r"C:\tools\agent.exe", "-p", INJECTION], windows=True)  # no cmd.exe
    proc_tree.check_args([], windows=True)


def test_check_args_is_a_no_op_off_windows():
    proc_tree.check_args(["/usr/local/bin/agent.cmd", "-p", INJECTION], windows=False)


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe argument parsing is Windows-only")
def test_injection_through_a_real_shim_is_refused_before_anything_runs(tmp_path):
    """End to end on a real cmd.exe: the refusal happens, and nothing executed.

    The shim writes a marker if it ever runs at all, so this also proves the check
    fires before the spawn rather than after cmd.exe has already parsed the line.
    """
    marker = tmp_path / "ran.txt"
    shim = tmp_path / "agent.cmd"
    shim.write_text(f'@echo off\r\necho ran> "{marker}"\r\necho ARGS: %*\r\n', encoding="ascii")
    for spawn in (
        lambda: proc_tree.run_captured([str(shim), "-p", INJECTION], timeout=30),
        lambda: proc_tree.popen([str(shim), "-p", INJECTION], stdout=subprocess.PIPE),
    ):
        with pytest.raises(ValueError, match="batch-file shim"):
            spawn()
    assert not marker.exists()


def _write_npm_shim(tmp_path, target_line: str):
    shim = tmp_path / "opencode.cmd"
    shim.write_text(
        "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\n"
        f"SETLOCAL\r\nCALL :find_dp0\r\n{target_line}   %*\r\n",
        encoding="ascii",
    )
    return shim


def test_npm_shim_target_resolves_the_native_exe(tmp_path):
    exe = tmp_path / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    shim = _write_npm_shim(tmp_path, r'"%dp0%\node_modules\opencode-ai\bin\opencode.exe"')
    assert proc_tree.npm_shim_target(str(shim)) == str(exe)


def test_npm_shim_target_leaves_node_script_shims_and_missing_targets_alone(tmp_path):
    node_shim = _write_npm_shim(tmp_path, r'"%_prog%"  "%dp0%\node_modules\x\cli.js"')
    assert proc_tree.npm_shim_target(str(node_shim)) is None
    missing = _write_npm_shim(tmp_path, r'"%dp0%\node_modules\gone\bin\gone.exe"')
    assert proc_tree.npm_shim_target(str(missing)) is None
    assert proc_tree.npm_shim_target(str(tmp_path / "agent.exe")) is None


# npm's node-script shim, verbatim from @github/copilot 1.0.97's node_modules/.bin.
_NODE_SHIM = (
    "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\n"
    "SETLOCAL\r\nCALL :find_dp0\r\n\r\n"
    'IF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n) ELSE (\r\n'
    '  SET "_prog=node"\r\n  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n)\r\n\r\n'
    'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
    '"%dp0%\\..\\@github\\copilot\\npm-loader.js" %*\r\n'
)


def test_npm_shim_script_finds_the_script_and_target_ignores_the_interpreter(tmp_path):
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "copilot.cmd"
    shim.write_bytes(_NODE_SHIM.encode("ascii"))
    script = tmp_path / "node_modules" / "@github" / "copilot" / "npm-loader.js"
    script.parent.mkdir(parents=True)
    script.write_text("")
    # node.exe beside the shim, as in an nvm-windows global prefix: the IF EXIST
    # line quotes it, and it must not be taken for the program.
    (bin_dir / "node.exe").write_bytes(b"MZ")
    assert proc_tree.npm_shim_target(str(shim)) is None
    assert proc_tree.npm_shim_script(str(shim)) == str(script)


@pytest.mark.parametrize(
    "machine, archs",
    [("AMD64", ["x64", "arm64"]), ("ARM64", ["arm64", "x64"]), ("riscv64", ["x64", "arm64"])],
)
def test_npm_archs_puts_this_machine_first(monkeypatch, machine, archs):
    monkeypatch.setattr(proc_tree.platform, "machine", lambda: machine)
    assert proc_tree.npm_archs() == archs


def test_npm_shim_script_is_none_for_exe_shims_and_missing_scripts(tmp_path):
    exe_shim = _write_npm_shim(tmp_path, r'"%dp0%\node_modules\opencode-ai\bin\opencode.exe"')
    assert proc_tree.npm_shim_script(str(exe_shim)) is None
    gone = tmp_path / "gone.cmd"
    gone.write_bytes(_NODE_SHIM.encode("ascii"))  # its npm-loader.js doesn't exist
    assert proc_tree.npm_shim_script(str(gone)) is None


def test_no_module_calls_subprocess_popen_directly():
    """Every spawn must pass proc_tree.check_args, which lives in popen/run_captured.

    A direct subprocess.Popen skips it — which is exactly how the cursor and opencode
    bridges came to hand prompts to cmd.exe. Route new spawns through proc_tree.popen.
    """
    offenders = []
    for path in _cli_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "Popen":
                if isinstance(fn.value, ast.Name) and fn.value.id == "subprocess":
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, "use proc_tree.popen so check_args runs: " + ", ".join(offenders)


# ------------------------------------------------------------ conftest's tripwire


@pytest.mark.parametrize(
    "args",
    [
        ["codex", "exec", "hi"],
        ["C:\\Users\\x\\.local\\bin\\agy.exe", "-p", "hi"],
        "copilot -p hi",
        ["cmd", "/c", "cursor-agent.cmd", "-p", "hi"],
        # cursor-agent on Windows: no file in the argv is named after it
        [
            "C:\\Users\\x\\AppData\\Local\\cursor-agent\\versions\\2026.08.11-e8db854\\node.exe",
            "C:\\Users\\x\\AppData\\Local\\cursor-agent\\versions\\2026.08.11-e8db854\\index.js",
            "models",
        ],
        ["node", "C:/npm/opencode.js", "run"],
        ["muse-bin-1.3.0.exe", "exec"],
        ["C:/Program Files/Google/Chrome/Application/chrome.exe", "--app=http://x"],
        # Claude Code: every way it can be started
        ["claude", "-p", "hi"],
        ["C:\\Users\\x\\.local\\bin\\claude.exe", "-p", "hi"],
        ["/usr/local/bin/claude", "-p", "hi"],
        "claude -p hi",
        ["cmd", "/c", "C:\\Users\\x\\AppData\\Roaming\\npm\\claude.cmd", "-p", "hi"],
        [
            "C:\\Program Files\\nodejs\\node.exe",
            "C:\\Users\\x\\AppData\\Roaming\\npm\\node_modules\\@anthropic-ai\\claude-code\\cli.js",
            "-p",
            "hi",
        ],
        ["npx", "@anthropic-ai/claude-code", "-p", "hi"],
    ],
)
def test_the_tripwire_stops_agent_clis_and_browsers(args):
    import conftest

    try:
        with pytest.raises(RuntimeError, match="tried to start the real"):
            subprocess.Popen(args)
    finally:
        conftest._trips.clear()  # the attempt was the point; don't fail teardown


def test_the_tripwire_lets_other_programs_run():
    out = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
    assert out.stdout.strip() == "ok"


@pytest.mark.parametrize(
    "args",
    [
        # a directory named like a CLI doesn't block an ordinary program
        ["/home/u/codex/.venv/bin/python", "-c", "pass"],
        ["C:\\work\\opencode\\tool.exe", "--help"],
        # a launcher running something unrelated
        ["node", "C:/work/site/build.js"],
        # Claude Code's own config directory and plugin folder are not the CLI
        ["python", "C:/Users/x/.claude/hooks/ruff_check.py"],
        ["node", "C:/Users/x/.claude/hooks/check.js"],
        ["bash", "/home/x/.claude/scripts/run.sh"],
        ["bash", "plugin/.claude-plugin/build.sh"],
        ["python", "C:/work/claude-plugin-notes/x.py"],
        ["C:\\work\\claudette.exe", "--help"],
    ],
)
def test_the_tripwire_does_not_flag_look_alikes(args):
    import conftest

    assert conftest._forbidden(args) == ""


def test_the_tripwire_catches_an_attempt_the_code_swallowed():
    """Probes like _get_agy_version catch everything and report "not installed",
    so a raised error alone would let the test pass. The attempt is recorded too."""
    import conftest

    try:
        try:
            subprocess.Popen(["grok", "--version"])
        except Exception:  # noqa: BLE001 - swallowed, as the code under test would
            pass
        assert conftest._trips and conftest._trips[0].startswith("grok:")
    finally:
        conftest._trips.clear()


# ------------------------------------------------------------ nesting depth guard
# AGENT_INTERN_DEPTH: every child gets this server's depth + 1, and a server at
# MAX_DEPTH starts no AGENT RUN. Probes (`--version`, auth and model checks, opening
# a viewer) are exempt through starts_agent=False, so status and doctor keep working
# where delegation is refused. The guard is in proc_tree.popen, which every spawn in
# the project goes through, so the cross-backend tests below call each backend's real
# entry point against recorders that stand in for subprocess.Popen.
#
# These tests never put an environment into a message or a comparison that can print:
# a failing assertion shows what it compared, and a real environment holds tokens.
# They record argv and the depth variable's value, wrap whole environments in
# HiddenEnv, and report variable NAMES only.


class _Spawned(Exception):
    """Raised by the recorder right after it notes a spawn, to stop the run there."""


@pytest.fixture
def spawns(monkeypatch):
    """Replace subprocess.Popen with a recorder of (argv, env). Nothing ever starts.
    The env is a HiddenEnv: its repr shows no values."""
    seen = []

    def record(args, *a, **k):
        env = k.get("env")
        seen.append((list(args), None if env is None else HiddenEnv(env)))
        raise _Spawned

    monkeypatch.setattr(subprocess, "Popen", record)
    monkeypatch.delenv(proc_tree.DEPTH_ENV, raising=False)
    return seen


@pytest.fixture
def popen_calls(monkeypatch):
    """Wrap proc_tree.popen to note every call: its argv, whether it declared itself
    an agent run, and whether the nesting guard refused it."""
    calls = []
    real = proc_tree.popen

    def wrapper(args, *, starts_agent=True, **kwargs):
        entry = {"argv": list(args), "starts_agent": starts_agent, "refused": False}
        calls.append(entry)
        try:
            return real(args, starts_agent=starts_agent, **kwargs)
        except proc_tree.NestingTooDeep:
            entry["refused"] = True
            raise

    monkeypatch.setattr(proc_tree, "popen", wrapper)
    return calls


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 0),
        ("", 0),
        ("   ", 0),
        ("0", 0),
        ("1", 1),
        (" 2 ", 2),
        ("7", 7),
        ("-1", 0),
        ("-5", 0),
        ("two", 0),
        ("1.5", 0),
        ("1e3", 0),
    ],
)
def test_current_depth_reads_the_variable_and_falls_back_to_zero(raw, expected):
    env = {} if raw is None else {proc_tree.DEPTH_ENV: raw}
    assert proc_tree.current_depth(env) == expected


def test_current_depth_defaults_to_this_process_environment(monkeypatch):
    monkeypatch.setenv(proc_tree.DEPTH_ENV, "1")
    assert proc_tree.current_depth() == 1
    monkeypatch.delenv(proc_tree.DEPTH_ENV)
    assert proc_tree.current_depth() == 0


def test_the_limit_is_two_and_the_error_is_a_runtime_error():
    assert proc_tree.DEPTH_ENV == "AGENT_INTERN_DEPTH"
    assert proc_tree.MAX_DEPTH == 2
    assert issubclass(proc_tree.NestingTooDeep, RuntimeError)


def test_nesting_refusal_tells_the_model_to_do_the_step_itself():
    msg = proc_tree.nesting_refusal(2)
    assert "nested 2 agents deep" in msg and "AGENT_INTERN_DEPTH=2" in msg
    assert "limit 2" in msg
    assert "Do this step yourself instead of delegating it." in msg


def test_popen_gives_the_child_one_level_more(spawns):
    with pytest.raises(_Spawned):
        proc_tree.popen(["tool", "x"])
    assert spawns[0][1][proc_tree.DEPTH_ENV] == "1"


@pytest.mark.parametrize("depth", [0, 1])
def test_popen_increments_from_the_current_depth(spawns, monkeypatch, depth):
    monkeypatch.setenv(proc_tree.DEPTH_ENV, str(depth))
    with pytest.raises(_Spawned):
        proc_tree.popen(["tool", "x"])
    assert spawns[0][1][proc_tree.DEPTH_ENV] == str(depth + 1)


def test_popen_keeps_a_callers_env_and_sets_the_depth_in_a_copy(spawns, monkeypatch):
    monkeypatch.setenv(proc_tree.DEPTH_ENV, "1")
    mine = {"PATH": "/x", "KEEP": "me", proc_tree.DEPTH_ENV: "99"}
    with pytest.raises(_Spawned):
        proc_tree.popen(["tool"], env=mine)
    child_env = spawns[0][1]
    assert child_env["KEEP"] == "me" and child_env["PATH"] == "/x"
    # the depth is this server's, not whatever the caller's dict carried
    assert child_env[proc_tree.DEPTH_ENV] == "2"
    assert mine[proc_tree.DEPTH_ENV] == "99"  # the caller's dict is not touched


def test_popen_without_an_env_passes_this_processs_environment_on(spawns, monkeypatch):
    monkeypatch.setenv("SOME_MARKER_VAR", "present")
    with pytest.raises(_Spawned):
        proc_tree.popen(["tool"])
    assert spawns[0][1]["SOME_MARKER_VAR"] == "present"


@pytest.mark.parametrize("depth", ["2", "3", "99"])
def test_popen_at_the_limit_refuses_an_agent_run_and_starts_nothing(spawns, monkeypatch, depth):
    monkeypatch.setenv(proc_tree.DEPTH_ENV, depth)
    with pytest.raises(proc_tree.NestingTooDeep, match="nested .* agents deep"):
        proc_tree.popen(["tool", "x"])
    with pytest.raises(proc_tree.NestingTooDeep):
        proc_tree.popen(["tool", "x"], starts_agent=True)
    assert spawns == []


@pytest.mark.parametrize("depth", [2, 3, 99])
def test_popen_at_the_limit_still_runs_a_probe(spawns, monkeypatch, depth):
    monkeypatch.setenv(proc_tree.DEPTH_ENV, str(depth))
    with pytest.raises(_Spawned):  # reached subprocess.Popen: not refused
        proc_tree.popen(["tool", "--version"], starts_agent=False)
    assert spawns[0][0] == ["tool", "--version"]
    assert spawns[0][1][proc_tree.DEPTH_ENV] == str(depth + 1)  # the child is still counted


def test_a_probe_below_the_limit_is_counted_like_any_spawn(spawns, monkeypatch):
    monkeypatch.setenv(proc_tree.DEPTH_ENV, "1")
    with pytest.raises(_Spawned):
        proc_tree.popen(["tool"], starts_agent=False)
    assert spawns[0][1][proc_tree.DEPTH_ENV] == "2"


def test_the_default_is_guarded_so_a_forgotten_site_stays_safe(spawns, monkeypatch):
    monkeypatch.setenv(proc_tree.DEPTH_ENV, "2")
    with pytest.raises(proc_tree.NestingTooDeep):
        proc_tree.popen(["tool"])
    with pytest.raises(proc_tree.NestingTooDeep):
        proc_tree.run_captured(["tool"], timeout=5)
    assert spawns == []


def test_popen_checks_the_shim_rule_before_the_depth(spawns, monkeypatch):
    # the batch-file rule exists on Windows only; make proc_tree (and only proc_tree)
    # see Windows, so the test means the same thing on every OS
    fake_os_name(monkeypatch, proc_tree, "nt")
    monkeypatch.setenv(proc_tree.DEPTH_ENV, "2")
    with pytest.raises(Exception) as exc:
        proc_tree.popen(["C:\\tools\\evil.cmd", "a & calc"])
    assert not isinstance(exc.value, proc_tree.NestingTooDeep)  # check_args spoke first
    assert spawns == []


def test_run_captured_goes_through_the_guard(spawns, monkeypatch):
    with pytest.raises(_Spawned):
        proc_tree.run_captured(["tool"], timeout=5)
    assert spawns[0][1][proc_tree.DEPTH_ENV] == "1"
    monkeypatch.setenv(proc_tree.DEPTH_ENV, "2")
    spawns.clear()
    with pytest.raises(proc_tree.NestingTooDeep):
        proc_tree.run_captured(["tool"], timeout=5)
    assert spawns == []


def test_run_captured_passes_starts_agent_through(spawns, monkeypatch):
    monkeypatch.setenv(proc_tree.DEPTH_ENV, "2")
    with pytest.raises(_Spawned):
        proc_tree.run_captured(["tool", "--version"], timeout=5, starts_agent=False)
    assert spawns[0][0] == ["tool", "--version"]


def test_a_real_child_sees_the_incremented_depth(monkeypatch):
    # The seam the recorder cannot show: the variable really reaches a child process.
    monkeypatch.setenv(proc_tree.DEPTH_ENV, "1")
    child = proc_tree.popen(
        [sys.executable, "-c", f"import os; print(os.environ['{proc_tree.DEPTH_ENV}'])"],
        stdout=subprocess.PIPE,
        text=True,
    )
    out, _ = child.communicate(timeout=60)
    assert out.strip() == "2"


def test_a_real_probe_runs_at_the_limit_and_a_real_agent_run_does_not(monkeypatch):
    monkeypatch.setenv(proc_tree.DEPTH_ENV, "2")
    probe = proc_tree.run_captured(
        [sys.executable, "-c", "print('probe ran')"], timeout=60, starts_agent=False, text=True
    )
    assert probe.stdout.strip() == "probe ran"
    with pytest.raises(proc_tree.NestingTooDeep):
        proc_tree.run_captured([sys.executable, "-c", "print('agent ran')"], timeout=60)


# ---- every backend

_PROBE_SITES = {
    "claude_bridge": {"claude_version", "auth_status"},
    "codex_bridge": {"codex_version", "codex_login_status"},
    "copilot_bridge": {"copilot_version"},
    "cursor_bridge": {"list_models", "cursor_version", "auth_status"},
    "grok_bridge": {"list_models", "grok_version", "auth_status"},
    "kimi_bridge": {"kimi_version", "auth_status"},
    "muse_bridge": {"muse_version", "windows_sandbox_status"},
    "opencode_bridge": {"list_models", "_read_version", "auth_status", "_auth_status_v2"},
    "server": {"_get_agy_version", "_read_agy_usage", "list_agy_models"},
    "swarm": {"_probe_isolated_auth"},
    "swarm_watch": {"_launch"},
    "watch_server": {"_open_watch_window"},
}


def _probe_sites():
    """{module: {function}} for every call that passes starts_agent=False, found in
    the source, plus the lines of those calls."""
    found: dict[str, set[str]] = {}
    lines: list[tuple[str, int, int]] = []
    for path in sorted(REPO.glob("*.py")):
        if path.name.startswith("test_") or path.name == "conftest.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            flags = [
                k
                for k in node.keywords
                if k.arg == "starts_agent"
                and isinstance(k.value, ast.Constant)
                and k.value.value is False
            ]
            if not flags:
                continue
            owner = node
            while owner in parents and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = parents[owner]
            name = owner.name if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)) else "?"
            found.setdefault(path.stem, set()).add(name)
            lines.append((path.name, node.lineno, node.end_lineno or node.lineno))
    return found, lines


def test_exactly_these_call_sites_are_marked_as_probes():
    # Marking a spawn starts_agent=False exempts it from the nesting guard. Adding a
    # site is a deliberate edit with its own entry here, never a side effect.
    found, _ = _probe_sites()
    assert found == _PROBE_SITES
    assert sum(len(v) for v in found.values()) == 25


def test_every_probe_site_says_so_in_a_comment():
    _, lines = _probe_sites()
    for name, first, last in lines:
        src = (REPO / name).read_text(encoding="utf-8").splitlines()
        window = "\n".join(src[max(0, first - 3) : last + 2])
        assert "a probe, not an agent run" in window, f"{name}:{first} has no probe comment"


def test_no_agent_run_is_marked_as_a_probe():
    # The run paths must stay guarded: none of these functions may pass the flag.
    found, _ = _probe_sites()
    runners = {
        "claude_bridge": {"_run_impl", "run_claude", "run_claude_streaming"},
        "codex_bridge": {"run_codex", "run_codex_streaming"},
        "copilot_bridge": {"run_copilot"},
        "cursor_bridge": {"run_cursor", "create_chat"},
        "grok_bridge": {"run_grok"},
        "kimi_bridge": {"run_kimi"},
        "muse_bridge": {"run_muse", "last_session"},
        "opencode_bridge": {"_run_impl", "run_opencode", "run_opencode_streaming"},
        "server": {"_run_agy", "_run_agy_watched", "_run_agy_image_watched"},
        "swarm": {"_run_text_worker", "_run_image_worker", "_run_text_worker_watched"},
    }
    for module, funcs in runners.items():
        assert not funcs & found.get(module, set()), f"{module}: {funcs & found[module]}"


def _backend_entries(tmp_path):
    """(name, callable) for every code path that starts an agent CLI, called the way
    its tool layer calls it. Each returns, or raises, once its first spawn is
    refused or recorded."""
    import claude_bridge
    import codex_bridge
    import copilot_bridge
    import cursor_bridge
    import grok_bridge
    import kimi_bridge
    import muse_bridge
    import opencode_bridge
    import server
    import swarm

    ws = str(tmp_path / "ws")
    target = str(tmp_path / "img.png")
    return [
        ("codex", lambda: codex_bridge.run_codex("p", ws, "read-only", None, False, 5, pin=False)),
        (
            "codex-stream",
            lambda: codex_bridge.run_codex_streaming("p", ws, "read-only", None, False, 5),
        ),
        ("copilot", lambda: copilot_bridge.run_copilot("p", ws, "read-only", None, False, 5)),
        ("cursor", lambda: cursor_bridge.run_cursor("p", ws, "read-only", None, False, 5)),
        ("grok", lambda: grok_bridge.run_grok("p", ws, "read-only", None, False, 5)),
        ("kimi", lambda: kimi_bridge.run_kimi("p", ws, None, False, 5)),
        ("muse", lambda: muse_bridge.run_muse("p", ws, "read-only", None, False, 5)),
        ("opencode", lambda: opencode_bridge.run_opencode("p", ws, "read-only", None, False, 5)),
        ("claude", lambda: claude_bridge.run_claude("p", ws, "read-only", None, False, 5)),
        ("agy", lambda: server._run_agy("p", ws, False, 5, pin=False)),
        ("swarm-agy-text", lambda: swarm._run_text_worker(0, "p", ws, None, 5)),
        ("swarm-agy-image", lambda: swarm._run_image_worker(0, "p", target, ws, 5)),
    ]


_BACKEND_ENTRY_NAMES = [
    "codex",
    "codex-stream",
    "copilot",
    "cursor",
    "grok",
    "kimi",
    "muse",
    "opencode",
    "claude",
    "agy",
    "swarm-agy-text",
    "swarm-agy-image",
]


def _run_entry(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - the point is what was spawned, not how it ended
        return e


@pytest.fixture
def no_probe_caches(monkeypatch):
    """Version probes are cached per process; clear them so each entry point starts
    from nothing."""
    import claude_bridge
    import opencode_bridge

    monkeypatch.setattr(opencode_bridge, "_VERSION_CACHE", None)
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", None)
    monkeypatch.setattr(claude_bridge, "_VERSION_CACHE", None)


@pytest.fixture
def gates_pinned(monkeypatch):
    """Pin the version gates and agy's capability probes, so an agent run's own spawn is
    the first one it makes and nothing stops it before the guard does."""
    import claude_bridge
    import opencode_bridge
    import server
    import swarm

    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", 1)
    monkeypatch.setattr(claude_bridge, "claude_version", lambda: "2.1.319 (Claude Code)")
    monkeypatch.setattr(server, "_AGY_JSON_SUPPORT", False)
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", False)
    monkeypatch.setattr(swarm, "_ISOLATION_OK", True)


@pytest.mark.parametrize("name", _BACKEND_ENTRY_NAMES)
def test_every_backend_spawn_carries_the_incremented_depth(
    name, spawns, no_probe_caches, tmp_path, monkeypatch
):
    monkeypatch.setenv(proc_tree.DEPTH_ENV, "1")
    _run_entry(dict(_backend_entries(tmp_path))[name])
    assert spawns, f"{name}: no spawn was attempted: the entry point never reached proc_tree"
    for args, env in spawns:
        assert env is not None, f"{name}: spawned {args[:2]} without an explicit env"
        assert env[proc_tree.DEPTH_ENV] == "2", f"{name}: child depth is not parent + 1"


@pytest.mark.parametrize("name", _BACKEND_ENTRY_NAMES)
def test_every_backend_starts_at_depth_one_when_nothing_is_set(
    name, spawns, no_probe_caches, tmp_path
):
    _run_entry(dict(_backend_entries(tmp_path))[name])
    assert spawns
    assert all(env[proc_tree.DEPTH_ENV] == "1" for _, env in spawns)


@pytest.mark.parametrize("name", _BACKEND_ENTRY_NAMES)
def test_at_the_limit_nothing_that_reaches_a_process_is_an_agent_run(
    name, spawns, popen_calls, no_probe_caches, tmp_path, monkeypatch
):
    # Probes may spawn at the limit (a version gate, agy's capability check); an agent
    # run may not. Every argv that reached subprocess.Popen must belong to a call that
    # declared itself a probe.
    monkeypatch.setenv(proc_tree.DEPTH_ENV, "2")
    _run_entry(dict(_backend_entries(tmp_path))[name])
    probe_argvs = [c["argv"] for c in popen_calls if not c["starts_agent"]]
    for argv, _ in spawns:
        assert argv in probe_argvs, f"{name}: an agent run reached Popen: {argv[:3]}"
    assert all(c["refused"] for c in popen_calls if c["starts_agent"]), name


@pytest.mark.parametrize("name", _BACKEND_ENTRY_NAMES)
def test_at_the_limit_an_agent_run_is_refused_not_merely_never_reached(
    name, spawns, popen_calls, gates_pinned, tmp_path, monkeypatch
):
    # With the gates pinned the agent's own spawn is attempted, and the guard says no.
    monkeypatch.setenv(proc_tree.DEPTH_ENV, "2")
    _run_entry(dict(_backend_entries(tmp_path))[name])
    agent_calls = [c for c in popen_calls if c["starts_agent"]]
    assert agent_calls, f"{name}: no agent run was even attempted"
    assert all(c["refused"] for c in agent_calls), f"{name}: an agent run was not refused"
    probe_argvs = [c["argv"] for c in popen_calls if not c["starts_agent"]]
    assert all(argv in probe_argvs for argv, _ in spawns)


def test_the_entry_list_covers_every_backend_that_spawns_a_cli():
    # A new backend must be added to _backend_entries, or its spawns go untested.
    import server

    covered = {n.split("-")[0] for n in _BACKEND_ENTRY_NAMES}
    wanted = {"agy" if b == "antigravity" else b for b in server.BACKENDS}
    assert wanted <= covered | {"swarm"}, wanted - covered


@pytest.mark.parametrize(
    "name", ["codex", "copilot", "cursor", "grok", "kimi", "muse", "opencode", "claude"]
)
def test_a_bridge_run_at_the_limit_says_why(name, spawns, gates_pinned, tmp_path, monkeypatch):
    monkeypatch.setenv(proc_tree.DEPTH_ENV, "2")
    with pytest.raises(RuntimeError, match="nested 2 agents deep"):
        dict(_backend_entries(tmp_path))[name]()


def test_a_swarm_worker_at_the_limit_fails_in_place_with_the_reason(
    spawns, gates_pinned, tmp_path, monkeypatch
):
    import swarm

    monkeypatch.setenv(proc_tree.DEPTH_ENV, "2")
    res = swarm._run_text_worker(0, "p", str(tmp_path / "ws"), None, 5)
    assert res.ok is False and "nested 2 agents deep" in (res.error or "")


# ---- status and doctor keep working at the limit


class _ProbeProc:
    """A process that exits at once with a short, harmless answer, for the probes."""

    def __init__(self, args, **kwargs):
        self.args = args
        self.pid = -1
        self.returncode = 0
        self.stdout = io.StringIO("1.2.3\n")
        self.stderr = io.StringIO("")

    def communicate(self, timeout=None):
        return "1.2.3\n", ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


@pytest.fixture
def probes_at_the_limit(monkeypatch):
    """Depth 2, every probe answered by _ProbeProc, and a record of what was spawned."""
    import claude_bridge
    import opencode_bridge

    spawned = []

    def fake(args, *a, **k):
        spawned.append(list(args))
        return _ProbeProc(args, **k)

    monkeypatch.setattr(subprocess, "Popen", fake)
    monkeypatch.setenv(proc_tree.DEPTH_ENV, "2")
    monkeypatch.setenv("AGY_BRIDGE_NO_UPDATE_CHECK", "1")
    for module in (claude_bridge, opencode_bridge):
        monkeypatch.setattr(module, "_VERSION_CACHE", None)
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", None)
    return spawned


def _status_entries():
    import claude_bridge
    import codex_bridge
    import copilot_bridge
    import cursor_bridge
    import grok_bridge
    import kimi_bridge
    import muse_bridge
    import opencode_bridge
    import server

    return {
        "codex": codex_bridge.status_rows,
        "copilot": copilot_bridge.status_rows,
        "cursor": cursor_bridge.status_rows,
        "grok": grok_bridge.status_rows,
        "kimi": kimi_bridge.status_rows,
        "muse": muse_bridge.status_rows,
        "opencode": opencode_bridge.status_rows,
        "claude": claude_bridge.status_rows,
        "antigravity": server._agy_status_rows,
    }


@pytest.mark.parametrize(
    "name",
    ["codex", "copilot", "cursor", "grok", "kimi", "muse", "opencode", "claude", "antigravity"],
)
def test_every_backends_status_rows_work_at_the_limit(name, probes_at_the_limit):
    rows = _status_entries()[name]()
    assert rows and all(len(r) == 3 and isinstance(r[1], bool) for r in rows)
    assert probes_at_the_limit, f"{name}: no probe ran at the limit"


def test_the_bridge_rows_show_the_nesting_row_without_hiding_the_others(probes_at_the_limit):
    import server

    rows = server._bridge_status_rows()
    labels = [r[0] for r in rows]
    assert labels[0] == "bridge version" and labels[-1] == "nesting"
    nesting = rows[-1]
    assert nesting[1] is False  # shown as a warning
    assert nesting[2].startswith("delegation from here is refused")
    assert "2 agents deep" in nesting[2] and "AGENT_INTERN_DEPTH=2" in nesting[2]
    assert nesting[2].endswith("the checks below still run")


def test_doctor_works_at_the_limit_and_shows_the_nesting_row(probes_at_the_limit, monkeypatch):
    import doctor
    import server

    # doctor.report asks the platform module for the OS name, which runs `uname` on
    # Linux: not a probe of ours, so keep the fake Popen out of it
    monkeypatch.setattr(doctor.platform, "platform", lambda *a, **k: "test-platform")
    bridge_rows, per_backend = doctor.collect(server.ENABLED_BACKENDS)
    data = doctor.report(bridge_rows, per_backend)
    assert any(r["label"] == "nesting" and r["ok"] is False for r in data["bridge"])
    for backend in data["backends"]:
        crashed = [r for r in backend["rows"] if r["label"] == f"{backend['name']} status"]
        assert not crashed, f"{backend['name']}: its status check crashed at the limit"
    text = doctor.render(data)
    assert "delegation from here is refused" in text and "status check crashed" not in text


def test_the_status_tools_work_at_the_limit(probes_at_the_limit):
    import claude_tools

    out = claude_tools.claude_status()
    assert "nesting" in out and "claude CLI" in out
    assert "Overall: PROBLEMS FOUND" in out  # the nesting row is a warning, not an error
