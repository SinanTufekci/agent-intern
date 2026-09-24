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


def _stem(arg) -> str:
    name = os.fsdecode(arg).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return os.path.splitext(name)[0].lower()


def _forbidden(args) -> str:
    """The forbidden program `args` would start, or "" if none."""
    if isinstance(args, (str, bytes)):
        text = os.fsdecode(args).strip()
        parts = text.split()[:1] if text else []
    else:
        parts = list(args)[:4]
    names = [_stem(p) for p in parts]
    if not names:
        return ""
    for n in names if names[0] in _LAUNCHERS else names[:1]:
        if n in _FORBIDDEN or n.startswith("muse-bin"):
            return n
    return ""


class _GuardedPopen(_REAL_POPEN):
    """subprocess.Popen, except for the programs above. A subclass, so isinstance
    checks and Popen[str] annotations keep working."""

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
