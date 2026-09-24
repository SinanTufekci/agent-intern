"""Offline unit tests for the pure logic in server.py.

These use temp fixtures and never invoke agy, so they cost no AI Pro quota and
can run anywhere (including CI). For the live end-to-end check, see
test_smoke.py instead.

    pytest test_server.py
"""

import asyncio
import io
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import codex_bridge
import copilot_bridge
import cursor_bridge
import server
import swarm
import watch_server

# --------------------------------------------------------------------------
# _normalize_workspace
# --------------------------------------------------------------------------


def test_normalize_workspace_none_returns_cwd():
    assert server._normalize_workspace(None) == os.getcwd()


def test_normalize_workspace_relative_is_absolutised(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert server._normalize_workspace("sub/dir") == os.path.abspath("sub/dir")


# --------------------------------------------------------------------------
# _read_last_conv_id
# --------------------------------------------------------------------------


@pytest.fixture
def last_conv_file(tmp_path, monkeypatch):
    f = tmp_path / "last_conversations.json"
    monkeypatch.setattr(server, "LAST_CONVERSATIONS", f)
    return f


def test_read_last_conv_id_exact_match(last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\proj": "conv-1"}), encoding="utf-8")
    assert server._read_last_conv_id("C:\\proj") == "conv-1"


def test_read_last_conv_id_is_case_insensitive(last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\Proj": "conv-2"}), encoding="utf-8")
    assert server._read_last_conv_id("c:\\proj") == "conv-2"


def test_read_last_conv_id_missing_file_returns_none(last_conv_file):
    assert server._read_last_conv_id("anything") is None


def test_read_last_conv_id_malformed_json_returns_none(last_conv_file):
    last_conv_file.write_text("{not valid json", encoding="utf-8")
    assert server._read_last_conv_id("x") is None


def test_read_last_conv_id_absent_key_returns_none(last_conv_file):
    last_conv_file.write_text(json.dumps({"other": "c"}), encoding="utf-8")
    assert server._read_last_conv_id("missing") is None


# --------------------------------------------------------------------------
# _find_newest_conv_after
# --------------------------------------------------------------------------


@pytest.fixture
def brain_dir(tmp_path, monkeypatch):
    d = tmp_path / "brain"
    d.mkdir()
    monkeypatch.setattr(server, "BRAIN_DIR", d)
    # Isolate the SQLite fallback too, so _read_response never reads the real store.
    conv = tmp_path / "conversations"
    conv.mkdir()
    monkeypatch.setattr(server, "CONVERSATIONS_DIR", conv)
    return d


@pytest.fixture(autouse=True)
def _clean_watch_runs():
    # The watch state is a persistent id->state map; reset it before each test so a
    # run left "working" by one test can't change which slot the next test's run claims.
    server._WATCH_RUNS.clear()
    yield
    server._WATCH_RUNS.clear()


@pytest.fixture(autouse=True)
def _clean_agy_json_state():
    """Pin structured-output support OFF and clear the recorded-conversation map.

    Both are process-global (the `--output-format` support probe and the
    workspace->conversation map filled from agy's json result). Defaulting the probe
    to False keeps these offline tests deterministic — otherwise every run of the
    pre-1.1.8 text/transcript paths would silently switch behaviour depending on
    which agy happens to be installed on the machine. Tests that exercise the 1.1.8
    paths opt in explicitly (fake_agy_json, or monkeypatching the flag).

    The 1.1.11 `/usage` probe is pinned OFF for the same reason plus a sharper one:
    left unresolved, _collect_status on a machine with a current agy would SPAWN
    that probe for real in a unit test. Quota tests opt in explicitly.
    """
    server._AGY_JSON_SUPPORT = False
    server._AGY_USAGE_GATE = False
    server._CONV_BY_WORKSPACE.clear()
    yield
    server._AGY_JSON_SUPPORT = None
    server._AGY_USAGE_GATE = None
    server._CONV_BY_WORKSPACE.clear()


def test_find_newest_conv_after_missing_brain_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BRAIN_DIR", tmp_path / "does-not-exist")
    assert server._find_newest_conv_after(time.time()) is None


def test_find_newest_conv_after_picks_newest_dir(brain_dir):
    start = time.time()
    old = brain_dir / "old-conv"
    old.mkdir()
    new = brain_dir / "new-conv"
    new.mkdir()
    os.utime(old, (start - 100, start - 100))
    os.utime(new, (start + 5, start + 5))
    assert server._find_newest_conv_after(start) == "new-conv"


def test_find_newest_conv_after_ignores_plain_files(brain_dir):
    start = time.time()
    f = brain_dir / "a-file"
    f.write_text("x", encoding="utf-8")
    os.utime(f, (start + 5, start + 5))
    assert server._find_newest_conv_after(start) is None


def test_find_newest_conv_after_skips_dirs_older_than_start(brain_dir):
    start = time.time()
    stale = brain_dir / "stale"
    stale.mkdir()
    os.utime(stale, (start - 100, start - 100))
    assert server._find_newest_conv_after(start) is None


# --------------------------------------------------------------------------
# _read_response
# --------------------------------------------------------------------------


def _entry(type_, content=None, status="DONE", source="MODEL"):
    e = {"source": source, "status": status, "type": type_}
    if content is not None:
        e["content"] = content
    return json.dumps(e)


def _write_transcript(brain_dir, conv_id, lines):
    logs = brain_dir / conv_id / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    (logs / "transcript.jsonl").write_text("\n".join(lines), encoding="utf-8")


def test_read_response_returns_last_planner_response_with_content(brain_dir):
    _write_transcript(
        brain_dir,
        "c1",
        [
            _entry("RUN_COMMAND", "step"),
            _entry("PLANNER_RESPONSE", "first"),
            _entry("PLANNER_RESPONSE", "final"),
        ],
    )
    assert server._read_response("c1") == "final"


def _user_input(text):
    return json.dumps(
        {"source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": text}
    )


def test_read_agy_history_pairs_turns_and_unwraps_user_request(brain_dir):
    _write_transcript(
        brain_dir,
        "hc",
        [
            _user_input("<USER_REQUEST>\nhello there\n</USER_REQUEST>"),
            _entry("PLANNER_RESPONSE", "hi, how can I help?"),
            _user_input("<USER_REQUEST>\nsecond question</USER_REQUEST>"),
            _entry("PLANNER_RESPONSE", "let me think"),  # mid-turn narration
            _entry("PLANNER_RESPONSE", "the final answer"),  # last wins as the turn's answer
        ],
    )
    assert server._read_agy_history("hc") == [
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": "hi, how can I help?"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "the final answer"},
    ]


def test_read_agy_history_empty_when_no_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BRAIN_DIR", tmp_path / "does-not-exist")
    assert server._read_agy_history("nope") == []


def test_read_response_ignores_contentless_and_malformed_lines(brain_dir):
    _write_transcript(
        brain_dir,
        "c2",
        [
            "{ broken json",
            "",
            _entry("PLANNER_RESPONSE"),  # no content
            _entry("PLANNER_RESPONSE", "answer"),
        ],
    )
    assert server._read_response("c2") == "answer"


def test_read_response_no_completed_response_raises(brain_dir):
    _write_transcript(
        brain_dir,
        "c3",
        [
            _entry("PLANNER_RESPONSE", "x", status="RUNNING"),
        ],
    )
    with pytest.raises(RuntimeError, match="No completed MODEL response"):
        server._read_response("c3")


def test_read_response_missing_transcript_no_db_mentions_sqlite(brain_dir):
    (brain_dir / "c4").mkdir()
    with pytest.raises(RuntimeError, match="SQLite"):
        server._read_response("c4")


# --------------------------------------------------------------------------
# _parse_agy_version
# --------------------------------------------------------------------------


def test_parse_agy_version_bare():
    assert server._parse_agy_version("1.0.4") == (1, 0, 4)


def test_parse_agy_version_trailing_newline():
    assert server._parse_agy_version("1.0.4\n") == (1, 0, 4)


def test_parse_agy_version_with_prefix_and_build():
    assert server._parse_agy_version("agy version 1.2.0 (build abc)") == (1, 2, 0)


def test_parse_agy_version_garbage_returns_none():
    assert server._parse_agy_version("no version here") is None


def test_parse_agy_version_empty_returns_none():
    assert server._parse_agy_version("") is None


# --------------------------------------------------------------------------
# _compat_warning
# --------------------------------------------------------------------------


def test_compat_warning_none_for_verified_version():
    assert server._compat_warning(server.VERIFIED_AGY_VERSION) is None


def test_compat_warning_none_for_older_version():
    assert server._compat_warning((1, 0, 3)) is None


def test_compat_warning_warns_for_newer_version():
    # Derive a version one patch above the verified baseline so this survives bumps.
    major, minor, patch = server.VERIFIED_AGY_VERSION
    newer = (major, minor, patch + 1)
    msg = server._compat_warning(newer)
    assert msg is not None
    assert ".".join(map(str, newer)) in msg  # the detected version
    # the verified baseline it's compared to (derived so this survives bumps)
    assert ".".join(map(str, server.VERIFIED_AGY_VERSION)) in msg


def test_compat_warning_none_when_version_unknown():
    assert server._compat_warning(None) is None


# --------------------------------------------------------------------------
# _debug_enabled
# --------------------------------------------------------------------------


def test_debug_enabled_false_when_unset(monkeypatch):
    monkeypatch.delenv("AGY_BRIDGE_DEBUG", raising=False)
    assert server._debug_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_debug_enabled_true_for_truthy(monkeypatch, value):
    monkeypatch.setenv("AGY_BRIDGE_DEBUG", value)
    assert server._debug_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_debug_enabled_false_for_falsy(monkeypatch, value):
    monkeypatch.setenv("AGY_BRIDGE_DEBUG", value)
    assert server._debug_enabled() is False


# --------------------------------------------------------------------------
# _env_truthy  (generic truthy env-var reader behind _debug_enabled etc.)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_env_truthy_true(monkeypatch, value):
    monkeypatch.setenv("AGY_TEST_FLAG", value)
    assert server._env_truthy("AGY_TEST_FLAG") is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_env_truthy_false(monkeypatch, value):
    monkeypatch.setenv("AGY_TEST_FLAG", value)
    assert server._env_truthy("AGY_TEST_FLAG") is False


def test_env_truthy_false_when_unset(monkeypatch):
    monkeypatch.delenv("AGY_TEST_FLAG", raising=False)
    assert server._env_truthy("AGY_TEST_FLAG") is False


# --------------------------------------------------------------------------
# _update_warning  (nag when a newer bridge tag exists on GitHub)
# --------------------------------------------------------------------------


def test_update_warning_warns_for_newer(monkeypatch):
    monkeypatch.setattr(server, "__version__", "0.8.0")
    msg = server._update_warning((0, 9, 0))
    assert msg is not None
    assert "0.9.0" in msg  # the newer version available
    assert "0.8.0" in msg  # the version currently running
    # The recommended install is `uvx agent-intern` from PyPI, so the uvx upgrade
    # is the one most readers need; `git pull` stays for source installs. This
    # assertion used to demand ONLY "git pull", which is why the suite was
    # defending advice that had no repo to run it in.
    assert "uvx agent-intern@latest" in msg
    assert "git pull" in msg


def test_update_warning_none_for_equal(monkeypatch):
    monkeypatch.setattr(server, "__version__", "0.8.0")
    assert server._update_warning((0, 8, 0)) is None


def test_both_update_notices_name_the_same_upgrade_command(monkeypatch):
    # An available update is announced in TWO places: the stderr warning at
    # startup (which only reaches the host's logs) and the `bridge version` row in
    # *_status (the one a user actually sees). They drifted apart once — the
    # status row said `uvx agent-intern@latest` while the startup warning still
    # said to `git pull` in a repo the recommended install never creates. One
    # codebase should not hand out two different upgrade commands, so pin them to
    # each other rather than to a literal spelled twice.
    monkeypatch.setattr(server, "__version__", "0.8.0")
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: (0, 9, 0))
    monkeypatch.delenv("AGY_BRIDGE_NO_UPDATE_CHECK", raising=False)
    warning = server._update_warning((0, 9, 0))
    _, _, row = server._bridge_version_status()
    assert warning is not None
    for notice in (warning, row):
        assert "0.9.0" in notice, f"update notice omits the new version: {notice!r}"
        assert "uvx agent-intern@latest" in notice, (
            f"update notice omits the upgrade command the other one gives: {notice!r}"
        )


def test_update_warning_none_for_older(monkeypatch):
    monkeypatch.setattr(server, "__version__", "0.8.0")
    assert server._update_warning((0, 7, 5)) is None


def test_update_warning_none_when_latest_unknown():
    assert server._update_warning(None) is None


def test_update_warning_none_when_current_unparseable(monkeypatch):
    monkeypatch.setattr(server, "__version__", "not-a-version")
    assert server._update_warning((9, 9, 9)) is None


# --------------------------------------------------------------------------
# _fetch_latest_release_version  (GitHub tags API; never raises on the network)
# --------------------------------------------------------------------------


class _FakeResp:
    """Minimal urlopen() stand-in: works as a context manager and feeds json.load."""

    def __init__(self, body: str):
        self._body = body.encode()

    def read(self, *_a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_fetch_latest_release_version_picks_highest(monkeypatch):
    body = '[{"name": "v0.5.0"}, {"name": "v0.8.0"}, {"name": "v0.7.1"}, {"name": "nightly"}]'
    monkeypatch.setattr(server.urllib.request, "urlopen", lambda *a, **k: _FakeResp(body))
    assert server._fetch_latest_release_version() == (0, 8, 0)


def test_fetch_latest_release_version_none_on_network_error(monkeypatch):
    def _raise(*_a, **_k):
        raise server.urllib.error.URLError("offline")

    monkeypatch.setattr(server.urllib.request, "urlopen", _raise)
    assert server._fetch_latest_release_version() is None


def test_fetch_latest_release_version_none_on_non_list(monkeypatch):
    # rate-limit / error bodies come back as a JSON object, not a list of tags
    monkeypatch.setattr(
        server.urllib.request, "urlopen", lambda *a, **k: _FakeResp('{"message": "rate limited"}')
    )
    assert server._fetch_latest_release_version() is None


def test_fetch_latest_release_version_none_when_no_semver_tags(monkeypatch):
    monkeypatch.setattr(
        server.urllib.request, "urlopen", lambda *a, **k: _FakeResp('[{"name": "latest"}]')
    )
    assert server._fetch_latest_release_version() is None


# --------------------------------------------------------------------------
# _bridge_version_status  (surfaces the update notice in antigravity_status)
# --------------------------------------------------------------------------


def test_bridge_version_status_flags_newer_release(monkeypatch):
    monkeypatch.delenv("AGY_BRIDGE_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(server, "__version__", "0.10.1")
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: (0, 10, 2))
    label, ok, detail = server._bridge_version_status()
    assert label == "bridge version"
    assert ok is True  # an available update is informational, not a fault
    assert "0.10.2" in detail and "available" in detail
    assert "uvx agent-intern@latest" in detail


def test_bridge_version_status_reports_latest(monkeypatch):
    monkeypatch.delenv("AGY_BRIDGE_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(server, "__version__", "0.10.1")
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: (0, 10, 1))
    _, ok, detail = server._bridge_version_status()
    assert ok is True
    assert "latest" in detail and "available" not in detail


def test_bridge_version_status_unavailable_when_offline(monkeypatch):
    monkeypatch.delenv("AGY_BRIDGE_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: None)
    _, ok, detail = server._bridge_version_status()
    assert ok is True
    assert "unavailable" in detail


def test_bridge_version_status_respects_opt_out(monkeypatch):
    monkeypatch.setenv("AGY_BRIDGE_NO_UPDATE_CHECK", "1")

    def _boom():
        raise AssertionError("update check must not run when disabled")

    monkeypatch.setattr(server, "_fetch_latest_release_version", _boom)
    _, ok, detail = server._bridge_version_status()
    assert ok is True
    assert "disabled" in detail


def test_collect_status_first_row_is_bridge_version(monkeypatch):
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: None)
    monkeypatch.setattr(server, "_get_agy_version", lambda: None)  # skip the agy subprocess
    rows = server._collect_status()
    assert rows[0][0] == "bridge version"


# --------------------------------------------------------------------------
# _run_with_progress  (threaded agy run + best-effort MCP progress notifications)
# --------------------------------------------------------------------------


def test_run_with_progress_no_ctx_returns_result():
    # ctx=None (direct call / no progressToken): plain threaded call, no progress.
    result = asyncio.run(server._run_with_progress(lambda a, b: f"{a}-{b}", ("x", "y"), None, 10))
    assert result == "x-y"


def test_run_with_progress_reports_progress_with_ctx(monkeypatch):
    monkeypatch.setattr(server, "_PROGRESS_NOTIFY_INTERVAL_S", 0.02)

    class _Ctx:
        def __init__(self):
            self.calls = 0

        async def report_progress(self, progress, total=None, message=None):
            self.calls += 1
            assert 0 <= progress <= total  # time bar stays within [0, timeout]

    ctx = _Ctx()

    def slow():
        time.sleep(0.15)  # spans several 0.02s notify intervals
        return "done"

    result = asyncio.run(server._run_with_progress(slow, (), ctx, 10))
    assert result == "done"
    assert ctx.calls >= 1


def test_run_with_progress_propagates_worker_errors():
    def boom():
        raise RuntimeError("agy failed")

    with pytest.raises(RuntimeError, match="agy failed"):
        asyncio.run(server._run_with_progress(boom, (), None, 10))


def test_run_with_progress_survives_progress_errors(monkeypatch):
    # A throwing report_progress must not break the run — progress is cosmetic.
    monkeypatch.setattr(server, "_PROGRESS_NOTIFY_INTERVAL_S", 0.02)

    class _BadCtx:
        async def report_progress(self, *a, **k):
            raise RuntimeError("transport down")

    def slow():
        time.sleep(0.1)
        return "ok"

    assert asyncio.run(server._run_with_progress(slow, (), _BadCtx(), 10)) == "ok"


# --------------------------------------------------------------------------
# _spawn_kwargs  (console-detach so agy's TTY writes don't leak to the host)
# --------------------------------------------------------------------------


def test_spawn_kwargs_detaches_per_platform():
    # Pass the platform explicitly — monkeypatching os.name globally would break
    # pathlib (and pytest's own per-test bookkeeping) on non-Windows CI runners.
    # CREATE_NO_WINDOW == 0x08000000; assert the literal so it's host-portable.
    assert server._spawn_kwargs("nt") == {"creationflags": 0x08000000}
    assert server._spawn_kwargs("posix") == {"start_new_session": True}


def test_spawn_kwargs_is_subprocess_run_compatible(monkeypatch):
    # The returned mapping must be valid **kwargs for subprocess on this host
    # (no platform-foreign keys leak through).
    kwargs = server._spawn_kwargs()
    assert isinstance(kwargs, dict)
    if server.os.name == "nt":
        assert "creationflags" in kwargs and "start_new_session" not in kwargs
    else:
        assert "start_new_session" in kwargs and "creationflags" not in kwargs


# --------------------------------------------------------------------------
# AGY_BIN  (configurable agy executable; AGY_BIN env var overrides "agy")
# --------------------------------------------------------------------------


@pytest.fixture
def agy_absent(monkeypatch):
    """No agy to probe, as on CI. Building argv asks `agy --version` which flags
    it can pass, and these tests used to run the real agy for that wherever one
    was installed, so what they checked depended on the machine."""
    monkeypatch.setattr(server, "_get_agy_version", lambda: None)


def test_build_agy_args_uses_default_agy_bin(monkeypatch, agy_absent):
    monkeypatch.setattr(server, "AGY_BIN", "agy")
    args, _ = server._build_agy_args("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert args[0] == "agy"


def test_build_agy_args_honors_custom_agy_bin(monkeypatch, agy_absent):
    custom = "C:\\Users\\x\\AppData\\Local\\agy\\bin\\agy.exe"
    monkeypatch.setattr(server, "AGY_BIN", custom)
    args, _ = server._build_agy_args("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert args[0] == custom
    # only argv[0] changes; the rest of the command line is unaffected
    assert "--print-timeout" in args
    assert args[-2:] == ["-p", "hi"]


# --------------------------------------------------------------------------
# --dangerously-skip-permissions: required, and required BEFORE -p (agy 1.1.3)
# --------------------------------------------------------------------------


def test_agy_base_args_passes_skip_permissions(agy_absent):
    """agy 1.1.3 soft-denies any tool needing a permission headlessly (print mode
    can't prompt), which kills every tool-using call. The flag is agy's own remedy.
    """
    assert "--dangerously-skip-permissions" in server._agy_base_args(10)


@pytest.mark.parametrize("model", [None, "gemini-3.1-pro-high"])
def test_build_agy_args_skip_permissions_precedes_prompt(model, agy_absent):
    """The flag MUST come before -p: agy's -p takes the prompt as its VALUE, so
    `-p --dangerously-skip-permissions <task>` makes the flag the prompt and drops
    the task (verified on 1.1.3 — agy replied describing the flag).
    """
    args, _ = server._build_agy_args("hi", "C:\\ws", continue_conv=False, timeout_s=10, model=model)
    assert args.index("--dangerously-skip-permissions") < args.index("-p")


# --------------------------------------------------------------------------
# --disable-slash-commands: required as of agy 1.1.9 (print-mode slash/skill
# expansion would otherwise EXECUTE a prompt instead of answering it)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version_out,expected",
    [
        ("1.1.9", True),
        ("1.1.10", True),
        ("1.2.0", True),
        ("2.0.0", True),
        ("1.1.8", False),  # expansion doesn't exist yet AND the flag isn't parsed
        ("1.1.3", False),
        ("1.0.15", False),
        ("", False),  # unparseable -> never pass a flag we can't confirm exists
        ("not a version", False),
    ],
)
def test_supports_disable_slash_commands_version_gate(monkeypatch, version_out, expected):
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", None)
    monkeypatch.setattr(server, "_get_agy_version", lambda: version_out)
    assert server.supports_disable_slash_commands() is expected


def test_supports_disable_slash_commands_is_cached(monkeypatch):
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", None)
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return "1.1.10"

    monkeypatch.setattr(server, "_get_agy_version", counted)
    assert server.supports_disable_slash_commands() is True
    assert server.supports_disable_slash_commands() is True
    assert calls["n"] == 1  # version probed once per process


def test_supports_disable_slash_commands_false_when_agy_missing(monkeypatch):
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", None)
    monkeypatch.setattr(server, "_get_agy_version", lambda: None)
    assert server.supports_disable_slash_commands() is False


def test_agy_base_args_disables_slash_commands(monkeypatch):
    """agy 1.1.9 made print mode EXPAND slash commands and skills, so a prompt whose
    first token names one is executed as that command and never reaches the model —
    verified live on 1.1.10: antigravity_ask("/help") returned agy's help page.
    Registered commands include side-effecting ones (`/goal`, `/schedule`), so this
    is a correctness AND a safety fix.
    """
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", True)
    assert "--disable-slash-commands" in server._agy_base_args(10)


def test_agy_base_args_omits_slash_flag_on_older_agy(monkeypatch):
    """Pre-1.1.9 agy has no such flag in its parser — passing it would break every
    call, and the expansion it guards against doesn't exist there anyway.
    """
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", False)
    assert "--disable-slash-commands" not in server._agy_base_args(10)


def test_agy_base_args_slash_flag_opt_out_via_env(monkeypatch):
    """AGY_BRIDGE_ALLOW_SLASH_COMMANDS=1 is the deliberate opt-in for callers who
    WANT `-p "/my-skill <args>"` to invoke a skill.
    """
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", True)
    monkeypatch.setenv("AGY_BRIDGE_ALLOW_SLASH_COMMANDS", "1")
    assert "--disable-slash-commands" not in server._agy_base_args(10)


@pytest.mark.parametrize("model", [None, "gemini-3.1-pro-high"])
def test_build_agy_args_slash_flag_precedes_prompt(monkeypatch, model):
    """Same -p rule as --dangerously-skip-permissions: agy's -p takes the prompt as
    its VALUE, so any flag landing after it would be swallowed as the prompt.
    """
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", True)
    args, _ = server._build_agy_args("hi", "C:\\ws", continue_conv=False, timeout_s=10, model=model)
    assert args.index("--disable-slash-commands") < args.index("-p")
    assert args[-2:] == ["-p", "hi"]


def test_run_agy_passes_slash_guard(fake_agy_slash, brain_dir, last_conv_file):
    """End-to-end through _run_agy: the guard reaches the real argv, not just
    _agy_base_args in isolation.
    """
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy_slash["stdout"] = "answer"
    server._run_agy("/help", "C:\\ws", continue_conv=False, timeout_s=10)
    argv = fake_agy_slash["args"]
    assert "--disable-slash-commands" in argv
    # the slash-prefixed prompt still reaches agy verbatim — guarded, not rewritten
    assert argv[-2:] == ["-p", "/help"]


# --------------------------------------------------------------------------
# --mode plan  (agy 1.1.12+): the one Antigravity restriction that survives
# --dangerously-skip-permissions. Live behaviour these tests encode, verified on
# 1.1.20 / Windows: a file write and a shell command were both refused and
# diverted into a plan document under agy's brain dir even when the prompt said
# "do it now, do not plan it", while a file read answered normally; and agy warns
# "--mode plan has no effect while slash command expansion is disabled" and then
# runs UNRESTRICTED if --disable-slash-commands is passed alongside it.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version,expected",
    [("1.1.12", True), ("1.1.20", True), ("1.1.11", False), ("1.0.9", False), (None, False)],
)
def test_supports_plan_mode_gate(monkeypatch, version, expected):
    """1.1.12 is where --mode started being HONORED in print mode; before it, agy
    parsed the flag and ignored it. An unreadable version answers False.
    """
    monkeypatch.setattr(server, "_AGY_PLAN_GATE", None)
    monkeypatch.setattr(server, "_get_agy_version", lambda: version)
    assert server.supports_plan_mode() is expected


def test_agy_base_args_plan_replaces_slash_shield(monkeypatch):
    """The exclusivity is the whole point: agy DISABLES plan mode when the slash
    shield is on, so passing both would hand back an unrestricted run that looks
    restricted. The shield moves to _guard_plan_mode_prompt instead.
    """
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", True)  # shield would otherwise apply
    args = server._agy_base_args(10, plan=True)
    assert args[args.index("--mode") + 1] == "plan"
    assert "--disable-slash-commands" not in args


def test_agy_base_args_plan_keeps_skip_permissions(monkeypatch):
    """Verified live that plan survives --dangerously-skip-permissions. Dropping it
    would reintroduce 1.1.3's soft-deny and kill the READS plan mode exists to allow.
    """
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", True)
    assert "--dangerously-skip-permissions" in server._agy_base_args(10, plan=True)


def test_agy_base_args_without_plan_is_unchanged(monkeypatch):
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", True)
    args = server._agy_base_args(10)
    assert "--mode" not in args and "--disable-slash-commands" in args


def test_build_agy_args_plan_precedes_prompt(monkeypatch):
    """Same -p rule as every other flag: agy's -p takes the prompt as its VALUE."""
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", True)
    args, _ = server._build_agy_args("hi", "C:\\ws", continue_conv=False, timeout_s=10, plan=True)
    assert args.index("--mode") < args.index("-p")
    assert args[-2:] == ["-p", "hi"]


@pytest.mark.parametrize("prompt", ["/schedule nightly build", "/goal", "  /help  "])
def test_guard_plan_mode_prompt_rejects_leading_command(prompt):
    """A single-segment leading /token is what agy's expansion actually fires on,
    and the registered set is side-effecting (`/goal`, `/schedule`).
    """
    with pytest.raises(ValueError, match="plan mode cannot run a prompt"):
        server._guard_plan_mode_prompt(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "/etc/hosts is wrong, why?",  # POSIX path: has a second separator
        "explain /schedule to me",  # not the FIRST token
        "summarise this repo",
        "",
    ],
)
def test_guard_plan_mode_prompt_allows_ordinary_prompts(prompt):
    server._guard_plan_mode_prompt(prompt)  # must not raise


def test_guard_plan_mode_prompt_honors_env_opt_in(monkeypatch):
    """Same deliberate opt-in _agy_base_args honors for callers who WANT expansion."""
    monkeypatch.setenv("AGY_BRIDGE_ALLOW_SLASH_COMMANDS", "1")
    server._guard_plan_mode_prompt("/help")  # must not raise


def test_check_plan_mode_raises_on_older_agy(monkeypatch):
    """REFUSES rather than degrading. Every other gate here falls back to a
    lesser-but-safe path; this one can't, because silently dropping a restriction
    returns a fully-empowered run to a caller who asked for the opposite.
    """
    monkeypatch.setattr(server, "_AGY_PLAN_GATE", False)
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.1.11")
    with pytest.raises(ValueError, match="needs agy 1.1.12"):
        server._check_plan_mode("summarise this repo")


def test_check_plan_mode_passes_on_supported_agy(monkeypatch):
    monkeypatch.setattr(server, "_AGY_PLAN_GATE", True)
    server._check_plan_mode("summarise this repo")  # must not raise


def test_run_agy_plan_reaches_argv(fake_agy_slash, brain_dir, last_conv_file):
    """End-to-end through _run_agy: the flag reaches the real argv, and the shield
    it replaces is genuinely absent rather than merely reordered.
    """
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy_slash["stdout"] = "a plan"
    out = server._run_agy("review this", "C:\\ws", continue_conv=False, timeout_s=10, plan=True)
    assert out == "a plan"
    argv = fake_agy_slash["args"]
    assert argv[argv.index("--mode") + 1] == "plan"
    assert "--disable-slash-commands" not in argv
    assert "--dangerously-skip-permissions" in argv


# --------------------------------------------------------------------------
# --json-schema  (agy 1.1.8+, same release as --output-format json): the
# validated object lands in the result's OWN `structured_output` field. Verified
# live on 1.1.20 that `response` on the same run is NOT a substitute — it carried
# the model's raw emission, the declared keys plus agy's internal toolAction /
# toolSummary, and in one run a line of prose ahead of the JSON.
# --------------------------------------------------------------------------


def test_normalize_json_schema_accepts_object():
    out = server._normalize_json_schema({"type": "object"})
    assert json.loads(out) == {"type": "object"}


def test_normalize_json_schema_accepts_json_text():
    """A model composing a tool call sends either form."""
    out = server._normalize_json_schema('  {"type": "object"}  ')
    assert json.loads(out) == {"type": "object"}


@pytest.mark.parametrize("bad", ["not json", "./schema.json", "[1, 2]", "42"])
def test_normalize_json_schema_rejects_unusable(bad):
    """A path is refused on purpose: treating a malformed schema as a filename is
    how you get an unschema'd run that still reports success.
    """
    with pytest.raises(ValueError, match="schema must be a JSON object"):
        server._normalize_json_schema(bad)


def test_check_schema_support_raises_below_118(monkeypatch):
    monkeypatch.setattr(server, "_AGY_JSON_SUPPORT", False)
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.1.7")
    with pytest.raises(ValueError, match="schema needs agy 1.1.8"):
        server._check_schema_support()


def test_build_agy_args_schema_precedes_prompt(monkeypatch):
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", False)
    args, _ = server._build_agy_args(
        "hi", "C:\\ws", continue_conv=False, timeout_s=10, schema='{"type":"object"}'
    )
    assert args[args.index("--json-schema") + 1] == '{"type":"object"}'
    assert args.index("--json-schema") < args.index("-p")
    assert args[-2:] == ["-p", "hi"]


def test_structured_answer_returns_the_validated_object():
    out = server._structured_answer({"structured_output": {"a": 1}}, "c1")
    assert json.loads(out) == {"a": 1}


def test_structured_answer_raises_when_absent():
    """Never fall back to `response`: the caller is about to json.loads this."""
    with pytest.raises(RuntimeError, match="no structured_output"):
        server._structured_answer({"status": "ERROR", "response": "some prose"}, "c1")


def test_run_agy_schema_returns_structured_not_response(fake_agy_json, last_conv_file):
    """End-to-end: the schema run returns structured_output, and the prose response
    on the same object is ignored rather than concatenated or preferred.
    """
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy_json["stdout"] = _json_result(
        response="prose the caller did not ask for",
        structured_output={"language": "Python", "files": 7},
    )
    out = server._run_agy(
        "hi", "C:\\ws", continue_conv=False, timeout_s=10, schema='{"type":"object"}'
    )
    assert json.loads(out) == {"language": "Python", "files": 7}
    argv = fake_agy_json["args"]
    assert argv[argv.index("--json-schema") + 1] == '{"type":"object"}'


def test_run_agy_schema_raises_on_plain_text_fallback(fake_agy_json, last_conv_file):
    """agy ignored the flag and answered plain text. Returning that prose would push
    the failure onto a caller who is going to parse it.
    """
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy_json["stdout"] = "just some prose"
    with pytest.raises(RuntimeError, match="no structured result object"):
        server._run_agy(
            "hi", "C:\\ws", continue_conv=False, timeout_s=10, schema='{"type":"object"}'
        )


def test_run_agy_schema_raises_when_result_lacks_structured_output(fake_agy_json, last_conv_file):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy_json["stdout"] = _json_result(response="prose")  # no structured_output key
    with pytest.raises(RuntimeError, match="no structured_output"):
        server._run_agy(
            "hi", "C:\\ws", continue_conv=False, timeout_s=10, schema='{"type":"object"}'
        )


def test_run_agy_without_schema_is_unchanged(fake_agy_json, last_conv_file):
    """The schema path must not disturb the ordinary one."""
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy_json["stdout"] = _json_result(response="ans", structured_output={"a": 1})
    out = server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == "ans"
    assert "--json-schema" not in fake_agy_json["args"]


# --------------------------------------------------------------------------
# --model plumbing: _build_agy_args / list_agy_models / validate_model
# --------------------------------------------------------------------------


def test_build_agy_args_includes_model_when_given(monkeypatch, agy_absent):
    monkeypatch.setattr(server, "AGY_BIN", "agy")
    args, _ = server._build_agy_args(
        "hi", "C:\\ws", continue_conv=False, timeout_s=10, model="gemini-3.1-pro-high"
    )
    assert "--model" in args
    assert args[args.index("--model") + 1] == "gemini-3.1-pro-high"
    # the prompt still trails the command line
    assert args[-2:] == ["-p", "hi"]


def test_build_agy_args_omits_model_when_none(monkeypatch, agy_absent):
    monkeypatch.setattr(server, "AGY_BIN", "agy")
    args, _ = server._build_agy_args("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert "--model" not in args


def test_list_agy_models_parses_and_caches(monkeypatch):
    monkeypatch.setattr(server, "_AGY_MODELS_CACHE", None)
    calls = {"n": 0}

    def fake_run(args, **kwargs):
        calls["n"] += 1
        return subprocess.CompletedProcess(args, 0, stdout="A\n B \n\nC\n", stderr="")

    monkeypatch.setattr(server.proc_tree, "run_captured", fake_run)
    assert server.list_agy_models() == ["A", "B", "C"]
    # second call is served from the process cache (no second subprocess)
    assert server.list_agy_models() == ["A", "B", "C"]
    assert calls["n"] == 1


# agy 1.1.12 made `agy models` machine-readable: every line became a tab-separated
# `<slug>\t<human label>` record. Reading the whole line as the slug made
# validate_model reject EVERY valid model, killing the `model` argument on all three
# antigravity tools while this suite stayed green — the mocks all spoke the old
# format. These pin BOTH formats down so the next change to it fails here.
def test_parse_models_output_reads_1112_tab_separated_records():
    stdout = (
        "gemini-3.6-flash-high\tGemini 3.6 Flash (High)\n"
        "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
    )
    assert server._parse_models_output(stdout) == ["gemini-3.6-flash-high", "claude-sonnet-4-6"]


def test_parse_models_output_still_reads_bare_slugs():
    # Pre-1.1.12 shape: one bare slug per line, no tab, no label.
    assert server._parse_models_output("gemini-3.5-flash-high\n gpt-oss-120b-medium \n\n") == [
        "gemini-3.5-flash-high",
        "gpt-oss-120b-medium",
    ]


def test_parse_models_output_drops_chatter_lines():
    # A slug never contains whitespace, so a progress/status line is not a model.
    # 1.1.12 prints "Fetching available models..." on stderr, but older and newer
    # builds may not, and it must never end up in the accepted-model list.
    stdout = "Fetching available models...\ngemini-3.1-pro-high\tGemini 3.1 Pro (High)\n"
    assert server._parse_models_output(stdout) == ["gemini-3.1-pro-high"]


def test_validate_model_accepts_slug_from_tab_separated_list(monkeypatch):
    """The 1.1.12 break, end to end: a valid slug must survive `agy models` output."""
    monkeypatch.setattr(server, "_AGY_MODELS_CACHE", None)

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout="gemini-3.6-flash-high\tGemini 3.6 Flash (High)\n", stderr=""
        )

    monkeypatch.setattr(server.proc_tree, "run_captured", fake_run)
    assert server.validate_model("gemini-3.6-flash-high") == "gemini-3.6-flash-high"


def test_list_agy_models_empty_on_subprocess_error(monkeypatch):
    monkeypatch.setattr(server, "_AGY_MODELS_CACHE", None)

    def boom(args, **kwargs):
        raise OSError("agy not found")

    monkeypatch.setattr(server.proc_tree, "run_captured", boom)
    assert server.list_agy_models() == []


def test_validate_model_none_and_empty_pass():
    assert server.validate_model(None) is None
    assert server.validate_model("") == ""


def test_validate_model_accepts_known(monkeypatch):
    monkeypatch.setattr(server, "list_agy_models", lambda: ["gemini-3.5-flash-high", "X"])
    assert server.validate_model("X") == "X"


def test_validate_model_rejects_unknown(monkeypatch):
    monkeypatch.setattr(server, "list_agy_models", lambda: ["gemini-3.5-flash-high"])
    with pytest.raises(ValueError, match="unknown agy model"):
        server.validate_model("Bogus 9000")


def test_validate_model_skips_when_list_unavailable(monkeypatch):
    # If we can't enumerate models, pass the label through rather than wrongly reject.
    monkeypatch.setattr(server, "list_agy_models", lambda: [])
    assert server.validate_model("Whatever") == "Whatever"


# Every model name our docs hand to callers, checked against the LIVE `agy models`.
# agy 1.1.5 renamed all of them ("Gemini 3.1 Pro (High)" -> gemini-3.1-pro-high)
# and this suite stayed fully green, because every other model test mocks
# list_agy_models and the plumbing is format-agnostic. Only the docstrings and
# README broke — steering callers into a guaranteed rejection — so this is the test
# that notices. Spends no AI Pro quota (`agy models` is a local subcommand) and
# skips where agy isn't installed.
#
# It has now earned its keep twice over: agy 1.1.25 both ADDED the gemini-3.8-flash
# family and DROPPED the gemini-3.5-flash one, and only the first of those appears
# in any changelog entry. This pair of tests is the only thing in the repo that
# fired on either.
DOCUMENTED_AGY_MODELS = [
    "gemini-3.8-flash-high",  # the default as of 1.1.25 (added the 3.8 family)
    "gemini-3.7-flash-high",  # 1.1.16's default
    "gemini-3.6-flash-high",  # 1.1.6's default (added the gemini-3.6-flash family)
    "gemini-3.1-pro-high",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
]


def _model_family(slug: str) -> str:
    """A model slug minus its reasoning-effort suffix: gemini-3.7-flash-high -> gemini-3.7-flash.

    agy 1.1.5 bakes the effort level into the slug, so one family shows up as up
    to three entries. The docs deliberately name only the -high variant of each,
    which is why the coverage test below compares families rather than slugs.
    """
    for effort in ("-low", "-medium", "-high"):
        if slug.endswith(effort):
            return slug[: -len(effort)]
    return slug


@pytest.mark.real_cli
def test_documented_model_slugs_still_accepted_by_live_agy(monkeypatch):
    monkeypatch.setattr(server, "_AGY_MODELS_CACHE", None)  # force a fresh read
    live = server.list_agy_models()
    if not live:
        pytest.skip("agy not installed or `agy models` unreadable")
    missing = [m for m in DOCUMENTED_AGY_MODELS if m not in live]
    assert not missing, (
        f"docs advertise model(s) agy no longer accepts: {missing}. Live list: {live}. "
        "agy renames models between releases — update the antigravity_ask/continue/"
        "agent_swarm docstrings and README, then this list."
    )


@pytest.mark.real_cli
def test_live_agy_model_families_are_all_documented(monkeypatch):
    """The reverse direction: agy grew a family our docs never mention.

    The test above only notices a model agy DROPPED, which is why agy adding the
    gemini-3.7-flash family (and moving the default onto it) slipped through a
    fully green suite and left every doc naming 3.6 as the default. A silently
    stale default is the more likely drift of the two: agy self-updates in the
    background, so nothing else in this repo ever fires when it happens.

    Compares families, not slugs, because the docs name only the -high variant of
    each family on purpose. Skips where agy isn't installed, so CI stays green.
    """
    monkeypatch.setattr(server, "_AGY_MODELS_CACHE", None)  # force a fresh read
    live = server.list_agy_models()
    if not live:
        pytest.skip("agy not installed or `agy models` unreadable")
    documented = {_model_family(m) for m in DOCUMENTED_AGY_MODELS}
    undocumented = sorted({_model_family(m) for m in live} - documented)
    assert not undocumented, (
        f"agy now offers model family/families the docs never mention: {undocumented}. "
        f"Live list: {live}. Check `agy changelog` for a default-model move too, then "
        "update the antigravity_ask/continue/agent_swarm docstrings, the README model "
        "list, and DOCUMENTED_AGY_MODELS."
    )


# --------------------------------------------------------------------------
# _startup_checks  (composition of the tested helpers; agy version injected)
# --------------------------------------------------------------------------


def test_startup_checks_warns_on_newer_agy(monkeypatch, caplog):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "2.0.0")
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: None)
    caplog.set_level("WARNING", logger="agy_bridge")
    server._startup_checks()
    assert "newer" in caplog.text


def test_startup_checks_silent_on_verified_agy(monkeypatch, caplog):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.10")
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: None)
    caplog.set_level("WARNING", logger="agy_bridge")
    server._startup_checks()
    assert caplog.text == ""


def test_startup_checks_silent_when_agy_unavailable(monkeypatch, caplog):
    monkeypatch.setattr(server, "_get_agy_version", lambda: None)
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: None)
    caplog.set_level("WARNING", logger="agy_bridge")
    server._startup_checks()
    assert caplog.text == ""


def test_startup_checks_warns_on_newer_bridge_release(monkeypatch, caplog):
    # agy is fine; a newer bridge tag exists on GitHub -> update nag fires.
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.10")
    monkeypatch.setattr(server, "__version__", "0.8.0")
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: (0, 9, 0))
    caplog.set_level("WARNING", logger="agy_bridge")
    server._startup_checks()
    assert "0.9.0" in caplog.text
    assert "uvx agent-intern@latest" in caplog.text


def test_startup_checks_skips_update_check_when_disabled(monkeypatch, caplog):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.10")
    monkeypatch.setenv("AGY_BRIDGE_NO_UPDATE_CHECK", "1")

    def _boom():
        raise AssertionError("update check must not run when disabled")

    monkeypatch.setattr(server, "_fetch_latest_release_version", _boom)
    caplog.set_level("WARNING", logger="agy_bridge")
    server._startup_checks()
    assert caplog.text == ""


# --------------------------------------------------------------------------
# _resolve_and_read
# --------------------------------------------------------------------------


def test_resolve_and_read_uses_pinned_conv(brain_dir):
    _write_transcript(brain_dir, "pinned", [_entry("PLANNER_RESPONSE", "P")])
    assert server._resolve_and_read("pinned", "C:\\ws", time.time()) == "P"


def test_resolve_and_read_uses_last_conv(brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\ws": "lc"}), encoding="utf-8")
    _write_transcript(brain_dir, "lc", [_entry("PLANNER_RESPONSE", "L")])
    assert server._resolve_and_read(None, "C:\\ws", time.time()) == "L"


def test_resolve_and_read_falls_back_to_newest(brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    start = time.time()
    _write_transcript(brain_dir, "newest", [_entry("PLANNER_RESPONSE", "N")])
    os.utime(brain_dir / "newest", (start + 5, start + 5))
    assert server._resolve_and_read(None, "C:\\ws", start) == "N"


def test_resolve_and_read_raises_when_unresolvable(brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="No conversation found"):
        server._resolve_and_read(None, "C:\\ws", time.time())


# --------------------------------------------------------------------------
# _run_agy bounded poll
# --------------------------------------------------------------------------


def _ok_proc(*args, **kwargs):
    return subprocess.CompletedProcess(args[0] if args else [], 0, stdout="", stderr="")


def test_run_agy_polls_until_resolve_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky(pinned, ws, start):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("not ready")
        return "answer"

    monkeypatch.setattr(server.proc_tree, "run_captured", _ok_proc)
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_resolve_and_read", flaky)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 5.0)

    out = server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == "answer"
    assert calls["n"] == 3


def test_run_agy_reraises_after_poll_deadline(monkeypatch):
    def always_fail(pinned, ws, start):
        raise RuntimeError("No conversation found after agy run")

    monkeypatch.setattr(server.proc_tree, "run_captured", _ok_proc)
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_resolve_and_read", always_fail)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 0.0)

    with pytest.raises(RuntimeError, match="No conversation found"):
        server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)


# --------------------------------------------------------------------------
# _run_agy orchestration (subprocess mocked)
# --------------------------------------------------------------------------


@pytest.fixture
def fake_agy(monkeypatch, brain_dir, last_conv_file):
    """Mock subprocess.run, capture args, no-op the poll sleep.

    Pins the agy version to a pre-1.1.8 one so `--output-format json` is OFF and
    these tests exercise the plain-text stdout path deterministically, regardless
    of what agy is installed on the machine running the suite. The json path has
    its own fixture (fake_agy_json).

    The slash-command gate is pinned OFF for the same reason: it is a process-wide
    cache resolved from the REAL `agy --version`, so without pinning, argv
    assertions here would depend on whether the machine running the suite has agy
    1.1.9+ installed (CI has no agy at all). fake_agy_slash turns it on.
    """
    cap = {"args": None, "kwargs": None, "returncode": 0, "stdout": "", "stderr": ""}

    def fake_run(args, **kwargs):
        cap["args"] = args
        cap["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args, cap["returncode"], stdout=cap["stdout"], stderr=cap["stderr"]
        )

    monkeypatch.setattr(server.proc_tree, "run_captured", fake_run)
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 0.0)
    monkeypatch.setattr(server, "_AGY_JSON_SUPPORT", False)
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", False)
    return cap


@pytest.fixture
def fake_agy_json(fake_agy, monkeypatch):
    """fake_agy with agy 1.1.8+ structured output ENABLED (--output-format json)."""
    monkeypatch.setattr(server, "_AGY_JSON_SUPPORT", True)
    return fake_agy


@pytest.fixture
def fake_agy_slash(fake_agy, monkeypatch):
    """fake_agy with agy 1.1.9+ slash-command expansion — so the guard flag is ON."""
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", True)
    return fake_agy


def test_run_antigravity_continue_with_pinned_id(fake_agy, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\ws": "c1"}), encoding="utf-8")
    _write_transcript(brain_dir, "c1", [_entry("PLANNER_RESPONSE", "ans")])
    out = server._run_agy("hi", "C:\\ws", continue_conv=True, timeout_s=10)
    assert out == "ans"
    assert "--conversation" in fake_agy["args"]
    assert "c1" in fake_agy["args"]


def test_run_antigravity_continue_without_id_uses_dash_c(fake_agy, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    _write_transcript(brain_dir, "newest", [_entry("PLANNER_RESPONSE", "ans")])
    os.utime(brain_dir / "newest", (time.time() + 5, time.time() + 5))
    out = server._run_agy("hi", "C:\\ws", continue_conv=True, timeout_s=10)
    assert out == "ans"
    assert "-c" in fake_agy["args"]
    assert "--conversation" not in fake_agy["args"]


def test_run_antigravity_ask_has_no_continue_flags(fake_agy, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\ws": "c1"}), encoding="utf-8")
    _write_transcript(brain_dir, "c1", [_entry("PLANNER_RESPONSE", "ans")])
    server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert "-c" not in fake_agy["args"]
    assert "--conversation" not in fake_agy["args"]


def test_run_agy_prefers_stdout_when_present(fake_agy, last_conv_file):
    # agy 1.0.15+ writes the clean answer to stdout. When present it must be used
    # directly, WITHOUT resolving/reading a transcript — here there is no conv on
    # record, so a transcript read would raise "No conversation found".
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy["stdout"] = "  stdout answer\n"
    out = server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == "stdout answer"


def test_run_agy_stdout_takes_precedence_over_transcript(fake_agy, brain_dir, last_conv_file):
    # Both stdout and a transcript are available; stdout wins (no schema parsing).
    last_conv_file.write_text(json.dumps({"C:\\ws": "c1"}), encoding="utf-8")
    _write_transcript(brain_dir, "c1", [_entry("PLANNER_RESPONSE", "transcript answer")])
    fake_agy["stdout"] = "stdout answer"
    out = server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == "stdout answer"


def test_run_agy_falls_back_to_transcript_when_stdout_empty(fake_agy, brain_dir, last_conv_file):
    # Older agy / non-Windows / --sandbox leave stdout empty: use the transcript.
    last_conv_file.write_text(json.dumps({"C:\\ws": "c1"}), encoding="utf-8")
    _write_transcript(brain_dir, "c1", [_entry("PLANNER_RESPONSE", "transcript answer")])
    fake_agy["stdout"] = "   \n"  # whitespace only -> treated as empty
    out = server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == "transcript answer"


def test_run_agy_nonzero_exit_raises(fake_agy):
    fake_agy["returncode"] = 1
    fake_agy["stderr"] = "boom"
    with pytest.raises(RuntimeError, match="boom"):
        server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)


def test_run_agy_unresolved_conversation_raises(fake_agy, last_conv_file):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="No conversation found"):
        server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)


def test_run_agy_exit0_without_answer_surfaces_agy_stderr(fake_agy, last_conv_file):
    """The agy 1.1.3 soft-deny shape: exit 0, empty stdout, reason only on stderr.

    The scrape failure is a symptom, so the error must carry agy's own notice —
    otherwise a permission denial reads as a bridge/transcript bug.
    """
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy["stderr"] = 'a tool required the "command" permission, so it was auto-denied'
    with pytest.raises(RuntimeError, match="auto-denied"):
        server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)


def test_run_agy_exit0_without_answer_keeps_scrape_error_when_stderr_empty(
    fake_agy, last_conv_file
):
    # No stderr to add: the original scrape failure must survive unchanged.
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="No conversation found"):
        server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)


# --------------------------------------------------------------------------
# agy 1.1.8 structured print-mode output (--output-format json)
# --------------------------------------------------------------------------


def _json_result(response="ans", conv="c-json", status="SUCCESS", **extra):
    """The result object agy 1.1.8 prints for `-p --output-format json`."""
    obj = {
        "conversation_id": conv,
        "status": status,
        "response": response,
        "duration_seconds": 2.17,
        "num_turns": 1,
        "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 0},
    }
    obj.update(extra)
    return json.dumps(obj)


@pytest.mark.parametrize(
    "version_out,expected",
    [
        ("1.1.8", True),
        ("1.2.0", True),
        ("2.0.0", True),
        ("1.1.7", False),
        ("1.1.6", False),
        ("1.0.15", False),
        ("", False),  # unparseable -> stay on the text path
        ("not a version", False),
    ],
)
def test_supports_json_output_version_gate(monkeypatch, version_out, expected):
    monkeypatch.setattr(server, "_AGY_JSON_SUPPORT", None)
    monkeypatch.setattr(server, "_get_agy_version", lambda: version_out)
    assert server.supports_json_output() is expected


def test_supports_json_output_is_cached(monkeypatch):
    monkeypatch.setattr(server, "_AGY_JSON_SUPPORT", None)
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return "1.1.8"

    monkeypatch.setattr(server, "_get_agy_version", counted)
    assert server.supports_json_output() is True
    assert server.supports_json_output() is True
    assert calls["n"] == 1  # version probed once per process


def test_supports_json_output_false_when_agy_missing(monkeypatch):
    monkeypatch.setattr(server, "_AGY_JSON_SUPPORT", None)
    monkeypatch.setattr(server, "_get_agy_version", lambda: None)
    assert server.supports_json_output() is False


def test_parse_json_result_reads_agy_object():
    out = server._parse_json_result(_json_result(response="hello", conv="abc"))
    assert out["response"] == "hello"
    assert out["conversation_id"] == "abc"
    assert out["usage"]["cache_read_tokens"] == 0


def test_parse_json_result_tolerates_surrounding_whitespace():
    assert server._parse_json_result("\n  " + _json_result() + "  \n")["response"] == "ans"


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "   ",
        "plain text answer",  # an agy that ignored --output-format
        "0.21.4",
        '{"conversation_id": "c", "status": "SUCCESS"}',  # no `response` field
        '{"broken": ',  # truncated / malformed
        "[1, 2, 3]",  # valid JSON, wrong shape
        '"just a string"',
    ],
)
def test_parse_json_result_returns_none_for_non_results(stdout):
    assert server._parse_json_result(stdout) is None


# --------------------------------------------------------------------------
# _parse_json_result banner tolerance (agy 1.1.10 advisory banners)
# --------------------------------------------------------------------------

_BANNER = (
    "This conversation is already open in another CLI instance on this machine.\n"
    "Use /fork to work on a copy instead.\n"
)


def test_parse_json_result_tolerates_leading_banner():
    """agy 1.1.10 prints a non-blocking advisory when the same conversation is open
    elsewhere — exactly what *_continue and the swarm can trigger. A strict
    startswith("{") test would miss the object and hand the user a raw JSON blob.
    """
    out = server._parse_json_result(_BANNER + _json_result(response="hello"))
    assert out["response"] == "hello"


def test_parse_json_result_tolerates_trailing_chatter():
    out = server._parse_json_result(_json_result(response="hello") + "\n" + _BANNER)
    assert out["response"] == "hello"


def test_parse_json_result_tolerates_banner_on_both_sides():
    out = server._parse_json_result(_BANNER + _json_result(response="hello") + _BANNER)
    assert out["response"] == "hello"


def test_parse_json_result_skips_non_result_objects_before_the_result():
    """A leading JSON object that isn't the result (no `response`) must not stop the
    scan — keep looking for the real one.
    """
    out = server._parse_json_result('{"note": "heads up"}\n' + _json_result(response="hello"))
    assert out["response"] == "hello"


def test_parse_json_result_ignores_braces_in_a_plain_text_answer():
    """Prose containing braces is still not a result object."""
    assert server._parse_json_result("the config is {a: 1} roughly") is None


# --------------------------------------------------------------------------
# UTF-8 decoding of backend stdout (locale codepage mangles non-ASCII)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [server, codex_bridge, copilot_bridge, cursor_bridge, swarm],
    ids=["server", "codex", "copilot", "cursor", "swarm"],
)
def test_bridges_decode_subprocess_output_as_utf8(module):
    """Every module that spawns a backend must decode its stdout as UTF-8.

    Bare text=True decodes with locale.getpreferredencoding() — cp1254 on a Turkish
    Windows, cp1252 elsewhere — which silently mangles every non-ASCII answer:
    "dosyası" came back from antigravity_ask as "dosyasÄ±", exactly
    'dosyası'.encode('utf-8').decode('cp1254').
    """
    assert module._TEXT == {"encoding": "utf-8", "errors": "replace"}


@pytest.mark.parametrize(
    "path",
    ["server.py", "codex_bridge.py", "copilot_bridge.py", "cursor_bridge.py", "swarm.py"],
)
def test_no_subprocess_call_uses_bare_text_true(path):
    """Regression guard for the mojibake bug: a new subprocess call must spread
    **_TEXT, never `text=True` (which silently picks the locale codepage). Matched
    with the trailing comma so the prose in the _TEXT comments doesn't trip it.
    """
    source = (Path(__file__).parent / path).read_text(encoding="utf-8")
    assert "text=True," not in source


def test_run_agy_decodes_stdout_as_utf8(fake_agy, brain_dir, last_conv_file):
    """The kwargs actually handed to subprocess.run, not just the constant."""
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy["stdout"] = "answer"
    server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert fake_agy["kwargs"]["encoding"] == "utf-8"
    assert fake_agy["kwargs"]["errors"] == "replace"


def test_utf8_decoding_survives_the_turkish_roundtrip(fake_agy, brain_dir, last_conv_file):
    """The real-world symptom: a Turkish answer must come back intact, not as the
    cp1254 mis-decode that shipped before this fix.
    """
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    answer = "dosyası açıklaması şudur"
    fake_agy["stdout"] = answer
    out = server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == answer
    assert "Ä" not in out


@pytest.mark.parametrize("fmt", ["json", "stream-json"])
def test_build_agy_args_adds_output_format_when_requested(monkeypatch, fmt):
    monkeypatch.setattr(server, "AGY_BIN", "agy")
    args, _ = server._build_agy_args(
        "hi", "C:\\ws", continue_conv=False, timeout_s=10, output_format=fmt
    )
    assert args[args.index("--output-format") + 1] == fmt
    # -p takes the prompt as its VALUE, so every other flag must precede it.
    assert args.index("--output-format") < args.index("-p")
    assert args[-2:] == ["-p", "hi"]


def test_build_agy_args_omits_output_format_by_default(monkeypatch):
    monkeypatch.setattr(server, "AGY_BIN", "agy")
    args, _ = server._build_agy_args("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert "--output-format" not in args


def test_run_agy_json_returns_response_field(fake_agy_json, last_conv_file):
    # No conversation on record: a transcript scrape would raise, so returning the
    # answer proves it came from the parsed `response` field.
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy_json["stdout"] = _json_result(response="  structured answer\n")
    out = server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == "structured answer"
    assert "--output-format" in fake_agy_json["args"]


def test_run_agy_json_records_conversation_id(fake_agy_json, last_conv_file):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy_json["stdout"] = _json_result(conv="conv-from-agy")
    server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert server._recorded_conv_id("C:\\ws") == "conv-from-agy"


def test_run_agy_json_continue_pins_recorded_id_over_last_conversations(
    fake_agy_json, last_conv_file
):
    """The whole point of capturing conversation_id: continue resumes OUR thread.

    last_conversations.json is shared state agy rewrites for every session, so a
    user's interactive run in the same folder could otherwise hijack the pin.
    """
    last_conv_file.write_text(json.dumps({"C:\\ws": "someone-elses-conv"}), encoding="utf-8")
    fake_agy_json["stdout"] = _json_result(conv="our-conv")
    server._run_agy("first", "C:\\ws", continue_conv=False, timeout_s=10)

    server._run_agy("second", "C:\\ws", continue_conv=True, timeout_s=10)
    args = fake_agy_json["args"]
    assert args[args.index("--conversation") + 1] == "our-conv"
    assert "someone-elses-conv" not in args


def test_run_agy_json_continue_falls_back_to_last_conversations(fake_agy_json, last_conv_file):
    # Nothing recorded yet (fresh process): the old resolution order still applies.
    last_conv_file.write_text(json.dumps({"C:\\ws": "from-file"}), encoding="utf-8")
    fake_agy_json["stdout"] = _json_result()
    server._run_agy("hi", "C:\\ws", continue_conv=True, timeout_s=10)
    args = fake_agy_json["args"]
    assert args[args.index("--conversation") + 1] == "from-file"


def test_recorded_conv_id_round_trips_the_same_path():
    server._record_conv_id("C:\\Proj", "c9")
    assert server._recorded_conv_id("C:\\Proj") == "c9"
    assert server._recorded_conv_id("C:\\Other") is None


def test_recorded_conv_id_case_matching_follows_the_platform():
    """Keys go through os.path.normcase, so matching mirrors the real filesystem.

    Case-insensitive on Windows; case-SENSITIVE on POSIX, where `/Proj` and `/proj`
    genuinely are different directories. Asserting one behaviour on both platforms
    is what broke CI on macOS/Linux while passing on Windows.
    """
    server._record_conv_id("C:\\Proj", "c9")
    hit = server._recorded_conv_id("c:\\proj")
    assert hit == ("c9" if os.name == "nt" else None)


def test_run_agy_json_failure_status_without_response_raises(fake_agy_json, last_conv_file):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy_json["stdout"] = _json_result(response="", status="ERROR", conv="c-bad")
    with pytest.raises(RuntimeError, match="status=ERROR"):
        server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)


def test_run_agy_json_failure_status_with_response_still_returns_it(fake_agy_json, last_conv_file):
    # agy's status vocabulary may grow; a real answer must not be thrown away.
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy_json["stdout"] = _json_result(response="partial answer", status="SOMETHING_NEW")
    out = server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == "partial answer"


# agy 1.2.10's stderr when --print-timeout expires mid-turn (verified live).
_PRINT_TIMEOUT_STDERR = (
    "[agy] print timeout after 10s with turn in progress; returning partial output\n"
)


def test_run_agy_print_timeout_is_an_error_not_a_truncated_answer(fake_agy_json, last_conv_file):
    """agy 1.2.0+ reports an expired --print-timeout as exit 0 + status SUCCESS.

    Verified on 1.2.10 — only stderr says the turn was cut. Returning `response`
    would hand the caller a truncated answer as if it were the whole one.
    """
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy_json["stdout"] = _json_result(response="The hurdle of Western movable", conv="cut")
    fake_agy_json["stderr"] = _PRINT_TIMEOUT_STDERR
    with pytest.raises(RuntimeError, match="10s timeout") as exc:
        server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert "Western movable" in str(exc.value)  # the partial rides along, labelled as cut
    # Still this workspace's newest thread, so a later continue must follow it.
    assert server._recorded_conv_id("C:\\ws") == "cut"


def test_run_agy_print_timeout_unpinned_leaves_the_continue_slot(fake_agy_json, last_conv_file):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy_json["stdout"] = _json_result(response="half", conv="swarm-worker")
    fake_agy_json["stderr"] = _PRINT_TIMEOUT_STDERR
    with pytest.raises(RuntimeError, match="timeout"):
        server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10, pin=False)
    assert server._recorded_conv_id("C:\\ws") is None


def test_run_agy_print_timeout_on_the_plain_text_path(fake_agy, last_conv_file):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy["stdout"] = "half an answ"
    fake_agy["stderr"] = _PRINT_TIMEOUT_STDERR
    with pytest.raises(RuntimeError, match="10s timeout") as exc:
        server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert "half an answ" in str(exc.value)


def test_run_agy_json_empty_response_falls_back_to_transcript(
    fake_agy_json, brain_dir, last_conv_file
):
    last_conv_file.write_text(json.dumps({"C:\\ws": "c1"}), encoding="utf-8")
    _write_transcript(brain_dir, "c1", [_entry("PLANNER_RESPONSE", "transcript answer")])
    fake_agy_json["stdout"] = _json_result(response="", conv="c1")
    out = server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == "transcript answer"


def test_run_agy_json_degrades_to_text_when_agy_ignores_the_flag(fake_agy_json, last_conv_file):
    """Verified on 1.1.8: an unrecognised --output-format VALUE prints plain text.

    So structured output being requested must never mean structured output is
    assumed — plain stdout still has to come back as the answer.
    """
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy_json["stdout"] = "plain text answer"
    out = server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == "plain text answer"


def test_run_agy_json_result_survives_json_schema_extra_fields(fake_agy_json, last_conv_file):
    # --json-schema adds `structured_output`; unknown keys must not break parsing.
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_agy_json["stdout"] = _json_result(
        response="ok", structured_output={"name": "x"}, json_schema={"type": "object"}
    )
    assert server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10) == "ok"


def test_build_agy_args_output_format_is_opt_in(monkeypatch):
    """Structured output is per-caller: the swarm builds its own argv and wants text."""
    monkeypatch.setattr(server, "AGY_BIN", "agy")
    for cont in (False, True):
        args, _ = server._build_agy_args("hi", "C:\\ws", continue_conv=cont, timeout_s=10)
        assert "--output-format" not in args
    assert "--output-format" not in server._agy_base_args(10)


# --------------------------------------------------------------------------
# _StreamWatch — agy 1.1.8 stream-json events -> live watch steps
# --------------------------------------------------------------------------


def _ev(**payload):
    """One NDJSON step_update line, in agy 1.1.8's shape."""
    return json.dumps({"event": "step_update", "step_update": payload})


def _feed(*lines, rid="r1"):
    sw = server._StreamWatch(rid, time.time())
    for line in lines:
        sw.feed_line(line)
    return sw


def _kinds(rid="r1"):
    return [(e["kind"], e["text"]) for e in server._watch_snapshot(rid)["events"]]


@pytest.fixture
def watch_slot():
    """A watch run to append events to, so _StreamWatch output is observable."""
    server._WATCH_RUNS["r1"] = server._watch_state("r1", "t", time.time(), 10, "agy", "p", [], 0.0)
    return "r1"


def test_stream_watch_captures_conversation_id_from_init(watch_slot):
    sw = _feed(json.dumps({"event": "init", "conversation_id": "conv-9", "init": {"cwd": "x"}}))
    assert sw.conv_id == "conv-9"
    assert sw.saw_event is True


def test_stream_watch_accumulates_text_deltas_into_one_narration(watch_slot):
    """text_delta arrives in fragments; the viewer must not get them one per line."""
    _feed(
        _ev(
            step_index=2, state="ACTIVE", step_type="agent_response", text_delta="I will count the "
        ),
        _ev(step_index=2, state="ACTIVE", step_type="agent_response", text_delta="py files."),
        _ev(step_index=2, state="DONE", step_type="agent_response", text_delta="\n"),
    )
    assert _kinds() == [("narration", "I will count the py files.")]


def test_stream_watch_emits_nothing_until_the_step_completes(watch_slot):
    _feed(_ev(step_index=1, state="ACTIVE", step_type="agent_response", text_delta="partial"))
    assert _kinds() == []


def test_stream_watch_narration_is_first_line_and_capped(watch_slot):
    _feed(
        _ev(
            step_index=1,
            state="DONE",
            step_type="agent_response",
            text_delta="x" * 300 + "\nsecond line",
        )
    )
    (kind, text) = _kinds()[0]
    assert kind == "narration"
    assert text == "x" * 200


def test_stream_watch_reports_the_real_command(watch_slot):
    """tool_info.parameters.CommandLine is a REAL nested object here — unlike the
    transcript, which stores it JSON-encoded inside a string."""
    _feed(
        _ev(
            step_index=3,
            state="ACTIVE",
            step_type="tool",
            tool_name="run_command",
            tool_info={"name": "run_command", "parameters": {"CommandLine": "git status"}},
        ),
        _ev(step_index=3, state="DONE", step_type="tool", tool_name="run_command", tool_info={}),
    )
    assert _kinds() == [("command", "git status"), ("result", "command finished")]


def test_stream_watch_falls_back_to_tool_name_without_a_command(watch_slot):
    _feed(_ev(step_index=4, state="ACTIVE", step_type="tool", tool_name="view_file"))
    assert _kinds() == [("command", "view_file")]


def test_stream_watch_captures_terminal_result(watch_slot):
    sw = _feed(
        json.dumps(
            {
                "event": "result",
                "result": {"conversation_id": "c5", "status": "SUCCESS", "response": "done\n"},
            }
        )
    )
    assert sw.result["response"] == "done\n"
    assert sw.conv_id == "c5"


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "not json at all",
        "{bad json",
        "[1,2,3]",
        '"a string"',
        json.dumps({"event": "unknown_kind", "payload": 1}),
        json.dumps({"event": "step_update", "step_update": "not a dict"}),
        json.dumps({"event": "result", "result": None}),
    ],
)
def test_stream_watch_ignores_malformed_lines(watch_slot, line):
    """A format change must degrade to 'no live steps', never kill the run."""
    sw = _feed(line)
    assert _kinds() == []
    assert sw.result is None


def test_stream_watch_does_not_double_emit_a_repeated_done(watch_slot):
    done = _ev(step_index=1, state="DONE", step_type="agent_response", text_delta="hello")
    _feed(done, done)
    assert _kinds() == [("narration", "hello")]


# --------------------------------------------------------------------------
# _run_agy_watched on stream-json (agy 1.1.8+): no transcript involved
# --------------------------------------------------------------------------


def _stream_lines(conv="c-stream", response="stream answer", status="SUCCESS"):
    """A realistic agy 1.1.8 stream-json run: init, narration, a tool, the result."""
    return (
        json.dumps({"event": "init", "conversation_id": conv, "init": {"cwd": "C:\\ws"}})
        + "\n"
        + _ev(step_index=0, state="DONE", step_type="user_input")
        + "\n"
        + _ev(step_index=1, state="ACTIVE", step_type="agent_response", text_delta="Checking ")
        + "\n"
        + _ev(step_index=1, state="DONE", step_type="agent_response", text_delta="the repo.\n")
        + "\n"
        + _ev(
            step_index=2,
            state="ACTIVE",
            step_type="tool",
            tool_name="run_command",
            tool_info={"name": "run_command", "parameters": {"CommandLine": "git status"}},
        )
        + "\n"
        + _ev(step_index=2, state="DONE", step_type="tool", tool_name="run_command")
        + "\n"
        + json.dumps(
            {
                "event": "result",
                "result": {"conversation_id": conv, "status": status, "response": response},
            }
        )
        + "\n"
    )


@pytest.fixture
def fake_watched_agy(monkeypatch):
    """Run _run_agy_watched against a scripted stdout, with no browser or server."""
    cfg = {"stdout": "", "stderr": "", "returncode": 0, "args": None}

    class _Popen:
        def __init__(self, args, **kwargs):
            cfg["args"] = args
            self.returncode = cfg["returncode"]
            self.stdout = io.StringIO(cfg["stdout"])
            self.stderr = io.StringIO(cfg["stderr"])
            self._n = 2

        def poll(self):
            self._n -= 1
            return None if self._n > 0 else self.returncode

        def kill(self):
            pass

    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: _Popen(*a, **k))
    monkeypatch.setattr(server, "_ensure_watch_server", lambda: 12345)
    monkeypatch.setattr(server, "_open_watch_window", lambda *a, **k: None)
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 0.0)
    monkeypatch.setattr(server, "_AGY_JSON_SUPPORT", True)
    # Pinned like fake_agy does: otherwise the gate resolves from a real `agy
    # --version`, which lands on the fake Popen above whenever no earlier test in
    # the run happened to warm the cache (e.g. under -k).
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", False)
    return cfg


def test_watched_stream_returns_result_response_without_any_transcript(
    fake_watched_agy, brain_dir, last_conv_file
):
    """The point of the migration: no JSONL, no SQLite, no conversation guessing."""
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")  # nothing to scrape
    fake_watched_agy["stdout"] = _stream_lines(response="stream answer")
    out = server._run_agy_watched("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == "stream answer"
    assert "--output-format" in fake_watched_agy["args"]
    assert fake_watched_agy["args"][fake_watched_agy["args"].index("--output-format") + 1] == (
        "stream-json"
    )


def test_watched_stream_populates_live_steps_in_the_viewer(
    fake_watched_agy, brain_dir, last_conv_file
):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_watched_agy["stdout"] = _stream_lines()
    server._run_agy_watched("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    events = [(e["kind"], e["text"]) for e in server._watch_snapshot()["events"]]
    assert ("narration", "Checking the repo.") in events
    assert ("command", "git status") in events
    assert ("result", "command finished") in events


def test_watched_stream_records_conversation_id_for_later_continue(
    fake_watched_agy, brain_dir, last_conv_file
):
    """Closes the asymmetry: a watched run now pins later continues like ask does."""
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_watched_agy["stdout"] = _stream_lines(conv="watched-conv")
    server._run_agy_watched("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert server._recorded_conv_id("C:\\ws") == "watched-conv"


def test_watched_stream_failure_status_raises(fake_watched_agy, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_watched_agy["stdout"] = _stream_lines(response="", status="ERROR")
    with pytest.raises(RuntimeError, match="status=ERROR"):
        server._run_agy_watched("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert server._watch_snapshot()["status"] == "error"


def test_watched_print_timeout_is_an_error_not_a_truncated_answer(
    fake_watched_agy, brain_dir, last_conv_file
):
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_watched_agy["stdout"] = _stream_lines(response="cut mid-sent", conv="wc-cut")
    fake_watched_agy["stderr"] = _PRINT_TIMEOUT_STDERR
    with pytest.raises(RuntimeError, match="10s timeout") as exc:
        server._run_agy_watched("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert "cut mid-sent" in str(exc.value)
    assert server._watch_snapshot()["status"] == "error"
    assert server._recorded_conv_id("C:\\ws") == "wc-cut"


def test_watched_stream_without_result_event_falls_back_to_transcript(
    fake_watched_agy, brain_dir, last_conv_file
):
    """agy died mid-stream or ignored the flag: the old scrape still has to work."""
    last_conv_file.write_text(json.dumps({"C:\\ws": "wc"}), encoding="utf-8")
    _write_transcript(brain_dir, "wc", [_entry("PLANNER_RESPONSE", "from transcript")])
    fake_watched_agy["stdout"] = ""  # no events at all
    out = server._run_agy_watched("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == "from transcript"


def test_watched_stream_answer_matches_the_non_watched_path(
    fake_watched_agy, fake_agy_json, brain_dir, last_conv_file, monkeypatch
):
    """watch=True and watch=False must not disagree about what the answer IS.

    Both now read agy's `result`/`response`, so a multi-step run returns the same
    full turn text either way.
    """
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")
    fake_watched_agy["stdout"] = _stream_lines(response="the same answer")
    watched = server._run_agy_watched("hi", "C:\\ws", continue_conv=False, timeout_s=10)

    monkeypatch.setattr(server.subprocess, "Popen", None)  # not used by the plain path
    fake_agy_json["stdout"] = _json_result(response="the same answer")
    plain = server._run_agy("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert watched == plain == "the same answer"


# --------------------------------------------------------------------------
# _pump_pipe — drains AND parses, so the child can't block on a full pipe
# --------------------------------------------------------------------------


def test_pump_pipe_hands_every_line_to_the_handler():
    seen = []
    t, chunks = server._pump_pipe(io.StringIO("a\nb\nc\n"), seen.append)
    t.join(timeout=5)
    assert [s.strip() for s in seen] == ["a", "b", "c"]
    assert "".join(chunks) == "a\nb\nc\n"


def test_pump_pipe_keeps_draining_when_the_handler_raises():
    """A parse bug must not stop the drain — that would re-create the pipe-buffer hang."""
    seen = []

    def flaky(line):
        seen.append(line)
        raise ValueError("boom")

    t, chunks = server._pump_pipe(io.StringIO("a\nb\n"), flaky)
    t.join(timeout=5)
    assert len(seen) == 2
    assert "".join(chunks) == "a\nb\n"


def test_run_agy_args_include_print_timeout_and_prompt(fake_agy, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\ws": "c1"}), encoding="utf-8")
    _write_transcript(brain_dir, "c1", [_entry("PLANNER_RESPONSE", "ans")])
    server._run_agy("my-prompt", "C:\\ws", continue_conv=False, timeout_s=42)
    args = fake_agy["args"]
    assert "--print-timeout" in args
    assert "42s" in args
    assert args[-2:] == ["-p", "my-prompt"]
    assert fake_agy["kwargs"]["cwd"] == "C:\\ws"


# --------------------------------------------------------------------------
# transcript reading: _transcript_entries
# --------------------------------------------------------------------------


def test_transcript_entries_parses_and_skips_malformed(brain_dir):
    _write_transcript(brain_dir, "te", ["{bad", "", _entry("PLANNER_RESPONSE", "ok")])
    entries = server._transcript_entries("te")
    assert len(entries) == 1
    assert entries[0]["content"] == "ok"


def test_transcript_entries_missing_returns_empty(brain_dir):
    assert server._transcript_entries("nope") == []


# --------------------------------------------------------------------------
# watch-mode formatters: _clean_tool_arg / _entry_to_watch_lines
# --------------------------------------------------------------------------


def test_clean_tool_arg_unwraps_json_encoded():
    # agy stores args double-encoded: a quoted/escaped string inside a string.
    assert server._clean_tool_arg('"python -c \\"print(1)\\""') == 'python -c "print(1)"'
    assert server._clean_tool_arg('"Compute 50 factorial"') == "Compute 50 factorial"


def test_clean_tool_arg_passthrough_and_none():
    assert server._clean_tool_arg("plain text") == "plain text"
    assert server._clean_tool_arg(None) == ""


def test_entry_to_watch_lines_planner_narration_and_command():
    cmd_arg = '"python -c \\"print(1)\\""'  # double-encoded as agy stores it
    entry = {
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "content": "I will compute it.",
        "tool_calls": [{"name": "run_command", "args": {"CommandLine": cmd_arg}}],
    }
    lines = server._entry_to_watch_lines(entry)
    assert ("narration", "I will compute it.") in lines
    assert ("command", 'python -c "print(1)"') in lines


def test_entry_to_watch_lines_command_falls_back_to_summary():
    entry = {
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "tool_calls": [{"name": "x", "args": {"toolSummary": '"Do the thing"'}}],
    }
    assert server._entry_to_watch_lines(entry) == [("command", "Do the thing")]


def test_entry_to_watch_lines_run_command_marker_and_skips_non_model():
    rc = {"source": "MODEL", "type": "RUN_COMMAND", "content": "Output: 1"}
    assert server._entry_to_watch_lines(rc) == [("result", "command finished")]
    user = {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "x"}
    assert server._entry_to_watch_lines(user) == []


# --------------------------------------------------------------------------
# subprocess test double (shared by the watched-run tests)
# --------------------------------------------------------------------------


class _FakePopen:
    def __init__(self, *a, polls=1, returncode=0, **k):
        self._polls = polls
        self.returncode = returncode
        # watched runners drain these; empty pipes keep the transcript the answer source
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")

    def poll(self):
        if self._polls > 0:
            self._polls -= 1
            return None
        return self.returncode

    def communicate(self, timeout=None):
        return ("", "")

    def kill(self):
        self.returncode = -9


# --------------------------------------------------------------------------
# watch mode: browser viewer state + _WatchFeed + _run_agy_watched
# --------------------------------------------------------------------------


def _tok() -> str:
    return server._WATCH_TOKEN


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "127.0.0.1", "LOCALHOST"])
def test_watch_authorized_accepts_loopback_with_token(host):
    assert server._watch_authorized(host, f"/events?id=main&k={_tok()}") is True


@pytest.mark.parametrize(
    "host",
    [
        "evil.example.com:8765",  # classic DNS-rebinding Host
        "127.0.0.1.nip.io:8765",  # resolves to loopback, but is NOT a loopback name
        "attacker.local",
        "",  # every browser sends Host; a missing one is not our viewer
        None,
    ],
)
def test_watch_authorized_rejects_non_loopback_host(host):
    """The Host check is what actually kills DNS rebinding: the attacker's page
    reaches the port under THEIR hostname, so refusing anything that isn't a
    loopback literal closes the vector regardless of the token.
    """
    assert server._watch_authorized(host, f"/events?id=main&k={_tok()}") is False


@pytest.mark.parametrize("query", ["", "?id=main", "?id=main&k=", "?id=main&k=wrong-token"])
def test_watch_authorized_rejects_missing_or_wrong_token(query):
    """A local process can send whatever Host it likes, so the token is the part
    that stops another user/process on the same machine from reading prompts.
    """
    assert server._watch_authorized("127.0.0.1:8765", f"/events{query}") is False


def test_watch_token_is_not_predictable():
    assert len(server._WATCH_TOKEN) >= 16
    assert server._WATCH_TOKEN != secrets.token_urlsafe(16)


def test_watch_url_carries_id_and_token():
    url = server._watch_url(4321, "rid-9")
    assert url.startswith("http://127.0.0.1:4321/?id=rid-9&k=")
    assert server._WATCH_TOKEN in url
    # and the URL the bridge opens must itself pass the guard
    path = url.split("127.0.0.1:4321", 1)[1]
    assert server._watch_authorized("127.0.0.1:4321", path) is True


def test_swarm_watch_shares_the_same_guard():
    """The swarm dashboard exposes every worker's prompt at once, so it must not
    have its own weaker copy of the check.
    """
    import swarm_watch

    assert swarm_watch._watch_authorized is server._watch_authorized


def test_watch_state_lifecycle():
    rid = server._watch_begin("my title", 100.0)
    snap = server._watch_snapshot(rid)
    assert snap["status"] == "working"
    assert snap["title"] == "my title"
    assert snap["events"] == []
    server._watch_append(rid, [{"kind": "command", "text": "ls", "t": 1.0}])
    assert len(server._watch_snapshot(rid)["events"]) == 1
    server._watch_finish(rid, "done", "the answer", 5.0)
    snap = server._watch_snapshot(rid)
    assert snap["status"] == "done"
    assert snap["answer"] == "the answer"
    assert snap["elapsed"] == 5.0
    # snapshot is a copy — mutating it must not affect the shared state
    snap["events"].append("x")
    assert len(server._watch_snapshot(rid)["events"]) == 1


def test_watch_concurrent_runs_get_separate_ids_and_states():
    # A run that starts while another is still working must NOT clobber it.
    a = server._watch_begin("A", 100.0, backend="codex", prompt="prompt A")
    b = server._watch_begin("B", 101.0, backend="copilot", prompt="prompt B")
    assert a == "main"  # first claims the shared slot
    assert b != a  # second is still working → its own id + window
    server._watch_append(a, [{"kind": "narration", "text": "a-step", "t": 0.1}])
    server._watch_append(b, [{"kind": "narration", "text": "b-step", "t": 0.1}])
    server._watch_finish(a, "done", "answer A", 1.0)
    sa, sb = server._watch_snapshot(a), server._watch_snapshot(b)
    assert sa["prompt"] == "prompt A" and sa["answer"] == "answer A" and sa["status"] == "done"
    assert sb["prompt"] == "prompt B" and sb["status"] == "working"  # untouched by A
    assert [e["text"] for e in sa["events"]] == ["a-step"]
    assert [e["text"] for e in sb["events"]] == ["b-step"]


def test_watch_feed_locks_on_new_conv_and_emits_rich_events(brain_dir):
    _write_transcript(brain_dir, "old", [_entry("PLANNER_RESPONSE", "OLD")])
    start = time.time()
    rid = server._watch_begin("t", start)
    feed = server._WatchFeed(None, start, rid)  # snapshots {"old"}
    cmd_arg = '"python -c \\"print(1)\\""'  # double-encoded as agy stores it
    logs = brain_dir / "new" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    entry = json.dumps(
        {
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "content": "I will run it.",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": cmd_arg}}],
        }
    )
    (logs / "transcript.jsonl").write_text(entry, encoding="utf-8")
    os.utime(brain_dir / "new", (start + 5, start + 5))

    feed.pump()
    assert feed.conv == "new"  # locked onto this run's conversation, not 'old'
    pairs = [(e["kind"], e["text"]) for e in server._watch_snapshot(rid)["events"]]
    assert ("narration", "I will run it.") in pairs
    assert ("command", 'python -c "print(1)"') in pairs


def test_run_agy_watched_returns_answer_and_populates_state(monkeypatch, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\ws": "wc"}), encoding="utf-8")

    def create_transcript():
        _write_transcript(brain_dir, "wc", [_entry("PLANNER_RESPONSE", "final watch answer")])
        os.utime(brain_dir / "wc", (time.time() + 5, time.time() + 5))

    class _CreatingPopen:
        def __init__(self, *a, **k):
            self.returncode = 0
            self._n = 2
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")

        def poll(self):
            self._n -= 1
            if self._n == 1:
                create_transcript()  # the conversation appears once agy "starts"
            return None if self._n > 0 else self.returncode

        def communicate(self, timeout=None):
            return ("", "")

        def kill(self):
            pass

    opened = {}
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: _CreatingPopen())
    monkeypatch.setattr(server, "_ensure_watch_server", lambda: 12345)  # no real server
    monkeypatch.setattr(server, "_open_watch_window", lambda *a, **k: opened.update(url=a[0]))
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 0.0)

    out = server._run_agy_watched("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == "final watch answer"
    assert opened.get("url", "").startswith("http://127.0.0.1:12345/")
    snap = server._watch_snapshot()
    assert snap["status"] == "done"
    assert snap["answer"] == "final watch answer"


def test_run_agy_watched_stores_full_prompt_not_just_title(monkeypatch, brain_dir, last_conv_file):
    # The watch panel must receive the FULL prompt; `title` stays the short caption.
    last_conv_file.write_text(json.dumps({"C:\\ws": "wc"}), encoding="utf-8")
    long_prompt = "first line of the task\n" + "\n".join(f"detail line {i}" for i in range(30))

    def create_transcript():
        _write_transcript(brain_dir, "wc", [_entry("PLANNER_RESPONSE", "ok")])
        os.utime(brain_dir / "wc", (time.time() + 5, time.time() + 5))

    class _CreatingPopen:
        def __init__(self, *a, **k):
            self.returncode = 0
            self._n = 2
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")

        def poll(self):
            self._n -= 1
            if self._n == 1:
                create_transcript()
            return None if self._n > 0 else self.returncode

        def communicate(self, timeout=None):
            return ("", "")

        def kill(self):
            pass

    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: _CreatingPopen())
    monkeypatch.setattr(server, "_ensure_watch_server", lambda: 12345)
    monkeypatch.setattr(server, "_open_watch_window", lambda *a, **k: None)
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 0.0)

    server._run_agy_watched(long_prompt, "C:\\ws", continue_conv=False, timeout_s=10)
    snap = server._watch_snapshot()
    assert snap["prompt"] == long_prompt  # full prompt reaches the expandable panel
    assert snap["title"] == "first line of the task"  # caption is just the first line
    assert "detail line 29" not in snap["title"]  # the tail is NOT in the caption


def test_run_agy_watched_exit0_without_answer_surfaces_agy_stderr(
    monkeypatch, brain_dir, last_conv_file
):
    """The watched runner must fold agy's stderr into the error too (mirrors
    _run_agy). agy 1.1.3 soft-deny shape: exit 0, no transcript, reason on stderr.
    """
    last_conv_file.write_text(json.dumps({}), encoding="utf-8")  # nothing resolves
    denial = 'a tool required the "command" permission, so it was auto-denied'

    class _DenyingPopen:
        def __init__(self, *a, **k):
            self.returncode = 0
            self._polls = 1
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO(denial)  # never writes a transcript

        def poll(self):
            if self._polls > 0:
                self._polls -= 1
                return None
            return self.returncode

        def communicate(self, timeout=None):
            return ("", denial)

        def kill(self):
            pass

    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: _DenyingPopen())
    monkeypatch.setattr(server, "_ensure_watch_server", lambda: 12345)
    monkeypatch.setattr(server, "_open_watch_window", lambda *a, **k: None)
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 0.0)

    with pytest.raises(RuntimeError, match="auto-denied"):
        server._run_agy_watched("hi", "C:\\ws", continue_conv=False, timeout_s=10)


def test_run_agy_watched_continue_seeds_prior_turns_as_history(
    monkeypatch, brain_dir, last_conv_file
):
    # In continue mode the viewer must show the prior conversation, not a blank window.
    last_conv_file.write_text(json.dumps({"C:\\ws": "wc"}), encoding="utf-8")
    _write_transcript(
        brain_dir,
        "wc",
        [
            _user_input("<USER_REQUEST>\nönceki soru</USER_REQUEST>"),
            _entry("PLANNER_RESPONSE", "önceki cevap"),
        ],
    )
    os.utime(brain_dir / "wc", (time.time() + 5, time.time() + 5))

    class _Popen:  # exits at once; the prior transcript is already on disk
        def __init__(self, *a, **k):
            self.returncode = 0
            self._n = 1
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")

        def poll(self):
            self._n -= 1
            return None if self._n > 0 else self.returncode

        def communicate(self, timeout=None):
            return ("", "")

        def kill(self):
            pass

    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: _Popen())
    monkeypatch.setattr(server, "_ensure_watch_server", lambda: 12345)
    monkeypatch.setattr(server, "_open_watch_window", lambda *a, **k: None)
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 0.0)

    server._run_agy_watched("yeni soru", "C:\\ws", continue_conv=True, timeout_s=10)
    snap = server._watch_snapshot()
    assert snap["history"] == [
        {"role": "user", "content": "önceki soru"},
        {"role": "assistant", "content": "önceki cevap"},
    ]
    assert snap["prompt"] == "yeni soru"  # the new turn is the live prompt, not history


def test_run_agy_watched_browser_failure_is_nonfatal(monkeypatch, brain_dir, last_conv_file):
    # If opening the browser blows up, the run must still complete and return.
    last_conv_file.write_text(json.dumps({"C:\\ws": "wc"}), encoding="utf-8")
    _write_transcript(brain_dir, "wc", [_entry("PLANNER_RESPONSE", "answer anyway")])
    os.utime(brain_dir / "wc", (time.time() + 5, time.time() + 5))

    def boom():
        raise OSError("no display")

    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: _FakePopen(polls=0))
    monkeypatch.setattr(server, "_ensure_watch_server", boom)
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 0.0)

    out = server._run_agy_watched("hi", "C:\\ws", continue_conv=False, timeout_s=10)
    assert out == "answer anyway"


def test_open_watch_window_uses_chromium_app_mode(monkeypatch):
    monkeypatch.setattr(watch_server, "_chromium_app_browsers", lambda: ["/opt/chrome"])
    captured = {}
    monkeypatch.setattr(
        server.subprocess, "Popen", lambda args, **k: captured.update(args=args) or object()
    )
    server._open_watch_window("http://127.0.0.1:9/")
    assert captured["args"][0] == "/opt/chrome"
    assert "--app=http://127.0.0.1:9/" in captured["args"]
    assert any(a.startswith("--window-size=") for a in captured["args"])


def test_open_watch_window_falls_back_to_new_window(monkeypatch):
    monkeypatch.setattr(watch_server, "_chromium_app_browsers", lambda: [])  # no Chromium found
    opened = {}
    monkeypatch.setattr(
        watch_server.webbrowser, "open", lambda url, new=0: opened.update(url=url, new=new)
    )
    server._open_watch_window("http://x/")
    assert opened == {"url": "http://x/", "new": 1}


def test_watch_html_substitutes_window_size(monkeypatch):
    monkeypatch.setattr(watch_server, "_WATCH_WINDOW_SIZE", "480,640")
    html = server._watch_html()
    assert "window.resizeTo(480,640)" in html
    assert "__WIN_W__" not in html and "__WIN_H__" not in html


def test_watch_html_bad_size_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(watch_server, "_WATCH_WINDOW_SIZE", "garbage")
    html = server._watch_html()
    assert "window.resizeTo(600,820)" in html


def test_both_chat_windows_render_through_the_shared_core():
    """The single-run viewer and the swarm's per-worker window used to carry two
    hand-synced copies of the same page, so a fix to one skipped the other. Both
    are now built from watch_ui; this keeps a third copy from growing back."""
    import swarm_watch
    import watch_ui

    for html in (server._watch_html(), swarm_watch._WORKER_HTML):
        assert watch_ui.CSS in html and watch_ui.BODY in html and watch_ui.JS in html
        assert html.count("function md(") == 1


def test_every_watch_backend_has_a_logo_that_cannot_run_anything():
    """A backend without an entry falls back to a monogram tile, which works but
    looks broken next to the others. The logos are third-party SVG, so also pin
    that none carries script, an event handler or an external reference."""
    import watch_ui

    backends = {"agy", "codex", "copilot", "cursor", "grok", "opencode", "muse"}
    assert set(watch_ui._LOGO_SVG) == backends | {"claude"}
    for svg in watch_ui._LOGO_SVG.values():
        low = svg.lower()
        assert "<script" not in low and "foreignobject" not in low
        assert not re.search(r"\son\w+\s*=", low)
        assert "http" not in low.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "__LOGOS__" not in watch_ui.BASE_JS


def test_watch_begin_gives_each_run_a_clean_image():
    rid = server._watch_begin("t", 1.0)
    assert server._watch_snapshot(rid)["image"] == ""  # fresh run: no image
    server._watch_set_image(rid, "C:/x/pic.png")
    assert server._watch_snapshot(rid)["image"] == "C:/x/pic.png"
    server._watch_finish(rid, "done", "", 1.0)  # free the slot
    rid2 = server._watch_begin("t2", 2.0)  # reuses the freed "main" slot...
    assert rid2 == "main" and rid == "main"
    assert server._watch_snapshot(rid2)["image"] == ""  # ...with a clean, image-less state


def test_run_agy_image_watched_shows_image_and_returns(monkeypatch, brain_dir, last_conv_file):
    last_conv_file.write_text(json.dumps({"C:\\ws": "ic"}), encoding="utf-8")

    def create_transcript():
        _write_transcript(brain_dir, "ic", [_entry("PLANNER_RESPONSE", "C:/out/art.jpg")])
        os.utime(brain_dir / "ic", (time.time() + 5, time.time() + 5))

    class _CreatingPopen:
        def __init__(self, *a, **k):
            self.returncode = 0
            self._n = 2
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")

        def poll(self):
            self._n -= 1
            if self._n == 1:
                create_transcript()
            return None if self._n > 0 else self.returncode

        def communicate(self, timeout=None):
            return ("", "")

        def kill(self):
            pass

    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: _CreatingPopen())
    monkeypatch.setattr(server, "_ensure_watch_server", lambda: 12345)
    monkeypatch.setattr(server, "_open_watch_window", lambda *a, **k: None)
    monkeypatch.setattr(
        server, "_finalize_image", lambda target, txt, start: ("C:/out/art.jpg", "JPEG", 2048)
    )
    monkeypatch.setattr(server.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(server, "_RESPONSE_POLL_DEADLINE_S", 0.0)

    out = server._run_agy_image_watched(
        "wrapped prompt", "C:/out/art.png", "C:\\ws", 10, "draw a cat"
    )
    assert "C:/out/art.jpg" in out
    assert "format=JPEG" in out
    assert server._watch_snapshot()["image"] == "C:/out/art.jpg"


# --------------------------------------------------------------------------
# _collect_status
# --------------------------------------------------------------------------


@pytest.fixture
def status_dirs(tmp_path, monkeypatch):
    data = tmp_path / "antigravity-cli"
    brain = data / "brain"
    conv = data / "conversations"
    last = data / "cache" / "last_conversations.json"
    brain.mkdir(parents=True)
    conv.mkdir(parents=True)
    last.parent.mkdir(parents=True, exist_ok=True)
    last.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server, "AGY_DATA", data)
    monkeypatch.setattr(server, "BRAIN_DIR", brain)
    monkeypatch.setattr(server, "CONVERSATIONS_DIR", conv)
    monkeypatch.setattr(server, "LAST_CONVERSATIONS", last)
    return {"data": data, "brain": brain, "conv": conv, "last": last}


def _status_dict(rows):
    return {label: (ok, detail) for label, ok, detail in rows}


def test_collect_status_all_ok(status_dirs, monkeypatch):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.5")
    _write_transcript(status_dirs["brain"], "c1", [_entry("PLANNER_RESPONSE", "ans")])
    (status_dirs["conv"] / "c1.db").write_text("", encoding="utf-8")
    rows = server._collect_status()
    d = _status_dict(rows)
    assert d["agy CLI"][0] is True
    assert d["base dir"][0] is True
    assert d["brain dir"][0] is True
    assert d["newest transcript"][0] is True
    assert all(ok for _, ok, _ in rows)


def test_collect_status_agy_missing(status_dirs, monkeypatch):
    monkeypatch.setattr(server, "_get_agy_version", lambda: None)
    rows = server._collect_status()
    assert _status_dict(rows)["agy CLI"][0] is False


def test_collect_status_dirs_absent(tmp_path, monkeypatch):
    missing = tmp_path / "nope"
    monkeypatch.setattr(server, "AGY_DATA", missing)
    monkeypatch.setattr(server, "BRAIN_DIR", missing / "brain")
    monkeypatch.setattr(server, "CONVERSATIONS_DIR", missing / "conversations")
    monkeypatch.setattr(server, "LAST_CONVERSATIONS", missing / "cache" / "last.json")
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.5")
    rows = server._collect_status()
    d = _status_dict(rows)
    assert d["base dir"][0] is False
    assert d["brain dir"][0] is False


def test_collect_status_unreadable_transcript(status_dirs, monkeypatch):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.5")
    (status_dirs["brain"] / "c1").mkdir()  # conv dir exists but no transcript
    rows = server._collect_status()
    assert _status_dict(rows)["newest transcript"][0] is False


def test_antigravity_status_formats_report(status_dirs, monkeypatch):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.0.5")
    out = server.antigravity_status()
    assert out.startswith("agy bridge status")
    assert "[ok]" in out
    assert "Overall:" in out


# --------------------------------------------------------------------------
# quota rows  (agy 1.1.11+ answers `-p "/usage"` itself, for free)
# --------------------------------------------------------------------------

_USAGE_TSV = (
    "Gemini Models\tWeekly Limit Remaining\t100%\t2026-08-11T18:50:23Z\n"
    "Gemini Models\tFive Hour Limit Remaining\t100%\t2026-08-11T11:44:37Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t99%\t2026-08-12T07:54:32Z\n"
)


@pytest.mark.parametrize(
    "version,expected",
    [("1.1.10", False), ("1.1.11", True), ("1.2.0", True), ("", False)],
)
def test_supports_print_usage_gate(monkeypatch, version, expected):
    # A SAFETY gate: below 1.1.11 the same argv is a prompt, so a probe advertised
    # as free would spend the user's quota. An unparseable version must answer False.
    monkeypatch.setattr(server, "_AGY_USAGE_GATE", None)
    monkeypatch.setattr(server, "_get_agy_version", lambda: version or None)
    assert server.supports_print_usage() is expected


def test_parse_usage_rows_groups_by_family():
    rows = server._parse_usage_rows(_USAGE_TSV)
    assert rows == [
        ("quota: Gemini Models", True, "Weekly 100%, Five Hour 100%"),
        ("quota: Claude and GPT models", True, "Weekly 99%"),
    ]


def test_parse_usage_rows_flags_an_exhausted_family():
    # 0% remaining means every call against that family fails until the window
    # resets — exactly what a pre-flight status check exists to surface.
    rows = server._parse_usage_rows(
        "Gemini Models\tWeekly Limit Remaining\t0%\t2026-08-11T18:50:23Z\n"
    )
    assert rows == [("quota: Gemini Models", False, "Weekly 0%")]


def test_parse_usage_rows_ignores_unparseable_lines():
    # agy's format may change again; a status probe must never be what raises.
    assert server._parse_usage_rows("some prose\n\nGemini Models\tWeekly\n") == []


def test_read_agy_usage_argv_keeps_agy_slash_expansion(monkeypatch):
    """The probe must NOT carry --disable-slash-commands.

    That flag is what makes agy treat a leading "/usage" as literal text, so passing
    it here would send the prompt to the model: quota spent, prose returned, and a
    conversation left behind — from the one tool that promises to spend nothing.
    """
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout=_USAGE_TSV, stderr="")

    monkeypatch.setattr(server.proc_tree, "run_captured", fake_run)
    assert server._read_agy_usage() == _USAGE_TSV
    assert "--disable-slash-commands" not in seen["args"]
    assert "--dangerously-skip-permissions" not in seen["args"]
    assert seen["args"][-2:] == ["-p", "/usage"]


def test_read_agy_usage_none_on_failure(monkeypatch):
    monkeypatch.setattr(
        server.proc_tree, "run_captured", lambda *a, **k: (_ for _ in ()).throw(OSError())
    )
    assert server._read_agy_usage() is None


def test_quota_status_rows_empty_on_old_agy(monkeypatch):
    # Nothing to say, and a failing row would flip a healthy setup to PROBLEMS FOUND.
    monkeypatch.setattr(server, "supports_print_usage", lambda: False)
    monkeypatch.setattr(server, "_read_agy_usage", lambda: pytest.fail("must not probe"))
    assert server._quota_status_rows() == []


def test_quota_status_rows_reports_unreadable_without_failing(monkeypatch):
    monkeypatch.setattr(server, "supports_print_usage", lambda: True)
    monkeypatch.setattr(server, "_read_agy_usage", lambda: None)
    rows = server._quota_status_rows()
    assert len(rows) == 1 and rows[0][0] == "quota" and rows[0][1] is True


def test_collect_status_includes_quota_rows(status_dirs, monkeypatch):
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.1.12")
    monkeypatch.setattr(server, "supports_print_usage", lambda: True)
    monkeypatch.setattr(server, "_read_agy_usage", lambda: _USAGE_TSV)
    d = _status_dict(server._collect_status())
    assert d["quota: Gemini Models"] == (True, "Weekly 100%, Five Hour 100%")
    assert d["quota: Claude and GPT models"][0] is True


def test_codex_status_includes_bridge_version_row(monkeypatch):
    # The bridge's update notice must surface on a Codex-only install too, not
    # just via antigravity_status. Stub the backend rows so the test doesn't need
    # codex installed, and pin a newer release so the notice shows.
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: (99, 0, 0))
    monkeypatch.setattr(codex_bridge, "status_rows", lambda: [("codex CLI", True, "v0.141.0")])
    out = server.codex_status()
    assert out.startswith("codex bridge status")
    assert "bridge version" in out
    assert f"v{server.__version__}" in out
    assert "v99.0.0 available" in out


def test_copilot_status_includes_bridge_version_row(monkeypatch):
    # Same guarantee for a Copilot-only install.
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: (99, 0, 0))
    monkeypatch.setattr(copilot_bridge, "status_rows", lambda: [("copilot CLI", True, "v1.0.68")])
    out = server.copilot_status()
    assert out.startswith("copilot bridge status")
    assert "bridge version" in out
    assert f"v{server.__version__}" in out
    assert "v99.0.0 available" in out


def test_cursor_status_includes_bridge_version_row(monkeypatch):
    # Same guarantee for a Cursor-only install.
    monkeypatch.setattr(server, "_fetch_latest_release_version", lambda: (99, 0, 0))
    monkeypatch.setattr(cursor_bridge, "status_rows", lambda: [("cursor CLI", True, "2026.07.08")])
    out = server.cursor_status()
    assert out.startswith("cursor bridge status")
    assert "bridge version" in out
    assert f"v{server.__version__}" in out
    assert "v99.0.0 available" in out


# --------------------------------------------------------------------------
# image generation: byte fixtures + _detect_image_format / ext helpers
# --------------------------------------------------------------------------

_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 8
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
_GIF = b"GIF89a" + b"\x00" * 8
_WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 4


def test_detect_image_format_jpeg(tmp_path):
    p = tmp_path / "a"
    p.write_bytes(_JPEG)
    assert server._detect_image_format(str(p)) == "JPEG"


def test_detect_image_format_png(tmp_path):
    p = tmp_path / "a"
    p.write_bytes(_PNG)
    assert server._detect_image_format(str(p)) == "PNG"


def test_detect_image_format_gif(tmp_path):
    p = tmp_path / "a"
    p.write_bytes(_GIF)
    assert server._detect_image_format(str(p)) == "GIF"


def test_detect_image_format_webp(tmp_path):
    p = tmp_path / "a"
    p.write_bytes(_WEBP)
    assert server._detect_image_format(str(p)) == "WEBP"


def test_detect_image_format_text_is_none(tmp_path):
    p = tmp_path / "a"
    p.write_bytes(b"not an image at all")
    assert server._detect_image_format(str(p)) is None


def test_detect_image_format_missing_file_is_none(tmp_path):
    assert server._detect_image_format(str(tmp_path / "nope")) is None


def test_canonical_ext_maps_known_formats():
    assert server._canonical_ext("JPEG") == ".jpg"
    assert server._canonical_ext("PNG") == ".png"
    assert server._canonical_ext("GIF") == ".gif"
    assert server._canonical_ext("WEBP") == ".webp"


def test_with_ext_replaces_extension():
    assert server._with_ext("C:\\a\\b.png", ".jpg") == "C:\\a\\b.jpg"
    assert server._with_ext("/a/b/c.jpeg", ".jpg") == "/a/b/c.jpg"


# --------------------------------------------------------------------------
# _resolve_output_path
# --------------------------------------------------------------------------


def test_resolve_output_path_default_name(tmp_path):
    out = server._resolve_output_path(None, str(tmp_path))
    assert out.startswith(os.path.join(str(tmp_path), "agy-image-"))
    assert out.endswith(".png")


def test_resolve_output_path_relative_joined_to_workspace(tmp_path):
    out = server._resolve_output_path("sub/pic.png", str(tmp_path))
    assert out == os.path.abspath(os.path.join(str(tmp_path), "sub/pic.png"))


def test_resolve_output_path_absolute_kept(tmp_path):
    p = str(tmp_path / "abs.png")
    assert server._resolve_output_path(p, "C:\\other") == os.path.abspath(p)


# --------------------------------------------------------------------------
# _newest_scratch_image_after
# --------------------------------------------------------------------------


@pytest.fixture
def scratch_dir(tmp_path, monkeypatch):
    d = tmp_path / "scratch"
    d.mkdir()
    monkeypatch.setattr(server, "SCRATCH_DIR", d)
    return d


def test_newest_scratch_image_after_picks_newest_image(scratch_dir):
    start = time.time()
    img = scratch_dir / "x.png"
    img.write_bytes(_JPEG)
    os.utime(img, (start + 5, start + 5))
    assert server._newest_scratch_image_after(start) == str(img)


def test_newest_scratch_image_after_ignores_nonimage(scratch_dir):
    start = time.time()
    f = scratch_dir / "notes.txt"
    f.write_bytes(b"hello")
    os.utime(f, (start + 5, start + 5))
    assert server._newest_scratch_image_after(start) is None


def test_newest_scratch_image_after_ignores_old(scratch_dir):
    start = time.time()
    img = scratch_dir / "old.png"
    img.write_bytes(_JPEG)
    os.utime(img, (start - 100, start - 100))
    assert server._newest_scratch_image_after(start) is None


def test_newest_scratch_image_after_missing_dir_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SCRATCH_DIR", tmp_path / "nope")
    assert server._newest_scratch_image_after(time.time()) is None


# --------------------------------------------------------------------------
# _wrap_image_prompt
# --------------------------------------------------------------------------


def test_wrap_image_prompt_embeds_target_and_prompt():
    w = server._wrap_image_prompt("a red cat", "C:\\out\\img.png")
    assert "a red cat" in w
    assert "C:\\out\\img.png" in w
    assert "absolute file path" in w


def test_wrap_image_prompt_avoids_double_period():
    w = server._wrap_image_prompt("a red cat.", "C:\\out\\img.png")
    assert w.startswith("a red cat. Save")  # no ".." when prompt already ends in '.'


# --------------------------------------------------------------------------
# _finalize_image
# --------------------------------------------------------------------------


def test_finalize_image_corrects_extension_at_target(tmp_path, scratch_dir):
    (tmp_path / "art.png").write_bytes(_JPEG)  # JPEG bytes under a .png name
    target = str(tmp_path / "art.png")
    final, fmt, size = server._finalize_image(target, None, time.time())
    assert final == str(tmp_path / "art.jpg")
    assert fmt == "JPEG"
    assert size == len(_JPEG)
    assert os.path.isfile(tmp_path / "art.jpg")
    assert not os.path.isfile(target)


def test_finalize_image_moves_scratch_file_to_target(tmp_path, scratch_dir):
    start = time.time()
    s = scratch_dir / "gen.png"
    s.write_bytes(_JPEG)
    os.utime(s, (start + 5, start + 5))
    target = str(tmp_path / "out.png")  # does not exist
    final, fmt, size = server._finalize_image(target, None, start)
    assert final == str(tmp_path / "out.jpg")
    assert fmt == "JPEG"
    assert os.path.isfile(final)
    assert not os.path.exists(s)


def test_finalize_image_not_found_raises(tmp_path, scratch_dir):
    target = str(tmp_path / "missing.png")
    with pytest.raises(RuntimeError, match="no image file found"):
        server._finalize_image(target, None, time.time())


def test_finalize_image_non_image_raises(tmp_path, scratch_dir):
    (tmp_path / "refusal.png").write_bytes(b"I cannot create that image.")
    target = str(tmp_path / "refusal.png")
    with pytest.raises(RuntimeError, match="not a recognized image"):
        server._finalize_image(target, None, time.time())


def test_finalize_image_uses_agy_text_when_target_missing(tmp_path, scratch_dir):
    (tmp_path / "actual.jpg").write_bytes(_JPEG)
    agy_path = str(tmp_path / "actual.jpg")
    target = str(tmp_path / "requested.png")  # never created
    final, fmt, size = server._finalize_image(target, agy_path, time.time())
    assert fmt == "JPEG"
    assert final == str(tmp_path / "requested.jpg")  # landed at target's base name
    assert os.path.isfile(final)
    assert not os.path.exists(tmp_path / "actual.jpg")  # moved, not copied


# --------------------------------------------------------------------------
# antigravity_image (orchestration; _run_agy mocked)
# --------------------------------------------------------------------------


def test_antigravity_image_happy_path(tmp_path, scratch_dir, monkeypatch):
    target = str(tmp_path / "art.png")

    def fake_run(prompt, ws, continue_conv, timeout_s):
        (tmp_path / "art.png").write_bytes(_JPEG)  # agy saves JPEG under .png
        return target

    monkeypatch.setattr(server, "_run_agy", fake_run)
    out = asyncio.run(
        server.antigravity_image("a cat", output_path=target, workspace=str(tmp_path))
    )
    assert str(tmp_path / "art.jpg") in out
    assert "format=JPEG" in out
    assert os.path.isfile(tmp_path / "art.jpg")


def test_antigravity_image_recovers_when_run_agy_raises(tmp_path, scratch_dir, monkeypatch):
    (tmp_path / "art.png").write_bytes(_JPEG)  # file already on disk
    target = str(tmp_path / "art.png")

    def boom(prompt, ws, continue_conv, timeout_s):
        raise RuntimeError("transcript read failed")

    monkeypatch.setattr(server, "_run_agy", boom)
    out = asyncio.run(
        server.antigravity_image("a cat", output_path=target, workspace=str(tmp_path))
    )
    assert "format=JPEG" in out


def test_antigravity_image_raises_when_nothing_produced(tmp_path, scratch_dir, monkeypatch):
    target = str(tmp_path / "art.png")

    def boom(prompt, ws, continue_conv, timeout_s):
        raise RuntimeError("agy exited 1")

    monkeypatch.setattr(server, "_run_agy", boom)
    with pytest.raises(RuntimeError, match="no image file found"):
        asyncio.run(server.antigravity_image("a cat", output_path=target, workspace=str(tmp_path)))


def test_antigravity_image_error_mentions_agy_failure(tmp_path, scratch_dir, monkeypatch):
    target = str(tmp_path / "art.png")

    def boom(prompt, ws, continue_conv, timeout_s):
        raise RuntimeError("agy exited 1")

    monkeypatch.setattr(server, "_run_agy", boom)
    with pytest.raises(RuntimeError, match="agy also failed"):
        asyncio.run(server.antigravity_image("a cat", output_path=target, workspace=str(tmp_path)))


def test_watch_viewer_live_reflects_recent_poll():
    rid = server._watch_begin("t", time.time())
    # A brand-new run has never been polled (last_poll=0) -> not live -> opens a window.
    assert server._watch_viewer_live(rid) is False
    # A /events poll marks it live, so a new run on this slot reuses the open window.
    server._watch_mark_poll(rid)
    assert server._watch_viewer_live(rid) is True
    # An unknown id is never live.
    assert server._watch_viewer_live("nope") is False


# --------------------------------------------------------------------------
# SQLite (.db) transcript fallback: protobuf helpers + _read_response_db
# --------------------------------------------------------------------------


def _pb_enc_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _pb_tag(field, wt):
    return _pb_enc_varint((field << 3) | wt)


def _pb_str(field, s):
    b = s.encode("utf-8")
    return _pb_tag(field, 2) + _pb_enc_varint(len(b)) + b


def _pb_varint_field(field, n):
    return _pb_tag(field, 0) + _pb_enc_varint(n)


def _pb_submsg(field, payload):
    return _pb_tag(field, 2) + _pb_enc_varint(len(payload)) + payload


def _planner_payload(text):
    """A step_payload shaped like agy's: step_type(f1)=15, status(f4)=3, and the
    answer at field 20 -> field 1 (the layout _read_response_db reads)."""
    return _pb_varint_field(1, 15) + _pb_varint_field(4, 3) + _pb_submsg(20, _pb_str(1, text))


def _make_steps_db(path, rows):
    """rows: (idx, step_type, status, step_payload) tuples -> a minimal agy `.db`."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE steps (idx integer, step_type integer NOT NULL DEFAULT 0, "
        "status integer NOT NULL DEFAULT 0, step_payload blob, PRIMARY KEY (idx))"
    )
    con.executemany(
        "INSERT INTO steps (idx, step_type, status, step_payload) VALUES (?,?,?,?)", rows
    )
    con.commit()
    con.close()


def test_pb_wire_roundtrip():
    blob = _pb_varint_field(1, 15) + _pb_str(3, "héllo") + _pb_submsg(20, _pb_str(1, "answer"))
    fields = server._pb_fields(blob)
    assert (1, 0, 15) in fields  # varint field
    assert server._pb_bytes(fields, 3)[0].decode("utf-8") == "héllo"  # string field
    sub = server._pb_bytes(fields, 20)[0]  # sub-message
    assert server._pb_bytes(server._pb_fields(sub), 1)[0].decode("utf-8") == "answer"


def test_pb_fields_tolerates_garbage():
    # Best-effort: malformed trailing bytes must not raise.
    assert isinstance(server._pb_fields(b"\xff\xff\xff"), list)
    assert server._pb_fields(b"") == []


def test_read_response_db_returns_last_done_planner(tmp_path, monkeypatch):
    conv = "11111111-1111-1111-1111-111111111111"
    _make_steps_db(
        str(tmp_path / f"{conv}.db"),
        [
            (0, 15, 3, _planner_payload("first draft")),  # earlier planner response
            (1, 8, 3, b"\x08\x08tool-step"),  # non-planner step (filtered out)
            (2, 15, 0, _planner_payload("still working")),  # planner but not DONE (filtered)
            (3, 15, 3, _planner_payload("FINAL ✓ answer")),  # last completed planner -> wins
        ],
    )
    monkeypatch.setattr(server, "CONVERSATIONS_DIR", tmp_path)
    assert server._read_response_db(conv) == "FINAL ✓ answer"


def test_read_response_db_missing_or_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CONVERSATIONS_DIR", tmp_path)
    assert server._read_response_db("does-not-exist") is None
    conv = "22222222-2222-2222-2222-222222222222"
    _make_steps_db(str(tmp_path / f"{conv}.db"), [(0, 8, 3, b"\x08\x08only-a-tool")])
    assert server._read_response_db(conv) is None  # no planner-response step


def test_read_response_falls_back_to_db(tmp_path, monkeypatch):
    conv = "33333333-3333-3333-3333-333333333333"
    monkeypatch.setattr(server, "BRAIN_DIR", tmp_path / "brain")  # no JSONL transcript
    monkeypatch.setattr(server, "CONVERSATIONS_DIR", tmp_path)
    _make_steps_db(str(tmp_path / f"{conv}.db"), [(0, 15, 3, _planner_payload("from the db"))])
    assert server._read_response(conv) == "from the db"


def test_read_response_raises_when_neither_source(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BRAIN_DIR", tmp_path / "brain")
    monkeypatch.setattr(server, "CONVERSATIONS_DIR", tmp_path)
    with pytest.raises(RuntimeError):
        server._read_response("44444444-4444-4444-4444-444444444444")


# --------------------------------------------------------------------------
# Server instructions (taught to the client's model on connect)
# --------------------------------------------------------------------------


def test_server_wires_instructions_into_the_mcp_object():
    # FastMCP surfaces .instructions from the underlying MCP server object, which
    # is exactly what the client receives in the initialize response and injects
    # into its model's context. Non-empty + same object == the wiring holds, so a
    # future refactor that drops the instructions= arg fails loudly here.
    instr = server.mcp.instructions
    assert instr and instr.strip()
    assert instr == server.SERVER_INSTRUCTIONS


def test_server_instructions_cover_all_backends():
    instr = server.mcp.instructions.lower()
    for backend in ("antigravity", "codex", "copilot", "cursor"):
        assert backend in instr, f"instructions never mention the {backend} backend"


def test_server_instructions_route_key_capabilities():
    # The high-value cues that justify an always-on, every-session block: image
    # generation, parallel fan-out, the workspace footgun, and the safety
    # boundary. Dropping any of these silently degrades how well client models
    # use the bridge, so guard them.
    instr = server.mcp.instructions.lower()
    for cue in ("antigravity_image", "agent_swarm", "workspace", "sandbox"):
        assert cue in instr, f"instructions omit the {cue!r} routing cue"


def test_server_instructions_tell_the_host_to_offer_and_to_ask_first():
    # The instructions used to only DESCRIBE when delegation fits, which left the
    # bridge invisible unless the user thought to ask for it. They now tell the
    # host model to propose it. Both halves are load-bearing and each one alone is
    # a distinct failure: "offer" without the consent/anti-nag guardrail turns into
    # a suggestion on every task (or quota spent silently), and the guardrail
    # without "offer" is the old passive behaviour again.
    instr = server.mcp.instructions.lower()
    assert "offer it" in instr, "instructions no longer tell the host to propose delegation"
    assert "and ask" in instr, "instructions no longer tell the host to ask before spending quota"
    assert "once per task" in instr, "instructions lost the anti-nag rule"


def test_server_instructions_tell_the_host_to_relay_an_available_update():
    # The bridge announces a new release in two places, and only one of them can
    # reach a user: the startup warning goes to the host's LOGS, while the
    # `bridge version` row rides a tool result into the model's context. So the
    # host has to be told to pass it on — otherwise a user on the recommended
    # (non-auto-upgrading) uvx install never hears that an update exists.
    instr = server.mcp.instructions.lower()
    assert "bridge version" in instr, "instructions no longer point at the update row"
    assert "upgrade" in instr, "instructions no longer ask the host to relay the upgrade"


# --------------------------------------------------------------------------
# Packaging: every runtime module ships in the wheel
# --------------------------------------------------------------------------


def test_every_tool_module_is_wired_into_server():
    """A <backend>_tools.py that server never imports registers no tools, and one
    left out of _TOOL_MODULES loses its server.<name> aliases, which tests and
    swarm.py call. Neither shows until something calls them."""
    on_disk = {p.stem for p in Path(__file__).parent.glob("*_tools.py")}
    assert on_disk == {m.__name__ for m in server._TOOL_MODULES}
    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    for mod in server._TOOL_MODULES:
        for name in mod.__all__:
            assert getattr(server, name) is getattr(mod, name), f"server.{name}"
        assert tools & set(mod.__all__), f"{mod.__name__} registers no tools"


def test_running_server_py_as_a_script_serves_every_tool(tmp_path):
    """`python server.py` makes server __main__. Without its sys.modules alias the
    tool modules' `import server` would load a second copy and register their tools
    on that copy's mcp, so the one actually serving would list Antigravity's only."""
    expected = len(asyncio.run(server.mcp.list_tools()))
    code = (
        "import asyncio, runpy, sys, fastmcp\n"
        "def run(self, *a, **k):\n"
        "    print(len(asyncio.run(self.list_tools())))\n"
        "fastmcp.FastMCP.run = run\n"
        "sys.argv = ['server.py']\n"
        "runpy.run_path('server.py', run_name='__main__')\n"
    )
    env = dict(
        os.environ,
        AGY_BRIDGE_NO_UPDATE_CHECK="1",  # main()'s startup checks: no network
        AGY_BIN=str(tmp_path / "no-agy"),  # ... and no real agy
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == str(expected)


def test_every_runtime_module_is_listed_in_py_modules():
    # 0.30.1 shipped a wheel that could not start: proc_tree.py was new, every
    # bridge imported it at module level, and nobody added it to py-modules. The
    # suite stayed green because tests import from the repo root, where the file
    # exists — only a clean install sees the wheel's contents. So compare the
    # two lists directly: any non-test module at the root is a runtime module
    # (the flat layout has no other place to put one) and must be in the wheel.
    root = Path(__file__).parent
    block = (root / "pyproject.toml").read_text(encoding="utf-8").split("py-modules = [", 1)[1]
    listed = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', block.split("]", 1)[0]))
    on_disk = {
        p.stem
        for p in root.glob("*.py")
        if not p.stem.startswith("test_") and p.stem != "conftest"  # pytest's, not the wheel's
    }
    assert on_disk - listed == set(), (
        f"not in pyproject py-modules, so missing from the published wheel: "
        f"{sorted(on_disk - listed)}"
    )
    assert listed - on_disk == set(), f"py-modules lists missing files: {sorted(listed - on_disk)}"


def test_version_is_the_same_everywhere_a_release_writes_it():
    # A release bumps the version in three files. The PyPI publish checks the tag
    # against pyproject only; the other two used to be kept in step by hand.
    # server.__version__ is what `*_status` compares against PyPI to announce an
    # update, and server.json is what the MCP Registry lists — so a missed bump
    # makes the bridge nag about its own release, or the registry advertise the
    # wrong one.
    root = Path(__file__).parent
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'(?m)^version = "([^"]+)"', pyproject).group(1)
    manifest = json.loads((root / "server.json").read_text(encoding="utf-8"))
    assert server.__version__ == version
    assert manifest["version"] == version
    assert [p["version"] for p in manifest["packages"]] == [version]
    plugin = json.loads((root / "plugin/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
    # Claude Code only offers a plugin update when this string changes.
    assert plugin["version"] == version
    # The registry proves we own the PyPI package by finding this line in the
    # README that PyPI renders; drop it and the next registry publish fails.
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert f"mcp-name: {manifest['name']}" in readme


# --------------------------------------------------------------------------
# Claude Code plugin (plugin/): its skills steer Claude by tool name
# --------------------------------------------------------------------------


def _registered_tool_names():
    # fastmcp 3 exposes list_tools() (a list of Tool); 2.x had get_tools() (a dict).
    lister = getattr(server.mcp, "list_tools", None)
    if lister is not None:
        return {t.name for t in asyncio.run(lister())}
    return set(asyncio.run(server.mcp.get_tools()))


def _plugin_root():
    return Path(__file__).parent / "plugin"


def test_plugin_skills_only_name_tools_the_server_registers():
    # A skill is prose, so nothing else notices when a tool it tells Claude to
    # call is renamed or removed — the skill just starts steering at nothing.
    tools = _registered_tool_names()
    named = set()
    for skill in _plugin_root().glob("skills/*/SKILL.md"):
        text = skill.read_text(encoding="utf-8")
        named |= set(re.findall(r"`((?:[a-z]+_)+(?:ask|continue|status|image|swarm))`", text))
    assert named, "found no tool names in the plugin skills; did the layout move?"
    assert named - tools == set(), f"skills name unregistered tools: {sorted(named - tools)}"


def test_doctor_skill_preapproves_exactly_the_status_tools():
    # doctor pre-approves the *_status tools (they spend no quota) by their
    # plugin-scoped names, mcp__plugin_<plugin>_<server>__<tool>. Derive that
    # prefix from the manifests so a rename can't strand the grants, and require
    # the set to be exactly the status tools: a new backend's status tool left
    # out means a surprise prompt, and an *_ask let in would spend quota unasked.
    root = _plugin_root()
    plugin = json.loads((root / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    servers = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert len(servers) == 1
    prefix = f"mcp__plugin_{plugin['name']}_{next(iter(servers))}__"
    text = (root / "skills/doctor/SKILL.md").read_text(encoding="utf-8")
    approved = set(re.findall(re.escape(prefix) + r"(\w+)", text))
    assert approved == {t for t in _registered_tool_names() if t.endswith("_status")}


def test_plugin_launches_the_published_package_unpinned():
    # Same update posture as the documented manual install: uvx caches the
    # first version it resolves and never upgrades behind the user's back.
    # Pinning a version here would silently strand plugin users on it.
    servers = json.loads((_plugin_root() / ".mcp.json").read_text(encoding="utf-8"))
    (entry,) = servers["mcpServers"].values()
    assert entry == {"command": "uvx", "args": ["agent-intern"]}
