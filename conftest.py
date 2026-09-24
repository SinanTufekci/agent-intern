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
import subprocess
import webbrowser

import pytest

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
