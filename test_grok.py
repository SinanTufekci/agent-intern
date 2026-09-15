"""Offline unit tests for the pure logic in grok_bridge.py.

Like test_cursor.py these use temp fixtures and monkeypatching and never invoke
`grok`, so they cost no xAI quota. That matters more here than for the other
bridges: this backend is EXPERIMENTAL and the author has no Grok subscription, so
there is no live round-trip to fall back on. What these tests pin down is the
argv we build and the shapes we parse — the argv side is live-verified against
grok 1.0.3 (every combination below was confirmed to parse), while the response
shapes come from xAI's headless docs and are exactly what a community verifier
would prove or disprove.

    pytest test_grok.py
"""

import json
import os
import subprocess

import pytest

import grok_bridge
import server
import swarm

SAMPLE_SID = "0b508e7b-296b-4e6c-9001-55f1be7e6230"

# The exact `grok models` output observed live on grok 1.0.3 while logged OUT.
MODELS_OUT_LOGGED_OUT = (
    "You are not authenticated.\n\nDefault model: grok-4.5\n\nAvailable models:\n"
    "  * grok-4.5 (default)\n"
)


class _P:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture(autouse=True)
def _clear_state(monkeypatch):
    """Every test starts with an empty model cache and no pinned sessions."""
    monkeypatch.setattr(grok_bridge, "_MODELS_CACHE", None)
    monkeypatch.setattr(grok_bridge, "_PINNED", {})


# --------------------------------------------------------------------------
# validate_sandbox / defaults / _sandbox_flags
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", grok_bridge.SANDBOX_MODES)
def test_validate_sandbox_accepts_valid(mode):
    assert grok_bridge.validate_sandbox(mode) == mode


def test_validate_sandbox_rejects_unknown():
    with pytest.raises(ValueError):
        grok_bridge.validate_sandbox("yolo")


def test_default_sandbox_is_read_only():
    assert grok_bridge.DEFAULT_SANDBOX == "read-only"
    assert "read-only" in grok_bridge.SANDBOX_MODES


def test_sandbox_flags_read_only_uses_profile_and_allowlist():
    flags = grok_bridge._sandbox_flags("read-only")
    assert flags[flags.index("--sandbox") + 1] == "read-only"
    # The allowlist is what makes read-only hold on Windows, where grok's OS
    # sandbox is silently not enforced.
    allow = flags[flags.index("--tools") + 1].split(",")
    assert allow == list(grok_bridge.READ_ONLY_TOOLS)
    assert "--no-subagents" in flags


@pytest.mark.parametrize("tool", ["bash", "run_terminal_cmd", "search_replace", "write", "task"])
def test_read_only_allowlist_excludes_write_tools(tool):
    assert tool not in grok_bridge.READ_ONLY_TOOLS


def test_sandbox_flags_workspace_write_uses_workspace_profile():
    flags = grok_bridge._sandbox_flags("workspace-write")
    assert flags[flags.index("--sandbox") + 1] == "workspace"
    assert "--tools" not in flags  # full toolset


def test_sandbox_flags_danger_turns_sandbox_off():
    flags = grok_bridge._sandbox_flags("danger-full-access")
    assert flags[flags.index("--sandbox") + 1] == "off"


@pytest.mark.parametrize("mode", grok_bridge.SANDBOX_MODES)
def test_every_sandbox_mode_auto_approves(mode):
    # Headless grok does NOT auto-approve on its own and there is no human to
    # answer a prompt, so every mode must pass --always-approve.
    assert "--always-approve" in grok_bridge._sandbox_flags(mode)


def test_sandbox_flags_invalid_raises():
    with pytest.raises(ValueError):
        grok_bridge._sandbox_flags("nope")


# --------------------------------------------------------------------------
# normalize_workspace
# --------------------------------------------------------------------------


def test_normalize_workspace_none_is_cwd():
    assert grok_bridge.normalize_workspace(None) == os.getcwd()


def test_normalize_workspace_abspath(tmp_path):
    assert grok_bridge.normalize_workspace(str(tmp_path)) == os.path.abspath(str(tmp_path))


# --------------------------------------------------------------------------
# build_args
# --------------------------------------------------------------------------


def test_build_args_fresh_basic(tmp_path):
    ws = str(tmp_path)
    args = grok_bridge.build_args("hello", ws, "read-only", None)
    assert args[0] == grok_bridge.GROK_BIN
    # -p takes the prompt as its VALUE (not positional) — verified on grok 1.0.3.
    assert args[args.index("-p") + 1] == "hello"
    assert args[args.index("--cwd") + 1] == ws
    assert args[args.index("--output-format") + 1] == "json"
    assert "-r" not in args and "-c" not in args


def test_build_args_with_model(tmp_path):
    args = grok_bridge.build_args("p", str(tmp_path), "workspace-write", "grok-4.5")
    assert args[args.index("-m") + 1] == "grok-4.5"


def test_build_args_resume_id(tmp_path):
    args = grok_bridge.build_args("p", str(tmp_path), "read-only", None, resume_id=SAMPLE_SID)
    assert args[args.index("-r") + 1] == SAMPLE_SID
    assert "-c" not in args


def test_build_args_continue(tmp_path):
    args = grok_bridge.build_args("p", str(tmp_path), "read-only", None, continue_conv=True)
    assert "-c" in args
    assert "-r" not in args


def test_build_args_resume_id_beats_continue(tmp_path):
    # grok itself rejects -r together with -c, so the bridge must pick one.
    args = grok_bridge.build_args(
        "p", str(tmp_path), "read-only", None, resume_id=SAMPLE_SID, continue_conv=True
    )
    assert args[args.index("-r") + 1] == SAMPLE_SID
    assert "-c" not in args


def test_build_args_never_passes_session_id(tmp_path):
    # -s means "create a NEW session with this UUID" on 1.0.3, not "resume".
    args = grok_bridge.build_args("p", str(tmp_path), "read-only", None, resume_id=SAMPLE_SID)
    assert "-s" not in args and "--session-id" not in args


def test_build_args_json_stream_swaps_output_format(tmp_path):
    args = grok_bridge.build_args("p", str(tmp_path), "read-only", None, json_stream=True)
    assert args[args.index("--output-format") + 1] == "streaming-json"


def test_build_args_omits_no_auto_update_flag(tmp_path):
    # Suppression is done with the documented env var, because --no-auto-update is
    # hidden in grok's clap definition and absent from --help.
    args = grok_bridge.build_args("p", str(tmp_path), "read-only", None)
    assert "--no-auto-update" not in args


def test_env_disables_autoupdater():
    assert grok_bridge._env()["GROK_DISABLE_AUTOUPDATER"] == "1"


# --------------------------------------------------------------------------
# _parse_result — the `--output-format json` envelope
# --------------------------------------------------------------------------


def test_parse_result_reads_text_and_session():
    out = json.dumps({"text": "the answer", "sessionId": SAMPLE_SID, "stopReason": "end_turn"})
    assert grok_bridge._parse_result(out, "") == ("the answer", SAMPLE_SID)


def test_parse_result_surfaces_error_envelope():
    # Verified live: this is exactly what an unauthenticated `grok -p` emits.
    out = json.dumps({"type": "error", "message": "Not signed in."})
    with pytest.raises(RuntimeError, match="Not signed in"):
        grok_bridge._parse_result(out, "")


def test_parse_result_tolerates_surrounding_lines():
    out = "warning: something\n" + json.dumps({"text": "hi", "sessionId": SAMPLE_SID})
    assert grok_bridge._parse_result(out, "")[0] == "hi"


def test_parse_result_takes_last_object():
    out = json.dumps({"text": "first"}) + "\n" + json.dumps({"text": "second"})
    assert grok_bridge._parse_result(out, "")[0] == "second"


def test_parse_result_missing_session_is_none():
    assert grok_bridge._parse_result(json.dumps({"text": "hi"}), "") == ("hi", None)


def test_parse_result_empty_answer_raises():
    with pytest.raises(RuntimeError, match="empty answer"):
        grok_bridge._parse_result(json.dumps({"text": "  ", "stopReason": "refusal"}), "")


def test_parse_result_no_json_raises():
    with pytest.raises(RuntimeError, match="no JSON result"):
        grok_bridge._parse_result("not json at all", "boom")


# --------------------------------------------------------------------------
# session pinning + _resume_flags
# --------------------------------------------------------------------------


def test_pin_and_get():
    grok_bridge._pin("/ws", SAMPLE_SID)
    assert grok_bridge.get_pinned("/ws") == SAMPLE_SID
    assert grok_bridge.get_pinned("/other") is None


def test_resume_flags_fresh():
    assert grok_bridge._resume_flags("/ws", False) == (None, False)


def test_resume_flags_continue_uses_pin():
    grok_bridge._pin("/ws", SAMPLE_SID)
    assert grok_bridge._resume_flags("/ws", True) == (SAMPLE_SID, False)


def test_resume_flags_continue_falls_back_to_dash_c():
    # No pin (e.g. after a server restart): let grok resolve "most recent for cwd".
    assert grok_bridge._resume_flags("/ws", True) == (None, True)


# --------------------------------------------------------------------------
# run_grok
# --------------------------------------------------------------------------


def test_run_grok_returns_answer_and_pins(tmp_path, monkeypatch):
    ws = str(tmp_path)
    out = json.dumps({"text": "42", "sessionId": SAMPLE_SID})
    monkeypatch.setattr(grok_bridge.proc_tree, "run_captured", lambda *a, **k: _P(stdout=out))
    assert grok_bridge.run_grok("q", ws) == "42"
    assert grok_bridge.get_pinned(ws) == SAMPLE_SID


def test_run_grok_pin_false_does_not_pin(tmp_path, monkeypatch):
    ws = str(tmp_path)
    out = json.dumps({"text": "42", "sessionId": SAMPLE_SID})
    monkeypatch.setattr(grok_bridge.proc_tree, "run_captured", lambda *a, **k: _P(stdout=out))
    grok_bridge.run_grok("q", ws, pin=False)
    assert grok_bridge.get_pinned(ws) is None  # swarm workers are one-shot


def test_run_grok_continue_does_not_repin(tmp_path, monkeypatch):
    ws = str(tmp_path)
    grok_bridge._pin(ws, SAMPLE_SID)
    out = json.dumps({"text": "ok", "sessionId": "aaaaaaaa-0000-4000-8000-000000000000"})
    monkeypatch.setattr(grok_bridge.proc_tree, "run_captured", lambda *a, **k: _P(stdout=out))
    grok_bridge.run_grok("q", ws, continue_conv=True)
    assert grok_bridge.get_pinned(ws) == SAMPLE_SID


def test_run_grok_error_exit_prefers_grok_message(tmp_path, monkeypatch):
    out = json.dumps({"type": "error", "message": "Not signed in."})
    monkeypatch.setattr(
        grok_bridge.proc_tree, "run_captured", lambda *a, **k: _P(stdout=out, returncode=1)
    )
    with pytest.raises(RuntimeError, match="Not signed in"):
        grok_bridge.run_grok("q", str(tmp_path))


def test_run_grok_error_exit_without_json_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(
        grok_bridge.proc_tree, "run_captured", lambda *a, **k: _P(stderr="segfault", returncode=139)
    )
    with pytest.raises(RuntimeError, match="139"):
        grok_bridge.run_grok("q", str(tmp_path))


def test_run_grok_rejects_bad_sandbox(tmp_path):
    with pytest.raises(ValueError):
        grok_bridge.run_grok("q", str(tmp_path), sandbox="bogus")


def test_run_grok_passes_resume_id_when_continuing(tmp_path, monkeypatch):
    ws = str(tmp_path)
    grok_bridge._pin(ws, SAMPLE_SID)
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return _P(stdout=json.dumps({"text": "ok"}))

    monkeypatch.setattr(grok_bridge.proc_tree, "run_captured", fake_run)
    grok_bridge.run_grok("q", ws, continue_conv=True)
    assert seen["args"][seen["args"].index("-r") + 1] == SAMPLE_SID


# --------------------------------------------------------------------------
# streaming events
# --------------------------------------------------------------------------


def test_answer_from_event_concatenates_text():
    state = {}
    grok_bridge._answer_from_event({"type": "text", "data": "Hello "}, state)
    grok_bridge._answer_from_event({"type": "text", "data": "world"}, state)
    assert state["text"] == "Hello world"


def test_answer_from_event_records_session_on_end():
    state = {}
    grok_bridge._answer_from_event({"type": "end", "sessionId": SAMPLE_SID}, state)
    assert state["session_id"] == SAMPLE_SID


def test_answer_from_event_records_error():
    state = {}
    grok_bridge._answer_from_event({"type": "error", "message": "boom"}, state)
    assert state["error"] == "boom"


def test_answer_from_event_ignores_unknown_types():
    state = {}
    grok_bridge._answer_from_event({"type": "available_commands", "tools": []}, state)
    assert state == {}


# --------------------------------------------------------------------------
# models + auth (both come from `grok models`)
# --------------------------------------------------------------------------


def test_list_models_parses_live_logged_out_format(monkeypatch):
    monkeypatch.setattr(
        grok_bridge.proc_tree, "run_captured", lambda *a, **k: _P(stdout=MODELS_OUT_LOGGED_OUT)
    )
    assert grok_bridge.list_models() == ["grok-4.5"]


def test_list_models_handles_multiple_entries(monkeypatch):
    out = "Available models:\n  * grok-4.5 (default)\n  * grok-5-mini\n  * grok-code-2\n"
    monkeypatch.setattr(grok_bridge.proc_tree, "run_captured", lambda *a, **k: _P(stdout=out))
    assert grok_bridge.list_models() == ["grok-4.5", "grok-5-mini", "grok-code-2"]


def test_list_models_empty_when_unrunnable(monkeypatch):
    def boom(*a, **k):
        raise OSError("not found")

    monkeypatch.setattr(grok_bridge.proc_tree, "run_captured", boom)
    assert grok_bridge.list_models() == []
    assert grok_bridge._MODELS_CACHE is None  # transient failure is not cached


def test_list_models_caches(monkeypatch):
    calls = []

    def fake(*a, **k):
        calls.append(1)
        return _P(stdout=MODELS_OUT_LOGGED_OUT)

    monkeypatch.setattr(grok_bridge.proc_tree, "run_captured", fake)
    grok_bridge.list_models()
    grok_bridge.list_models()
    assert len(calls) == 1


def test_validate_model_accepts_known(monkeypatch):
    monkeypatch.setattr(grok_bridge, "list_models", lambda: ["grok-4.5"])
    assert grok_bridge.validate_model("grok-4.5") == "grok-4.5"


def test_validate_model_rejects_unknown(monkeypatch):
    monkeypatch.setattr(grok_bridge, "list_models", lambda: ["grok-4.5"])
    with pytest.raises(ValueError, match="unknown grok model"):
        grok_bridge.validate_model("gpt-5")


def test_validate_model_none_passthrough():
    assert grok_bridge.validate_model(None) is None
    assert grok_bridge.validate_model("  ") is None


def test_validate_model_lenient_when_list_unavailable(monkeypatch):
    monkeypatch.setattr(grok_bridge, "list_models", lambda: [])
    assert grok_bridge.validate_model("anything") == "anything"


def test_auth_status_detects_logged_out(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setattr(
        grok_bridge.proc_tree, "run_captured", lambda *a, **k: _P(stdout=MODELS_OUT_LOGGED_OUT)
    )
    ok, detail = grok_bridge.auth_status()
    assert ok is False
    assert "grok login" in detail


def test_auth_status_api_key_counts_as_authenticated(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    monkeypatch.setattr(
        grok_bridge.proc_tree, "run_captured", lambda *a, **k: _P(stdout=MODELS_OUT_LOGGED_OUT)
    )
    ok, detail = grok_bridge.auth_status()
    assert ok is True
    assert "XAI_API_KEY" in detail


def test_auth_status_detects_logged_in(monkeypatch):
    out = "Default model: grok-4.5\n\nAvailable models:\n  * grok-4.5 (default)\n"
    monkeypatch.setattr(grok_bridge.proc_tree, "run_captured", lambda *a, **k: _P(stdout=out))
    ok, detail = grok_bridge.auth_status()
    assert ok is True
    assert "grok-4.5" in detail


def test_auth_status_handles_unrunnable(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(grok_bridge.proc_tree, "run_captured", boom)
    ok, detail = grok_bridge.auth_status()
    assert ok is False
    assert "could not run" in detail


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------


def test_grok_version_first_line(monkeypatch):
    monkeypatch.setattr(
        grok_bridge.proc_tree,
        "run_captured",
        lambda *a, **k: _P(stdout="grok 1.0.3 (1a29d5bc12)\nx\n"),
    )
    assert grok_bridge.grok_version() == "grok 1.0.3 (1a29d5bc12)"


def test_grok_version_none_when_missing(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(grok_bridge.proc_tree, "run_captured", boom)
    assert grok_bridge.grok_version() is None


def test_status_rows_shape(monkeypatch):
    monkeypatch.setattr(grok_bridge, "grok_version", lambda: "grok 1.0.3")
    monkeypatch.setattr(grok_bridge, "auth_status", lambda: (True, "ok"))
    monkeypatch.setattr(grok_bridge, "list_models", lambda: ["grok-4.5"])
    rows = grok_bridge.status_rows()
    assert all(len(r) == 3 and isinstance(r[1], bool) for r in rows)
    assert rows[0][0] == "grok CLI"


def test_status_rows_flags_missing_cli(monkeypatch):
    monkeypatch.setattr(grok_bridge, "grok_version", lambda: None)
    monkeypatch.setattr(grok_bridge, "auth_status", lambda: (False, "no"))
    monkeypatch.setattr(grok_bridge, "list_models", lambda: [])
    rows = grok_bridge.status_rows()
    assert rows[0][1] is False
    assert "GROK_BIN" in rows[0][2]


def test_read_history_is_empty(tmp_path):
    assert grok_bridge.read_history(str(tmp_path), True) == []


# --------------------------------------------------------------------------
# server-side watch mapping
# --------------------------------------------------------------------------


def test_event_to_watch_lines_text_is_narration():
    lines = server._grok_event_to_watch_lines({"type": "text", "data": "thinking about it"})
    assert lines == [("narration", "thinking about it")]


def test_event_to_watch_lines_tool_call_prefers_command():
    ev = {"type": "tool_call", "toolName": "bash", "rawInput": {"command": "ls -la"}}
    assert server._grok_event_to_watch_lines(ev) == [("command", "ls -la")]


def test_event_to_watch_lines_tool_call_falls_back_to_title():
    ev = {"type": "tool_call", "toolName": "read_file", "title": "Read", "rawInput": {}}
    assert server._grok_event_to_watch_lines(ev) == [("command", "Read")]


def test_event_to_watch_lines_tool_update_completed():
    ev = {"type": "tool_call_update", "status": "completed"}
    assert server._grok_event_to_watch_lines(ev) == [("result", "done")]


def test_event_to_watch_lines_error():
    lines = server._grok_event_to_watch_lines({"type": "error", "message": "boom"})
    assert lines[0][0] == "result" and "boom" in lines[0][1]


def test_event_to_watch_lines_ignores_unknown():
    assert server._grok_event_to_watch_lines({"type": "available_commands"}) == []


# --------------------------------------------------------------------------
# swarm wiring
# --------------------------------------------------------------------------


@pytest.mark.parametrize("alias", ["grok", "xai", "grok-build", "GROK"])
def test_swarm_accepts_grok_aliases(alias, tmp_path):
    tasks = swarm._normalize_tasks([{"backend": alias, "prompt": "hi", "workspace": str(tmp_path)}])
    assert tasks[0]["backend"] == "grok"


def test_swarm_grok_defaults_to_read_only(tmp_path):
    tasks = swarm._normalize_tasks(
        [{"backend": "grok", "prompt": "hi", "workspace": str(tmp_path)}]
    )
    assert tasks[0]["sandbox"] == "read-only"


def test_swarm_grok_rejects_bad_sandbox(tmp_path):
    with pytest.raises(ValueError):
        swarm._normalize_tasks(
            [{"backend": "grok", "prompt": "hi", "workspace": str(tmp_path), "sandbox": "nope"}]
        )


def test_swarm_unknown_backend_message_lists_grok():
    with pytest.raises(ValueError, match="grok"):
        swarm._normalize_tasks([{"backend": "nope", "prompt": "hi"}])


def test_swarm_grok_worker_isolates_errors(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("grok error: Not signed in.")

    monkeypatch.setattr(grok_bridge, "run_grok", boom)
    r = swarm._run_grok_worker(0, "hi", str(tmp_path), "read-only", None, 30)
    assert r.ok is False
    assert r.backend == "grok"
    assert "Not signed in" in r.error


def test_swarm_grok_worker_returns_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(grok_bridge, "run_grok", lambda *a, **k: "the answer")
    r = swarm._run_grok_worker(3, "hi", str(tmp_path), "read-only", None, 30)
    assert r.ok is True and r.answer == "the answer" and r.index == 3


def test_swarm_grok_worker_never_pins(tmp_path, monkeypatch):
    seen = {}

    def fake(prompt, workspace, sandbox, model, cont, timeout, pin=True):
        seen["pin"] = pin
        return "x"

    monkeypatch.setattr(grok_bridge, "run_grok", fake)
    swarm._run_grok_worker(0, "hi", str(tmp_path), "read-only", None, 30)
    assert seen["pin"] is False


# --------------------------------------------------------------------------
# bin resolution
# --------------------------------------------------------------------------


def test_resolve_bin_prefers_path(monkeypatch):
    monkeypatch.setattr(grok_bridge.shutil, "which", lambda n: "/usr/local/bin/grok")
    monkeypatch.setattr(grok_bridge, "GROK_BIN_ENV", "grok")
    assert grok_bridge._resolve_bin() == "/usr/local/bin/grok"


def test_resolve_bin_falls_back_to_install_dir(tmp_path, monkeypatch):
    # The installer appends its dir to the user PATH, which never reaches an
    # already-running server process — so a PATH miss must still find the binary.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / ("grok.exe" if os.name == "nt" else "grok")
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(grok_bridge.shutil, "which", lambda n: None)
    monkeypatch.setattr(grok_bridge, "GROK_BIN_ENV", "grok")
    monkeypatch.setattr(grok_bridge, "_INSTALL_BIN_DIR", bin_dir)
    assert grok_bridge._resolve_bin() == str(exe)


def test_resolve_bin_returns_name_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.setattr(grok_bridge.shutil, "which", lambda n: None)
    monkeypatch.setattr(grok_bridge, "GROK_BIN_ENV", "grok")
    monkeypatch.setattr(grok_bridge, "_INSTALL_BIN_DIR", tmp_path / "nothing")
    assert grok_bridge._resolve_bin() == "grok"


def test_spawn_kwargs_platform_appropriate():
    kw = grok_bridge._spawn_kwargs()
    assert ("creationflags" in kw) if os.name == "nt" else ("start_new_session" in kw)


def test_subprocess_import_is_used():
    # Guards the monkeypatch target above: tests patch grok_bridge.subprocess.run.
    assert grok_bridge.subprocess is subprocess
