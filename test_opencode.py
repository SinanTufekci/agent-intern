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
import re
import subprocess

import pytest

import opencode_bridge
import server
import swarm
from conftest import hide_env

# A real session id from a live run (opencode's own format: "ses_" + base62).
SAMPLE_SID = "ses_f7eb4c38affeqZ8CxUdONNt7Gz"

# The exact `opencode models` output observed live on 1.18.29 with ZERO
# credentials configured — one "provider/model" id per line, nothing else.
MODELS_OUT = (
    "opencode/big-pickle\n"
    "opencode/ling-3.0-flash-fin-free\n"
    "opencode/mimo-v2.6-flash-free\n"
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
    # The version gate would spawn `opencode --version` before every run; pin the
    # supported major so the fake Popen only ever serves the run itself. The gate's
    # own tests set _MAJOR_CACHE (or patch opencode_version) themselves.
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", 1)
    # The version TEXT is cached per binary (path, mtime, size). On a machine with a
    # real opencode that key is valid and would carry one test's fake version into
    # the next, so every test starts with it empty.
    monkeypatch.setattr(opencode_bridge, "_VERSION_CACHE", None)
    # A vanished OPENCODE_BIN the user did not choose is re-resolved through PATH
    # (_refresh_bin), which on a machine with a real opencode would find it. Treat
    # the path as chosen so no test can swap in the real install; the tests of that
    # swap turn it off again.
    monkeypatch.setattr(opencode_bridge, "_BIN_EXPLICIT", True)


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


@pytest.mark.parametrize("tool", ["edit", "task", "external_directory", "webfetch", "websearch"])
def test_read_only_denies_every_mutating_or_outbound_tool(tool):
    assert opencode_bridge._permission_policy("read-only")[tool] == "deny"


def test_read_only_fences_bash_with_ask_which_headless_run_rejects(tmp_path):
    # "deny" would leave bash out of the request, and opencode's free tier refuses
    # such a request outright. "ask" is rejected headlessly, but only without
    # --auto, so read-only must never pass it.
    assert opencode_bridge._permission_policy("read-only")["bash"] == "ask"
    assert "--auto" not in opencode_bridge.build_args("hi", str(tmp_path), "read-only", None)


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
            seen.append((args, hide_env(kwargs)))
        return _P(stdout, stderr, returncode)

    return _run


def _fake_popen(stdout="", stderr="", returncode=0, seen=None, hang=False):
    """A Popen stand-in. `hang=True` never exits, to exercise the timeout path."""

    class _FP:
        def __init__(self, args, **kwargs):
            if seen is not None:
                seen.append((args, hide_env(kwargs)))
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
    assert policy["edit"] == "deny" and policy["bash"] == "ask"


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
    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", _fake_run(MODELS_OUT))
    assert opencode_bridge.list_models()[0] == "opencode/big-pickle"
    assert "opencode/nemotron-3.5-lightning-free" in opencode_bridge.list_models()


def test_list_models_skips_lines_without_a_provider(monkeypatch):
    monkeypatch.setattr(
        opencode_bridge.proc_tree, "run_captured", _fake_run("\nheader\nopencode/x\n")
    )
    assert opencode_bridge.list_models() == ["opencode/x"]


def test_list_models_empty_when_unrunnable(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", boom)
    assert opencode_bridge.list_models() == []


def test_list_models_does_not_cache_a_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", boom)
    assert opencode_bridge.list_models() == []
    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", _fake_run(MODELS_OUT))
    assert opencode_bridge.list_models()


def test_list_models_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(
        opencode_bridge.proc_tree, "run_captured", _fake_run(MODELS_OUT, seen=calls)
    )
    opencode_bridge.list_models()
    opencode_bridge.list_models()
    assert len(calls) == 1


def test_validate_model_accepts_known(monkeypatch):
    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", _fake_run(MODELS_OUT))
    assert opencode_bridge.validate_model("opencode/big-pickle") == "opencode/big-pickle"


def test_validate_model_rejects_unknown(monkeypatch):
    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", _fake_run(MODELS_OUT))
    with pytest.raises(ValueError, match="unknown opencode model"):
        opencode_bridge.validate_model("opencode/nope")


def test_validate_model_none_passthrough():
    assert opencode_bridge.validate_model(None) is None
    assert opencode_bridge.validate_model("  ") is None


def test_validate_model_lenient_when_list_unavailable(monkeypatch):
    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", _fake_run(""))
    assert opencode_bridge.validate_model("whatever/x") == "whatever/x"


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------

# The real `opencode providers list` box, logged out.
PROVIDERS_OUT_EMPTY = (
    "\n┌  Credentials ~\\.local\\share\\opencode\\auth.json\n│\n└  0 credentials\n"
)


def test_auth_status_no_credentials_is_ok_when_models_exist(monkeypatch):
    monkeypatch.setattr(
        opencode_bridge.proc_tree, "run_captured", _fake_run(PROVIDERS_OUT_EMPTY + MODELS_OUT)
    )
    ok, detail = opencode_bridge.auth_status()
    assert ok is True
    assert "0 credentials" in detail


def test_auth_status_no_credentials_and_no_models_is_a_problem(monkeypatch):
    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", _fake_run(PROVIDERS_OUT_EMPTY))
    ok, _ = opencode_bridge.auth_status()
    assert ok is False


def test_auth_status_reports_configured_credentials(monkeypatch):
    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", _fake_run("└  2 credentials\n"))
    ok, detail = opencode_bridge.auth_status()
    assert ok is True
    assert "2 credentials" in detail


def test_auth_status_handles_unrunnable(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", boom)
    ok, detail = opencode_bridge.auth_status()
    assert ok is False
    assert "providers list" in detail


def test_opencode_version_first_line(monkeypatch):
    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", _fake_run("1.18.29\n"))
    assert opencode_bridge.opencode_version() == "1.18.29"


def test_opencode_version_none_when_missing(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", boom)
    assert opencode_bridge.opencode_version() is None


def test_status_rows_shape(monkeypatch):
    monkeypatch.setattr(
        opencode_bridge.proc_tree, "run_captured", _fake_run("1.18.29\n" + MODELS_OUT)
    )
    rows = opencode_bridge.status_rows()
    assert all(len(r) == 3 and isinstance(r[1], bool) for r in rows)
    assert rows[0][0] == "opencode CLI"


def test_status_rows_flags_missing_cli(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", boom)
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


def test_watched_opencode_worker_draws_its_bar_against_the_raised_budget(tmp_path, monkeypatch):
    """The dashboard's time bar must use the budget the worker really has; drawn
    against the swarm's 180s it turned amber and pinned near full while opencode
    still had minutes left."""
    import swarm_watch

    updates = []
    monkeypatch.setattr(swarm_watch, "worker_update", lambda i, **kw: updates.append(kw))
    monkeypatch.setattr(swarm_watch, "worker_finish", lambda *a, **k: None)
    monkeypatch.setattr(opencode_bridge, "run_opencode_streaming", lambda *a, **k: "ok")
    swarm._run_opencode_worker_watched(0, "hi", str(tmp_path), "read-only", None, 180)
    assert updates[0]["status"] == "working"
    assert updates[0]["timeout"] == opencode_bridge.DEFAULT_TIMEOUT_S


def test_timeout_message_offers_both_a_long_task_and_a_congested_tier(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(hang=True))
    monkeypatch.setattr(opencode_bridge, "_kill_tree", lambda p: None)
    with pytest.raises(RuntimeError) as exc:
        opencode_bridge.run_opencode("hi", str(tmp_path), timeout_s=1)
    msg = str(exc.value)
    assert msg.startswith("opencode timed out after 31s.")
    assert "raise timeout_s" in msg  # a long task
    assert "may be congested" in msg and "retry later" in msg  # a short prompt that timed out
    assert "slow" not in msg.lower()  # free models are no longer described as slow


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


# --------------------------------------------------------------------------
# Version gate: only a major whose permission fence is verified may run
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("opencode v2.2.5", 2),
        ("1.18.29", 1),
        ("1.18.29\n", 1),
        ("opencode 10.0.1-beta.2", 10),
        ("v3.0.0 (build 1.2.3)", 3),  # the FIRST X.Y.Z
        ("garbage", None),
        ("1.2", None),  # not X.Y.Z
        ("", None),
        (None, None),
    ],
)
def test_parse_major(text, expected):
    assert opencode_bridge.parse_major(text) == expected


@pytest.fixture
def fake_binary(tmp_path, monkeypatch):
    """OPENCODE_BIN pointing at a real file of ours, so the cache key is valid and
    the test controls it (touch the file to change its mtime/size) whatever is
    installed on the machine. Returns the path and the list of `--version` reads."""
    exe = tmp_path / "opencode-fake"
    exe.write_text("v1", encoding="utf-8")
    monkeypatch.setattr(opencode_bridge, "OPENCODE_BIN", str(exe))
    reads = []
    versions = {"text": "opencode v2.2.5"}

    def read():
        reads.append(1)
        return versions["text"]

    monkeypatch.setattr(opencode_bridge, "_read_version", read)
    return exe, reads, versions


def test_opencode_major_reads_the_version_once(fake_binary):
    _, reads, _ = fake_binary
    opencode_bridge._MAJOR_CACHE = None  # unpinned (the autouse fixture restores it)
    assert opencode_bridge.opencode_major() == 2
    assert opencode_bridge.opencode_major() == 2
    assert len(reads) == 1
    assert opencode_bridge._MAJOR_CACHE is None  # opencode_major never writes the pin


def test_opencode_major_does_not_cache_a_failed_read(fake_binary):
    _, reads, versions = fake_binary
    opencode_bridge._MAJOR_CACHE = None
    versions["text"] = None
    assert opencode_bridge.opencode_major() is None
    assert opencode_bridge.opencode_major() is None
    assert len(reads) == 2  # a failure is read again
    versions["text"] = "1.18.29"
    assert opencode_bridge.opencode_major() == 1  # a later call can still succeed


def test_unparseable_version_text_is_cached_until_the_binary_changes(fake_binary):
    # Text with no X.Y.Z is still a successful read: the same binary will print the
    # same thing, so it is not spawned again. Only a changed binary is re-read.
    exe, reads, versions = fake_binary
    opencode_bridge._MAJOR_CACHE = None
    versions["text"] = "garbage"
    assert opencode_bridge.opencode_major() is None
    versions["text"] = "1.18.29"
    assert opencode_bridge.opencode_major() is None  # still the cached "garbage"
    assert len(reads) == 1
    st = exe.stat()
    os.utime(exe, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert opencode_bridge.opencode_major() == 1


def test_a_pinned_major_never_reads_the_version(fake_binary):
    _, reads, _ = fake_binary
    opencode_bridge._MAJOR_CACHE = 1
    assert opencode_bridge.opencode_major() == 1
    assert reads == []


def test_version_text_is_cached_per_binary_key(fake_binary):
    _, reads, _ = fake_binary
    assert opencode_bridge.opencode_version() == "opencode v2.2.5"
    assert opencode_bridge.opencode_version() == "opencode v2.2.5"
    assert len(reads) == 1
    key, text = opencode_bridge._VERSION_CACHE
    assert key[0] == opencode_bridge.OPENCODE_BIN and text == "opencode v2.2.5"


def test_a_changed_binary_mtime_rereads_the_version(fake_binary):
    exe, reads, versions = fake_binary
    assert opencode_bridge.opencode_version() == "opencode v2.2.5"
    versions["text"] = "opencode v2.3.0"  # opencode upgraded itself
    st = exe.stat()
    os.utime(exe, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert opencode_bridge.opencode_version() == "opencode v2.3.0"
    assert len(reads) == 2


def test_a_changed_binary_size_rereads_the_version(fake_binary):
    exe, reads, versions = fake_binary
    opencode_bridge.opencode_version()
    versions["text"] = "1.18.29"
    mtime = exe.stat().st_mtime_ns
    exe.write_text("a longer binary", encoding="utf-8")
    os.utime(exe, ns=(mtime, mtime))  # same mtime, different size
    assert opencode_bridge.opencode_version() == "1.18.29"
    assert len(reads) == 2


def test_a_failed_version_read_is_never_cached(fake_binary):
    _, reads, versions = fake_binary
    versions["text"] = None
    assert opencode_bridge.opencode_version() is None
    assert opencode_bridge._VERSION_CACHE is None
    versions["text"] = "1.18.29"
    assert opencode_bridge.opencode_version() == "1.18.29"
    assert len(reads) == 2


def test_an_unstatable_binary_is_never_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode_bridge, "OPENCODE_BIN", str(tmp_path / "does-not-exist"))
    # the user chose this path, so _refresh_bin must not swap in a real install
    monkeypatch.setattr(opencode_bridge, "_BIN_EXPLICIT", True)
    assert opencode_bridge._binary_key() is None
    reads = []
    monkeypatch.setattr(opencode_bridge, "_read_version", lambda: reads.append(1) or "1.18.29")
    assert opencode_bridge.opencode_version() == "1.18.29"
    assert opencode_bridge.opencode_version() == "1.18.29"
    assert len(reads) == 2 and opencode_bridge._VERSION_CACHE is None


def test_binary_key_is_path_mtime_and_size(fake_binary):
    exe, _, _ = fake_binary
    st = exe.stat()
    assert opencode_bridge._binary_key() == (str(exe), st.st_mtime_ns, st.st_size)


def test_status_rows_and_the_gate_share_one_version_read(fake_binary, monkeypatch):
    _, reads, _ = fake_binary
    opencode_bridge._MAJOR_CACHE = None
    monkeypatch.setattr(opencode_bridge, "auth_status", lambda: (True, "free models"))
    monkeypatch.setattr(opencode_bridge, "list_models", lambda: ["opencode/x-free"])
    opencode_bridge.status_rows()
    opencode_bridge.check_supported_version()
    assert len(reads) == 1


def test_permission_policies_cover_exactly_the_verified_majors():
    # Adding a major here is the act of declaring it verified: this pins the
    # table so that is a deliberate edit, with its own test, never a side effect.
    assert set(opencode_bridge._PERMISSION_POLICIES) == {1, 2}
    for major in (1, 2):
        assert set(opencode_bridge._PERMISSION_POLICIES[major]) == {"read-only", "workspace-write"}


@pytest.mark.parametrize("major", [1, 2])
def test_unsupported_version_reason_supported_major_is_none(major):
    assert opencode_bridge.unsupported_version_reason(major) is None


def test_unsupported_version_reason_unreadable_version_refuses_to_run_unfenced():
    reason = opencode_bridge.unsupported_version_reason(None)
    assert "could not read opencode's version" in reason
    assert "will not run it unfenced" in reason


@pytest.mark.parametrize("major", [0, 3, 99])
def test_unsupported_version_reason_unknown_major_names_the_supported_ones(major):
    reason = opencode_bridge.unsupported_version_reason(major)
    assert f"opencode {major}.x is not supported yet" in reason
    assert "Supported: 1.x, 2.x" in reason
    assert "@opencode/cli" in reason


def test_check_supported_version_returns_the_major(monkeypatch):
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", 1)
    assert opencode_bridge.check_supported_version() == 1


@pytest.fixture
def popen_must_not_run(monkeypatch):
    """Fail the test if anything is spawned: the gate must stop a run first."""
    spawned = []

    def forbidden(*a, **k):
        spawned.append(a)
        raise AssertionError("opencode must not be spawned for an unsupported version")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(opencode_bridge.proc_tree, "popen", forbidden)
    return spawned


@pytest.mark.parametrize("sandbox", opencode_bridge.SANDBOX_MODES)
def test_unsupported_major_refuses_every_sandbox_mode(
    tmp_path, monkeypatch, popen_must_not_run, sandbox
):
    # danger-full-access included: the refusal is about the version, not the fence.
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", 3)
    with pytest.raises(RuntimeError, match=r"opencode 3\.x is not supported yet"):
        opencode_bridge.run_opencode("hi", str(tmp_path), sandbox)
    assert popen_must_not_run == []


def test_unsupported_major_from_the_version_string_refuses(
    tmp_path, monkeypatch, popen_must_not_run
):
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", None)
    monkeypatch.setattr(opencode_bridge, "opencode_version", lambda: "opencode v3.0.0")
    with pytest.raises(RuntimeError, match="not supported yet"):
        opencode_bridge.run_opencode("hi", str(tmp_path))
    assert popen_must_not_run == []


def test_unreadable_version_refuses_and_is_not_cached(tmp_path, monkeypatch, popen_must_not_run):
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", None)
    monkeypatch.setattr(opencode_bridge, "opencode_version", lambda: None)
    with pytest.raises(RuntimeError, match="could not read opencode's version"):
        opencode_bridge.run_opencode("hi", str(tmp_path))
    assert popen_must_not_run == []
    assert opencode_bridge._MAJOR_CACHE is None


def test_streaming_run_is_gated_too(tmp_path, monkeypatch, popen_must_not_run):
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", 3)
    seen = []
    with pytest.raises(RuntimeError, match="not supported yet"):
        opencode_bridge.run_opencode_streaming("hi", str(tmp_path), on_event=seen.append)
    assert popen_must_not_run == [] and seen == []


def test_gate_runs_before_the_workspace_is_created(tmp_path, monkeypatch, popen_must_not_run):
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", 3)
    ws = tmp_path / "not-created"
    with pytest.raises(RuntimeError):
        opencode_bridge.run_opencode("hi", str(ws))
    assert not ws.exists()


def test_invalid_sandbox_is_still_a_value_error_not_a_version_error(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", 3)
    with pytest.raises(ValueError, match="invalid sandbox"):
        opencode_bridge.run_opencode("hi", str(tmp_path), "wide-open")


def test_permission_policy_and_env_default_to_major_1():
    assert opencode_bridge._permission_policy("read-only") == opencode_bridge._permission_policy(
        "read-only", 1
    )
    default, explicit = opencode_bridge._env("read-only"), opencode_bridge._env("read-only", 1)
    # names and per-key equality only: a mismatch must never print the values
    assert default.keys() == explicit.keys()
    assert all(default[k] == explicit[k] for k in default)
    assert opencode_bridge._permission_policy("danger-full-access") is None


def test_permission_policy_for_an_unlisted_major_raises_keyerror():
    with pytest.raises(KeyError):
        opencode_bridge._permission_policy("read-only", 3)
    with pytest.raises(KeyError):
        opencode_bridge._env("workspace-write", 3)


def test_run_passes_the_detected_major_to_the_environment(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(TEXT_EVENT), seen=seen))
    opencode_bridge.run_opencode("hi", str(tmp_path), "read-only")
    env = seen[0][1]["env"]
    assert json.loads(env["OPENCODE_PERMISSION"]) == opencode_bridge._permission_policy(
        "read-only", 1
    )


# ---- status rows


def _row(monkeypatch, version):
    monkeypatch.setattr(opencode_bridge, "opencode_version", lambda: version)
    monkeypatch.setattr(opencode_bridge, "auth_status", lambda: (True, "free models"))
    monkeypatch.setattr(opencode_bridge, "list_models", lambda: ["opencode/x-free"])
    return opencode_bridge.status_rows()[0]


def test_status_row_supported_version_is_ok_and_shows_the_version(monkeypatch):
    assert _row(monkeypatch, "1.18.29") == ("opencode CLI", True, "1.18.29")


def test_status_row_unsupported_major_is_not_ok_and_says_why(monkeypatch):
    label, ok, detail = _row(monkeypatch, "opencode v3.0.0")
    assert (label, ok) == ("opencode CLI", False)
    assert detail.startswith("opencode v3.0.0 — ")
    assert "3.x is not supported yet" in detail


def test_status_row_2x_is_ok(monkeypatch):
    assert _row(monkeypatch, "opencode v2.2.5") == ("opencode CLI", True, "opencode v2.2.5")


def test_status_row_unparseable_version_is_not_ok(monkeypatch):
    _, ok, detail = _row(monkeypatch, "garbage")
    assert ok is False
    assert detail.startswith("garbage — ") and "could not read" in detail


# --------------------------------------------------------------------------
# A fenced run that ends with no text: name the fence, not "no text"
# --------------------------------------------------------------------------

_REJECTED_MSG = "The user rejected permission to use this specific tool call."


def _tool_use(status="error", error=_REJECTED_MSG, **extra):
    return {
        "type": "tool_use",
        "sessionID": SAMPLE_SID,
        "part": {"type": "tool", "tool": "bash", "state": {"status": status, "error": error}},
        **extra,
    }


def test_consume_event_counts_tool_calls_the_policy_rejected():
    state = {}
    opencode_bridge._consume_event(_tool_use(), state)
    opencode_bridge._consume_event(_tool_use(error="Auto-Rejecting a permission prompt"), state)
    assert state["rejected"] == 2


@pytest.mark.parametrize(
    "event",
    [
        _tool_use(status="completed", error=""),
        _tool_use(status="error", error="ENOENT: no such file"),  # failed for another reason
        _tool_use(status="running", error=_REJECTED_MSG),  # not finished
        {"type": "tool_use", "part": {"state": "error"}},
        {"type": "tool_use", "part": "not a dict"},
        {"type": "tool_use"},
        _tool_use(error=None),
    ],
)
def test_consume_event_ignores_tool_events_that_were_not_rejections(event):
    state = {}
    opencode_bridge._consume_event(event, state)
    assert "rejected" not in state


def test_finish_names_the_fence_when_rejected_calls_left_no_answer():
    with pytest.raises(RuntimeError) as exc:
        opencode_bridge._finish({"rejected": 2}, "", 0, "read-only")
    msg = str(exc.value)
    assert "opencode produced no answer" in msg
    assert "2 tool call(s)" in msg
    assert "sandbox='read-only'" in msg
    assert "workspace-write" in msg  # the way out
    assert "no text" not in msg


def test_finish_stderr_marker_alone_says_a_tool_call():
    with pytest.raises(RuntimeError) as exc:
        opencode_bridge._finish({}, "permission requested ... auto-rejecting", 0, "read-only")
    assert "a tool call it attempted" in str(exc.value)
    assert "tool call(s)" not in str(exc.value)


def test_finish_stderr_marker_is_case_insensitive():
    with pytest.raises(RuntimeError, match="produced no answer"):
        opencode_bridge._finish({}, "AUTO-REJECTING", 0, "workspace-write")


def test_finish_without_a_sandbox_still_explains():
    with pytest.raises(RuntimeError) as exc:
        opencode_bridge._finish({"rejected": 1}, "", 0)
    assert "blocked by the sandbox permission policy" in str(exc.value)
    assert "the the" not in str(exc.value)


def test_finish_returns_the_answer_even_if_calls_were_rejected():
    # Under read-only a model that TRIES to write is meant to be blocked; its
    # answer is fine, so rejections only matter when there is no answer.
    state = {"rejected": 3, "chunks": ["the real answer"]}
    assert opencode_bridge._finish(state, "auto-rejecting", 0, "read-only") == "the real answer"


def test_finish_error_event_and_exit_code_still_come_first():
    with pytest.raises(RuntimeError, match="opencode error: boom"):
        opencode_bridge._finish({"error": "boom", "rejected": 2}, "", 0, "read-only")
    with pytest.raises(RuntimeError, match="opencode exited 1"):
        opencode_bridge._finish({"rejected": 2}, "auto-rejecting", 1, "read-only")


def test_finish_other_empty_answers_keep_the_generic_message():
    with pytest.raises(RuntimeError, match="no text in its --format json output"):
        opencode_bridge._finish({}, "some unrelated log", 0, "read-only")


def test_run_with_only_rejected_tool_calls_names_the_sandbox(tmp_path, monkeypatch):
    # The seam: the event stream through _run_impl into _finish, sandbox included.
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(_tool_use(), _tool_use())))
    with pytest.raises(RuntimeError) as exc:
        opencode_bridge.run_opencode("hi", str(tmp_path), "read-only")
    assert "2 tool call(s)" in str(exc.value)
    assert "sandbox='read-only'" in str(exc.value)


def test_streaming_run_with_only_rejected_calls_names_the_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(_tool_use())))
    seen = []
    with pytest.raises(RuntimeError, match="sandbox='workspace-write'"):
        opencode_bridge.run_opencode_streaming(
            "hi", str(tmp_path), "workspace-write", on_event=seen.append
        )
    assert len(seen) == 1  # the viewer still saw the tool event


def test_run_with_rejections_and_an_answer_returns_it(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(_tool_use(), TEXT_EVENT)))
    assert opencode_bridge.run_opencode("hi", str(tmp_path), "read-only") == "TURQUOISE-31415"


# --------------------------------------------------------------------------
# opencode 2.x, side by side with 1.x: policy as a rule list in
# OPENCODE_CONFIG_CONTENT, --standalone instead of --dir, `auth list`
# --------------------------------------------------------------------------


_ENV_RULES = [
    {"action": "read", "resource": "*.env", "effect": "deny"},
    {"action": "read", "resource": "*.env.*", "effect": "deny"},
    {"action": "read", "resource": "*.env.example", "effect": "allow"},
]


def test_v2_workspace_write_rules_in_order():
    assert opencode_bridge._permission_policy("workspace-write", 2) == [
        {"action": "shell", "resource": "*", "effect": "allow"},
        {"action": "edit", "resource": "*", "effect": "allow"},
        {"action": "edit", "resource": "../*", "effect": "deny"},
        {"action": "edit", "resource": "?:/*", "effect": "deny"},
        {"action": "edit", "resource": "/*", "effect": "deny"},
        {"action": "external_directory", "resource": "*", "effect": "deny"},
        *_ENV_RULES,
    ]


def _wildcard(pattern, text, ignore_case=False):
    """A port of opencode 2.2.5's wildcard matcher (packages/core/src/util/wildcard.ts,
    lines 3-14), not fnmatch: fnmatch applies os.path.normcase, which lowercases and
    turns "/" into "\\\\" on Windows, so it would behave differently per OS.

    Both sides get "\\\\" -> "/"; regex metacharacters in the pattern are escaped; then
    "*" -> ".*" and "?" -> "."; the result is anchored ^...$ with the dotall flag, plus
    case-insensitive on win32. `ignore_case` is a parameter here instead of reading
    os.name, so both behaviours run on every OS."""
    pattern = pattern.replace("\\", "/")
    text = text.replace("\\", "/")
    regex = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    flags = re.DOTALL | (re.IGNORECASE if ignore_case else 0)
    return re.fullmatch(regex, text, flags) is not None


def _verdict(rules, action, resource, ignore_case=False):
    """What opencode decides for (action, resource): the LAST matching rule wins
    (packages/core/src/permission.ts, lines 86-95), and "ask" when none matches.

    Caveat: that "ask on no match" holds for OUR rule list in isolation. Real opencode
    flattens its built-in agent defaults ahead of our rules (rulesets.flat()), and
    those defaults allow reading ordinary files. So a read-only run is NOT "all reads
    ask": only the actions and paths our rules name are fenced here."""
    result = "ask"
    for rule in rules:
        if rule["action"] == action and _wildcard(rule["resource"], resource, ignore_case):
            result = rule["effect"]
    return result


@pytest.mark.parametrize(
    "resource,expected",
    [
        ("src/app.py", "allow"),  # an edit inside the run directory is a RELATIVE path
        ("notes.txt", "allow"),
        ("a/b/c.txt", "allow"),
        ("../outside.txt", "deny"),  # climbs out of it
        ("../../etc/passwd", "deny"),
        ("C:/Users/x/file.txt", "deny"),  # Windows drive path
        ("d:/data/file.txt", "deny"),
        ("/etc/passwd", "deny"),  # POSIX absolute
        ("//host/share/file.txt", "deny"),  # UNC
    ],
)
def test_v2_workspace_write_edit_verdicts(resource, expected):
    rules = opencode_bridge._permission_policy("workspace-write", 2)
    assert _verdict(rules, "edit", resource) == expected


@pytest.mark.parametrize("ignore_case", [False, True])
@pytest.mark.parametrize(
    "resource,expected",
    [
        ("src/app.py", "allow"),
        ("..\\outside.txt", "deny"),  # backslashes read as "/" on both sides
        ("C:\\Users\\x\\f.txt", "deny"),
        ("\\etc\\passwd", "deny"),
        ("\\\\host\\share\\f.txt", "deny"),
        ("../outside.txt", "deny"),
        ("C:/x", "deny"),
        ("/etc/passwd", "deny"),
    ],
)
def test_v2_workspace_write_edit_verdicts_are_the_same_either_case_mode(
    resource, expected, ignore_case
):
    rules = opencode_bridge._permission_policy("workspace-write", 2)
    assert _verdict(rules, "edit", resource, ignore_case) == expected


@pytest.mark.parametrize(
    "ignore_case,resource,expected",
    [
        (False, ".ENV", "ask"),  # case-sensitive: "*.env" does not match ".ENV"
        (True, ".ENV", "deny"),  # win32: it does
        (False, "app/.Env.Local", "ask"),
        (True, "app/.Env.Local", "deny"),
        (True, ".ENV.EXAMPLE", "allow"),
    ],
)
def test_v2_env_rules_depend_on_the_case_flag(ignore_case, resource, expected):
    rules = opencode_bridge._permission_policy("read-only", 2)
    assert _verdict(rules, "read", resource, ignore_case) == expected


def test_v2_drive_rule_matches_any_single_letter_drive_in_either_case():
    # "?:/*": "?" is exactly one character
    rules = opencode_bridge._permission_policy("workspace-write", 2)
    for resource in ("C:/x", "z:/x", "D:\\x"):
        assert _verdict(rules, "edit", resource) == "deny"
    # a name that merely has a colon later on is a relative path, so it is allowed
    assert _verdict(rules, "edit", "notes/a:b.txt") == "allow"
    assert _verdict(rules, "edit", "ab:/x") == "allow"  # two characters before the colon


@pytest.mark.parametrize(
    "pattern,text,expected",
    [
        ("*", "", True),
        ("*", "a/b\nc", True),  # dotall: "*" crosses "/" and newlines
        ("?", "a", True),
        ("?", "", False),
        ("?", "ab", False),
        ("a.c", "abc", False),  # "." is escaped, not a regex wildcard
        ("a+b", "a+b", True),
        ("a+b", "aab", False),
        ("(x)", "(x)", True),
        ("[ab]", "a", False),
        ("[ab]", "[ab]", True),
        ("*.env", ".env", True),
        ("*.env", "x.envy", False),  # anchored at both ends
        ("a\\*", "a/anything", True),  # the pattern's backslash is a slash too
    ],
)
def test_the_wildcard_port(pattern, text, expected):
    assert _wildcard(pattern, text) is expected


def test_the_wildcard_port_case_flag():
    assert _wildcard("*.env", ".ENV") is False
    assert _wildcard("*.env", ".ENV", ignore_case=True) is True


def test_verdict_is_last_match_wins_and_ask_when_nothing_matches():
    rules = [
        {"action": "edit", "resource": "*", "effect": "deny"},
        {"action": "edit", "resource": "a/*", "effect": "allow"},
        {"action": "edit", "resource": "a/b", "effect": "deny"},
    ]
    assert _verdict(rules, "edit", "a/c") == "allow"
    assert _verdict(rules, "edit", "a/b") == "deny"  # the later rule wins
    assert _verdict(rules, "edit", "z") == "deny"
    assert _verdict(rules, "read", "a/c") == "ask"  # no rule for the action
    assert _verdict([], "edit", "a") == "ask"


def test_v2_workspace_write_allows_the_shell_and_denies_external_directories():
    rules = opencode_bridge._permission_policy("workspace-write", 2)
    assert _verdict(rules, "shell", "git status") == "allow"
    assert _verdict(rules, "external_directory", "/anywhere") == "deny"


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
@pytest.mark.parametrize(
    "resource,expected",
    [(".env", "deny"), ("app/.env", "deny"), (".env.local", "deny"), (".env.example", "allow")],
)
def test_v2_env_files_stay_unreadable_except_the_example(mode, resource, expected):
    rules = opencode_bridge._permission_policy(mode, 2)
    assert _verdict(rules, "read", resource) == expected


def test_v2_read_only_denies_every_edit():
    rules = opencode_bridge._permission_policy("read-only", 2)
    for resource in ("src/app.py", "../x", "/etc/passwd", "C:/x"):
        assert _verdict(rules, "edit", resource) == "deny"
    assert _verdict(rules, "shell", "ls") == "ask"  # headless: auto-rejected


@pytest.mark.parametrize("major", [1, 2])
@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_no_policy_names_a_workspace_placeholder(major, mode):
    assert "{workspace}" not in json.dumps(opencode_bridge._permission_policy(mode, major))


def test_policy_and_env_take_no_workspace_argument():
    # The fence is workspace-independent: the run directory is the process cwd.
    import inspect

    for fn in (opencode_bridge._permission_policy, opencode_bridge._env):
        assert "workspace" not in inspect.signature(fn).parameters


def test_v2_read_only_rules_in_order():
    assert opencode_bridge._permission_policy("read-only", 2) == [
        {"action": "shell", "resource": "*", "effect": "ask"},
        {"action": "edit", "resource": "*", "effect": "deny"},
        {"action": "subagent", "resource": "*", "effect": "deny"},
        {"action": "external_directory", "resource": "*", "effect": "deny"},
        {"action": "webfetch", "resource": "*", "effect": "deny"},
        {"action": "websearch", "resource": "*", "effect": "deny"},
        {"action": "execute", "resource": "*", "effect": "deny"},
        *_ENV_RULES,
    ]


def test_v2_policy_is_a_copy_the_table_never_sees_changes_to():
    before = json.dumps(opencode_bridge._PERMISSION_POLICIES[2], sort_keys=True)
    for mode in ("read-only", "workspace-write"):
        rules = opencode_bridge._permission_policy(mode, 2)
        rules[0]["effect"] = "allow"  # a rule dict is a copy too, not just the list
        rules[1]["resource"] = "mutated"
        rules.append({"action": "x"})
        rules.clear()
    assert json.dumps(opencode_bridge._PERMISSION_POLICIES[2], sort_keys=True) == before


def test_v1_policy_is_still_a_dict_copy():
    before = json.dumps(opencode_bridge._PERMISSION_POLICIES[1], sort_keys=True)
    policy = opencode_bridge._permission_policy("read-only", 1)
    assert isinstance(policy, dict)
    policy["edit"] = "allow"
    assert json.dumps(opencode_bridge._PERMISSION_POLICIES[1], sort_keys=True) == before


def test_danger_full_access_has_no_policy_on_any_major():
    for major in (1, 2):
        assert opencode_bridge._permission_policy("danger-full-access", major) is None


# ---- _config_content_with


def test_config_content_sets_permissions_on_an_empty_config():
    rules = [{"action": "edit", "resource": "*", "effect": "deny"}]
    for inherited in (None, "", "{}"):
        out = opencode_bridge._config_content_with(inherited, rules)
        assert json.loads(out) == {"permissions": rules}


def test_config_content_keeps_the_callers_other_settings():
    rules = [{"action": "edit", "resource": "*", "effect": "deny"}]
    inherited = json.dumps({"model": "x", "theme": "dark"})
    assert json.loads(opencode_bridge._config_content_with(inherited, rules)) == {
        "model": "x",
        "theme": "dark",
        "permissions": rules,
    }


def test_config_content_replaces_an_inherited_policy_so_it_cannot_loosen_the_mode():
    rules = [{"action": "edit", "resource": "*", "effect": "deny"}]
    inherited = json.dumps(
        {
            "model": "x",
            "permission": {"edit": "allow"},
            "permissions": [{"action": "edit", "resource": "*", "effect": "allow"}],
        }
    )
    assert json.loads(opencode_bridge._config_content_with(inherited, rules)) == {
        "model": "x",
        "permissions": rules,
    }


def test_config_content_no_rules_drops_the_permission_keys():
    inherited = json.dumps({"model": "x", "permission": {"edit": "deny"}, "permissions": []})
    assert json.loads(opencode_bridge._config_content_with(inherited, None)) == {"model": "x"}


@pytest.mark.parametrize("inherited", [None, "", "{}", "not json", "[1, 2]", '"str"', "null", "3"])
def test_config_content_nothing_left_means_unset(inherited):
    assert opencode_bridge._config_content_with(inherited, None) is None


@pytest.mark.parametrize("inherited", ["not json", "[1, 2]", '"str"', "null", "3"])
def test_config_content_treats_anything_but_an_object_as_empty(inherited):
    rules = [{"action": "edit", "resource": "*", "effect": "deny"}]
    assert json.loads(opencode_bridge._config_content_with(inherited, rules)) == {
        "permissions": rules
    }


# ---- _env


@pytest.fixture
def clean_opencode_env(monkeypatch):
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    monkeypatch.delenv("OPENCODE_PERMISSION", raising=False)


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_v2_env_puts_the_policy_in_config_content(clean_opencode_env, mode):
    env = opencode_bridge._env(mode, 2)
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == {
        "permissions": opencode_bridge._permission_policy(mode, 2)
    }
    assert "OPENCODE_PERMISSION" not in env  # 2.x does not read it
    assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"


def test_v2_env_danger_full_access_with_nothing_inherited_has_no_config(clean_opencode_env):
    assert "OPENCODE_CONFIG_CONTENT" not in opencode_bridge._env("danger-full-access", 2)


def test_v2_env_danger_full_access_strips_an_inherited_policy_but_keeps_the_rest(monkeypatch):
    monkeypatch.setenv(
        "OPENCODE_CONFIG_CONTENT", json.dumps({"model": "x", "permission": {"edit": "deny"}})
    )
    env = opencode_bridge._env("danger-full-access", 2)
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == {"model": "x"}


def test_v2_env_inherited_policy_cannot_loosen_a_fenced_run(monkeypatch):
    monkeypatch.setenv(
        "OPENCODE_CONFIG_CONTENT",
        json.dumps({"permissions": [{"action": "edit", "resource": "*", "effect": "allow"}]}),
    )
    env = opencode_bridge._env("read-only", 2)
    rules = json.loads(env["OPENCODE_CONFIG_CONTENT"])["permissions"]
    assert rules == opencode_bridge._permission_policy("read-only", 2)


def test_v2_env_leaves_an_inherited_1x_policy_alone(monkeypatch):
    monkeypatch.setenv("OPENCODE_PERMISSION", '{"edit":"allow"}')
    env = opencode_bridge._env("read-only", 2)
    assert env["OPENCODE_PERMISSION"] == '{"edit":"allow"}'


def test_v2_env_without_a_sandbox_changes_nothing(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"model":"x"}')
    assert opencode_bridge._env(None, 2)["OPENCODE_CONFIG_CONTENT"] == '{"model":"x"}'
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT")
    assert "OPENCODE_CONFIG_CONTENT" not in opencode_bridge._env(None, 2)


def test_v1_env_never_touches_config_content(monkeypatch):
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"model":"x"}')
    env = opencode_bridge._env("read-only", 1)
    assert env["OPENCODE_CONFIG_CONTENT"] == '{"model":"x"}'
    assert json.loads(env["OPENCODE_PERMISSION"]) == opencode_bridge._permission_policy(
        "read-only", 1
    )


def test_v2_env_rejects_a_bad_sandbox():
    with pytest.raises(ValueError):
        opencode_bridge._env("nope", 2)


# ---- build_args


def test_v2_build_args_use_standalone_and_never_dir(tmp_path):
    args = opencode_bridge.build_args("hello", str(tmp_path), "read-only", None, major=2)
    assert args[:5] == [opencode_bridge.OPENCODE_BIN, "run", "--format", "json", "--standalone"]
    assert "--dir" not in args
    assert str(tmp_path) not in args  # the directory is the process cwd, not an argument
    assert args[-2:] == ["--", "hello"]


def test_v1_build_args_still_use_dir_and_never_standalone(tmp_path):
    args = opencode_bridge.build_args("hello", str(tmp_path), "read-only", None, major=1)
    assert args[args.index("--dir") + 1] == str(tmp_path)
    assert "--standalone" not in args
    # the default major is 1, so every older call keeps its argv
    assert args == opencode_bridge.build_args("hello", str(tmp_path), "read-only", None)


@pytest.mark.parametrize("major", [1, 2])
def test_build_args_shared_flags_are_the_same_on_both_majors(tmp_path, major):
    args = opencode_bridge.build_args(
        "p", str(tmp_path), "danger-full-access", "opencode/m", "ses_1", False, "plan", major
    )
    assert args[args.index("-s") + 1] == "ses_1"
    assert args[args.index("-m") + 1] == "opencode/m"
    assert args[args.index("--agent") + 1] == "plan"
    assert "--auto" in args
    assert args[-2:] == ["--", "p"]
    cont = opencode_bridge.build_args(
        "p", str(tmp_path), "read-only", None, None, True, major=major
    )
    assert "-c" in cont and "-s" not in cont and "--auto" not in cont


# ---- a whole run on each major


def test_run_on_2x_spawns_standalone_in_the_workspace_with_the_rule_list(
    tmp_path, monkeypatch, clean_opencode_env
):
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", 2)
    seen = []
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(TEXT_EVENT), seen=seen))
    assert opencode_bridge.run_opencode("hi", str(tmp_path), "workspace-write") == "TURQUOISE-31415"
    args, kwargs = seen[0]
    assert "--standalone" in args and "--dir" not in args
    assert kwargs["cwd"] == str(tmp_path)  # the run directory is the cwd, not an argument
    rules = json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])["permissions"]
    assert rules == opencode_bridge._permission_policy("workspace-write", 2)
    assert "OPENCODE_PERMISSION" not in kwargs["env"]


def test_run_on_1x_spawns_with_dir_and_the_permission_dict(
    tmp_path, monkeypatch, clean_opencode_env
):
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", 1)
    seen = []
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(TEXT_EVENT), seen=seen))
    opencode_bridge.run_opencode("hi", str(tmp_path), "read-only")
    args, kwargs = seen[0]
    assert args[args.index("--dir") + 1] == str(tmp_path) and "--standalone" not in args
    assert kwargs["cwd"] == str(tmp_path)
    assert "OPENCODE_CONFIG_CONTENT" not in kwargs["env"]
    assert json.loads(kwargs["env"]["OPENCODE_PERMISSION"])["edit"] == "deny"


def test_streaming_run_on_2x_is_fenced_the_same_way(tmp_path, monkeypatch, clean_opencode_env):
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", 2)
    seen = []
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(TEXT_EVENT), seen=seen))
    opencode_bridge.run_opencode_streaming("hi", str(tmp_path), "read-only", on_event=lambda e: 0)
    args, kwargs = seen[0]
    assert "--standalone" in args and "--dir" not in args
    assert json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])["permissions"][1] == {
        "action": "edit",
        "resource": "*",
        "effect": "deny",
    }


# ---- 2.x event shapes

_V2_REJECTED = (
    "This non-interactive run cannot ask the user for permission, so the request was "
    "rejected. Continue without this action."
)


def test_consume_event_counts_a_2x_rejection():
    state = {}
    opencode_bridge._consume_event(_tool_use(error=_V2_REJECTED), state)
    assert state["rejected"] == 1


def test_finish_names_the_fence_for_2x_rejections():
    with pytest.raises(RuntimeError) as exc:
        opencode_bridge._finish({"rejected": 1}, "", 0, "read-only")
    assert "sandbox='read-only'" in str(exc.value)


def test_finish_recognises_the_2x_marker_in_stderr():
    with pytest.raises(RuntimeError, match="produced no answer"):
        opencode_bridge._finish({}, "Cannot Ask The User For Permission", 0, "read-only")


_V2_ERROR_EVENT = {
    "type": "error",
    "timestamp": 1788876008398,
    "sessionID": SAMPLE_SID,
    "error": {
        "type": "provider.no-route",
        "message": "Model unavailable: opencode/does-not-exist",
    },
}


def test_consume_event_reads_a_2x_error_message():
    state = {}
    opencode_bridge._consume_event(_V2_ERROR_EVENT, state)
    assert state["error"] == "Model unavailable: opencode/does-not-exist"
    with pytest.raises(RuntimeError, match="opencode error: Model unavailable: opencode/does-not"):
        opencode_bridge._finish(state, "", 1)


def test_consume_event_prefers_data_message_over_message():
    ev = {"type": "error", "error": {"data": {"message": "inner"}, "message": "outer"}}
    state = {}
    opencode_bridge._consume_event(ev, state)
    assert state["error"] == "inner"  # 1.x shape unchanged


def test_a_2x_error_event_through_a_whole_run(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", 2)
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(_ndjson(_V2_ERROR_EVENT), returncode=1))
    with pytest.raises(RuntimeError, match="Model unavailable: opencode/does-not-exist"):
        opencode_bridge.run_opencode("hi", str(tmp_path))


def test_watch_lines_show_a_2x_error_message():
    assert server._opencode_event_to_watch_lines(_V2_ERROR_EVENT) == [
        ("result", "error: Model unavailable: opencode/does-not-exist")
    ]


def test_watch_lines_keep_the_1x_error_message():
    assert server._opencode_event_to_watch_lines(ERROR_EVENT) == [
        ("result", "error: Unexpected server error. Check server logs for details.")
    ]


# ---- credentials row via `auth list`


def _auth_with(monkeypatch, major, stdout="", stderr="", boom=None):
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", major)
    seen = []

    def run(args, **kwargs):
        seen.append(args)
        if boom is not None and args[-2:] == ["auth", "list"]:
            raise boom
        return _P(stdout, stderr, 0)

    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", run)
    return seen


def test_auth_status_2x_runs_auth_list(monkeypatch):
    seen = _auth_with(monkeypatch, 2, "No authenticated integrations\n")
    opencode_bridge.auth_status()
    assert seen[0] == [opencode_bridge.OPENCODE_BIN, "--standalone", "auth", "list"]
    assert all("providers" not in a for a in seen)


def test_auth_status_1x_still_runs_providers_list(monkeypatch):
    seen = _auth_with(monkeypatch, 1, PROVIDERS_OUT_EMPTY)
    opencode_bridge.auth_status()
    assert seen[0][1:] == ["providers", "list"]


def test_auth_status_with_an_unreadable_version_uses_the_1x_command(monkeypatch):
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", None)
    monkeypatch.setattr(opencode_bridge, "opencode_version", lambda: None)
    seen = []

    def run(args, **kwargs):
        seen.append(args)
        return _P(PROVIDERS_OUT_EMPTY, "", 0)

    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", run)
    opencode_bridge.auth_status()
    assert seen[0][1:] == ["providers", "list"]


def test_auth_status_2x_no_integrations_is_ok_while_free_models_exist(monkeypatch):
    _auth_with(monkeypatch, 2, "No authenticated integrations\n" + MODELS_OUT)
    ok, detail = opencode_bridge.auth_status()
    assert ok is True
    assert detail.startswith("none — free `opencode/*` models only")


def test_auth_status_2x_no_integrations_matches_case_insensitively(monkeypatch):
    _auth_with(monkeypatch, 2, "no AUTHENTICATED integrations\n" + MODELS_OUT)
    assert opencode_bridge.auth_status()[1].startswith("none")


def test_auth_status_2x_no_integrations_and_no_models_is_not_ok(monkeypatch):
    _auth_with(monkeypatch, 2, "No authenticated integrations\n")
    # the same fake serves `opencode models`, which then lists nothing
    ok, detail = opencode_bridge.auth_status()
    assert ok is False
    assert detail.startswith("none")


def test_auth_status_2x_other_output_reports_its_first_line(monkeypatch):
    _auth_with(monkeypatch, 2, "anthropic (api key)\nopenai (oauth)\n")
    assert opencode_bridge.auth_status() == (True, "anthropic (api key)")


def test_auth_status_2x_first_line_is_cut_at_120_characters(monkeypatch):
    _auth_with(monkeypatch, 2, "x" * 300 + "\nsecond")
    ok, detail = opencode_bridge.auth_status()
    assert ok is True and detail == "x" * 120


def test_auth_status_2x_strips_ansi_colour(monkeypatch):
    _auth_with(monkeypatch, 2, "\x1b[1m\x1b[33mNo authenticated integrations\x1b[0m\n" + MODELS_OUT)
    ok, detail = opencode_bridge.auth_status()
    assert ok is True and detail.startswith("none")
    _auth_with(monkeypatch, 2, "\x1b[32manthropic\x1b[0m (key)\n")
    assert opencode_bridge.auth_status() == (True, "anthropic (key)")


def test_auth_status_2x_empty_output_is_not_ok(monkeypatch):
    _auth_with(monkeypatch, 2, "", "")
    assert opencode_bridge.auth_status() == (False, "`opencode auth list` printed nothing")


def test_auth_status_2x_reads_stderr_too(monkeypatch):
    _auth_with(monkeypatch, 2, "", "anthropic (key)\n")
    assert opencode_bridge.auth_status() == (True, "anthropic (key)")


def test_auth_status_2x_unrunnable_is_not_ok(monkeypatch):
    _auth_with(monkeypatch, 2, boom=OSError("nope"))
    assert opencode_bridge.auth_status() == (False, "could not run `opencode auth list`")


def test_status_rows_on_2x_use_the_auth_list_row(monkeypatch):
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", 2)
    monkeypatch.setattr(opencode_bridge, "opencode_version", lambda: "opencode v2.2.5")
    monkeypatch.setattr(opencode_bridge, "list_models", lambda: ["opencode/x-free"])
    seen = []

    def run(args, **kwargs):
        seen.append(args)
        return _P("No authenticated integrations\n", "", 0)

    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", run)
    rows = {label: (ok, detail) for label, ok, detail in opencode_bridge.status_rows()}
    assert rows["opencode CLI"] == (True, "opencode v2.2.5")
    assert rows["credentials"][0] is True
    assert [a[1:] for a in seen] == [["--standalone", "auth", "list"]]


# ---- doctor's install hint


def test_doctor_opencode_install_hint_names_both_packages():
    import doctor

    hint = doctor._CHECKS["opencode"][2]
    assert "@opencode/cli" in hint and "opencode-ai" in hint and "OPENCODE_BIN" in hint


# ---- diagnostics must not start opencode 2.x's shared background service
# (verified live on 2.2.5: `models` and `auth list` left it running); the global
# --standalone BEFORE the subcommand prevents that.


@pytest.mark.parametrize(
    "major,expected", [(2, ["--standalone"]), (3, ["--standalone"]), (1, []), (None, [])]
)
def test_diagnostic_prefix(monkeypatch, major, expected):
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", major)
    monkeypatch.setattr(opencode_bridge, "opencode_version", lambda: None)
    assert opencode_bridge._diagnostic_prefix() == expected


def _argv_of_list_models(monkeypatch, major):
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", major)
    seen = []

    def run(args, **kwargs):
        seen.append(args)
        return _P(MODELS_OUT, "", 0)

    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", run)
    assert opencode_bridge.list_models()
    return seen[0]


def test_list_models_on_2x_passes_standalone_before_the_subcommand(monkeypatch):
    argv = _argv_of_list_models(monkeypatch, 2)
    assert argv == [opencode_bridge.OPENCODE_BIN, "--standalone", "models"]
    assert argv.index("--standalone") < argv.index("models")  # after it, it did not help


def test_list_models_on_1x_argv_is_unchanged(monkeypatch):
    assert _argv_of_list_models(monkeypatch, 1) == [opencode_bridge.OPENCODE_BIN, "models"]


def test_auth_status_on_1x_argv_is_unchanged(monkeypatch):
    seen = _auth_with(monkeypatch, 1, PROVIDERS_OUT_EMPTY)
    opencode_bridge.auth_status()
    assert seen[0] == [opencode_bridge.OPENCODE_BIN, "providers", "list"]


@pytest.mark.parametrize("major", [1, 2])
def test_version_probe_never_gets_the_prefix(monkeypatch, major):
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", major)
    seen = []

    def run(args, **kwargs):
        seen.append(args)
        return _P("opencode v2.2.5\n", "", 0)

    monkeypatch.setattr(opencode_bridge.proc_tree, "run_captured", run)
    opencode_bridge.opencode_version()
    assert seen[0] == [opencode_bridge.OPENCODE_BIN, "--version"]


# ---- an inherited OPENCODE_CONFIG_CONTENT that is not plain JSON (e.g. JSONC)

_JSONC = '{\n  // my settings\n  "model": "x",\n}'


@pytest.mark.parametrize("inherited", [_JSONC, "not json", "[1, 2]", '"str"', "3", "null"])
def test_config_content_warns_once_when_it_replaces_an_unreadable_config(inherited, caplog):
    rules = [{"action": "edit", "resource": "*", "effect": "deny"}]
    with caplog.at_level("WARNING", logger="agy_bridge"):
        out = opencode_bridge._config_content_with(inherited, rules)
    assert json.loads(out) == {"permissions": rules}  # still replaced
    warnings = [r for r in caplog.records if r.name == "agy_bridge"]
    assert len(warnings) == 1
    assert "not a plain JSON object" in warnings[0].getMessage()
    assert "JSONC" in warnings[0].getMessage()
    assert warnings[0].levelname == "WARNING"


@pytest.mark.parametrize("inherited", [None, "", "   ", "\n", "{}", '{"model": "x"}'])
def test_config_content_is_silent_for_blank_or_valid_configs(inherited, caplog):
    with caplog.at_level("WARNING", logger="agy_bridge"):
        opencode_bridge._config_content_with(inherited, [])
    assert [r for r in caplog.records if r.name == "agy_bridge"] == []


def test_a_jsonc_config_warns_through_a_whole_env_build(monkeypatch, caplog):
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", _JSONC)
    with caplog.at_level("WARNING", logger="agy_bridge"):
        env = opencode_bridge._env("read-only", 2)
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"])["permissions"]
    assert any("JSONC" in r.getMessage() for r in caplog.records)


# ---- _refresh_bin: an upgrade that moves the binary must not strand the server


@pytest.fixture
def moved_binary(tmp_path, monkeypatch):
    gone = tmp_path / "old" / "opencode.exe"  # never created: the upgrade deleted it
    new = tmp_path / "new" / "opencode.exe"
    new.parent.mkdir()
    new.write_text("x", encoding="utf-8")
    monkeypatch.setattr(opencode_bridge, "OPENCODE_BIN", str(gone))
    monkeypatch.setattr(opencode_bridge, "_BIN_EXPLICIT", False)
    monkeypatch.setattr(opencode_bridge, "_resolve_bin", lambda: str(new))
    return gone, new


def test_refresh_bin_follows_an_upgrade_that_moved_the_binary(moved_binary, caplog):
    _, new = moved_binary
    with caplog.at_level("WARNING", logger="agy_bridge"):
        assert opencode_bridge._refresh_bin() == str(new)
    assert opencode_bridge.OPENCODE_BIN == str(new)
    assert any("is gone" in r.getMessage() for r in caplog.records)


def test_refresh_bin_never_swaps_a_path_the_user_chose(moved_binary):
    gone, _ = moved_binary
    opencode_bridge._BIN_EXPLICIT = True
    assert opencode_bridge._refresh_bin() == str(gone)
    assert opencode_bridge.OPENCODE_BIN == str(gone)


def test_refresh_bin_leaves_an_existing_binary_alone(tmp_path, monkeypatch):
    here = tmp_path / "opencode.exe"
    here.write_text("x", encoding="utf-8")
    monkeypatch.setattr(opencode_bridge, "OPENCODE_BIN", str(here))
    monkeypatch.setattr(opencode_bridge, "_BIN_EXPLICIT", False)
    monkeypatch.setattr(opencode_bridge, "_resolve_bin", lambda: pytest.fail("must not re-resolve"))
    assert opencode_bridge._refresh_bin() == str(here)


def test_refresh_bin_leaves_a_bare_name_alone(monkeypatch):
    monkeypatch.setattr(opencode_bridge, "OPENCODE_BIN", "opencode")
    monkeypatch.setattr(opencode_bridge, "_BIN_EXPLICIT", False)
    monkeypatch.setattr(opencode_bridge, "_resolve_bin", lambda: pytest.fail("must not re-resolve"))
    assert opencode_bridge._refresh_bin() == "opencode"


def test_refresh_bin_keeps_the_old_path_when_nothing_better_resolves(tmp_path, monkeypatch, caplog):
    gone = tmp_path / "old" / "opencode.exe"
    monkeypatch.setattr(opencode_bridge, "OPENCODE_BIN", str(gone))
    monkeypatch.setattr(opencode_bridge, "_BIN_EXPLICIT", False)
    monkeypatch.setattr(opencode_bridge, "_resolve_bin", lambda: str(gone))
    with caplog.at_level("WARNING", logger="agy_bridge"):
        assert opencode_bridge._refresh_bin() == str(gone)
    assert caplog.records == []  # nothing changed, nothing to say


def test_reason_names_opencode_bin_when_an_explicit_path_vanished(tmp_path, monkeypatch):
    gone = tmp_path / "old" / "opencode.exe"
    monkeypatch.setattr(opencode_bridge, "OPENCODE_BIN", str(gone))
    monkeypatch.setattr(opencode_bridge, "_BIN_EXPLICIT", True)
    reason = opencode_bridge.unsupported_version_reason(None)
    assert f"OPENCODE_BIN points at {gone}, which does not exist" in reason
    assert "moved by an upgrade" in reason and "unset it" in reason
    assert "could not read" not in reason


def test_reason_for_a_vanished_explicit_relative_path_with_a_separator(monkeypatch):
    rel = "bin" + os.sep + "opencode"
    monkeypatch.setattr(opencode_bridge, "OPENCODE_BIN", rel)
    monkeypatch.setattr(opencode_bridge, "_BIN_EXPLICIT", True)
    assert "OPENCODE_BIN points at" in opencode_bridge.unsupported_version_reason(None)


def test_reason_for_an_explicit_bare_name_is_the_generic_unreadable_text(monkeypatch):
    monkeypatch.setattr(opencode_bridge, "OPENCODE_BIN", "opencode-not-installed")
    monkeypatch.setattr(opencode_bridge, "_BIN_EXPLICIT", True)
    assert "could not read opencode's version" in opencode_bridge.unsupported_version_reason(None)


def test_reason_for_an_existing_explicit_path_is_the_generic_unreadable_text(tmp_path, monkeypatch):
    here = tmp_path / "opencode.exe"
    here.write_text("x", encoding="utf-8")
    monkeypatch.setattr(opencode_bridge, "OPENCODE_BIN", str(here))
    monkeypatch.setattr(opencode_bridge, "_BIN_EXPLICIT", True)
    assert "could not read opencode's version" in opencode_bridge.unsupported_version_reason(None)


def test_reason_for_a_vanished_path_the_user_did_not_choose_is_the_generic_text(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(opencode_bridge, "OPENCODE_BIN", str(tmp_path / "gone" / "opencode.exe"))
    monkeypatch.setattr(opencode_bridge, "_BIN_EXPLICIT", False)
    assert "could not read opencode's version" in opencode_bridge.unsupported_version_reason(None)


def test_a_run_with_a_vanished_explicit_binary_is_refused_with_that_reason(tmp_path, monkeypatch):
    gone = tmp_path / "old" / "opencode.exe"
    monkeypatch.setattr(opencode_bridge, "OPENCODE_BIN", str(gone))
    monkeypatch.setattr(opencode_bridge, "_BIN_EXPLICIT", True)
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", None)
    spawned = []

    def no_such_file(args, **kwargs):
        spawned.append(args)
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(subprocess, "Popen", no_such_file)
    with pytest.raises(RuntimeError, match="OPENCODE_BIN points at"):
        opencode_bridge.run_opencode("hi", str(tmp_path / "ws"))
    # only the version probe ever tried to start (and failed); never a run
    assert all(argv[-1] == "--version" for argv in spawned)


def test_a_vanished_non_explicit_binary_is_followed_by_the_gate(moved_binary, monkeypatch):
    # The owner's 1.x -> 2.x upgrade deleted the old exe under a running server: the
    # next call must resolve the new one and read ITS version, not fail to start.
    _, new = moved_binary
    monkeypatch.setattr(opencode_bridge, "_MAJOR_CACHE", None)
    monkeypatch.setattr(opencode_bridge, "_read_version", lambda: "opencode v2.2.5")
    assert opencode_bridge.check_supported_version() == 2
    assert opencode_bridge.OPENCODE_BIN == str(new)
