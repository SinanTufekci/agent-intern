"""Offline unit tests for the pure logic in cursor_bridge.py.

Like test_copilot.py these use temp fixtures and monkeypatching and never invoke
cursor-agent, so they cost no Cursor quota. The live round-trip is exercised
manually / in the smoke checks.

    pytest test_cursor.py
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import cursor_bridge
import server

SAMPLE_CID = "0b508e7b-296b-4e6c-9001-55f1be7e6230"


# --------------------------------------------------------------------------
# validate_sandbox / defaults / _sandbox_flags
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", cursor_bridge.SANDBOX_MODES)
def test_validate_sandbox_accepts_valid(mode):
    assert cursor_bridge.validate_sandbox(mode) == mode


def test_validate_sandbox_rejects_unknown():
    with pytest.raises(ValueError):
        cursor_bridge.validate_sandbox("yolo")


def test_default_sandbox_is_read_only():
    assert cursor_bridge.DEFAULT_SANDBOX == "read-only"
    assert "read-only" in cursor_bridge.SANDBOX_MODES


def test_sandbox_flags_read_only_is_ask_mode():
    # Ask mode is agent-enforced read-only (write/shell tools unavailable).
    assert cursor_bridge._sandbox_flags("read-only") == ["--mode", "ask"]


def test_sandbox_flags_workspace_write_forces():
    assert cursor_bridge._sandbox_flags("workspace-write") == ["--force"]


def test_sandbox_flags_danger_disables_sandbox():
    flags = cursor_bridge._sandbox_flags("danger-full-access")
    assert flags == ["--force", "--sandbox", "disabled"]


def test_sandbox_flags_invalid_raises():
    with pytest.raises(ValueError):
        cursor_bridge._sandbox_flags("nope")


# --------------------------------------------------------------------------
# normalize_workspace
# --------------------------------------------------------------------------


def test_normalize_workspace_none_is_cwd():
    assert cursor_bridge.normalize_workspace(None) == os.getcwd()


def test_normalize_workspace_abspath(tmp_path):
    assert cursor_bridge.normalize_workspace(str(tmp_path)) == os.path.abspath(str(tmp_path))


# --------------------------------------------------------------------------
# build_args — fresh vs resume argv shape (both pass --resume)
# --------------------------------------------------------------------------


def test_build_args_fresh_basic():
    args = cursor_bridge.build_args("hello", "C:\\ws", "read-only", None, SAMPLE_CID)
    assert args[0] == cursor_bridge.CURSOR_BIN
    assert "-p" in args
    assert args[args.index("--output-format") + 1] == "text"  # default clean text
    assert "--trust" in args
    assert args[args.index("--workspace") + 1] == "C:\\ws"
    assert args[args.index("--resume") + 1] == SAMPLE_CID
    assert args[args.index("--mode") + 1] == "ask"  # read-only
    assert args[-1] == "hello"  # prompt positional, last


def test_build_args_with_model():
    args = cursor_bridge.build_args("p", "ws", "workspace-write", "gpt-5.2", SAMPLE_CID)
    assert args[args.index("--model") + 1] == "gpt-5.2"
    assert "--force" in args  # workspace-write


def test_build_args_json_stream_swaps_output_format():
    args = cursor_bridge.build_args("p", "ws", "read-only", None, SAMPLE_CID, json_stream=True)
    assert args[args.index("--output-format") + 1] == "stream-json"


def test_build_args_text_by_default():
    args = cursor_bridge.build_args("p", "ws", "read-only", None, SAMPLE_CID)
    assert args[args.index("--output-format") + 1] == "text"


def test_build_args_prompt_is_last_even_with_flags():
    args = cursor_bridge.build_args("do it", "ws", "danger-full-access", "auto", SAMPLE_CID)
    assert args[-1] == "do it"
    assert args[args.index("--resume") + 1] == SAMPLE_CID


# --------------------------------------------------------------------------
# _workspace_hash + on-disk chat lookup (CHATS_DIR monkeypatched)
# --------------------------------------------------------------------------


def test_workspace_hash_is_md5_of_abspath():
    ws = os.path.abspath("C:\\proj")
    expect = hashlib.md5(ws.encode("utf-8")).hexdigest()
    assert cursor_bridge._workspace_hash(ws) == expect
    assert len(cursor_bridge._workspace_hash(ws)) == 32


def _write_chat(chats_dir: Path, hash_name: str, chat_id: str, cwd=None, mtime=None) -> Path:
    """Create a minimal chat dir (meta.json) under chats_dir/<hash>/<chat_id>/."""
    d = chats_dir / hash_name / chat_id
    d.mkdir(parents=True, exist_ok=True)
    meta = {"schemaVersion": 1, "hasConversation": True}
    if cwd is not None:
        meta["cwd"] = cwd
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if mtime is not None:
        os.utime(d, (mtime, mtime))
    return d


def test_read_meta_reads_cwd(tmp_path):
    d = _write_chat(tmp_path, "hash", SAMPLE_CID, cwd="C:\\proj\\repo")
    assert cursor_bridge._read_meta(d / "meta.json")["cwd"] == "C:\\proj\\repo"


def test_read_meta_missing_returns_empty(tmp_path):
    assert cursor_bridge._read_meta(tmp_path / "nope.json") == {}


def test_resume_target_fast_path_hash_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cursor_bridge, "CHATS_DIR", tmp_path)
    ws = str(tmp_path / "proj")
    h = cursor_bridge._workspace_hash(ws)
    _write_chat(tmp_path, h, SAMPLE_CID, cwd=ws, mtime=200)
    assert cursor_bridge._resume_target_for(ws) == SAMPLE_CID


def test_resume_target_trusts_hash_dir_without_cwd(tmp_path, monkeypatch):
    # A chat in the workspace's OWN hash dir with no recorded cwd is still trusted.
    monkeypatch.setattr(cursor_bridge, "CHATS_DIR", tmp_path)
    ws = str(tmp_path / "proj")
    h = cursor_bridge._workspace_hash(ws)
    _write_chat(tmp_path, h, SAMPLE_CID, cwd=None, mtime=200)
    assert cursor_bridge._resume_target_for(ws) == SAMPLE_CID


def test_resume_target_scans_other_hash_dirs_by_cwd(tmp_path, monkeypatch):
    # Fallback: cwd match in a DIFFERENT hash dir (e.g. cross-version hashing).
    monkeypatch.setattr(cursor_bridge, "CHATS_DIR", tmp_path)
    ws = str(tmp_path / "proj")
    _write_chat(tmp_path, "some-other-hash", SAMPLE_CID, cwd=ws, mtime=200)
    assert cursor_bridge._resume_target_for(ws) == SAMPLE_CID


def test_resume_target_picks_newest_for_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(cursor_bridge, "CHATS_DIR", tmp_path)
    ws = str(tmp_path / "proj")
    h = cursor_bridge._workspace_hash(ws)
    old = "11111111-1111-4111-8111-111111111111"
    new = "22222222-2222-4222-8222-222222222222"
    _write_chat(tmp_path, h, old, cwd=ws, mtime=100)
    _write_chat(tmp_path, h, new, cwd=ws, mtime=300)
    assert cursor_bridge._resume_target_for(ws) == new


def test_resume_target_none_when_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr(cursor_bridge, "CHATS_DIR", tmp_path)
    _write_chat(tmp_path, "other", SAMPLE_CID, cwd="C:\\elsewhere")
    assert cursor_bridge._resume_target_for(str(tmp_path / "proj")) is None


def test_resume_target_ignores_non_uuid_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(cursor_bridge, "CHATS_DIR", tmp_path)
    ws = str(tmp_path / "proj")
    h = cursor_bridge._workspace_hash(ws)
    _write_chat(tmp_path, h, "not-a-uuid", cwd=ws, mtime=300)  # skipped
    assert cursor_bridge._resume_target_for(ws) is None


# --------------------------------------------------------------------------
# read_history — always [] for cursor (opaque SQLite blob store)
# --------------------------------------------------------------------------


def test_read_history_always_empty(tmp_path):
    assert cursor_bridge.read_history(str(tmp_path), True) == []
    assert cursor_bridge.read_history(str(tmp_path), False) == []


# --------------------------------------------------------------------------
# _resolve_session + pinning
# --------------------------------------------------------------------------


def test_resolve_session_fresh_calls_create_chat(monkeypatch):
    monkeypatch.setattr(cursor_bridge, "_PINNED", {})
    monkeypatch.setattr(cursor_bridge, "create_chat", lambda ws: SAMPLE_CID)
    assert cursor_bridge._resolve_session("C:\\ws", False) == SAMPLE_CID


def test_resolve_session_uses_pin(monkeypatch):
    monkeypatch.setattr(cursor_bridge, "_PINNED", {})
    cursor_bridge._pin("C:\\ws", SAMPLE_CID)
    assert cursor_bridge._resolve_session("C:\\ws", True) == SAMPLE_CID


def test_resolve_session_raises_without_prior(tmp_path, monkeypatch):
    monkeypatch.setattr(cursor_bridge, "_PINNED", {})
    monkeypatch.setattr(cursor_bridge, "CHATS_DIR", tmp_path / "none")
    with pytest.raises(RuntimeError):
        cursor_bridge._resolve_session("C:\\ws", True)


def test_pin_and_get(monkeypatch):
    monkeypatch.setattr(cursor_bridge, "_PINNED", {})
    assert cursor_bridge.get_pinned("C:\\ws") is None
    cursor_bridge._pin("C:\\ws", SAMPLE_CID)
    assert cursor_bridge.get_pinned("C:\\ws") == SAMPLE_CID


def test_create_chat_parses_uuid(monkeypatch):
    class P:
        returncode = 0
        stdout = f"{SAMPLE_CID}\n"
        stderr = ""

    monkeypatch.setattr(cursor_bridge.subprocess, "run", lambda *a, **k: P())
    assert cursor_bridge.create_chat("C:\\ws") == SAMPLE_CID


def test_create_chat_raises_without_id(monkeypatch):
    class P:
        returncode = 0
        stdout = "no id here"
        stderr = ""

    monkeypatch.setattr(cursor_bridge.subprocess, "run", lambda *a, **k: P())
    with pytest.raises(RuntimeError):
        cursor_bridge.create_chat("C:\\ws")


# --------------------------------------------------------------------------
# models: list_models parsing + validate_model
# --------------------------------------------------------------------------

_MODELS_OUT = (
    "Available models\n\n"
    "auto - Auto (current, default)\n"
    "gpt-5.2 - GPT-5.2\n"
    "sonnet-4-thinking - Claude Sonnet 4 Thinking\n"
)


def test_list_models_parses_ids(monkeypatch):
    monkeypatch.setattr(cursor_bridge, "_MODELS_CACHE", None)

    class P:
        returncode = 0
        stdout = _MODELS_OUT
        stderr = ""

    monkeypatch.setattr(cursor_bridge.subprocess, "run", lambda *a, **k: P())
    ids = cursor_bridge.list_models()
    assert "auto" in ids and "gpt-5.2" in ids and "sonnet-4-thinking" in ids
    assert "Available" not in ids  # header line skipped (no ' - ')


def test_validate_model_accepts_known(monkeypatch):
    monkeypatch.setattr(cursor_bridge, "list_models", lambda: ["auto", "gpt-5.2"])
    assert cursor_bridge.validate_model("gpt-5.2") == "gpt-5.2"


def test_validate_model_rejects_typo(monkeypatch):
    monkeypatch.setattr(cursor_bridge, "list_models", lambda: ["auto", "gpt-5.2"])
    with pytest.raises(ValueError):
        cursor_bridge.validate_model("gpt-6-nope")


def test_validate_model_allows_parameterized(monkeypatch):
    monkeypatch.setattr(cursor_bridge, "list_models", lambda: ["claude-opus-4-8"])
    assert (
        cursor_bridge.validate_model("claude-opus-4-8[context=1m]") == "claude-opus-4-8[context=1m]"
    )


def test_validate_model_none_returns_none():
    assert cursor_bridge.validate_model(None) is None


def test_validate_model_lenient_when_list_unavailable(monkeypatch):
    monkeypatch.setattr(cursor_bridge, "list_models", lambda: [])
    assert cursor_bridge.validate_model("anything") == "anything"


# --------------------------------------------------------------------------
# _answer_from_event (stream-json answer reconstruction)
# --------------------------------------------------------------------------


def test_answer_from_event_uses_result_event():
    state = {"answer": "", "assistant": ""}
    cursor_bridge._answer_from_event(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}}, state
    )
    cursor_bridge._answer_from_event(
        {"type": "result", "is_error": False, "result": "FINAL"}, state
    )
    assert state["answer"] == "FINAL"


def test_answer_from_event_assistant_is_backup_and_error_result_ignored():
    state = {"answer": "", "assistant": ""}
    cursor_bridge._answer_from_event(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "backup"}]}}, state
    )
    assert state["assistant"] == "backup"
    cursor_bridge._answer_from_event({"type": "result", "is_error": True, "result": "err"}, state)
    assert state["answer"] == ""  # a failed result does not become the answer


# --------------------------------------------------------------------------
# diagnostics: auth_status / status_rows
# --------------------------------------------------------------------------


def test_auth_status_logged_in(monkeypatch):
    class P:
        returncode = 0
        stdout = "✓ Logged in as me@example.com\n"
        stderr = ""

    monkeypatch.setattr(cursor_bridge.subprocess, "run", lambda *a, **k: P())
    ok, detail = cursor_bridge.auth_status()
    assert ok is True
    assert "Logged in as me@example.com" in detail
    assert "✓" not in detail  # leading glyph stripped


def test_auth_status_not_logged_in(monkeypatch):
    class P:
        returncode = 1
        stdout = "Not authenticated. Run cursor-agent login.\n"
        stderr = ""

    monkeypatch.setattr(cursor_bridge.subprocess, "run", lambda *a, **k: P())
    ok, _ = cursor_bridge.auth_status()
    assert ok is False


def test_status_rows_cursor_missing(monkeypatch):
    monkeypatch.setattr(cursor_bridge, "cursor_version", lambda: None)
    monkeypatch.setattr(cursor_bridge, "auth_status", lambda: (True, "x"))
    rows = {label: (ok, detail) for label, ok, detail in cursor_bridge.status_rows()}
    assert rows["cursor CLI"][0] is False


def test_status_rows_cursor_ok(monkeypatch):
    monkeypatch.setattr(cursor_bridge, "cursor_version", lambda: "2026.07.08-abc")
    monkeypatch.setattr(cursor_bridge, "auth_status", lambda: (True, "Logged in as x"))
    rows = {label: (ok, detail) for label, ok, detail in cursor_bridge.status_rows()}
    assert rows["cursor CLI"] == (True, "2026.07.08-abc")
    assert rows["cursor auth"][0] is True


# --------------------------------------------------------------------------
# watch mode: event -> watch-line mapping + tool-line extraction (in server.py)
# --------------------------------------------------------------------------


def test_cursor_tool_line_prefers_command():
    tc = {"shellToolCall": {"args": {"command": "ls -la"}, "description": "List"}}
    assert server._cursor_tool_line(tc) == "ls -la"


def test_cursor_tool_line_falls_back_to_description():
    tc = {"readToolCall": {"args": {}, "description": "Read file x"}}
    assert server._cursor_tool_line(tc) == "Read file x"


def test_cursor_tool_line_falls_back_to_tool_name():
    tc = {"writeToolCall": {"args": {}}}
    assert server._cursor_tool_line(tc) == "write"


def test_cursor_tool_line_empty_for_junk():
    assert server._cursor_tool_line({}) == ""
    assert server._cursor_tool_line(None) == ""


def test_cursor_watch_lines_assistant_first_line():
    ev = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello\nworld"}]}}
    assert server._cursor_event_to_watch_lines(ev) == [("narration", "hello")]


def test_cursor_watch_lines_tool_started():
    ev = {
        "type": "tool_call",
        "subtype": "started",
        "tool_call": {"shellToolCall": {"args": {"command": "ls -la"}}},
    }
    assert server._cursor_event_to_watch_lines(ev) == [("command", "ls -la")]


def test_cursor_watch_lines_tool_completed():
    ev = {"type": "tool_call", "subtype": "completed", "tool_call": {"shellToolCall": {}}}
    assert server._cursor_event_to_watch_lines(ev) == [("result", "done")]


def test_cursor_watch_lines_ignores_noise():
    assert server._cursor_event_to_watch_lines({"type": "thinking", "subtype": "delta"}) == []
    assert server._cursor_event_to_watch_lines({"type": "system", "subtype": "init"}) == []
    assert server._cursor_event_to_watch_lines({"type": "user", "message": {}}) == []
    # the final result is shown as the answer card, not a live step line
    assert server._cursor_event_to_watch_lines({"type": "result", "is_error": False}) == []


# --------------------------------------------------------------------------
# swarm integration: cursor is a valid backend
# --------------------------------------------------------------------------


def test_swarm_normalize_accepts_cursor():
    import swarm

    out = swarm._normalize_tasks([{"backend": "cursor", "prompt": "hi", "workspace": "."}])
    assert out[0]["backend"] == "cursor"
    assert out[0]["sandbox"] == "read-only"  # default applied


# --------------------------------------------------------------------------
# run_cursor_streaming completes on PROCESS EXIT, not stdout EOF (mirrors the
# codex/copilot guarantee): a lingering child can hold the stdout pipe open after
# the turn, so completion is driven by the process exiting. The answer is
# reconstructed from the stream's `result` event.
# --------------------------------------------------------------------------

_FAKE_CURSOR = """
import sys, subprocess, json
for ev in [
    {"type": "system", "subtype": "init", "session_id": "x"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "backup"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "FAKE ANSWER"},
]:
    sys.stdout.write(json.dumps(ev) + "\\n")
sys.stdout.flush()
# a lingering child keeps the inherited stdout pipe open after main exits
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"])
sys.exit(0)
"""


def test_run_cursor_streaming_completes_on_process_exit_not_stdout_eof(tmp_path, monkeypatch):
    fake = tmp_path / "fake_cursor.py"
    fake.write_text(_FAKE_CURSOR, encoding="utf-8")
    monkeypatch.setattr(cursor_bridge, "_PINNED", {})
    monkeypatch.setattr(cursor_bridge, "create_chat", lambda ws: SAMPLE_CID)  # no real create-chat
    real_popen = subprocess.Popen

    def fake_popen(args, **kwargs):  # launch the fake CLI (answer comes from the stream)
        return real_popen(
            [sys.executable, str(fake)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    monkeypatch.setattr(cursor_bridge.subprocess, "Popen", fake_popen)
    events = []
    t = time.time()
    ans = cursor_bridge.run_cursor_streaming(
        "p", str(tmp_path), "read-only", None, False, 30, on_event=events.append, pin=False
    )
    dt = time.time() - t
    assert ans == "FAKE ANSWER"
    assert dt < 5.0, f"must return on process exit (~2s), not wait for the 8s child; took {dt:.1f}s"
    assert any(e.get("type") == "result" for e in events)
