"""Offline unit tests for the pure logic in copilot_bridge.py.

Like test_codex.py these use temp fixtures and monkeypatching and never invoke
copilot, so they cost no Copilot quota. The live round-trip lives in the smoke
test.

    pytest test_copilot.py
"""

import os
from pathlib import Path

import pytest

import copilot_bridge
import server

SAMPLE_SID = "a9ae8023-1e47-4881-8ef0-74f2d439c802"


# --------------------------------------------------------------------------
# validate_sandbox / defaults / _sandbox_flags
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", copilot_bridge.SANDBOX_MODES)
def test_validate_sandbox_accepts_valid(mode):
    assert copilot_bridge.validate_sandbox(mode) == mode


def test_validate_sandbox_rejects_unknown():
    with pytest.raises(ValueError):
        copilot_bridge.validate_sandbox("yolo")


def test_default_sandbox_is_read_only():
    # Safe default, parity with codex: best-effort read-only.
    assert copilot_bridge.DEFAULT_SANDBOX == "read-only"
    assert "read-only" in copilot_bridge.SANDBOX_MODES


def test_sandbox_flags_read_only_denies_write_and_shell():
    flags = copilot_bridge._sandbox_flags("read-only")
    assert "--allow-all-tools" in flags  # required to run headless
    assert "--deny-tool=write" in flags
    assert "--deny-tool=shell" in flags


def test_sandbox_flags_workspace_write_allows_tools_only():
    flags = copilot_bridge._sandbox_flags("workspace-write")
    assert flags == ["--allow-all-tools"]  # writes allowed; paths still workspace


def test_sandbox_flags_danger_is_allow_all():
    assert copilot_bridge._sandbox_flags("danger-full-access") == ["--allow-all"]


def test_sandbox_flags_invalid_raises():
    with pytest.raises(ValueError):
        copilot_bridge._sandbox_flags("nope")


# --------------------------------------------------------------------------
# normalize_workspace
# --------------------------------------------------------------------------


def test_normalize_workspace_none_is_cwd():
    assert copilot_bridge.normalize_workspace(None) == os.getcwd()


def test_normalize_workspace_abspath(tmp_path):
    assert copilot_bridge.normalize_workspace(str(tmp_path)) == os.path.abspath(str(tmp_path))


# --------------------------------------------------------------------------
# build_args — fresh vs resume argv shape (both pass --session-id)
# --------------------------------------------------------------------------


def test_build_args_fresh_basic():
    args = copilot_bridge.build_args("hello", "C:\\ws", "read-only", None, SAMPLE_SID)
    assert args[0] == copilot_bridge.COPILOT_BIN
    assert args[args.index("--session-id") + 1] == SAMPLE_SID
    assert args[args.index("-C") + 1] == "C:\\ws"
    assert "--no-ask-user" in args
    assert "--no-auto-update" in args
    assert "--allow-all-tools" in args
    assert "-s" in args  # silent text mode by default
    assert args[-2:] == ["-p", "hello"]  # prompt positional, last


def test_build_args_disables_builtin_mcps_by_default():
    args = copilot_bridge.build_args("p", "ws", "read-only", None, SAMPLE_SID)
    assert "--disable-builtin-mcps" in args


def test_build_args_keeps_builtin_mcps_when_opted_in(monkeypatch):
    monkeypatch.setattr(copilot_bridge, "_BUILTIN_GITHUB_MCP", True)
    args = copilot_bridge.build_args("p", "ws", "read-only", None, SAMPLE_SID)
    assert "--disable-builtin-mcps" not in args


def test_build_args_with_model():
    args = copilot_bridge.build_args("p", "ws", "workspace-write", "gpt-5.3-codex", SAMPLE_SID)
    assert args[args.index("--model") + 1] == "gpt-5.3-codex"


def test_build_args_json_stream_swaps_silent_for_output_format():
    args = copilot_bridge.build_args("p", "ws", "read-only", None, SAMPLE_SID, json_stream=True)
    assert args[args.index("--output-format") + 1] == "json"
    assert "-s" not in args  # json mode replaces silent text mode
    assert args.index("--output-format") < args.index("-p")


def test_build_args_no_json_by_default():
    args = copilot_bridge.build_args("p", "ws", "read-only", None, SAMPLE_SID)
    assert "--output-format" not in args


def test_build_args_resume_uses_session_id():
    # Continue passes the SAME flag (--session-id) with an existing id; copilot
    # resumes it. Unlike codex, sandbox/-C are re-applied on resume.
    args = copilot_bridge.build_args("again", "C:\\ws", "workspace-write", None, SAMPLE_SID)
    assert args[args.index("--session-id") + 1] == SAMPLE_SID
    assert args[args.index("-C") + 1] == "C:\\ws"
    assert "--allow-all-tools" in args


# --------------------------------------------------------------------------
# workspace.yaml parsing + _resume_target_for (SESSION_STATE_DIR monkeypatched)
# --------------------------------------------------------------------------


def _write_session(state_dir: Path, sid: str, cwd: str, mtime=None) -> Path:
    """Create a minimal session-state dir with a workspace.yaml, like copilot's."""
    d = state_dir / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "workspace.yaml").write_text(
        f"id: {sid}\ncwd: {cwd}\nclient_name: github/cli\nname: a test\n",
        encoding="utf-8",
    )
    if mtime is not None:
        os.utime(d, (mtime, mtime))
    return d


def test_parse_workspace_yaml_reads_id_and_cwd(tmp_path):
    d = _write_session(tmp_path, SAMPLE_SID, "C:\\proj\\repo")
    meta = copilot_bridge._parse_workspace_yaml(d / "workspace.yaml")
    assert meta["id"] == SAMPLE_SID
    assert meta["cwd"] == "C:\\proj\\repo"  # value keeps its own colon (drive letter)


def test_parse_workspace_yaml_missing_returns_empty(tmp_path):
    assert copilot_bridge._parse_workspace_yaml(tmp_path / "nope.yaml") == {}


def test_resume_target_matches_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(copilot_bridge, "SESSION_STATE_DIR", tmp_path)
    _write_session(tmp_path, "11111111-1111-4111-8111-111111111111", "C:\\other", mtime=100)
    want = "22222222-2222-4222-8222-222222222222"
    _write_session(tmp_path, want, "C:\\proj", mtime=200)
    assert copilot_bridge._resume_target_for("C:\\proj") == want


def test_resume_target_none_when_no_cwd_match(tmp_path, monkeypatch):
    monkeypatch.setattr(copilot_bridge, "SESSION_STATE_DIR", tmp_path)
    _write_session(tmp_path, SAMPLE_SID, "C:\\elsewhere")
    assert copilot_bridge._resume_target_for("C:\\proj") is None


def test_resume_target_picks_newest_for_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(copilot_bridge, "SESSION_STATE_DIR", tmp_path)
    old = "33333333-3333-4333-8333-333333333333"
    new = "44444444-4444-4444-8444-444444444444"
    _write_session(tmp_path, old, "C:\\proj", mtime=100)
    _write_session(tmp_path, new, "C:\\proj", mtime=300)
    assert copilot_bridge._resume_target_for("C:\\proj") == new


# --------------------------------------------------------------------------
# _resolve_session + pinning
# --------------------------------------------------------------------------


def test_resolve_session_fresh_returns_new_uuid(monkeypatch):
    monkeypatch.setattr(copilot_bridge, "_PINNED", {})
    sid1 = copilot_bridge._resolve_session("C:\\ws", False)
    sid2 = copilot_bridge._resolve_session("C:\\ws", False)
    assert copilot_bridge._UUID_RE.fullmatch(sid1)
    assert sid1 != sid2  # a fresh id per ask


def test_resolve_session_uses_pin(monkeypatch):
    monkeypatch.setattr(copilot_bridge, "_PINNED", {})
    copilot_bridge._pin("C:\\ws", SAMPLE_SID)
    assert copilot_bridge._resolve_session("C:\\ws", True) == SAMPLE_SID


def test_resolve_session_raises_without_prior(tmp_path, monkeypatch):
    monkeypatch.setattr(copilot_bridge, "_PINNED", {})
    monkeypatch.setattr(copilot_bridge, "SESSION_STATE_DIR", tmp_path / "none")
    with pytest.raises(RuntimeError):
        copilot_bridge._resolve_session("C:\\ws", True)


def test_pin_and_get(monkeypatch):
    monkeypatch.setattr(copilot_bridge, "_PINNED", {})
    assert copilot_bridge.get_pinned("C:\\ws") is None
    copilot_bridge._pin("C:\\ws", SAMPLE_SID)
    assert copilot_bridge.get_pinned("C:\\ws") == SAMPLE_SID


# --------------------------------------------------------------------------
# _answer_from_event (json-stream answer reconstruction)
# --------------------------------------------------------------------------


def test_answer_from_event_uses_last_assistant_message():
    state = {"answer": "", "delta": ""}
    copilot_bridge._answer_from_event(
        {"type": "assistant.message", "data": {"content": "first"}}, state
    )
    copilot_bridge._answer_from_event(
        {"type": "assistant.message", "data": {"content": "final answer"}}, state
    )
    assert state["answer"] == "final answer"


def test_answer_from_event_accumulates_deltas_as_backup():
    state = {"answer": "", "delta": ""}
    copilot_bridge._answer_from_event({"type": "assistant.message_start", "data": {}}, state)
    copilot_bridge._answer_from_event(
        {"type": "assistant.message_delta", "data": {"deltaContent": "ZE"}}, state
    )
    copilot_bridge._answer_from_event(
        {"type": "assistant.message_delta", "data": {"deltaContent": "BRA"}}, state
    )
    assert state["delta"] == "ZEBRA"


# --------------------------------------------------------------------------
# diagnostics: auth_hint / status_rows
# --------------------------------------------------------------------------


def test_auth_hint_reports_env_token(monkeypatch):
    for var in copilot_bridge._TOKEN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghp_xxx")
    ok, detail = copilot_bridge.auth_hint()
    assert ok is True and "COPILOT_GITHUB_TOKEN" in detail


def test_auth_hint_without_env_token(monkeypatch):
    for var in copilot_bridge._TOKEN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    ok, detail = copilot_bridge.auth_hint()
    assert ok is True and "credential store" in detail


def test_status_rows_copilot_missing(monkeypatch):
    monkeypatch.setattr(copilot_bridge, "copilot_version", lambda: None)
    rows = {label: (ok, detail) for label, ok, detail in copilot_bridge.status_rows()}
    assert rows["copilot CLI"][0] is False


def test_status_rows_copilot_ok(monkeypatch):
    monkeypatch.setattr(copilot_bridge, "copilot_version", lambda: "GitHub Copilot CLI 1.0.68.")
    rows = {label: (ok, detail) for label, ok, detail in copilot_bridge.status_rows()}
    assert rows["copilot CLI"] == (True, "GitHub Copilot CLI 1.0.68.")
    assert rows["copilot auth"][0] is True


# --------------------------------------------------------------------------
# watch mode: event -> watch-line mapping + tool-arg extraction
# --------------------------------------------------------------------------


def test_copilot_tool_arg_prefers_command_then_path():
    assert server._copilot_tool_arg({"command": "ls -la", "path": "x"}) == "ls -la"
    assert server._copilot_tool_arg({"path": "C:/x/y.txt"}) == "C:/x/y.txt"
    assert server._copilot_tool_arg({}) == ""
    assert server._copilot_tool_arg(None) == ""


def test_watch_lines_assistant_message_first_line():
    ev = {"type": "assistant.message", "data": {"content": "hello\nworld"}}
    assert server._copilot_event_to_watch_lines(ev) == [("narration", "hello")]


def test_watch_lines_turn_start_is_thinking():
    ev = {"type": "assistant.turn_start", "data": {}}
    assert server._copilot_event_to_watch_lines(ev) == [("narration", "thinking…")]


def test_watch_lines_tool_execution_start():
    ev = {
        "type": "tool.execution_start",
        "data": {"toolName": "view", "arguments": {"path": "C:/a.txt"}},
    }
    assert server._copilot_event_to_watch_lines(ev) == [("command", "view C:/a.txt")]


def test_watch_lines_tool_execution_complete():
    ok = {"type": "tool.execution_complete", "data": {"success": True}}
    bad = {"type": "tool.execution_complete", "data": {"success": False}}
    assert server._copilot_event_to_watch_lines(ok) == [("result", "done")]
    assert server._copilot_event_to_watch_lines(bad) == [("result", "tool failed")]


def test_watch_lines_ignores_noise():
    assert server._copilot_event_to_watch_lines({"type": "session.mcp_servers_loaded"}) == []
    assert server._copilot_event_to_watch_lines({"type": "result", "sessionId": "x"}) == []
    assert server._copilot_event_to_watch_lines({"type": "assistant.idle", "data": {}}) == []
