"""Offline unit tests for the pure logic in codex_bridge.py.

Like test_server.py / test_swarm.py these use temp fixtures and monkeypatching and
never invoke codex, so they cost no OpenAI quota. The live round-trip lives in the
smoke test.

    pytest test_codex.py
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import codex_bridge
import server
from conftest import fake_os_name

SAMPLE_SID = "019ef10a-fc0e-7180-b22f-5bd19fe8fc5b"


# --------------------------------------------------------------------------
# validate_sandbox / defaults
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", codex_bridge.SANDBOX_MODES)
def test_validate_sandbox_accepts_valid(mode):
    assert codex_bridge.validate_sandbox(mode) == mode


def test_validate_sandbox_rejects_unknown():
    with pytest.raises(ValueError):
        codex_bridge.validate_sandbox("yolo")


def test_default_sandbox_is_read_only():
    # The safe default agreed for codex_ask: reads/answers, writes nothing.
    assert codex_bridge.DEFAULT_SANDBOX == "read-only"
    assert "read-only" in codex_bridge.SANDBOX_MODES


# --------------------------------------------------------------------------
# normalize_workspace
# --------------------------------------------------------------------------


def test_normalize_workspace_none_is_cwd():
    assert codex_bridge.normalize_workspace(None) == os.getcwd()


def test_normalize_workspace_abspath(tmp_path):
    assert codex_bridge.normalize_workspace(str(tmp_path)) == os.path.abspath(str(tmp_path))


# --------------------------------------------------------------------------
# build_args — the heart of the bridge: fresh vs resume argv shape
# --------------------------------------------------------------------------


def test_build_args_fresh_basic():
    args = codex_bridge.build_args("hello", "C:\\ws", "read-only", None, None, "out.txt")
    assert args[0] == codex_bridge.CODEX_BIN
    assert args[1] == "exec"
    assert "resume" not in args
    assert args[args.index("-s") + 1] == "read-only"
    assert args[args.index("-C") + 1] == "C:\\ws"
    assert "--skip-git-repo-check" in args
    assert args[args.index("-o") + 1] == "out.txt"
    assert args[-1] == "hello"  # prompt is positional and last


def test_build_args_fresh_with_model():
    args = codex_bridge.build_args("p", "ws", "workspace-write", "gpt-5.5", None, "o.txt")
    assert args[args.index("-m") + 1] == "gpt-5.5"
    assert args[args.index("-s") + 1] == "workspace-write"


def test_build_args_resume_omits_sandbox_and_cd():
    # resume inherits the session's recorded cwd/sandbox; codex's resume subcommand
    # has no -s/-C, so we must NOT pass them.
    args = codex_bridge.build_args("again", "ws", "read-only", None, SAMPLE_SID, "o.txt")
    assert args[1] == "exec"
    assert args[2] == "resume"
    assert args[3] == SAMPLE_SID
    assert "-s" not in args
    assert "-C" not in args
    assert args[args.index("-o") + 1] == "o.txt"
    assert args[-1] == "again"


def test_build_args_resume_keeps_skip_git_repo_check():
    """This assertion used to say the opposite, and that is what the bug was.

    `codex exec resume` enforces the trusted-directory check exactly like a fresh
    run: verified on 0.149.1 that a resume in a plain non-git workspace died with
    "Not inside a trusted directory and --skip-git-repo-check was not specified"
    while the fresh ask that created that very session succeeded. It only bites
    outside a git repo, which is why hermetic tests and everyday use inside a
    project never caught it. `codex exec resume --help` lists the flag.
    """
    args = codex_bridge.build_args("again", "ws", "read-only", None, SAMPLE_SID, "o.txt")
    assert "--skip-git-repo-check" in args
    assert args.index("--skip-git-repo-check") < args.index("-o")


def test_build_args_resume_with_model():
    args = codex_bridge.build_args("p", "ws", "read-only", "o3", SAMPLE_SID, "o.txt")
    assert "resume" in args
    assert args[args.index("-m") + 1] == "o3"


# --------------------------------------------------------------------------
# session id extraction + rollout parsing
# --------------------------------------------------------------------------


def _write_rollout(sessions_dir, sid, cwd, mtime=None, name=None):
    """Create a minimal rollout file under sessions_dir/2026/06/22/ with a
    session_meta first line, mirroring codex's real layout."""
    day = sessions_dir / "2026" / "06" / "22"
    day.mkdir(parents=True, exist_ok=True)
    path = day / (name or f"rollout-2026-06-22T23-34-49-{sid}.jsonl")
    meta = {
        "timestamp": "2026-06-22T20:37:31.604Z",
        "type": "session_meta",
        "payload": {"id": sid, "cwd": cwd, "cli_version": "0.141.0"},
    }
    path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_session_id_from_name():
    p = Path(f"rollout-2026-06-22T23-34-49-{SAMPLE_SID}.jsonl")
    assert codex_bridge._session_id_from_name(p) == SAMPLE_SID


def test_session_id_from_name_no_uuid():
    assert codex_bridge._session_id_from_name(Path("rollout-nope.jsonl")) is None


def test_session_meta_reads_cwd_and_id(tmp_path):
    p = _write_rollout(tmp_path, SAMPLE_SID, "C:\\proj")
    meta = codex_bridge._session_meta(p)
    assert meta["id"] == SAMPLE_SID
    assert meta["cwd"] == "C:\\proj"


def test_session_meta_returns_none_for_non_meta(tmp_path):
    p = tmp_path / "junk.jsonl"
    p.write_text("not json\n", encoding="utf-8")
    assert codex_bridge._session_meta(p) is None


def test_session_id_of_prefers_filename_over_bad_body(tmp_path):
    day = tmp_path / "2026" / "06" / "22"
    day.mkdir(parents=True)
    p = day / f"rollout-x-{SAMPLE_SID}.jsonl"
    p.write_text("not json\n", encoding="utf-8")
    assert codex_bridge._session_id_of(p) == SAMPLE_SID


# --------------------------------------------------------------------------
# _resume_target_for / _capture_new_session  (SESSIONS_DIR monkeypatched)
# --------------------------------------------------------------------------


def test_resume_target_matches_cwd(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    _write_rollout(sessions, "11111111-1111-7111-8111-111111111111", "C:\\other", mtime=100)
    want = "22222222-2222-7222-8222-222222222222"
    _write_rollout(sessions, want, "C:\\proj", mtime=200)
    assert codex_bridge._resume_target_for("C:\\proj") == want


def test_resume_target_none_when_no_cwd_match(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    _write_rollout(sessions, SAMPLE_SID, "C:\\elsewhere")
    assert codex_bridge._resume_target_for("C:\\proj") is None


def test_resume_target_picks_newest_for_cwd(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    old = "33333333-3333-7333-8333-333333333333"
    new = "44444444-4444-7444-8444-444444444444"
    _write_rollout(sessions, old, "C:\\proj", mtime=100, name=f"rollout-a-{old}.jsonl")
    _write_rollout(sessions, new, "C:\\proj", mtime=300, name=f"rollout-b-{new}.jsonl")
    assert codex_bridge._resume_target_for("C:\\proj") == new


def _append_events(path, events):
    with open(path, "a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def test_read_history_pairs_user_and_agent_messages(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(codex_bridge, "_PINNED", {})
    path = _write_rollout(sessions, SAMPLE_SID, "C:\\proj")
    _append_events(
        path,
        [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "first Q"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "first A"}},
            # a system-injected user-role response_item must NOT be treated as a turn
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "<environment_context>ignore</environment_context>",
                        }
                    ],
                },
            },
            {"type": "event_msg", "payload": {"type": "user_message", "message": "second Q"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "second A"}},
        ],
    )
    assert codex_bridge.read_history("C:\\proj", True) == [
        {"role": "user", "content": "first Q"},
        {"role": "assistant", "content": "first A"},
        {"role": "user", "content": "second Q"},
        {"role": "assistant", "content": "second A"},
    ]


def _item(kind, text, **extra):
    # The event_msg/item_completed shape codex has written since 0.149.1, as read
    # off a 0.162.1 rollout: user parts are "text", agent parts "Text".
    part = "text" if kind == "UserMessage" else "Text"
    item = {"type": kind, "id": "x", "content": [{"type": part, "text": text}], **extra}
    return {"type": "event_msg", "payload": {"type": "item_completed", "item": item}}


def test_read_history_reads_item_completed_messages(tmp_path, monkeypatch):
    # Through 0.144.1 the turns were user_message/agent_message events; from
    # 0.149.1 on they are only item_completed items, and the watch view of
    # codex_continue showed no history at all until this was read.
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(codex_bridge, "_PINNED", {})
    path = _write_rollout(sessions, SAMPLE_SID, "C:\\proj")
    _append_events(
        path,
        [
            _item("UserMessage", "first Q"),
            {
                "type": "event_msg",
                "payload": {"type": "item_completed", "item": {"type": "Reasoning"}},
            },
            _item("AgentMessage", "I'll read it first.", phase="commentary"),
            _item("AgentMessage", "first A", phase="final_answer"),
            _item("UserMessage", "second Q"),
            _item("AgentMessage", "second A"),  # no phase: an answer, as before
        ],
    )
    assert codex_bridge.read_history("C:\\proj", True) == [
        {"role": "user", "content": "first Q"},
        {"role": "assistant", "content": "first A"},
        {"role": "user", "content": "second Q"},
        {"role": "assistant", "content": "second A"},
    ]


def test_read_history_empty_for_fresh_or_unresolved(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(codex_bridge, "_PINNED", {})
    _write_rollout(sessions, SAMPLE_SID, "C:\\proj")
    assert codex_bridge.read_history("C:\\proj", False) == []  # fresh ask
    assert codex_bridge.read_history("C:\\nowhere", True) == []  # no session for cwd


def test_capture_new_session_finds_added_rollout(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    _write_rollout(sessions, SAMPLE_SID, "C:\\proj", name=f"rollout-old-{SAMPLE_SID}.jsonl")
    before = codex_bridge._rollout_names()
    new = "55555555-5555-7555-8555-555555555555"
    _write_rollout(sessions, new, "C:\\proj", name=f"rollout-new-{new}.jsonl")
    assert codex_bridge._capture_new_session(before) == new


def test_capture_new_session_none_when_nothing_new(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(codex_bridge, "_SESSION_POLL_DEADLINE_S", 0.2)
    _write_rollout(sessions, SAMPLE_SID, "C:\\proj")
    before = codex_bridge._rollout_names()
    assert codex_bridge._capture_new_session(before) is None


# --------------------------------------------------------------------------
# pinning + output reading + diagnostics
# --------------------------------------------------------------------------


def test_pin_and_get(monkeypatch):
    monkeypatch.setattr(codex_bridge, "_PINNED", {})
    assert codex_bridge.get_pinned("C:\\ws") is None
    codex_bridge._pin("C:\\ws", SAMPLE_SID)
    assert codex_bridge.get_pinned("C:\\ws") == SAMPLE_SID


def test_read_output_reads_and_strips(tmp_path):
    f = tmp_path / "out.txt"
    f.write_text("  the answer  \n", encoding="utf-8")
    assert codex_bridge._read_output(str(f)) == "the answer"


def test_read_output_missing_returns_empty(tmp_path):
    assert codex_bridge._read_output(str(tmp_path / "nope.txt")) == ""


def test_ansi_strip_regex():
    raw = "\x1b[31;1mLogged in using ChatGPT\x1b[0m"
    assert codex_bridge._ANSI_RE.sub("", raw) == "Logged in using ChatGPT"


def test_status_rows_codex_missing(monkeypatch):
    monkeypatch.setattr(codex_bridge, "codex_version", lambda: None)
    monkeypatch.setattr(codex_bridge, "codex_login_status", lambda: (False, "not logged in"))
    rows = {label: (ok, detail) for label, ok, detail in codex_bridge.status_rows()}
    assert rows["codex CLI"][0] is False
    assert rows["codex auth"][0] is False


def test_status_rows_codex_ok(monkeypatch):
    monkeypatch.setattr(codex_bridge, "codex_version", lambda: "codex-cli 0.141.0")
    monkeypatch.setattr(
        codex_bridge, "codex_login_status", lambda: (True, "Logged in using ChatGPT")
    )
    rows = {label: (ok, detail) for label, ok, detail in codex_bridge.status_rows()}
    assert rows["codex CLI"] == (True, "codex-cli 0.141.0")
    assert rows["codex auth"][0] is True


# --------------------------------------------------------------------------
# watch mode: --json flag, resume resolution, event -> watch-line mapping
# --------------------------------------------------------------------------


def test_build_args_json_stream_adds_flag_before_output():
    args = codex_bridge.build_args("p", "ws", "read-only", None, None, "o.txt", json_stream=True)
    assert "--json" in args
    assert args.index("--json") < args.index("-o")  # --json before -o/prompt


def test_build_args_no_json_by_default():
    args = codex_bridge.build_args("p", "ws", "read-only", None, None, "o.txt")
    assert "--json" not in args


def test_resolve_resume_session_fresh_is_none():
    assert codex_bridge._resolve_resume_session("C:\\ws", False) is None


def test_resolve_resume_session_uses_pin(monkeypatch):
    monkeypatch.setattr(codex_bridge, "_PINNED", {})
    codex_bridge._pin("C:\\ws", SAMPLE_SID)
    assert codex_bridge._resolve_resume_session("C:\\ws", True) == SAMPLE_SID


def test_resolve_resume_session_raises_without_prior(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_bridge, "_PINNED", {})
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", tmp_path / "none")
    with pytest.raises(RuntimeError):
        codex_bridge._resolve_resume_session("C:\\ws", True)


# --------------------------------------------------------------------------
# run_codex_streaming completes on PROCESS EXIT, not stdout EOF (0.18.1 fix):
# codex (node) can leave a child holding the stdout pipe open after the turn, so a
# stdout-EOF loop would hang until the watchdog. Fake CLI reproduces that lingering
# child; the run must still return promptly.
# --------------------------------------------------------------------------

_FAKE_CODEX = """
import sys, subprocess, json
out_path = sys.argv[1]
for ev in [
    {"type": "thread.started"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "FAKE ANSWER"}},
    {"type": "turn.completed"},
]:
    sys.stdout.write(json.dumps(ev) + "\\n")
sys.stdout.flush()
with open(out_path, "w", encoding="utf-8") as f:
    f.write("FAKE ANSWER")
# a lingering child keeps the inherited stdout pipe open after main exits
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"])
sys.exit(0)
"""


def test_run_codex_streaming_completes_on_process_exit_not_stdout_eof(tmp_path, monkeypatch):
    fake = tmp_path / "fake_codex.py"
    fake.write_text(_FAKE_CODEX, encoding="utf-8")
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", tmp_path / "sessions")
    real_popen = subprocess.Popen

    def fake_popen(args, **kwargs):  # launch the fake CLI, honoring codex's -o path
        out_path = args[args.index("-o") + 1]
        return real_popen(
            [sys.executable, str(fake), out_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    monkeypatch.setattr(codex_bridge.subprocess, "Popen", fake_popen)
    events = []
    t = time.time()
    ans = codex_bridge.run_codex_streaming(
        "p", str(tmp_path), "read-only", None, False, 30, on_event=events.append, pin=False
    )
    dt = time.time() - t
    assert ans == "FAKE ANSWER"
    assert dt < 5.0, f"must return on process exit (~2s), not wait for the 8s child; took {dt:.1f}s"
    assert any(e.get("type") == "item.completed" for e in events)  # steps streamed to on_event


def test_watch_lines_agent_message_first_line():
    ev = {"type": "item.completed", "item": {"type": "agent_message", "text": "hello\nworld"}}
    assert server._codex_event_to_watch_lines(ev) == [("narration", "hello")]


def test_watch_lines_command_execution():
    ev = {"type": "item.completed", "item": {"type": "command_execution", "command": "ls -la"}}
    assert server._codex_event_to_watch_lines(ev) == [("command", "ls -la")]


def test_watch_lines_file_change():
    ev = {"type": "item.completed", "item": {"type": "file_change", "changes": [1, 2]}}
    kind, text = server._codex_event_to_watch_lines(ev)[0]
    assert kind == "result" and "2" in text


def test_watch_lines_ignores_noise():
    assert server._codex_event_to_watch_lines({"type": "turn.completed", "usage": {}}) == []
    assert server._codex_event_to_watch_lines({"type": "thread.started"}) == []


# --------------------------------------------------------------------------
# Sandbox policy refusals: codex exits 0 and never mentions them
# --------------------------------------------------------------------------

# One stderr line in the real shape codex logs (0.149.1): the marker the bridge
# keys on is the trailing "rejected: blocked by policy".
_BLOCKED_LINE = (
    "ERROR codex_core::tools::router: error=exec_command failed for "
    "`pwsh.exe -Command 'rg -n version pyproject.toml'`: "
    "CreateProcess { message: Rejected(... rejected: blocked by policy) }"
)
TWO_BLOCKED = _BLOCKED_LINE + chr(10) + _BLOCKED_LINE


def _fake_run(answer, stderr="", rc=0):
    """A codex that writes `answer` to its -o file and exits with `rc`."""

    def run(args, **kwargs):
        Path(args[args.index("-o") + 1]).write_text(answer, encoding="utf-8")
        return subprocess.CompletedProcess(args, rc, stdout="", stderr=stderr)

    return run


def test_policy_block_count_counts_refusals():
    assert codex_bridge._policy_block_count(TWO_BLOCKED) == 2
    assert codex_bridge._policy_block_count("nothing interesting") == 0
    assert codex_bridge._policy_block_count("") == 0


def test_run_codex_flags_policy_blocked_commands(tmp_path, monkeypatch):
    """The silent-wrong-answer case, which is the reason this exists.

    Verified on codex 0.149.1 / Windows: every command was refused under both
    sandbox modes, codex could not read the workspace, fell back to a web search,
    and answered with a version from an unrelated repository — exit 0, non-empty
    -o file, and not a word about it in the answer.
    """
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(codex_bridge.proc_tree, "run_captured", _fake_run("1.2.0", TWO_BLOCKED))
    out = codex_bridge.run_codex("p", str(tmp_path), "read-only", None, False, 30, pin=False)
    assert out.startswith("1.2.0")  # the answer is still returned, not swallowed
    assert "WARNING" in out
    assert "refused to run 2 command(s)" in out
    assert "read-only" in out


def test_run_codex_stays_quiet_without_refusals(tmp_path, monkeypatch):
    """A clean run must not grow a warning — this fires often enough to matter."""
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(
        codex_bridge.proc_tree, "run_captured", _fake_run("0.27.0", "some ordinary log")
    )
    out = codex_bridge.run_codex("p", str(tmp_path), "read-only", None, False, 30, pin=False)
    assert out == "0.27.0"


def test_run_codex_appends_rather_than_raises(tmp_path, monkeypatch):
    """Appended, not raised, on purpose: under read-only a model that TRIES to write
    is supposed to be blocked, and that run's answer is perfectly good.
    """
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(
        codex_bridge.proc_tree, "run_captured", _fake_run("the real answer", _BLOCKED_LINE)
    )
    out = codex_bridge.run_codex("p", str(tmp_path), "read-only", None, False, 30, pin=False)
    assert out.startswith("the real answer")


# --------------------------------------------------------------------------
# binary resolution: an npm shim is traded for the native codex.exe
# --------------------------------------------------------------------------


def _npm_node_shim(path: Path, script: str) -> Path:
    """npm's node-script cmd-shim (as npm 10 writes it), running `script`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(  # bytes, so Windows text mode can't turn \r\n into \r\r\n
        (
            "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\n"
            "SETLOCAL\r\nCALL :find_dp0\r\n\r\n"
            'IF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n) ELSE (\r\n'
            '  SET "_prog=node"\r\n  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n)\r\n\r\n'
            'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
            f'"%dp0%\\{script}" %*\r\n'
        ).encode("ascii")
    )
    return path


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


@pytest.fixture
def npm_prefix(tmp_path, monkeypatch):
    """A global npm prefix holding codex as npm installs it on Windows."""
    monkeypatch.setattr(codex_bridge.proc_tree, "npm_archs", lambda: ["x64", "arm64"])
    monkeypatch.setattr(codex_bridge, "CODEX_BIN_ENV", "codex")
    shim = _npm_node_shim(tmp_path / "codex.cmd", r"node_modules\@openai\codex\bin\codex.js")
    _touch(tmp_path / "node_modules" / "@openai" / "codex" / "bin" / "codex.js")
    monkeypatch.setattr(codex_bridge.shutil, "which", lambda name: str(shim))
    return tmp_path


def test_resolve_bin_trades_an_npm_shim_for_the_native_exe_and_its_env(npm_prefix):
    scope = npm_prefix / "node_modules" / "@openai"
    vendor = scope / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc"
    exe = _touch(vendor / "bin" / "codex.exe")
    assert codex_bridge._resolve_bin() == (
        str(exe),
        {
            "CODEX_MANAGED_PACKAGE_ROOT": os.path.realpath(scope / "codex"),
            "CODEX_MANAGED_BY_NPM": "1",
        },
    )


def test_resolve_bin_falls_back_to_the_codex_package_vendor_dir(npm_prefix):
    # codex.js's own fallback when the platform package can't be resolved.
    package = npm_prefix / "node_modules" / "@openai" / "codex"
    exe = _touch(package / "vendor" / "aarch64-pc-windows-msvc" / "bin" / "codex.exe")
    assert codex_bridge._resolve_bin()[0] == str(exe)


def test_resolve_bin_keeps_the_shim_path_when_no_native_exe_exists(npm_prefix):
    assert codex_bridge._resolve_bin() == (str(npm_prefix / "codex.cmd"), {})


@pytest.mark.parametrize("found", [None, "C:/Programs/OpenAI/Codex/bin/codex.exe"])
def test_resolve_bin_leaves_a_native_install_or_a_missing_one_as_named(monkeypatch, found):
    monkeypatch.setattr(codex_bridge, "CODEX_BIN_ENV", "codex")
    monkeypatch.setattr(codex_bridge.shutil, "which", lambda name: found)
    assert codex_bridge._resolve_bin() == ("codex", {})


def test_spawn_kwargs_add_the_npm_env_only_for_an_npm_resolved_codex(monkeypatch):
    monkeypatch.setattr(codex_bridge, "_LAUNCH_ENV", {})
    assert "env" not in codex_bridge._spawn_kwargs()

    launch = {"CODEX_MANAGED_PACKAGE_ROOT": "/p", "CODEX_MANAGED_BY_NPM": "1"}
    monkeypatch.setattr(codex_bridge, "_LAUNCH_ENV", launch)
    monkeypatch.setenv("CODEX_MANAGED_BY_BUN", "1")  # codex.js drops any other one
    env = codex_bridge._spawn_kwargs()["env"]
    assert env["CODEX_MANAGED_BY_NPM"] == "1" and env["CODEX_MANAGED_PACKAGE_ROOT"] == "/p"
    assert "CODEX_MANAGED_BY_BUN" not in env
    assert env["PATH"] == os.environ["PATH"]


# --------------------------------------------------------------------------
# Windows sandbox downgrade: `-s workspace-write` is accepted, then run read-only
# --------------------------------------------------------------------------

# The header codex 0.166.0 prints on stderr in plain (non --json) mode, verified live.
_HEADER_RO = "OpenAI Codex v0.166.0\n--------\nworkdir: C:\\ws\nsandbox: read-only\n--------\n"
_HEADER_WW = (
    "OpenAI Codex v0.166.0\n--------\nsandbox: workspace-write [workdir, /tmp, $TMPDIR]\n--------\n"
)


def _block(*lines):
    """A codex plain-mode header: banner, rule, `lines`, rule (then the transcript)."""
    return "OpenAI Codex v0.166.0\n--------\n" + "\n".join(lines) + "\n--------\n"


@pytest.mark.parametrize(
    "stderr,expected",
    [
        (_HEADER_RO, "read-only"),
        (_HEADER_WW, "workspace-write"),
        (_block("sandbox: danger-full-access"), "danger-full-access"),
        (_block("workdir: C:\\ws", "sandbox: workspace-write [workdir, /tmp]"), "workspace-write"),
        (_block("  sandbox: read-only  "), "read-only"),  # line-stripped
        ("", None),
        (None, None),
        ("no header here", None),
        # only inside the header block, anchored at the start of its line:
        ("sandbox: danger-full-access", None),  # no block at all
        ("a\nsandbox: workspace-write [workdir, /tmp]\nb", None),
        ("using sandbox: read-only mid-line", None),
        (_block("note: sandbox: read-only"), None),
        (_HEADER_RO + "sandbox: workspace-write\n", "read-only"),  # a transcript line after
        ("OpenAI Codex\n--------\nsandbox: read-only\n", None),  # the block never closes
        ("sandbox: read-only\n--------\nx\n--------\n", None),  # before the first rule
    ],
)
def test_header_sandbox(stderr, expected):
    assert codex_bridge._header_sandbox(stderr) == expected


def _rollout_file(tmp_path, *lines, name="rollout-x.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _turn_context(policy_type, **extra):
    return json.dumps(
        {"type": "turn_context", "payload": {"sandbox_policy": {"type": policy_type}, **extra}}
    )


def test_rollout_sandbox_reads_the_first_turn_context(tmp_path):
    p = _rollout_file(
        tmp_path,
        json.dumps({"type": "session_meta", "payload": {"id": SAMPLE_SID}}),
        _turn_context("read-only"),
        _turn_context("workspace-write"),
    )
    assert codex_bridge._rollout_sandbox(p) == "read-only"


def test_rollout_sandbox_skips_bad_lines_and_lines_without_a_policy(tmp_path):
    p = _rollout_file(
        tmp_path,
        '{"type":"turn_context" this is not json',
        json.dumps({"type": "turn_context", "payload": {}}),
        json.dumps({"type": "turn_context", "payload": {"sandbox_policy": "read-only"}}),
        json.dumps({"type": "turn_context", "payload": {"sandbox_policy": {"type": 3}}}),
        _turn_context("workspace-write"),
    )
    assert codex_bridge._rollout_sandbox(p) == "workspace-write"


def test_rollout_sandbox_returns_none_when_it_cannot_tell(tmp_path):
    assert codex_bridge._rollout_sandbox(None) is None
    assert codex_bridge._rollout_sandbox(tmp_path / "missing.jsonl") is None
    assert codex_bridge._rollout_sandbox(_rollout_file(tmp_path, "not json", "{}")) is None
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert codex_bridge._rollout_sandbox(empty) is None


def _fake_os_name(monkeypatch, name):
    fake_os_name(monkeypatch, codex_bridge, name)


_WARN_HEAD = "\n\n---\n[agent-intern] WARNING: you asked for sandbox="


@pytest.mark.parametrize(
    "requested,effective", [("workspace-write", None), ("read-only", "read-only"), ("x", "x")]
)
def test_downgrade_note_is_empty_when_unknown_or_equal(requested, effective):
    assert codex_bridge._sandbox_downgrade_note(requested, effective) == ""


def test_downgrade_note_names_both_modes_off_windows(monkeypatch):
    _fake_os_name(monkeypatch, "posix")
    note = codex_bridge._sandbox_downgrade_note("workspace-write", "read-only")
    assert note.startswith(_WARN_HEAD + "'workspace-write', but codex ran this under")
    assert "sandbox='read-only'" in note
    assert note.endswith(".")
    assert "[windows]" not in note and "config.toml" not in note


def test_downgrade_note_adds_the_windows_hint_for_a_read_only_downgrade(monkeypatch):
    _fake_os_name(monkeypatch, "nt")
    note = codex_bridge._sandbox_downgrade_note("workspace-write", "read-only")
    assert note.startswith(_WARN_HEAD + "'workspace-write'")
    for part in ("[windows]", 'sandbox = "unelevated"', "~/.codex/config.toml", "windows.sandbox"):
        assert part in note


@pytest.mark.parametrize(
    "requested,effective",
    [
        ("read-only", "workspace-write"),  # not a downgrade to read-only
        ("danger-full-access", "workspace-write"),
        ("workspace-write", "danger-full-access"),
    ],
)
def test_downgrade_note_has_no_windows_hint_for_other_mismatches(monkeypatch, requested, effective):
    _fake_os_name(monkeypatch, "nt")
    note = codex_bridge._sandbox_downgrade_note(requested, effective)
    assert note.startswith(_WARN_HEAD + repr(requested))
    assert note.endswith(".")
    assert "config.toml" not in note


# ---- run_codex: the note rides on a FRESH run's answer


def _run_fresh(tmp_path, monkeypatch, stderr, sandbox="workspace-write", answer="done", **kw):
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", tmp_path / "sessions")
    _fake_os_name(monkeypatch, kw.pop("os_name", "posix"))
    monkeypatch.setattr(codex_bridge.proc_tree, "run_captured", _fake_run(answer, stderr))
    return codex_bridge.run_codex("p", str(tmp_path), sandbox, None, False, 30, pin=False)


def test_run_codex_appends_the_downgrade_note(tmp_path, monkeypatch):
    out = _run_fresh(tmp_path, monkeypatch, _HEADER_RO)
    assert out.startswith("done" + _WARN_HEAD + "'workspace-write'")
    assert "sandbox='read-only'" in out


def test_run_codex_downgrade_note_comes_after_the_policy_note(tmp_path, monkeypatch):
    out = _run_fresh(tmp_path, monkeypatch, _BLOCKED_LINE + "\n" + _HEADER_RO)
    assert out.index("refused to run") < out.index("but codex ran this under")


def test_run_codex_windows_hint_reaches_the_answer(tmp_path, monkeypatch):
    out = _run_fresh(tmp_path, monkeypatch, _HEADER_RO, os_name="nt")
    assert 'sandbox = "unelevated"' in out


@pytest.mark.parametrize("stderr", [_HEADER_WW, "", "some ordinary log"])
def test_run_codex_stays_quiet_when_the_sandbox_matches_or_is_unknown(
    tmp_path, monkeypatch, stderr
):
    assert _run_fresh(tmp_path, monkeypatch, stderr) == "done"


def test_run_codex_resume_never_gets_the_note(tmp_path, monkeypatch):
    # A resume sends no -s, so a header there says nothing about what was asked.
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(codex_bridge, "_resolve_resume_session", lambda ws, cont: SAMPLE_SID)
    monkeypatch.setattr(codex_bridge.proc_tree, "run_captured", _fake_run("done", _HEADER_RO))
    out = codex_bridge.run_codex("p", str(tmp_path), "workspace-write", None, True, 30, pin=False)
    assert out == "done"


def test_build_args_never_adds_a_windows_sandbox_override():
    args = codex_bridge.build_args("p", "C:\\ws", "workspace-write", None, None, "o.txt")
    assert not any("windows.sandbox" in a for a in args)


# ---- run_codex_streaming: --json has no header, so the rollout is the source


class _FakeStreamProc:
    """A codex --json process: its stdout events, stderr text, and an -o answer."""

    def __init__(self, args, events, stderr="", answer="done"):
        import io

        Path(args[args.index("-o") + 1]).write_text(answer, encoding="utf-8")
        self.stdout = io.StringIO("".join(json.dumps(e) + "\n" for e in events))
        self.stderr = io.StringIO(stderr)
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


def _stream(tmp_path, monkeypatch, events, stderr="", on_event=None, resume=None):
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", tmp_path / "sessions")
    _fake_os_name(monkeypatch, "posix")
    monkeypatch.setattr(codex_bridge, "_resolve_resume_session", lambda ws, cont: resume)
    monkeypatch.setattr(
        codex_bridge.proc_tree, "popen", lambda args, **kw: _FakeStreamProc(args, events, stderr)
    )
    return codex_bridge.run_codex_streaming(
        "p",
        str(tmp_path),
        "workspace-write",
        None,
        resume is not None,
        30,
        on_event=on_event,
        pin=False,
    )


def _thread_started(sid=SAMPLE_SID):
    return {"type": "thread.started", "thread_id": sid}


def _session_rollout(tmp_path, policy_type, sid=SAMPLE_SID):
    sessions = tmp_path / "sessions" / "2026" / "11" / "05"
    sessions.mkdir(parents=True, exist_ok=True)
    p = sessions / f"rollout-2026-11-05T10-00-00-{sid}.jsonl"
    p.write_text(_turn_context(policy_type) + "\n", encoding="utf-8")
    return p


def test_streaming_reads_the_effective_sandbox_from_the_rollout(tmp_path, monkeypatch):
    _session_rollout(tmp_path, "read-only")
    out = _stream(tmp_path, monkeypatch, [_thread_started()])
    assert out.startswith("done" + _WARN_HEAD + "'workspace-write'")
    assert "sandbox='read-only'" in out


def test_streaming_prefers_the_stderr_header_over_the_rollout(tmp_path, monkeypatch):
    _session_rollout(tmp_path, "workspace-write")  # would say "no downgrade"
    out = _stream(tmp_path, monkeypatch, [_thread_started()], stderr=_HEADER_RO)
    assert "sandbox='read-only'" in out


def test_streaming_matching_rollout_adds_nothing(tmp_path, monkeypatch):
    _session_rollout(tmp_path, "workspace-write")
    assert _stream(tmp_path, monkeypatch, [_thread_started()]) == "done"


def test_streaming_without_a_thread_or_rollout_stays_quiet(tmp_path, monkeypatch):
    assert _stream(tmp_path, monkeypatch, []) == "done"  # no thread.started
    _session_rollout(tmp_path, "read-only", sid="11111111-1111-7111-8111-111111111111")
    assert _stream(tmp_path, monkeypatch, [_thread_started()]) == "done"  # another session's


def test_streaming_captures_the_thread_id_even_without_on_event(tmp_path, monkeypatch):
    _session_rollout(tmp_path, "read-only")
    out = _stream(tmp_path, monkeypatch, [_thread_started()], on_event=None)
    assert "sandbox='read-only'" in out


def test_streaming_still_delivers_every_event_to_on_event(tmp_path, monkeypatch):
    _session_rollout(tmp_path, "read-only")
    events = [
        _thread_started(),
        {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}},
        {"type": "turn.completed", "usage": {}},
    ]
    seen = []
    _stream(tmp_path, monkeypatch, events, on_event=seen.append)
    assert seen == events


def test_streaming_resume_never_gets_the_note(tmp_path, monkeypatch):
    _session_rollout(tmp_path, "read-only")
    out = _stream(tmp_path, monkeypatch, [_thread_started()], stderr=_HEADER_RO, resume=SAMPLE_SID)
    assert out == "done"


# ---- windows_sandbox_setting / status


@pytest.fixture(params=["tomllib", "line-scan"])
def config_toml(request, tmp_path, monkeypatch):
    """Write codex's config.toml; runs once with tomllib, once with the 3.10 line
    scan (the tomllib import blocked, as on 3.10)."""
    if request.param == "line-scan":
        monkeypatch.setitem(sys.modules, "tomllib", None)  # `import tomllib` -> ImportError
    path = tmp_path / "config.toml"
    monkeypatch.setattr(codex_bridge, "CODEX_CONFIG", path)

    def write(text):
        path.write_text(text, encoding="utf-8")

    return write


@pytest.mark.parametrize(
    "text,expected",
    [
        ('[windows]\nsandbox = "unelevated"\n', "unelevated"),
        ('windows.sandbox = "elevated"\n', "elevated"),
        ("[windows]\nsandbox = 'mxc'\n", "mxc"),
        ('model = "x"\n\n[windows]\nsandbox = "unelevated"  # comment\n', "unelevated"),
        ('[windows]\n# sandbox = "elevated"\nsandbox = "mxc"\n', "mxc"),
        ('[other]\nsandbox = "unelevated"\n', None),  # a different table
        ('[other]\nwindows.sandbox = "unelevated"\n', None),  # dotted key inside a table
        ('model = "x"\n', None),
        ("", None),
        ("this is [not valid toml\n", None),
    ],
)
def test_windows_sandbox_setting(config_toml, text, expected):
    config_toml(text)
    assert codex_bridge.windows_sandbox_setting() == expected


def test_windows_sandbox_setting_unreadable_file_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_bridge, "CODEX_CONFIG", tmp_path / "nope.toml")
    assert codex_bridge.windows_sandbox_setting() is None


def test_windows_sandbox_setting_non_string_value_is_none(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("[windows]\nsandbox = 3\n", encoding="utf-8")
    monkeypatch.setattr(codex_bridge, "CODEX_CONFIG", path)
    assert codex_bridge.windows_sandbox_setting() is None


@pytest.mark.parametrize("value", ["mxc", "unelevated", "elevated"])
def test_windows_sandbox_status_known_value(config_toml, value):
    config_toml(f'[windows]\nsandbox = "{value}"\n')
    assert codex_bridge.windows_sandbox_status() == (True, f"configured: {value}")


def test_windows_sandbox_status_unknown_value_is_still_ok(config_toml):
    config_toml('[windows]\nsandbox = "bespoke"\n')
    ok, detail = codex_bridge.windows_sandbox_status()
    assert ok is True
    assert detail == "configured: bespoke (not a value this bridge knows)"


def test_windows_sandbox_status_unset_explains_the_readonly_downgrade(config_toml):
    config_toml("")
    ok, detail = codex_bridge.windows_sandbox_status()
    assert ok is True  # never a failure
    assert detail.startswith("not configured")
    assert "READ-ONLY" in detail and "unelevated" in detail


def _status_labels(monkeypatch, os_name):
    monkeypatch.setattr(codex_bridge, "codex_version", lambda: "codex-cli 0.166.0")
    monkeypatch.setattr(codex_bridge, "codex_login_status", lambda: (True, "ok"))
    _fake_os_name(monkeypatch, os_name)
    return [label for label, _, _ in codex_bridge.status_rows()]


def test_status_rows_windows_sandbox_row_follows_auth_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_bridge, "CODEX_CONFIG", tmp_path / "none.toml")
    labels = _status_labels(monkeypatch, "nt")
    assert labels.index("windows sandbox") == labels.index("codex auth") + 1


def test_status_rows_has_no_windows_sandbox_row_elsewhere(monkeypatch):
    assert "windows sandbox" not in _status_labels(monkeypatch, "posix")


# ---- _header_block / _header_session_id: only codex's own header counts


def test_header_block_is_the_lines_between_the_first_two_rules():
    stderr = _block("workdir: C:\\ws", "model: gpt-x", "sandbox: read-only") + "user\nprompt\n"
    assert codex_bridge._header_block(stderr) == [
        "workdir: C:\\ws",
        "model: gpt-x",
        "sandbox: read-only",
    ]


@pytest.mark.parametrize(
    "stderr",
    [
        "",
        None,
        "no rules at all\nsandbox: read-only\n",
        "banner\n--------\nonly one rule\nsandbox: read-only\n",
        "banner\n---\nsandbox: read-only\n---\n",  # rules are four or more dashes
    ],
)
def test_header_block_is_empty_without_two_rules(stderr):
    assert codex_bridge._header_block(stderr) == []


def test_header_block_accepts_a_longer_rule_and_trailing_spaces():
    stderr = "banner\n----------  \nsandbox: read-only\n--------\n"
    assert codex_bridge._header_block(stderr) == ["sandbox: read-only"]


def test_header_block_only_looks_in_the_first_40_lines():
    late = "\n".join(["noise"] * 40) + "\n--------\nsandbox: read-only\n--------\n"
    assert codex_bridge._header_block(late) == []
    early = "\n".join(["noise"] * 37) + "\n--------\nsandbox: read-only\n--------\n"
    assert codex_bridge._header_block(early) == ["sandbox: read-only"]


def test_header_block_uses_only_the_first_pair_of_rules():
    stderr = _block("sandbox: read-only") + "x\n--------\nsandbox: workspace-write\n--------\n"
    assert codex_bridge._header_block(stderr) == ["sandbox: read-only"]


@pytest.mark.parametrize(
    "stderr,expected",
    [
        (_block("session id: " + SAMPLE_SID), SAMPLE_SID),
        (_block("workdir: C:\\ws", "session id: " + SAMPLE_SID, "sandbox: read-only"), SAMPLE_SID),
        (_block("  session id: " + SAMPLE_SID + "  "), SAMPLE_SID),
        ("session id: " + SAMPLE_SID, None),  # no block
        (_block("note: session id: " + SAMPLE_SID), None),  # not at the start of its line
        (_HEADER_RO + "session id: " + SAMPLE_SID + "\n", None),  # in the transcript
        (_block("sandbox: read-only"), None),
        ("", None),
        (None, None),
    ],
)
def test_header_session_id(stderr, expected):
    assert codex_bridge._header_session_id(stderr) == expected


def test_a_transcript_sandbox_line_never_overrides_the_header():
    stderr = _HEADER_RO + "sandbox: workspace-write\nthe model printed: sandbox: danger\n"
    assert codex_bridge._header_sandbox(stderr) == "read-only"
    assert codex_bridge._header_sandbox(_block("model: x") + "sandbox: workspace-write\n") is None


# ---- run_codex: no header -> the rollout, found by the header's session id or the
# ---- session _capture_new_session saw appear


def _fresh_rollout(sessions, sid, policy_type, name=None):
    day = sessions / "2026" / "11" / "05"
    day.mkdir(parents=True, exist_ok=True)
    path = day / (name or f"rollout-2026-11-05T10-00-00-{sid}.jsonl")
    meta = {"type": "session_meta", "payload": {"id": sid, "cwd": "C:\\ws"}}
    path.write_text(
        json.dumps(meta) + "\n" + _turn_context(policy_type) + "\n",
        encoding="utf-8",
    )
    return path


def _fake_run_writing_a_rollout(answer, stderr, sessions, sid, policy_type):
    """A codex run that, like the real one, leaves a new rollout behind it."""

    def run(args, **kwargs):
        Path(args[args.index("-o") + 1]).write_text(answer, encoding="utf-8")
        _fresh_rollout(sessions, sid, policy_type)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr=stderr)

    return run


def _run_with_rollout(tmp_path, monkeypatch, stderr, policy_type, pin=True, resume=None):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(codex_bridge, "_SESSION_POLL_DEADLINE_S", 0.0)
    _fake_os_name(monkeypatch, "posix")
    monkeypatch.setattr(codex_bridge, "_resolve_resume_session", lambda ws, cont: resume)
    monkeypatch.setattr(
        codex_bridge.proc_tree,
        "run_captured",
        _fake_run_writing_a_rollout("done", stderr, sessions, SAMPLE_SID, policy_type),
    )
    return codex_bridge.run_codex(
        "p", str(tmp_path), "workspace-write", None, resume is not None, 30, pin=pin
    )


def test_run_codex_reads_the_rollout_the_run_left_when_there_is_no_header(tmp_path, monkeypatch):
    out = _run_with_rollout(tmp_path, monkeypatch, "", "read-only")
    assert out.startswith("done" + _WARN_HEAD + "'workspace-write'")
    assert "sandbox='read-only'" in out


def test_run_codex_pins_the_captured_session_after_the_note(tmp_path, monkeypatch):
    _run_with_rollout(tmp_path, monkeypatch, "", "read-only")
    assert codex_bridge.get_pinned(str(tmp_path)) == SAMPLE_SID


def test_run_codex_finds_the_rollout_by_the_headers_session_id_without_pinning(
    tmp_path, monkeypatch
):
    # pin=False means nothing is captured; the header's own session id still names it
    stderr = _block("session id: " + SAMPLE_SID, "model: x")
    out = _run_with_rollout(tmp_path, monkeypatch, stderr, "read-only", pin=False)
    assert "sandbox='read-only'" in out
    assert codex_bridge.get_pinned(str(tmp_path)) is None


def test_run_codex_without_header_or_capture_stays_quiet(tmp_path, monkeypatch):
    # pin=False: nothing captured and no header: nothing to ask, so no guess
    assert _run_with_rollout(tmp_path, monkeypatch, "", "read-only", pin=False) == "done"


def test_run_codex_header_sandbox_wins_over_the_rollout(tmp_path, monkeypatch):
    out = _run_with_rollout(tmp_path, monkeypatch, _HEADER_WW, "read-only")
    assert out == "done"  # the header says workspace-write: no downgrade


def test_run_codex_matching_rollout_adds_nothing(tmp_path, monkeypatch):
    assert _run_with_rollout(tmp_path, monkeypatch, "", "workspace-write") == "done"


def test_run_codex_resume_does_not_read_the_rollout(tmp_path, monkeypatch):
    assert _run_with_rollout(tmp_path, monkeypatch, "", "read-only", resume=SAMPLE_SID) == "done"


# ---- _rollout_for_session: by filename first, the scan only as a fallback


def test_rollout_for_session_finds_a_file_by_its_name_without_opening_the_others(
    tmp_path, monkeypatch
):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    want = _write_rollout(sessions, SAMPLE_SID, "C:\\ws")
    other = _write_rollout(sessions, "11111111-1111-7111-8111-111111111111", "C:\\other")
    opened = []
    real = codex_bridge._session_meta
    monkeypatch.setattr(codex_bridge, "_session_meta", lambda p: opened.append(p) or real(p))
    assert codex_bridge._rollout_for_session(SAMPLE_SID) == want
    assert opened == []  # no rollout was read: the name was enough
    assert other.exists()


def test_rollout_for_session_falls_back_to_the_meta_when_the_name_has_no_id(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    odd = _write_rollout(sessions, SAMPLE_SID, "C:\\ws", name="rollout-renamed.jsonl")
    assert codex_bridge._rollout_for_session(SAMPLE_SID) == odd


def test_rollout_for_session_newest_wins_among_name_matches(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    _write_rollout(sessions, SAMPLE_SID, "C:\\ws", mtime=100, name=f"rollout-a-{SAMPLE_SID}.jsonl")
    new = _write_rollout(
        sessions, SAMPLE_SID, "C:\\ws", mtime=300, name=f"rollout-b-{SAMPLE_SID}.jsonl"
    )
    assert codex_bridge._rollout_for_session(SAMPLE_SID) == new


@pytest.mark.parametrize("sid", ["", None, "not-a-uuid", SAMPLE_SID[:-1], "*"])
def test_rollout_for_session_never_globs_a_non_uuid(tmp_path, monkeypatch, sid):
    # a "*" or a partial id must not become a glob that matches other sessions' files
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", sessions)
    _write_rollout(sessions, SAMPLE_SID, "C:\\ws")
    assert codex_bridge._rollout_for_session(sid) is None


def test_rollout_for_session_without_a_sessions_dir_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_bridge, "SESSIONS_DIR", tmp_path / "missing")
    assert codex_bridge._rollout_for_session(SAMPLE_SID) is None
