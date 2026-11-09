"""Shared pytest setup: a tripwire against unit tests starting real programs.

The unit tests fake every agent CLI and never open a browser. A test that stops
faking, typically because the code it covers moved to another module while the
test still patches the old name, would instead start the real CLI (spending the
user's quota, or hanging on a login prompt) or open a real browser window, and it
could still pass. The tripwire turns that into a failure.

It fails the test even when the code under test swallows the error (several
probes catch everything and report "not installed"): each attempt is recorded, and
the test fails at teardown if there was one.

A test that runs a real CLI on purpose is marked @pytest.mark.real_cli.
"""

from __future__ import annotations

import os
import re
import subprocess
import webbrowser

import pytest

# server reads this at import and drops the tools of every backend it leaves out,
# so a developer who set it for their own Claude Code would see tests fail for
# tools that are simply not registered. Pytest loads this file before any test
# module imports server.
os.environ.pop("AGENT_INTERN_BACKENDS", None)
# Two more variables a developer's own session sets and a test must not inherit.
# CLAUDECODE=1 (set by Claude Code in every process it starts) makes server hide the
# claude backend at import, so the same suite would register different tools inside
# and outside Claude Code. AGENT_INTERN_DEPTH is the nesting counter: a test run from
# inside a delegated agent would otherwise start at depth >= 1 and hit the guard.
os.environ.pop("CLAUDECODE", None)
os.environ.pop("AGENT_INTERN_DEPTH", None)
# The rest of what Claude Code exports to its children (the session id, the
# cross-session messaging socket and token, AI_AGENT, ...) and the settings that
# move the claude backend's binary or config home: none may reach a test.
for _var in [
    v
    for v in os.environ
    if v.startswith("CLAUDE_CODE_")
    or v in ("CLAUDE_PID", "AI_AGENT", "CLAUDE_CONFIG_DIR", "CLAUDE_BIN")
]:
    os.environ.pop(_var, None)

# The bridge's own switches, and the variables that point each backend at a binary or a
# home. A developer who exported one (AGY_BRIDGE_NO_UPDATE_CHECK while trying a live
# run, say) would see tests that expect the default fail, and only some of the time.
# A test that wants one sets it with monkeypatch.
for _var in [
    v
    for v in os.environ
    if v.startswith("AGY_BRIDGE_")
    or v
    in (
        "AGY_BIN",
        "CODEX_BIN",
        "CODEX_HOME",
        "COPILOT_BIN",
        "CURSOR_BIN",
        "GROK_BIN",
        "KIMI_BIN",
        "MUSE_BIN",
        "OPENCODE_BIN",
    )
]:
    os.environ.pop(_var, None)

# Secrets. Several tests record the environment a fake process was started with, and
# a failing assertion prints what it compared: a developer's or a CI runner's real
# tokens and API keys must never be there to print. Nothing here talks to a service,
# so no test needs the real ones (the tests that want a key set a fake one).
_SECRET_NAME = re.compile(
    r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|BEARER|PRIVATE|^(ANTHROPIC|OPENAI|XAI)_",
    re.IGNORECASE,
)
for _var in [v for v in os.environ if _SECRET_NAME.search(v)]:
    os.environ.pop(_var, None)


class HiddenEnv(dict):
    """A process environment whose repr shows no values.

    Tests that record the env a fake process got wrap it in this, so a failing
    assertion (which prints the objects it compared) can never print the values."""

    def __repr__(self) -> str:
        return f"<environment: {len(self)} variables, values hidden>"

    __str__ = __repr__


def hide_env(kwargs: dict) -> dict:
    """`kwargs` (Popen keyword arguments) with its env, if any, wrapped in HiddenEnv."""
    env = kwargs.get("env")
    if env is None:
        return kwargs
    return {**kwargs, "env": HiddenEnv(env)}


# What a unit test must never start: the agent CLIs, and the browsers the watch
# window opens in. Matched on the executable's name without its extension.
_FORBIDDEN = {
    "agy",
    "antigravity",
    "codex",
    "copilot",
    "cursor",
    "cursor-agent",
    "grok",
    "kimi",
    "opencode",
    "muse",
    # Claude Code itself: the user has a real one installed, and a test that forgot
    # to fake it would run it on their quota. "claude-code" is the npm package's
    # directory, which is all a node launcher's argv names (@anthropic-ai/claude-code/cli.js).
    "claude",
    "claude-code",
    "chrome",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "msedge",
    "microsoft-edge",
    "brave",
    "brave-browser",
}
# Programs that start another one named in their next few arguments.
_LAUNCHERS = {"cmd", "node", "powershell", "pwsh", "sh", "bash", "npx", "uvx"}

# test_smoke.py is a live script (`python test_smoke.py`), not a pytest module:
# collecting it rewraps sys.stdout at import, which closes pytest's capture file
# and crashes a bare `pytest` run before it can report anything.
collect_ignore = ["test_smoke.py"]

_REAL_POPEN = subprocess.Popen
_trips: list[str] = []


def _components(arg) -> list[str]:
    """Every path component of `arg`, lowercased, the last without its extension."""
    parts = [p for p in os.fsdecode(arg).replace("\\", "/").split("/") if p]
    if parts:
        parts[-1] = os.path.splitext(parts[-1])[0]
    return [p.lower() for p in parts]


def _hit(name: str) -> bool:
    return name in _FORBIDDEN or name.startswith("muse-bin")


def _forbidden(args) -> str:
    """The forbidden program `args` would start, or "" if none."""
    if isinstance(args, (str, bytes)):
        text = os.fsdecode(args).strip()
        parts = text.split()[:1] if text else []
    else:
        parts = list(args)[:4]
    if not parts:
        return ""
    first = _components(parts[0])
    if first and _hit(first[-1]):
        return first[-1]
    if first and first[-1] in _LAUNCHERS:
        # A launcher runs the real program, and the program's name may only be in
        # its path: on Windows cursor-agent is `...\cursor-agent\versions\<v>\
        # node.exe ...\index.js`, where no file is called cursor-agent. So look at
        # every directory too, but only here, where a false match can't block an
        # ordinary program.
        for p in parts:
            for c in _components(p):
                if _hit(c):
                    return c
    return ""


class _GuardedPopen(_REAL_POPEN):
    """subprocess.Popen, except for the programs above. A subclass, so isinstance
    checks and Popen[str] annotations keep working."""

    # Set before __init__ can raise, so pytest can print the half-made object.
    args = None
    returncode = None

    def __init__(self, args, *a, **k):
        hit = _forbidden(args)
        if hit:
            _trips.append(f"{hit}: {args!r}"[:300])
            raise RuntimeError(
                f"a unit test tried to start the real {hit!r} ({args!r}). Fake it, or mark "
                "the test @pytest.mark.real_cli if running it is the point."
            )
        super().__init__(args, *a, **k)


def _no_browser(url, *a, **k):
    _trips.append(f"browser: {url}"[:300])
    raise RuntimeError(f"a unit test tried to open a real browser at {url!r}. Fake it.")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_cli: the test runs a real agent CLI on purpose; the tripwire allows it"
    )


class _OsProxy:
    """The os module, but with its own `name`: see fake_os_name."""

    def __init__(self, real, name):
        self._real = real
        self.name = name

    def __getattr__(self, attr):
        return getattr(self._real, attr)


def fake_os_name(monkeypatch, module, name):
    """Make `module` (and only `module`) see os.name == `name` for this test.

    Patching os.name itself would reach subprocess, pathlib, tempfile and any
    thread the test starts, all of which branch on it, and the same test would then
    mean something different on each CI platform. A stand-in for the `os` the module
    imported keeps the fake platform inside the code under test. Use it as
    `from conftest import fake_os_name`."""
    monkeypatch.setattr(module, "os", _OsProxy(os, name))


@pytest.fixture(autouse=True)
def _no_real_programs(request, monkeypatch):
    if request.node.get_closest_marker("real_cli"):
        yield
        return
    _trips.clear()
    # A test's own monkeypatch of Popen or webbrowser.open lands after this one
    # and wins, which is what a test that fakes the program wants.
    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)
    monkeypatch.setattr(webbrowser, "open", _no_browser)
    yield
    if _trips:
        found = "; ".join(_trips)
        _trips.clear()
        pytest.fail(f"started a real program during a unit test: {found}", pytrace=False)
