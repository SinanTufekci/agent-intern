"""Offline unit tests for the pure logic in opencode_bridge.py.

Like test_grok.py these use temp fixtures and monkeypatching and never invoke
`opencode`, so they run in CI on a machine that has never installed it. Unlike
test_grok.py, though, the shapes asserted here are not docs-derived: every event
payload, argv and CLI output string below was copied from a REAL opencode 1.18.29
run (its free `opencode/*` models answer with zero credentials, so the whole path
was exercised end-to-end). The fixtures are therefore regression anchors — if
opencode changes its event envelope or its `models` output, these fail.

    pytest test_opencode.py
"""

import io
import json
import os
import subprocess

import pytest

import opencode_bridge
import server
import swarm

# A real session id from a live run (opencode's own format: "ses_" + base62).
SAMPLE_SID = "ses_f7eb4c38affeqZ8CxUdONNt7Gz"

# The exact `opencode models` output observed live on 1.18.29 with ZERO
# credentials configured — one "provider/model" id per line, nothing else.
MODELS_OUT = (
    "opencode/big-pickle\n"
    "opencode/ling-3.0-flash-fin-free\n"
    "opencode/mimo-v2.5-free\n"
    "opencode/nemotron-3.5-lightning-free\n"
)

# A real `--format json` text event, trimmed of nothing that matters.
TEXT_EVENT = {
    "type": "text",
    "timestamp": 1788875738171,
    "sessionID": SAMPLE_SID,
    "part": {
        "id": "prt_0814d8bc9001khwflshfU9h6h7",
        "messageID": "msg_0814b3d92001SXqoESWYjYC1K2",
        "sessionID": SAMPLE_SID,
        "type": "text",
        "text": "TURQUOISE-31415",
        "time": {"start": 1788875998104, "end": 1788875998578},
    },
}

# The real error envelope observed live for an unknown model id.
ERROR_EVENT = {
    "type": "error",
    "timestamp": 1788876008398,
    "sessionID": "ses_f7eae5551ffeAY69hteE7pzHcX",
    "error": {
        "name": "UnknownError",
        "data": {"message": "Unexpected server error. Check server logs for details.", "ref": "e1"},
    },
}


class _P:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _ndjson(*events) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


@pytest.fixture(autouse=True)
def _clear_state(monkeypatch):
    """Every test starts with an empty model cache and no pinned sessions."""
    monkeypatch.setattr(opencode_bridge, "_MODELS_CACHE", None)
    monkeypatch.setattr(opencode_bridge, "_PINNED", {})


# --------------------------------------------------------------------------
# validate_sandbox / permission policies
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", opencode_bridge.SANDBOX_MODES)
def test_validate_sandbox_accepts_valid(mode):
    assert opencode_bridge.validate_sandbox(mode) == mode


def test_validate_sandbox_rejects_unknown():
    with pytest.raises(ValueError, match="invalid sandbox"):
        opencode_bridge.validate_sandbox("wide-open")


def test_default_sandbox_is_read_only():
    assert opencode_bridge.DEFAULT_SANDBOX == "read-only"


@pytest.mark.parametrize(
    "tool", ["edit", "bash", "task", "external_directory", "webfetch", "websearch"]
)
def test_read_only_denies_every_mutating_or_outbound_tool(tool):
    assert opencode_bridge._permission_policy("read-only")[tool] == "deny"


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_every_fenced_mode_denies_the_loop_guard(mode):
    """A user config that set doom_loop to "allow" must not un-bound a fenced run."""
    assert opencode_bridge._permission_policy(mode)["doom_loop"] == "deny"


@pytest.mark.parametrize("tool", ["glob", "grep", "list"])
def test_read_only_keeps_search_tools(tool):
    assert opencode_bridge._permission_policy("read-only")[tool] == "allow"


def test_read_only_denies_dotenv_but_allows_the_example():
    read = opencode_bridge._permission_policy("read-only")["read"]
    assert read["*"] == "allow"
    assert read["*.env"] == "deny"
    assert read["*.env.example"] == "allow"


def test_workspace_write_allows_edits_but_pins_the_boundary():
    policy = opencode_bridge._permission_policy("workspace-write")
    assert policy["external_directory"] == "deny"
    assert "edit" not in policy  # opencode's own default (allow, rooted at --dir)
    assert "bash" not in policy


def test_danger_full_access_has_no_policy():
    assert opencode_bridge._permission_policy("danger-full-access") is None


def test_permission_policy_rejects_bad_mode():
    with pytest.raises(ValueError):
        opencode_bridge._permission_policy("nope")


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_policy_is_a_mutable_copy(mode):
    """Callers must never be able to poison the module-level policy."""
    first = opencode_bridge._permission_policy(mode)
    first["edit"] = "allow"
    assert opencode_bridge._permission_policy(mode).get("edit") != "allow"


# --------------------------------------------------------------------------
# _env
# --------------------------------------------------------------------------


def test_env_disables_autoupdate():
    assert opencode_bridge._env()["OPENCODE_DISABLE_AUTOUPDATE"] == "1"


def test_env_without_sandbox_sets_no_policy(monkeypatch):
    monkeypatch.delenv("OPENCODE_PERMISSION", raising=False)
    assert "OPENCODE_PERMISSION" not in opencode_bridge._env()


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_env_permission_is_always_valid_json(mode):
    """The whole point of json.dumps: opencode SILENTLY ignores a malformed policy."""
    raw = opencode_bridge._env(mode)["OPENCODE_PERMISSION"]
    assert json.loads(raw) == opencode_bridge._permission_policy(mode)


def test_env_danger_drops_an_inherited_policy(monkeypatch):
    monkeypatch.setenv("OPENCODE_PERMISSION", '{"edit":"deny"}')
    assert "OPENCODE_PERMISSION" not in opencode_bridge._env("danger-full-access")


def test_env_overrides_an_inherited_policy(monkeypatch):
    monkeypatch.setenv("OPENCODE_PERMISSION", '{"edit":"allow"}')
    assert json.loads(opencode_bridge._env("read-only")["OPENCODE_PERMISSION"])["edit"] == "deny"


def test_env_rejects_bad_sandbox():
    with pytest.raises(ValueError):
        opencode_bridge._env("nope")


# --------------------------------------------------------------------------
# normalize_workspace
# --------------------------------------------------------------------------


def test_normalize_workspace_none_is_cwd():
    assert opencode_bridge.normalize_workspace(None) == os.getcwd()


def test_normalize_workspace_abspath(tmp_path):
    assert opencode_bridge.normalize_workspace(str(tmp_path)) == os.path.abspath(str(tmp_path))


# --------------------------------------------------------------------------
# build_args
# --------------------------------------------------------------------------


def test_build_args_fresh_basic(tmp_path):
    args = opencode_bridge.build_args("hello", str(tmp_path), "read-only", None)
    assert args[0] == opencode_bridge.OPENCODE_BIN
    assert args[1] == "run"
    assert "--format" in args and args[args.index("--format") + 1] == "json"
    assert args[args.index("--dir") + 1] == str(tmp_path)
    assert "-c" not in args and "-s" not in args


def test_build_args_prompt_is_positional_after_a_separator(tmp_path):
    """opencode takes the prompt positionally; `--` keeps a leading dash literal."""
    args = opencode_bridge.build_args("--not-a-flag", str(tmp_path), "read-only", None)
    assert args[-2:] == ["--", "--not-a-flag"]


def test_build_args_with_model(tmp_path):
    args = opencode_bridge.build_args("hi", str(tmp_path), "read-only", "opencode/big-pickle")
    assert args[args.index("-m") + 1] == "opencode/big-pickle"


def test_build_args_with_agent(tmp_path):
    args = opencode_bridge.build_args("hi", str(tmp_path), "read-only", None, agent="plan")
    assert args[args.index("--agent") + 1] == "plan"


def test_build_args_resume_id(tmp_path):
    args = opencode_bridge.build_args("hi", str(tmp_path), "read-only", None, resume_id=SAMPLE_SID)
    assert args[args.index("-s") + 1] == SAMPLE_SID
    assert "-c" not in args


def test_build_args_continue(tmp_path):
    args = opencode_bridge.build_args("hi", str(tmp_path), "read-only", None, continue_conv=True)
    assert "-c" in args
    assert "-s" not in args


def test_build_args_resume_id_beats_continue(tmp_path):
    args = opencode_bridge.build_args(
        "hi", str(tmp_path), "read-only", None, resume_id=SAMPLE_SID, continue_conv=True
    )
    assert "-s" in args
    assert "-c" not in args


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_build_args_never_auto_approves_a_fenced_run(mode, tmp_path):
    assert "--auto" not in opencode_bridge.build_args("hi", str(tmp_path), mode, None)


def test_build_args_danger_passes_auto(tmp_path):
    args = opencode_bridge.build_args("hi", str(tmp_path), "danger-full-access", None)
    assert "--auto" in args


def test_build_args_rejects_bad_sandbox(tmp_path):
    with pytest.raises(ValueError):
        opencode_bridge.build_args("hi", str(tmp_path), "nope", None)


# --------------------------------------------------------------------------
# _consume_event / _finish
# --------------------------------------------------------------------------


def test_consume_event_reads_text_and_session():
    state = {}
    opencode_bridge._consume_event(TEXT_EVENT, state)
    assert state["chunks"] == ["TURQUOISE-31415"]
    assert state["session_id"] == SAMPLE_SID


def test_consume_event_concatenates_multiple_text_parts():
    state = {}
    for text in ("first", "second"):
        ev = json.loads(json.dumps(TEXT_EVENT))
        ev["part"]["text"] = text
        opencode_bridge._consume_event(ev, state)
    assert opencode_bridge._finish(state, "", 0) == "first\n\nsecond"


def test_consume_event_records_error_message():
    state = {}
    opencode_bridge._consume_event(ERROR_EVENT, state)
    assert "Unexpected server error" in state["error"]


def test_consume_event_error_falls_back_to_name():
    state = {}
    opencode_bridge._consume_event({"type": "error", "error": {"name": "AuthError"}}, state)
    assert state["error"] == "AuthError"


def test_consume_event_ignores_unknown_types():
    state = {}
    opencode_bridge._consume_event({"type": "step_start", "sessionID": SAMPLE_SID}, state)
    assert "chunks" not in state
    assert state["session_id"] == SAMPLE_SID


def test_finish_prefers_the_opencode_error_over_the_exit_code():
    with pytest.raises(RuntimeError, match="Unexpected server error"):
        opencode_bridge._finish({"error": "Unexpected server error"}, "", 1)


def test_finish_reports_a_nonzero_exit():
    """The live "Session not found" shape: no stdout at all, message on stderr."""
    with pytest.raises(RuntimeError, match="Session not found"):
        opencode_bridge._finish({}, "Error: Session not found", 1)


def test_finish_empty_answer_raises():
    with pytest.raises(RuntimeError, match="no text"):
        opencode_bridge._finish({"session_id": SAMPLE_SID}, "", 0)


# --------------------------------------------------------------------------
# session pinning
# --------------------------------------------------------------------------


def test_pin_and_get(tmp_path):
    assert opencode_bridge.get_pinned(str(tmp_path)) is None
    opencode_bridge._pin(str(tmp_path), SAMPLE_SID)
    assert opencode_bridge.get_pinned(str(tmp_path)) == SAMPLE_SID


def test_resume_flags_fresh(tmp_path):
    assert opencode_bridge._resume_flags(str(tmp_path), False) == (None, False)


def test_resume_flags_continue_uses_pin(tmp_path):
    opencode_bridge._pin(str(tmp_path), SAMPLE_SID)
    assert opencode_bridge._resume_flags(str(tmp_path), True) == (SAMPLE_SID, False)


def test_resume_flags_continue_falls_back_to_dash_c(tmp_path):
    assert opencode_bridge._resume_flags(str(tmp_path), True) == (None, True)


# --------------------------------------------------------------------------
# run_opencode
# --------------------------------------------------------------------------


def _fake_run(stdout="", stderr="", returncode=0, seen=None):
    def _run(args, **kwargs):
        if seen is not None:
            seen.append((args, kwargs))
        return _P(stdout, stderr, returncode)

    return _run


def _fake_popen(stdout="", stderr="", returncode=0, seen=None, hang=False):
    """A Popen stand-in. `hang=True` never exits, to exercise the timeout path."""

    class _FP:
        def __init__(self, args, **kwargs):
            if seen is not None:
                seen.append((args, kwargs))
            self.pid = -1  # never a real pid: _kill_tree must be stubbed in tests
            self.stdout = io.StringIO(stdout)
            self.stderr = io.StringIO(stderr)
            self.returncode = returncode
            self.killed = False

        def wait(self, timeout=None):
            if hang and not self.killed:
                raise subprocess.TimeoutExpired("opencode", timeout or 0)
            return self.returncode

        def kill(self):
            self.killed = True

    return _FP


def test_run_opencode_returns_answer_and_pins(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(TEXT_EVENT)))
    out = opencode_bridge.run_opencode("hi", str(tmp_path))
    assert out == "TURQUOISE-31415"
    assert opencode_bridge.get_pinned(str(tmp_path)) == SAMPLE_SID


def test_run_opencode_pin_false_does_not_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(TEXT_EVENT)))
    opencode_bridge.run_opencode("hi", str(tmp_path), pin=False)
    assert opencode_bridge.get_pinned(str(tmp_path)) is None


def test_run_opencode_continue_does_not_repin(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(TEXT_EVENT)))
    opencode_bridge.run_opencode("hi", str(tmp_path), continue_conv=True)
    assert opencode_bridge.get_pinned(str(tmp_path)) is None


def test_run_opencode_closes_stdin(tmp_path, monkeypatch):
    """opencode reads a non-TTY stdin to EOF and would block on an open pipe."""
    seen = []
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(TEXT_EVENT), seen=seen))
    opencode_bridge.run_opencode("hi", str(tmp_path))
    assert seen[0][1]["stdin"] is subprocess.DEVNULL


def test_run_opencode_passes_the_permission_policy(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(TEXT_EVENT), seen=seen))
    opencode_bridge.run_opencode("hi", str(tmp_path), sandbox="read-only")
    policy = json.loads(seen[0][1]["env"]["OPENCODE_PERMISSION"])
    assert policy["edit"] == "deny" and policy["bash"] == "deny"


def test_run_opencode_surfaces_the_error_event(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(ERROR_EVENT), returncode=1))
    with pytest.raises(RuntimeError, match="Unexpected server error"):
        opencode_bridge.run_opencode("hi", str(tmp_path))


def test_run_opencode_bad_session_surfaces_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _fake_popen("", "Error: Session not found", 1))
    with pytest.raises(RuntimeError, match="Session not found"):
        opencode_bridge.run_opencode("hi", str(tmp_path))


def test_run_opencode_tolerates_non_json_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _fake_popen("banner\n" + _ndjson(TEXT_EVENT)))
    assert opencode_bridge.run_opencode("hi", str(tmp_path)) == "TURQUOISE-31415"


def test_run_opencode_rejects_bad_sandbox(tmp_path):
    with pytest.raises(ValueError):
        opencode_bridge.run_opencode("hi", str(tmp_path), sandbox="nope")


def test_run_opencode_passes_resume_id_when_continuing(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(TEXT_EVENT), seen=seen))
    opencode_bridge._pin(str(tmp_path), SAMPLE_SID)
    opencode_bridge.run_opencode("hi", str(tmp_path), continue_conv=True)
    assert SAMPLE_SID in seen[0][0]


def test_run_opencode_never_uses_subprocess_run(tmp_path, monkeypatch):
    """subprocess.run's Windows timeout branch re-reads a pipe an orphan still holds.

    That is not hypothetical here: measured live, a 270s timeout returned at 396s
    and only because the orphaned grandchild was killed by hand. The blocking path
    must therefore go through Popen + _kill_tree, never subprocess.run.
    """

    def forbidden(*a, **k):
        raise AssertionError("run_opencode must not call subprocess.run")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(TEXT_EVENT)))
    assert opencode_bridge.run_opencode("hi", str(tmp_path)) == "TURQUOISE-31415"


def test_run_opencode_timeout_kills_the_whole_tree(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(hang=True))
    monkeypatch.setattr(opencode_bridge, "_kill_tree", lambda p: killed.append(p))
    with pytest.raises(RuntimeError, match="timed out"):
        opencode_bridge.run_opencode("hi", str(tmp_path), timeout_s=1)
    assert len(killed) == 1


def test_kill_tree_uses_taskkill_on_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run(seen=calls))

    class _P2:
        pid = 4242

        def kill(self):
            calls.append(("kill",))

    monkeypatch.setattr(os, "name", "nt")
    opencode_bridge._kill_tree(_P2())
    assert calls[0][0][:4] == ["taskkill", "/F", "/T", "/PID"]
    assert calls[0][0][4] == "4242"


def test_kill_tree_signals_the_process_group_on_posix(monkeypatch):
    """No shim layer there, but opencode's own `bash` tool can leave children."""
    signalled = []

    class _P3:
        pid = 4242

        def kill(self):
            signalled.append(("kill",))

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(
        os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)), raising=False
    )
    opencode_bridge._kill_tree(_P3())
    assert signalled[0] == (4242, opencode_bridge._KILL_SIGNAL)
    assert signalled[-1] == ("kill",)


def test_kill_tree_survives_an_already_reaped_process(monkeypatch):
    class _P4:
        pid = 4242

        def kill(self):
            raise OSError("no such process")

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(
        os, "getpgid", lambda pid: (_ for _ in ()).throw(OSError("gone")), raising=False
    )
    opencode_bridge._kill_tree(_P4())  # must not raise


def test_streaming_calls_on_event_per_line(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(TEXT_EVENT, ERROR_EVENT)))
    seen = []
    with pytest.raises(RuntimeError):
        opencode_bridge.run_opencode_streaming(
            "hi", str(tmp_path), on_event=lambda ev: seen.append(ev["type"])
        )
    assert seen == ["text", "error"]


def test_streaming_survives_a_broken_on_event(tmp_path, monkeypatch):
    """A viewer hiccup must never take the run down with it."""

    def boom(ev):
        raise RuntimeError("viewer died")

    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(TEXT_EVENT)))
    out = opencode_bridge.run_opencode_streaming("hi", str(tmp_path), on_event=boom)
    assert out == "TURQUOISE-31415"


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


def test_list_models_parses_live_output(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(MODELS_OUT))
    assert opencode_bridge.list_models()[0] == "opencode/big-pickle"
    assert "opencode/nemotron-3.5-lightning-free" in opencode_bridge.list_models()


def test_list_models_skips_lines_without_a_provider(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("\nheader\nopencode/x\n"))
    assert opencode_bridge.list_models() == ["opencode/x"]


def test_list_models_empty_when_unrunnable(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(subprocess, "run", boom)
    assert opencode_bridge.list_models() == []


def test_list_models_does_not_cache_a_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(subprocess, "run", boom)
    assert opencode_bridge.list_models() == []
    monkeypatch.setattr(subprocess, "run", _fake_run(MODELS_OUT))
    assert opencode_bridge.list_models()


def test_list_models_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run(MODELS_OUT, seen=calls))
    opencode_bridge.list_models()
    opencode_bridge.list_models()
    assert len(calls) == 1


def test_validate_model_accepts_known(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(MODELS_OUT))
    assert opencode_bridge.validate_model("opencode/big-pickle") == "opencode/big-pickle"


def test_validate_model_rejects_unknown(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(MODELS_OUT))
    with pytest.raises(ValueError, match="unknown opencode model"):
        opencode_bridge.validate_model("opencode/nope")


def test_validate_model_none_passthrough():
    assert opencode_bridge.validate_model(None) is None
    assert opencode_bridge.validate_model("  ") is None


def test_validate_model_lenient_when_list_unavailable(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(""))
    assert opencode_bridge.validate_model("whatever/x") == "whatever/x"


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------

# The real `opencode providers list` box, logged out.
PROVIDERS_OUT_EMPTY = (
    "\n┌  Credentials ~\\.local\\share\\opencode\\auth.json\n│\n└  0 credentials\n"
)


def test_auth_status_no_credentials_is_ok_when_models_exist(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(PROVIDERS_OUT_EMPTY + MODELS_OUT))
    ok, detail = opencode_bridge.auth_status()
    assert ok is True
    assert "0 credentials" in detail


def test_auth_status_no_credentials_and_no_models_is_a_problem(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(PROVIDERS_OUT_EMPTY))
    ok, _ = opencode_bridge.auth_status()
    assert ok is False


def test_auth_status_reports_configured_credentials(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("└  2 credentials\n"))
    ok, detail = opencode_bridge.auth_status()
    assert ok is True
    assert "2 credentials" in detail


def test_auth_status_handles_unrunnable(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(subprocess, "run", boom)
    ok, detail = opencode_bridge.auth_status()
    assert ok is False
    assert "providers list" in detail


def test_opencode_version_first_line(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("1.18.29\n"))
    assert opencode_bridge.opencode_version() == "1.18.29"


def test_opencode_version_none_when_missing(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(subprocess, "run", boom)
    assert opencode_bridge.opencode_version() is None


def test_status_rows_shape(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("1.18.29\n" + MODELS_OUT))
    rows = opencode_bridge.status_rows()
    assert all(len(r) == 3 and isinstance(r[1], bool) for r in rows)
    assert rows[0][0] == "opencode CLI"


def test_status_rows_flags_missing_cli(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(subprocess, "run", boom)
    rows = opencode_bridge.status_rows()
    assert rows[0][1] is False
    assert "OPENCODE_BIN" in rows[0][2]


def test_read_history_is_empty(tmp_path):
    assert opencode_bridge.read_history(str(tmp_path), True) == []


# --------------------------------------------------------------------------
# watch-line mapping (server)
# --------------------------------------------------------------------------


def test_event_to_watch_lines_text_is_narration():
    assert server._opencode_event_to_watch_lines(TEXT_EVENT) == [("narration", "TURQUOISE-31415")]


def test_event_to_watch_lines_tool_use_shows_command_and_result():
    ev = {
        "type": "tool_use",
        "part": {
            "type": "tool",
            "tool": "bash",
            "state": {"status": "completed", "input": {"command": "ls -la"}},
        },
    }
    assert server._opencode_event_to_watch_lines(ev) == [
        ("command", "bash: ls -la"),
        ("result", "done"),
    ]


def test_event_to_watch_lines_tool_use_falls_back_to_the_state_title():
    ev = {
        "type": "tool_use",
        "part": {
            "type": "tool",
            "tool": "todowrite",
            "state": {"status": "completed", "input": {}, "title": "Plan the refactor"},
        },
    }
    assert server._opencode_event_to_watch_lines(ev)[0] == ("command", "Plan the refactor")


def test_event_to_watch_lines_tool_use_reports_a_denied_call():
    ev = {
        "type": "tool_use",
        "part": {
            "type": "tool",
            "tool": "edit",
            "state": {"status": "error", "error": "permission denied", "input": {}},
        },
    }
    kinds = dict(server._opencode_event_to_watch_lines(ev))
    assert "permission denied" in kinds["result"]


def test_event_to_watch_lines_error():
    assert (
        "Unexpected server error"
        in dict(server._opencode_event_to_watch_lines(ERROR_EVENT))["result"]
    )


def test_event_to_watch_lines_ignores_unknown():
    assert server._opencode_event_to_watch_lines({"type": "step_start", "part": {}}) == []


# --------------------------------------------------------------------------
# swarm wiring
# --------------------------------------------------------------------------


@pytest.mark.parametrize("alias", ["opencode", "oc", "sst", "OpenCode"])
def test_swarm_accepts_opencode_aliases(alias, tmp_path, monkeypatch):
    monkeypatch.setattr(opencode_bridge, "list_models", lambda: [])
    tasks = swarm._normalize_tasks([{"backend": alias, "prompt": "hi", "workspace": str(tmp_path)}])
    assert tasks[0]["backend"] == "opencode"


def test_swarm_opencode_defaults_to_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode_bridge, "list_models", lambda: [])
    tasks = swarm._normalize_tasks(
        [{"backend": "opencode", "prompt": "hi", "workspace": str(tmp_path)}]
    )
    assert tasks[0]["sandbox"] == "read-only"


def test_swarm_opencode_rejects_bad_sandbox(tmp_path):
    with pytest.raises(ValueError):
        swarm._normalize_tasks(
            [{"backend": "opencode", "prompt": "hi", "workspace": str(tmp_path), "sandbox": "no"}]
        )


def test_swarm_opencode_rejects_bad_model(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode_bridge, "list_models", lambda: ["opencode/big-pickle"])
    with pytest.raises(ValueError, match="unknown opencode model"):
        swarm._normalize_tasks(
            [{"backend": "opencode", "prompt": "hi", "workspace": str(tmp_path), "model": "x/y"}]
        )


def test_swarm_unknown_backend_message_lists_opencode():
    with pytest.raises(ValueError, match="opencode"):
        swarm._normalize_tasks([{"backend": "nope", "prompt": "hi"}])


def test_swarm_opencode_worker_isolates_errors(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("opencode error: Unexpected server error")

    monkeypatch.setattr(opencode_bridge, "run_opencode", boom)
    r = swarm._run_opencode_worker(0, "hi", str(tmp_path), "read-only", None, 30)
    assert r.ok is False
    assert r.backend == "opencode"
    assert "Unexpected server error" in r.error


def test_swarm_opencode_worker_returns_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode_bridge, "run_opencode", lambda *a, **k: "done")
    r = swarm._run_opencode_worker(0, "hi", str(tmp_path), "read-only", None, 30)
    assert r.ok is True
    assert r.answer == "done"


def test_swarm_raises_a_short_budget_to_opencodes_own_default(tmp_path, monkeypatch):
    """The swarm's shared 180s default would kill about half of opencode's runs."""
    seen = {}

    def fake(prompt, workspace, sandbox, model, cont, timeout_s, pin=True):
        seen["timeout_s"] = timeout_s
        return "ok"

    monkeypatch.setattr(opencode_bridge, "run_opencode", fake)
    swarm._run_opencode_worker(0, "hi", str(tmp_path), "read-only", None, 180)
    assert seen["timeout_s"] == opencode_bridge.DEFAULT_TIMEOUT_S


def test_swarm_never_lowers_a_generous_budget(tmp_path, monkeypatch):
    seen = {}

    def fake(prompt, workspace, sandbox, model, cont, timeout_s, pin=True):
        seen["timeout_s"] = timeout_s
        return "ok"

    monkeypatch.setattr(opencode_bridge, "run_opencode", fake)
    swarm._run_opencode_worker(0, "hi", str(tmp_path), "read-only", None, 900)
    assert seen["timeout_s"] == 900


def test_timeout_message_names_the_free_model_slowness(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(hang=True))
    monkeypatch.setattr(opencode_bridge, "_kill_tree", lambda p: None)
    with pytest.raises(RuntimeError, match="raise timeout_s"):
        opencode_bridge.run_opencode("hi", str(tmp_path), timeout_s=1)


def test_swarm_opencode_worker_never_pins(tmp_path, monkeypatch):
    seen = {}

    def fake(prompt, workspace, sandbox, model, cont, timeout_s, pin=True):
        seen["pin"] = pin
        return "ok"

    monkeypatch.setattr(opencode_bridge, "run_opencode", fake)
    swarm._run_opencode_worker(0, "hi", str(tmp_path), "read-only", None, 30)
    assert seen["pin"] is False


# --------------------------------------------------------------------------
# binary resolution / spawn
# --------------------------------------------------------------------------


def test_resolve_bin_prefers_path(monkeypatch):
    monkeypatch.setattr(opencode_bridge.shutil, "which", lambda n: "/usr/bin/opencode")
    monkeypatch.setattr(opencode_bridge, "OPENCODE_BIN_ENV", "opencode")
    assert opencode_bridge._resolve_bin() == "/usr/bin/opencode"


def test_resolve_bin_returns_name_when_nothing_found(monkeypatch):
    monkeypatch.setattr(opencode_bridge.shutil, "which", lambda n: None)
    monkeypatch.setattr(opencode_bridge, "OPENCODE_BIN_ENV", "opencode")
    assert opencode_bridge._resolve_bin() == "opencode"


def test_resolve_bin_honours_an_explicit_path(tmp_path, monkeypatch):
    exe = tmp_path / "opencode.exe"
    exe.write_text("")
    monkeypatch.setattr(opencode_bridge, "OPENCODE_BIN_ENV", str(exe))
    assert opencode_bridge._resolve_bin() == str(exe)


def test_spawn_kwargs_platform_appropriate():
    kwargs = opencode_bridge._spawn_kwargs()
    if os.name == "nt":
        assert "creationflags" in kwargs
    else:
        assert kwargs == {"start_new_session": True}
