"""Offline unit tests for the Claude Code backend: claude_bridge.py, claude_tools.py,
and the pieces of server.py, swarm.py and doctor.py that know about it.

Like the other backend suites these fake the CLI and never run `claude`, so they cost
no Claude quota; conftest's tripwire fails any test that would start the real one.
The fake lives in tmp_path under a directory that is not named `claude`.

    pytest test_claude.py
"""

import asyncio
import functools
import io
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import claude_bridge
import claude_tools
import doctor
import proc_tree
import server  # first: the tool modules import it back
import swarm
from conftest import HiddenEnv, hide_env

REPO = Path(__file__).parent

_REAL_CLAUDE_VERSION = claude_bridge.claude_version
SAMPLE_SID = "0b0d5f5e-6c1d-4a39-9d3f-2f6f6a8d9c11"

# A real `claude -p --output-format stream-json --verbose` run is NDJSON: a system
# init event, the assistant's messages, then one `result` event with the answer.
INIT_EVENT = {"type": "system", "subtype": "init", "session_id": SAMPLE_SID, "tools": []}


def assistant_event(text="", tool=None, sid=SAMPLE_SID):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "id": "toolu_1", "name": tool[0], "input": tool[1]})
    return {
        "type": "assistant",
        "session_id": sid,
        "message": {"role": "assistant", "content": content},
    }


def result_event(result="PONG", sid=SAMPLE_SID, is_error=False, subtype="success", **extra):
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": result,
        "session_id": sid,
        "permission_denials": [],
        **extra,
    }


def ndjson(*events) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


OK_RUN = ndjson(INIT_EVENT, assistant_event("PONG"), result_event("PONG"))


class _Stdin:
    """What the prompt is written to: records it and whether it was closed."""

    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, text):
        assert not self.closed, "wrote to stdin after closing it"
        self.written.append(text)

    def close(self):
        self.closed = True

    @property
    def text(self):
        return "".join(self.written)


def fake_popen(stdout="", stderr="", returncode=0, seen=None, hang=False):
    """A Popen stand-in for `claude`. `seen` collects (args, kwargs, stdin) per spawn.
    `hang=True` never exits, to exercise the timeout path."""

    class _FP:
        def __init__(self, args, **kwargs):
            self.pid = -1
            self.stdin = _Stdin()
            self.stdout = io.StringIO(stdout)
            self.stderr = io.StringIO(stderr)
            self.returncode = returncode
            self.killed = False
            if seen is not None:
                seen.append((args, hide_env(kwargs), self.stdin))

        def wait(self, timeout=None):
            if hang and not self.killed:
                raise subprocess.TimeoutExpired("claude", timeout or 0)
            return self.returncode

        def poll(self):
            return None if (hang and not self.killed) else self.returncode

        def kill(self):
            self.killed = True

    return _FP


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """No pins, no cached version, a supported claude, and a quiet environment."""
    monkeypatch.setattr(claude_bridge, "_PINNED", {})
    monkeypatch.setattr(claude_bridge, "_VERSION_CACHE", None)
    # The version gate would otherwise run `claude --version` before every run.
    monkeypatch.setattr(claude_bridge, "claude_version", lambda: "2.1.319 (Claude Code)")
    for var in (
        *claude_bridge._SCRUBBED_ENV,
        "ANTHROPIC_API_KEY",
        proc_tree.DEPTH_ENV,
    ):
        monkeypatch.delenv(var, raising=False)


def run(tmp_path, monkeypatch, popen, *args, **kwargs):
    """claude_bridge.run_claude against `popen`, in a fresh workspace."""
    monkeypatch.setattr(subprocess, "Popen", popen)
    ws = str(tmp_path / "ws")
    return claude_bridge.run_claude("hi", ws, *args, **kwargs)


# --------------------------------------------------------------------------
# sandbox + argv
# --------------------------------------------------------------------------


def test_the_sandbox_modes_and_default():
    assert claude_bridge.SANDBOX_MODES == ("read-only", "workspace-write", "danger-full-access")
    assert claude_bridge.DEFAULT_SANDBOX == "read-only"


@pytest.mark.parametrize("mode", claude_bridge.SANDBOX_MODES)
def test_validate_sandbox_accepts_valid(mode):
    assert claude_bridge.validate_sandbox(mode) == mode


def test_validate_sandbox_rejects_unknown():
    with pytest.raises(ValueError, match="invalid sandbox"):
        claude_bridge.validate_sandbox("yolo")


def test_validate_model_is_lenient():
    for model in ("haiku", "sonnet", "opus", "claude-sonnet-4-6", "anything-at-all"):
        assert claude_bridge.validate_model(model) == model
    assert claude_bridge.validate_model(None) is None
    assert claude_bridge.validate_model("") is None


def test_normalize_workspace(tmp_path):
    assert claude_bridge.normalize_workspace(str(tmp_path)) == os.path.abspath(str(tmp_path))
    assert claude_bridge.normalize_workspace(None) == os.getcwd()


def test_read_only_flags():
    assert claude_bridge._sandbox_flags("read-only") == [
        "--restricted",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--disallowedTools=Edit,Write,NotebookEdit",
    ]


def test_workspace_write_flags():
    assert claude_bridge._sandbox_flags("workspace-write") == [
        "--restricted",
        "--permission-mode",
        "acceptEdits",
        "--permission-prompts",
        "none",
    ]


def test_danger_full_access_flags():
    assert claude_bridge._sandbox_flags("danger-full-access") == ["--dangerously-skip-permissions"]


def test_sandbox_flags_reject_anything_else():
    with pytest.raises(ValueError):
        claude_bridge._sandbox_flags("nope")


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_a_fenced_mode_never_skips_permissions(mode):
    flags = claude_bridge._sandbox_flags(mode)
    assert "--dangerously-skip-permissions" not in flags
    assert "--restricted" in flags and "--permission-prompts" in flags


def test_danger_full_access_has_no_fence_flags():
    flags = claude_bridge._sandbox_flags("danger-full-access")
    assert "--restricted" not in flags and "--permission-mode" not in flags


def test_read_only_keeps_the_variadic_option_in_one_token():
    # "--disallowedTools" is variadic; the = form stops it swallowing the next argument
    args = claude_bridge.build_args("read-only")
    assert "--disallowedTools=Edit,Write,NotebookEdit" in args
    assert "--disallowedTools" not in args


def test_build_args_fresh_basic():
    args = claude_bridge.build_args("read-only", session_id=SAMPLE_SID)
    assert args[0] == claude_bridge.CLAUDE_BIN
    assert args[1:4] == ["-p", "--output-format", "stream-json"]
    for flag in ("--verbose", "--disable-slash-commands", "--strict-mcp-config", "--restricted"):
        assert flag in args
    assert args[args.index("--session-id") + 1] == SAMPLE_SID
    assert "--resume" not in args and "--continue" not in args and "--model" not in args


def test_build_args_never_puts_the_prompt_in_argv():
    args = claude_bridge.build_args("read-only", session_id=SAMPLE_SID)
    assert args[-2:] == ["--session-id", SAMPLE_SID]  # nothing positional after the flags
    assert "--" not in args


def test_build_args_with_model():
    args = claude_bridge.build_args("read-only", model="haiku", session_id=SAMPLE_SID)
    assert args[args.index("--model") + 1] == "haiku"


def test_build_args_resume_beats_continue_beats_session_id():
    both = claude_bridge.build_args(
        "read-only", session_id=SAMPLE_SID, resume_id="abc", continue_conv=True
    )
    assert both[both.index("--resume") + 1] == "abc"
    assert "--continue" not in both and "--session-id" not in both
    cont = claude_bridge.build_args("read-only", session_id=SAMPLE_SID, continue_conv=True)
    assert "--continue" in cont and "--session-id" not in cont and "--resume" not in cont


def test_build_args_rejects_a_bad_sandbox():
    with pytest.raises(ValueError):
        claude_bridge.build_args("nope")


def test_build_args_keeps_slash_commands_and_mcp_servers_off():
    # a "/name" prompt would run a skill instead of reaching the model, and the
    # user's own MCP servers (this one among them) would give the child a recursion
    args = claude_bridge.build_args("danger-full-access", session_id=SAMPLE_SID)
    assert "--disable-slash-commands" in args and "--strict-mcp-config" in args
    assert "--mcp-config" not in args


def test_build_args_never_uses_bare():
    # --bare ignores OAuth logins, which would lock out every subscription user
    for mode in claude_bridge.SANDBOX_MODES:
        assert "--bare" not in claude_bridge.build_args(mode, session_id=SAMPLE_SID)


# --------------------------------------------------------------------------
# the child's environment
# --------------------------------------------------------------------------


def test_env_drops_the_host_sessions_variables(monkeypatch):
    for var in claude_bridge._SCRUBBED_ENV:
        monkeypatch.setenv(var, "from-the-host-session")
    env = HiddenEnv(claude_bridge._env())
    assert not set(claude_bridge._SCRUBBED_ENV) & set(env)


def test_the_scrub_list_is_the_documented_one():
    assert set(claude_bridge._SCRUBBED_ENV) == {
        "CLAUDECODE",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_SESSION_ATTENDED",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_PID",
        "AI_AGENT",
    }


def test_env_keeps_auth_and_provider_settings(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("SOME_OTHER_VAR", "kept")
    env = HiddenEnv(claude_bridge._env())
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example"
    assert env["CLAUDE_CONFIG_DIR"] == "/somewhere"
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["SOME_OTHER_VAR"] == "kept"


def test_env_turns_background_tasks_off():
    assert claude_bridge._env()["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_env_does_not_change_this_processs_environment(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    claude_bridge._env()
    assert os.environ["CLAUDECODE"] == "1"


def test_a_run_spawns_with_the_scrubbed_env_and_the_depth(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "secret-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    seen = []
    run(tmp_path, monkeypatch, fake_popen(OK_RUN, seen=seen))
    env = seen[0][1]["env"]
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_MESSAGING_TOKEN" not in env
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    assert env[proc_tree.DEPTH_ENV] == "1"  # added by the shared guard in proc_tree.popen


# --------------------------------------------------------------------------
# a whole run, fed through the fake
# --------------------------------------------------------------------------


def test_run_returns_the_result_text(tmp_path, monkeypatch):
    assert run(tmp_path, monkeypatch, fake_popen(OK_RUN)) == "PONG"


def test_the_prompt_goes_in_on_stdin_and_never_in_argv(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "Popen", fake_popen(OK_RUN, seen=seen))
    prompt = '--dangerously-skip-permissions /compact "quotes" & calc\nline two'
    claude_bridge.run_claude(prompt, str(tmp_path / "ws"))
    args, kwargs, stdin = seen[0]
    assert stdin.text == prompt
    assert stdin.closed
    assert not any(prompt in a or "calc" in a for a in args)
    assert kwargs["stdin"] is subprocess.PIPE


def test_a_very_long_prompt_still_fits(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "Popen", fake_popen(OK_RUN, seen=seen))
    prompt = "x" * 500_000  # far beyond any command-line limit
    claude_bridge.run_claude(prompt, str(tmp_path / "ws"))
    assert seen[0][2].text == prompt


def test_the_run_starts_in_the_workspace_and_creates_it(tmp_path, monkeypatch):
    seen = []
    ws = tmp_path / "does" / "not" / "exist"
    monkeypatch.setattr(subprocess, "Popen", fake_popen(OK_RUN, seen=seen))
    claude_bridge.run_claude("hi", str(ws))
    assert seen[0][1]["cwd"] == str(ws) and ws.is_dir()


def test_a_fresh_run_names_its_own_session(tmp_path, monkeypatch):
    seen = []
    run(tmp_path, monkeypatch, fake_popen(OK_RUN, seen=seen))
    args = seen[0][0]
    sid = args[args.index("--session-id") + 1]
    assert str(uuid.UUID(sid)) == sid  # a uuid4 the bridge chose up front
    assert "--resume" not in args and "--continue" not in args


def test_each_fresh_run_gets_a_different_session_id(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "Popen", fake_popen(OK_RUN, seen=seen))
    for _ in range(2):
        claude_bridge.run_claude("hi", str(tmp_path / "ws"), pin=False)
    ids = [a[0][a[0].index("--session-id") + 1] for a in seen]
    assert ids[0] != ids[1]


def test_the_sandbox_reaches_the_command_line(tmp_path, monkeypatch):
    seen = []
    run(tmp_path, monkeypatch, fake_popen(OK_RUN, seen=seen), "workspace-write", "opus")
    args = seen[0][0]
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    assert args[args.index("--model") + 1] == "opus"


def test_a_bad_sandbox_is_rejected_before_anything_runs(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "Popen", fake_popen(OK_RUN, seen=seen))
    with pytest.raises(ValueError):
        claude_bridge.run_claude("hi", str(tmp_path / "ws"), "yolo")
    assert seen == []


def test_the_run_pins_the_session_the_result_reports(tmp_path, monkeypatch):
    ws = str(tmp_path / "ws")
    run(tmp_path, monkeypatch, fake_popen(OK_RUN))
    assert claude_bridge.get_pinned(ws) == SAMPLE_SID


def test_the_run_pins_its_own_id_when_the_stream_names_none(tmp_path, monkeypatch):
    seen = []
    stream = ndjson({"type": "result", "subtype": "success", "is_error": False, "result": "ok"})
    run(tmp_path, monkeypatch, fake_popen(stream, seen=seen))
    args = seen[0][0]
    assert claude_bridge.get_pinned(str(tmp_path / "ws")) == args[args.index("--session-id") + 1]


def test_pin_false_leaves_the_continue_slot_alone(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, fake_popen(OK_RUN), pin=False)
    assert claude_bridge.get_pinned(str(tmp_path / "ws")) is None


def test_a_failed_run_pins_nothing(tmp_path, monkeypatch):
    bad = ndjson(result_event("boom", is_error=True, subtype="error_during_execution"))
    with pytest.raises(RuntimeError):
        run(tmp_path, monkeypatch, fake_popen(bad, returncode=1))
    assert claude_bridge.get_pinned(str(tmp_path / "ws")) is None


def test_continue_resumes_the_pinned_session_and_does_not_repin(tmp_path, monkeypatch):
    ws = str(tmp_path / "ws")
    claude_bridge._pin(ws, "pinned-id")
    seen = []
    monkeypatch.setattr(subprocess, "Popen", fake_popen(OK_RUN, seen=seen))
    claude_bridge.run_claude("again", ws, continue_conv=True)
    args = seen[0][0]
    assert args[args.index("--resume") + 1] == "pinned-id"
    assert "--session-id" not in args and "--continue" not in args
    assert claude_bridge.get_pinned(ws) == "pinned-id"  # not replaced by this run's id


def test_continue_without_a_pin_falls_back_to_the_newest_session(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "Popen", fake_popen(OK_RUN, seen=seen))
    claude_bridge.run_claude("again", str(tmp_path / "ws"), continue_conv=True)
    args = seen[0][0]
    assert "--continue" in args and "--resume" not in args and "--session-id" not in args


def test_a_pin_is_per_workspace(tmp_path, monkeypatch):
    claude_bridge._pin(str(tmp_path / "a"), "id-a")
    seen = []
    monkeypatch.setattr(subprocess, "Popen", fake_popen(OK_RUN, seen=seen))
    claude_bridge.run_claude("x", str(tmp_path / "b"), continue_conv=True)
    assert "--resume" not in seen[0][0]  # b has no pin: the newest-session fallback


def test_a_nonjson_line_or_a_noise_line_does_not_break_the_stream(tmp_path, monkeypatch):
    stream = "warming up...\n[1, 2]\n{not json\n" + OK_RUN
    assert run(tmp_path, monkeypatch, fake_popen(stream)) == "PONG"


def test_the_assistant_text_is_the_fallback_when_the_result_has_none(tmp_path, monkeypatch):
    stream = ndjson(assistant_event("the answer"), result_event(""))
    assert run(tmp_path, monkeypatch, fake_popen(stream)) == "the answer"


def test_the_result_text_wins_over_the_assistant_text(tmp_path, monkeypatch):
    stream = ndjson(assistant_event("partial thought"), result_event("final answer"))
    assert run(tmp_path, monkeypatch, fake_popen(stream)) == "final answer"


def test_the_last_assistant_text_is_the_fallback(tmp_path, monkeypatch):
    stream = ndjson(assistant_event("first"), assistant_event("second"), result_event(""))
    assert run(tmp_path, monkeypatch, fake_popen(stream)) == "second"


def test_on_event_gets_every_event_and_a_broken_viewer_cannot_kill_the_run(tmp_path, monkeypatch):
    events = [INIT_EVENT, assistant_event("hi"), result_event("hi")]
    seen = []

    def viewer(ev):
        seen.append(ev)
        raise RuntimeError("viewer crashed")

    monkeypatch.setattr(subprocess, "Popen", fake_popen(ndjson(*events)))
    out = claude_bridge.run_claude_streaming(
        "hi", str(tmp_path / "ws"), "read-only", None, False, 30, on_event=viewer
    )
    assert out == "hi" and seen == events


def test_a_run_writes_the_prompt_after_the_pipes_are_drained_by_threads(tmp_path, monkeypatch):
    # the order matters: a chatty child must never block on stdout while we block
    # on its stdin. The fake records when stdin was written relative to the reads.
    order = []

    class _FP(fake_popen(OK_RUN)):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            real_write = self.stdin.write
            self.stdin.write = lambda t: (order.append("stdin"), real_write(t))[1]

    monkeypatch.setattr(subprocess, "Popen", _FP)
    claude_bridge.run_claude("hi", str(tmp_path / "ws"), pin=False)
    assert order == ["stdin"]


def test_a_child_that_dies_before_reading_its_prompt_reports_why(tmp_path, monkeypatch):
    class _FP(fake_popen("", "claude: authentication failed", 1)):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)

            def broken(_):
                raise BrokenPipeError

            self.stdin.write = broken

    with pytest.raises(RuntimeError, match="authentication failed"):
        run(tmp_path, monkeypatch, _FP)


# ---- failures


def test_a_timeout_kills_the_whole_tree_and_says_how_to_raise_it(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(
        claude_bridge.proc_tree, "kill_tree", lambda p: killed.append(p) or p.kill()
    )
    monkeypatch.setattr(subprocess, "Popen", fake_popen(hang=True))
    with pytest.raises(RuntimeError, match=r"claude timed out after 35s\. Raise timeout_s"):
        claude_bridge.run_claude("hi", str(tmp_path / "ws"), timeout_s=5)
    assert len(killed) == 1


def test_an_error_result_is_reported_with_its_subtype(tmp_path, monkeypatch):
    stream = ndjson(result_event("max turns reached", is_error=True, subtype="error_max_turns"))
    with pytest.raises(RuntimeError, match=r"claude failed \(error_max_turns\): max turns reached"):
        run(tmp_path, monkeypatch, fake_popen(stream))


def test_a_nonzero_exit_without_a_result_reports_stderr(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="claude failed .*segfault"):
        run(tmp_path, monkeypatch, fake_popen("", "segfault", 139))


def test_a_nonzero_exit_without_stderr_still_says_the_exit_code(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="exit 3"):
        run(tmp_path, monkeypatch, fake_popen("", "", 3))


@pytest.mark.parametrize(
    "text", ["usage limit", "limit reached", "rate limit", "rate_limit", "quota exceeded"]
)
def test_a_usage_limit_failure_gets_the_plan_note(text):
    state = {"result": result_event(f"Claude {text} for your plan", is_error=True)}
    with pytest.raises(RuntimeError) as exc:
        claude_bridge._finish(state, "", 1, "read-only")
    assert "usage limit" in str(exc.value) and "another backend" in str(exc.value)


def test_a_usage_limit_on_stderr_alone_also_gets_the_note():
    state = {"result": result_event("", is_error=True)}
    with pytest.raises(RuntimeError, match="plan's usage limit"):
        claude_bridge._finish(state, "Error: rate_limit hit", 1, "read-only")


def test_an_ordinary_failure_gets_no_usage_note():
    state = {"result": result_event("tool crashed", is_error=True)}
    with pytest.raises(RuntimeError) as exc:
        claude_bridge._finish(state, "", 1, "read-only")
    assert "usage limit" not in str(exc.value)


def test_an_empty_answer_with_denials_names_the_tools_and_the_sandbox():
    denials = [
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        {"tool_name": "Edit", "tool_input": {}},
        {"tool_name": "Bash", "tool_input": {"command": "pwd"}},
    ]
    state = {"result": result_event("", permission_denials=denials)}
    with pytest.raises(RuntimeError) as exc:
        claude_bridge._finish(state, "", 0, "read-only")
    msg = str(exc.value)
    assert "claude produced no answer: 3 tool call(s) (Bash, Edit)" in msg
    assert "sandbox='read-only'" in msg and "no result" not in msg


def test_denials_without_tool_names_still_explain():
    state = {"result": result_event("", permission_denials=[{}, "junk"])}
    with pytest.raises(RuntimeError, match=r"2 tool call\(s\) \(\?\)"):
        claude_bridge._finish(state, "", 0, "workspace-write")


def test_an_answer_wins_over_denials():
    # Under read-only a model that TRIES to write is meant to be blocked; its answer is fine
    state = {"result": result_event("done anyway", permission_denials=[{"tool_name": "Edit"}])}
    assert claude_bridge._finish(state, "", 0, "read-only") == "done anyway"


def test_no_result_event_at_all_is_reported():
    with pytest.raises(RuntimeError, match="no result in its stream-json output"):
        claude_bridge._finish({}, "some stderr", 0, "read-only")


def test_the_error_flag_beats_a_present_answer():
    state = {"result": result_event("partial", is_error=True, subtype="error_during_execution")}
    with pytest.raises(RuntimeError, match="claude failed"):
        claude_bridge._finish(state, "", 0, "read-only")


# --------------------------------------------------------------------------
# the real pipes: a fake claude as a real process
# --------------------------------------------------------------------------

_FAKE_CLAUDE = r"""
import json, os, sys
prompt = sys.stdin.buffer.read().decode("utf-8")
seen = {k: os.environ.get(k) for k in (
    "CLAUDECODE", "CLAUDE_CODE_MESSAGING_TOKEN", "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "AGENT_INTERN_DEPTH")}
body = json.dumps({"argv": sys.argv[1:], "prompt": prompt, "env": seen, "cwd": os.getcwd()})
sid = "11111111-2222-3333-4444-555555555555"
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": body, "session_id": sid, "permission_denials": []}), flush=True)
"""


def test_a_real_child_process_gets_the_prompt_the_args_and_the_scrubbed_env(tmp_path, monkeypatch):
    # The seam the Popen stand-in cannot show: the prompt really arrives on a real
    # stdin pipe, and the env and cwd really reach a real process.
    script = tmp_path / "fakecli" / "fake_cli.py"
    script.parent.mkdir()
    script.write_text(_FAKE_CLAUDE, encoding="utf-8")
    real_popen = proc_tree.popen

    def popen_as_fake(args, **kwargs):
        return real_popen([sys.executable, str(script), *args[1:]], **kwargs)

    monkeypatch.setattr(claude_bridge.proc_tree, "popen", popen_as_fake)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "secret-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    ws = tmp_path / "work"
    answer = claude_bridge.run_claude(
        "say PONG\nplease é", str(ws), "read-only", "haiku", pin=False
    )
    got = json.loads(answer)
    assert got["prompt"] == "say PONG\nplease é"
    assert got["cwd"] == str(ws) or os.path.samefile(got["cwd"], ws)
    assert "--session-id" in got["argv"] and "haiku" in got["argv"]
    assert got["env"]["CLAUDECODE"] is None
    assert got["env"]["CLAUDE_CODE_MESSAGING_TOKEN"] is None
    assert got["env"]["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert got["env"]["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    assert got["env"]["AGENT_INTERN_DEPTH"] == "1"


# --------------------------------------------------------------------------
# version gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2.1.319 (Claude Code)", (2, 1, 319)),
        ("2.1.319", (2, 1, 319)),
        ("claude v10.20.30-beta.1", (10, 20, 30)),
        ("garbage", None),
        ("2.1", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_version(text, expected):
    assert claude_bridge.parse_version(text) == expected


def test_version_constants():
    assert claude_bridge.MIN_VERSION == (2, 1, 259)
    assert claude_bridge.VERIFIED_VERSION >= claude_bridge.MIN_VERSION


def test_a_supported_version_has_no_reason():
    assert claude_bridge.unsupported_version_reason(claude_bridge.MIN_VERSION) is None
    assert claude_bridge.unsupported_version_reason((2, 1, 319)) is None
    assert claude_bridge.unsupported_version_reason((3, 0, 0)) is None


def test_an_unreadable_version_is_refused_with_a_hint():
    reason = claude_bridge.unsupported_version_reason(None)
    assert "could not read claude's version" in reason and "CLAUDE_BIN" in reason


@pytest.mark.parametrize("old", [(2, 1, 258), (2, 0, 999), (1, 99, 99)])
def test_a_too_old_version_names_both_versions_and_the_fix(old):
    reason = claude_bridge.unsupported_version_reason(old)
    assert f"claude {'.'.join(map(str, old))} is too old" in reason
    assert "2.1.259+" in reason and "claude update" in reason


@pytest.mark.parametrize("sandbox", claude_bridge.SANDBOX_MODES)
def test_the_gate_refuses_every_sandbox_mode_before_spawning(tmp_path, monkeypatch, sandbox):
    monkeypatch.setattr(claude_bridge, "claude_version", lambda: "2.0.1 (Claude Code)")
    seen = []
    monkeypatch.setattr(subprocess, "Popen", fake_popen(OK_RUN, seen=seen))
    with pytest.raises(RuntimeError, match="too old"):
        claude_bridge.run_claude("hi", str(tmp_path / "ws"), sandbox)
    assert seen == []


def test_the_gate_refuses_an_unreadable_version(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_bridge, "claude_version", lambda: None)
    seen = []
    monkeypatch.setattr(subprocess, "Popen", fake_popen(OK_RUN, seen=seen))
    with pytest.raises(RuntimeError, match="could not read claude's version"):
        claude_bridge.run_claude("hi", str(tmp_path / "ws"))
    assert seen == []


def test_the_gate_runs_before_the_workspace_is_created(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_bridge, "claude_version", lambda: None)
    ws = tmp_path / "not-created"
    with pytest.raises(RuntimeError):
        claude_bridge.run_claude("hi", str(ws))
    assert not ws.exists()


def test_the_streaming_run_is_gated_too(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_bridge, "claude_version", lambda: None)
    seen = []
    with pytest.raises(RuntimeError):
        claude_bridge.run_claude_streaming(
            "hi", str(tmp_path / "ws"), "read-only", None, False, 5, on_event=seen.append
        )
    assert seen == []


@pytest.fixture
def fake_binary(tmp_path, monkeypatch):
    """CLAUDE_BIN at a real file of ours, so the cache key is valid and under the
    test's control, whatever is installed. `claude --version` is served by a fake."""
    exe = tmp_path / "fakecli" / "claude-fake"
    exe.parent.mkdir()
    exe.write_text("v1", encoding="utf-8")
    monkeypatch.setattr(claude_bridge, "CLAUDE_BIN", str(exe))
    monkeypatch.setattr(claude_bridge, "claude_version", _REAL_CLAUDE_VERSION)
    calls = []
    out = {"text": "2.1.319 (Claude Code)"}

    def run_captured(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=out["text"] + "\n", stderr="")

    monkeypatch.setattr(claude_bridge.proc_tree, "run_captured", run_captured)
    return exe, calls, out


def test_claude_version_runs_dash_dash_version_and_takes_the_first_line(fake_binary):
    exe, calls, out = fake_binary
    out["text"] = "2.1.319 (Claude Code)\nextra line"
    assert claude_bridge.claude_version() == "2.1.319 (Claude Code)"
    assert calls == [[str(exe), "--version"]]


def test_claude_version_is_cached_per_binary(fake_binary):
    _, calls, _ = fake_binary
    claude_bridge.claude_version()
    claude_bridge.claude_version()
    assert len(calls) == 1


def test_claude_version_rereads_when_the_binary_changes(fake_binary):
    exe, calls, out = fake_binary
    assert claude_bridge.claude_version() == "2.1.319 (Claude Code)"
    out["text"] = "2.2.0 (Claude Code)"  # claude updated itself in place
    st = exe.stat()
    os.utime(exe, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert claude_bridge.claude_version() == "2.2.0 (Claude Code)"
    assert len(calls) == 2
    exe.write_text("a longer binary", encoding="utf-8")  # a size change too
    out["text"] = "2.3.0 (Claude Code)"
    assert claude_bridge.claude_version() == "2.3.0 (Claude Code)"


def test_claude_version_is_none_when_claude_cannot_run(fake_binary, monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(claude_bridge.proc_tree, "run_captured", boom)
    assert claude_bridge.claude_version() is None
    assert claude_bridge._VERSION_CACHE is None  # a failure is not cached


def test_claude_version_never_caches_an_unstatable_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_bridge, "CLAUDE_BIN", str(tmp_path / "missing" / "claude-fake"))
    monkeypatch.setattr(claude_bridge, "claude_version", _REAL_CLAUDE_VERSION)
    calls = []

    def run_captured(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="2.1.319\n", stderr="")

    monkeypatch.setattr(claude_bridge.proc_tree, "run_captured", run_captured)
    assert claude_bridge._binary_key() is None
    claude_bridge.claude_version()
    claude_bridge.claude_version()
    assert len(calls) == 2 and claude_bridge._VERSION_CACHE is None


def test_the_version_probe_gets_the_scrubbed_env(fake_binary, monkeypatch):
    exe, calls, out = fake_binary
    seen = {}

    def run_captured(args, **kwargs):
        seen.update(hide_env(kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="2.1.319\n", stderr="")

    monkeypatch.setattr(claude_bridge.proc_tree, "run_captured", run_captured)
    monkeypatch.setenv("CLAUDECODE", "1")
    claude_bridge.claude_version()
    assert "CLAUDECODE" not in seen["env"]
    assert seen["stdin"] is subprocess.DEVNULL


# --------------------------------------------------------------------------
# auth status and the status rows
# --------------------------------------------------------------------------

LOGGED_IN = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "subscriptionType": "team",
    "loginExpiresAt": "2026-12-06T09:30:00.000Z",
    "email": "ann.smith@example.com",
    "orgId": "11111111-2222-3333-4444-555555555555",
    "orgName": "Example Org",
}


def _auth(monkeypatch, stdout="", boom=None, returncode=0):
    seen = []

    def run_captured(args, **kwargs):
        seen.append(args)
        if boom is not None:
            raise boom
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(claude_bridge.proc_tree, "run_captured", run_captured)
    return seen


def test_auth_status_logged_in_shows_how_not_who(monkeypatch):
    seen = _auth(monkeypatch, json.dumps(LOGGED_IN))
    ok, detail = claude_bridge.auth_status()
    assert ok is True
    assert detail == "claude.ai, plan: team, login expires 2026-12-06"
    assert seen[0][1:] == ["auth", "status"]


@pytest.mark.parametrize("secret", ["ann.smith", "example.com", "Example Org", "11111111-2222"])
def test_auth_status_never_shows_the_account(monkeypatch, secret):
    _auth(monkeypatch, json.dumps(LOGGED_IN))
    assert secret not in claude_bridge.auth_status()[1]


def test_auth_status_minimal_login(monkeypatch):
    _auth(monkeypatch, json.dumps({"loggedIn": True}))
    assert claude_bridge.auth_status() == (True, "logged in")


def test_auth_status_not_logged_in(monkeypatch):
    _auth(monkeypatch, json.dumps({"loggedIn": False}))
    ok, detail = claude_bridge.auth_status()
    assert ok is False and "claude auth login" in detail and "ANTHROPIC_API_KEY" in detail


def test_an_api_key_counts_as_signed_in_when_not_logged_in(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _auth(monkeypatch, json.dumps({"loggedIn": False}))
    assert claude_bridge.auth_status() == (True, "ANTHROPIC_API_KEY is set (no saved login)")


@pytest.mark.parametrize("stdout", ["", "not json", "[1, 2]", "null", "3"])
def test_auth_status_unparseable_output_is_not_ok(monkeypatch, stdout):
    _auth(monkeypatch, stdout)
    ok, detail = claude_bridge.auth_status()
    assert ok is False and "claude auth login" in detail


@pytest.mark.parametrize("stdout", ["", "not json"])
def test_an_api_key_covers_unparseable_output_too(monkeypatch, stdout):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _auth(monkeypatch, stdout)
    assert claude_bridge.auth_status() == (True, "ANTHROPIC_API_KEY is set")


def test_auth_status_when_claude_cannot_run(monkeypatch):
    _auth(monkeypatch, boom=OSError("nope"))
    assert claude_bridge.auth_status() == (False, "could not run `claude auth status`")


def test_an_api_key_is_never_shown(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-supersecret")
    _auth(monkeypatch, "")
    assert "supersecret" not in claude_bridge.auth_status()[1]


def _rows(monkeypatch, version="2.1.319 (Claude Code)", auth=(True, "claude.ai"), projects=None):
    monkeypatch.setattr(claude_bridge, "claude_version", lambda: version)
    monkeypatch.setattr(claude_bridge, "auth_status", lambda: auth)
    # The "projects dir" row fails where Claude Code has never run (a CI runner); a
    # directory that always exists keeps these tests about what they say they test.
    monkeypatch.setattr(claude_bridge, "PROJECTS_DIR", REPO if projects is None else projects)
    return claude_bridge.status_rows()


def test_status_rows_labels_and_order(monkeypatch):
    rows = _rows(monkeypatch)
    assert [r[0] for r in rows] == ["claude CLI", "claude auth", "projects dir", "pinned sessions"]
    assert rows[0] == ("claude CLI", True, "2.1.319 (Claude Code)")
    assert all(len(r) == 3 and isinstance(r[1], bool) for r in rows)


def test_status_row_missing_cli_is_not_ok_and_names_the_variable(monkeypatch):
    label, ok, detail = _rows(monkeypatch, version=None)[0]
    assert (label, ok) == ("claude CLI", False)
    assert "CLAUDE_BIN" in detail


def test_status_row_old_cli_is_not_ok_with_the_reason(monkeypatch):
    _, ok, detail = _rows(monkeypatch, version="2.0.0 (Claude Code)")[0]
    assert ok is False and detail.startswith("2.0.0 (Claude Code) — ") and "too old" in detail


def test_status_rows_pinned_sessions_count(monkeypatch):
    claude_bridge._pin("a", "1")
    claude_bridge._pin("b", "2")
    assert "2 workspace(s)" in _rows(monkeypatch)[3][2]


def test_status_rows_projects_dir_follows_the_directory(monkeypatch, tmp_path):
    assert _rows(monkeypatch, projects=tmp_path)[2] == ("projects dir", True, str(tmp_path))
    assert _rows(monkeypatch, projects=tmp_path / "missing")[2][1] is False


def test_the_projects_dir_follows_claude_config_dir():
    assert claude_bridge.PROJECTS_DIR == claude_bridge.CLAUDE_HOME / "projects"


def test_read_history_is_empty():
    assert claude_bridge.read_history("ws", True) == []


# --------------------------------------------------------------------------
# claude_tools: registration, watch lines, the tools themselves
# --------------------------------------------------------------------------


def _tool_names():
    lister = getattr(server.mcp, "list_tools", None)
    if lister is not None:
        return {t.name for t in asyncio.run(lister())}
    return set(asyncio.run(server.mcp.get_tools()))


def test_the_three_tools_are_exported_and_registered_modules():
    assert claude_tools in server._TOOL_MODULES
    assert [n for n in claude_tools.__all__ if not n.startswith("_")] == [
        "claude_ask",
        "claude_continue",
        "claude_status",
    ]
    names = [m.__name__ for m in server._TOOL_MODULES]
    assert names.index("cursor_tools") < names.index("claude_tools") < names.index("grok_tools")


def test_server_reaches_the_tool_helpers_by_name():
    assert server._claude_event_to_watch_lines is claude_tools._claude_event_to_watch_lines
    assert server.claude_ask is claude_tools.claude_ask


def test_claude_status_references_only_status_rows_no_quota(monkeypatch):
    monkeypatch.setattr(claude_bridge, "status_rows", lambda: [("claude CLI", True, "2.1.319")])
    monkeypatch.setattr(server, "_bridge_status_rows", lambda: [("bridge version", True, "v1")])
    out = claude_tools.claude_status()
    assert out.splitlines()[0] == "claude bridge status"
    assert "[ok] 2.1.319" in out and out.endswith("Overall: OK")


def test_claude_status_reports_problems(monkeypatch):
    monkeypatch.setattr(claude_bridge, "status_rows", lambda: [("claude CLI", False, "missing")])
    monkeypatch.setattr(server, "_bridge_status_rows", lambda: [])
    out = claude_tools.claude_status()
    assert "[!!] missing" in out and out.endswith("Overall: PROBLEMS FOUND")


def _call_tool(fn, *args, **kwargs):
    return asyncio.run(fn(*args, **kwargs))


def test_claude_ask_runs_a_fresh_session(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        claude_bridge, "run_claude", lambda *a, **k: calls.append(a) or "the answer"
    )
    out = _call_tool(
        claude_tools.claude_ask, "q", str(tmp_path), "workspace-write", "haiku", timeout_s=42
    )
    assert out == "the answer"
    assert calls == [("q", os.path.abspath(str(tmp_path)), "workspace-write", "haiku", False, 42)]


def test_claude_ask_defaults_to_read_only_and_the_server_cwd(monkeypatch):
    calls = []
    monkeypatch.setattr(claude_bridge, "run_claude", lambda *a, **k: calls.append(a) or "x")
    _call_tool(claude_tools.claude_ask, "q")
    assert calls[0][1] == os.getcwd() and calls[0][2] == "read-only" and calls[0][3] is None


def test_claude_ask_rejects_a_bad_sandbox_before_running(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_bridge, "run_claude", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(ValueError, match="invalid sandbox"):
        _call_tool(claude_tools.claude_ask, "q", str(tmp_path), "yolo")


def test_claude_continue_resumes_with_the_continue_flag(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(claude_bridge, "run_claude", lambda *a, **k: calls.append(a) or "more")
    out = _call_tool(claude_tools.claude_continue, "next", str(tmp_path), "workspace-write", 30)
    assert out == "more"
    assert calls == [("next", os.path.abspath(str(tmp_path)), "workspace-write", None, True, 30)]


def test_claude_continue_rejects_a_bad_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_bridge, "run_claude", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(ValueError):
        _call_tool(claude_tools.claude_continue, "q", str(tmp_path), "yolo")


def test_the_tool_descriptions_state_the_sandbox_facts():
    ask = " ".join(claude_tools.claude_ask.__doc__.split())  # the docstring is line-wrapped
    assert "NO shell" in ask or "runs NO shell" in ask
    assert "not an OS sandbox" in ask
    assert "other than Claude Code" in ask
    assert "read-only" in claude_tools.claude_continue.__doc__


def test_claude_event_to_watch_lines_text_is_narration():
    assert claude_tools._claude_event_to_watch_lines(assistant_event("Checking.\nMore")) == [
        ("narration", "Checking.")
    ]


def test_claude_event_to_watch_lines_text_is_cut_at_200_characters():
    ((kind, text),) = claude_tools._claude_event_to_watch_lines(assistant_event("x" * 500))
    assert kind == "narration" and len(text) == 200


def test_claude_event_to_watch_lines_tool_use_shows_the_command_or_path():
    ev = assistant_event(tool=("Bash", {"command": "git status\nsecond line"}))
    assert claude_tools._claude_event_to_watch_lines(ev) == [("command", "Bash: git status")]
    ev = assistant_event(tool=("Read", {"file_path": "src/app.py"}))
    assert claude_tools._claude_event_to_watch_lines(ev) == [("command", "Read: src/app.py")]


@pytest.mark.parametrize("field", ["command", "file_path", "path", "pattern", "query", "url"])
def test_claude_tool_line_uses_each_known_field(field):
    block = {"name": "Tool", "input": {field: "VALUE"}}
    assert claude_tools._claude_tool_line(block) == "Tool: VALUE"


def test_claude_tool_line_prefers_the_command_and_falls_back_to_the_name():
    block = {"name": "Tool", "input": {"url": "u", "command": "c"}}
    assert claude_tools._claude_tool_line(block) == "Tool: c"
    assert claude_tools._claude_tool_line({"name": "Tool", "input": {"other": 1}}) == "Tool"
    assert claude_tools._claude_tool_line({"name": "Tool", "input": "junk"}) == "Tool"
    assert claude_tools._claude_tool_line({"name": "Tool"}) == "Tool"
    assert claude_tools._claude_tool_line({"name": "", "input": {"command": "ls"}}) == "ls"


def test_claude_event_to_watch_lines_mixed_content_keeps_order():
    ev = assistant_event("Looking.", tool=("Grep", {"pattern": "TODO"}))
    assert claude_tools._claude_event_to_watch_lines(ev) == [
        ("narration", "Looking."),
        ("command", "Grep: TODO"),
    ]


def test_claude_event_to_watch_lines_result():
    assert claude_tools._claude_event_to_watch_lines(result_event()) == [("result", "done")]
    assert claude_tools._claude_event_to_watch_lines(result_event(is_error=True)) == [
        ("result", "error")
    ]


@pytest.mark.parametrize(
    "ev",
    [
        INIT_EVENT,
        {"type": "user", "message": {}},
        {"type": "assistant"},
        {"type": "assistant", "message": {"content": ["junk", 3, {"type": "thinking"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "  "}]}},
        {},
    ],
)
def test_claude_event_to_watch_lines_ignores_noise(ev):
    assert claude_tools._claude_event_to_watch_lines(ev) == []


@pytest.fixture
def watch_calls(monkeypatch):
    """Record the watch window's life: begin, appended lines, finish."""
    calls = {"begin": [], "append": [], "finish": []}
    monkeypatch.setattr(
        server, "_watch_begin", lambda *a, **k: calls["begin"].append((a, k)) or "rid"
    )
    monkeypatch.setattr(server, "_ensure_watch_server", lambda: 12345)
    monkeypatch.setattr(server, "_open_watch_window", lambda *a, **k: None)
    monkeypatch.setattr(server, "_watch_url", lambda port, rid: f"http://x/{port}/{rid}")
    monkeypatch.setattr(server, "_watch_append", lambda rid, lines: calls["append"].extend(lines))
    monkeypatch.setattr(
        server,
        "_watch_finish",
        lambda rid, status, text, elapsed: calls["finish"].append((status, text)),
    )
    return calls


def test_a_watched_run_streams_steps_and_returns_the_same_answer(
    tmp_path, monkeypatch, watch_calls
):
    stream = ndjson(
        INIT_EVENT,
        assistant_event("Looking.", tool=("Bash", {"command": "ls"})),
        result_event("final"),
    )
    monkeypatch.setattr(subprocess, "Popen", fake_popen(stream))
    out = claude_tools._run_claude_watched(
        "do it\nsecond line", str(tmp_path / "ws"), "read-only", None, False, 30
    )
    assert out == "final"
    assert [(x["kind"], x["text"]) for x in watch_calls["append"]] == [
        ("narration", "Looking."),
        ("command", "Bash: ls"),
        ("result", "done"),
    ]
    assert watch_calls["finish"] == [("done", "final")]
    ((_, kwargs),) = watch_calls["begin"]
    assert kwargs["backend"] == "claude" and kwargs["prompt"] == "do it\nsecond line"


def test_a_watched_run_shows_a_failure_in_the_window_then_raises(
    tmp_path, monkeypatch, watch_calls
):
    stream = ndjson(result_event("boom", is_error=True, subtype="error_during_execution"))
    monkeypatch.setattr(subprocess, "Popen", fake_popen(stream, returncode=1))
    with pytest.raises(RuntimeError, match="claude failed"):
        claude_tools._run_claude_watched("x", str(tmp_path / "ws"), "read-only", None, False, 30)
    status, text = watch_calls["finish"][0]
    assert status == "error" and "claude failed" in text


def test_a_watched_run_survives_a_viewer_that_cannot_start(tmp_path, monkeypatch, watch_calls):
    def no_viewer():
        raise OSError("no browser")

    monkeypatch.setattr(server, "_ensure_watch_server", no_viewer)
    monkeypatch.setattr(subprocess, "Popen", fake_popen(OK_RUN))
    assert (
        claude_tools._run_claude_watched("x", str(tmp_path / "ws"), "read-only", None, False, 30)
        == "PONG"
    )


def test_claude_ask_with_watch_uses_the_watched_runner(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        claude_tools, "_run_claude_watched", lambda *a: calls.append(a) or "watched answer"
    )
    out = _call_tool(claude_tools.claude_ask, "q", str(tmp_path), watch=True, timeout_s=9)
    assert out == "watched answer"
    assert calls == [("q", os.path.abspath(str(tmp_path)), "read-only", None, False, 9)]
    calls.clear()
    _call_tool(claude_tools.claude_continue, "q", str(tmp_path), watch=True)
    assert calls[0][4] is True  # continue_conv


# --------------------------------------------------------------------------
# server: the backend list, the guide, and being hidden inside Claude Code
# --------------------------------------------------------------------------


def test_claude_is_a_backend_between_cursor_and_grok():
    b = server.BACKENDS
    assert b == (
        "antigravity",
        "codex",
        "copilot",
        "cursor",
        "claude",
        "grok",
        "opencode",
        "kimi",
        "muse",
    )
    assert "claude" in server._SWARM_BACKENDS  # a swarm worker, and not experimental


def test_claude_has_a_title_and_a_guide():
    assert server._BACKEND_TITLES["claude"] == "Claude Code"
    assert server._BACKEND_GUIDES["claude"].startswith("- claude_* (Claude Code) —")


def test_the_instructions_mention_claude_only_when_it_is_on():
    on = server._server_instructions(("codex", "claude"))
    off = server._server_instructions(("codex",))
    assert "claude_*" in on and "Claude Code" in on
    assert "claude_*" not in off and "Claude Code" not in off


@pytest.mark.parametrize(
    "env,raw,enabled,expected",
    [
        # Claude Code is the host, claude was not named: left out, and flagged
        ({"CLAUDECODE": "1"}, None, server.BACKENDS, False),
        ({"CLAUDECODE": "1"}, "", server.BACKENDS, False),
        ({"CLAUDECODE": "1"}, "codex, copilot, claude-ish", ("codex", "copilot", "claude"), False),
    ],
)
def test_claude_is_left_out_when_the_host_is_claude_code(env, raw, enabled, expected):
    got, hidden = server._claude_hidden_in_claude_code(enabled, raw, env)
    assert hidden is True
    assert "claude" not in got
    assert got == tuple(b for b in enabled if b != "claude")


@pytest.mark.parametrize(
    "raw", ["claude", "Claude", "codex,claude", "codex claude", " CLAUDE , codex "]
)
def test_naming_claude_explicitly_wins(raw):
    enabled = server._parse_backends(raw)[0]
    got, hidden = server._claude_hidden_in_claude_code(enabled, raw, {"CLAUDECODE": "1"})
    assert got == enabled and "claude" in got and hidden is False


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes", "2"])
def test_only_claudecode_equal_to_one_hides_claude(value):
    env = {} if value is None else {"CLAUDECODE": value}
    assert server._claude_hidden_in_claude_code(server.BACKENDS, None, env) == (
        server.BACKENDS,
        False,
    )


def test_nothing_is_hidden_when_claude_is_not_enabled_anyway():
    enabled = ("codex", "copilot")
    assert server._claude_hidden_in_claude_code(enabled, "codex,copilot", {"CLAUDECODE": "1"}) == (
        enabled,
        False,
    )


def test_claude_stays_when_hiding_it_would_leave_no_backend():
    # a server with no backend has nothing to offer: unchanged, and not flagged
    assert server._claude_hidden_in_claude_code(("claude",), None, {"CLAUDECODE": "1"}) == (
        ("claude",),
        False,
    )


def test_the_hide_rule_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    got, hidden = server._claude_hidden_in_claude_code(server.BACKENDS, None)
    assert hidden is True and "claude" not in got
    monkeypatch.delenv("CLAUDECODE")
    assert server._claude_hidden_in_claude_code(server.BACKENDS, None) == (server.BACKENDS, False)


def test_the_hidden_note_says_how_to_turn_claude_on():
    note = server.CLAUDE_HIDDEN_NOTE
    assert "claude is off because the host is Claude Code (CLAUDECODE=1)" in note
    assert "name claude in AGENT_INTERN_BACKENDS" in note


def test_this_test_run_does_not_inherit_claude_code_from_the_developer():
    # conftest pops CLAUDECODE before server is imported: the same tools register
    # inside and outside a Claude Code session.
    assert "claude" in server.ENABLED_BACKENDS and server.CLAUDE_HIDDEN is False
    assert os.environ.get("CLAUDECODE") is None


def test_backends_status_mentions_the_env_var_only_when_it_is_set(monkeypatch):
    monkeypatch.setattr(server, "ENABLED_BACKENDS", ("codex", "claude"))
    monkeypatch.delenv("AGENT_INTERN_BACKENDS", raising=False)
    _, _, detail = server._backends_status()
    assert "(AGENT_INTERN_BACKENDS)" not in detail
    monkeypatch.setenv("AGENT_INTERN_BACKENDS", "   ")
    assert "(AGENT_INTERN_BACKENDS)" not in server._backends_status()[2]
    monkeypatch.setenv("AGENT_INTERN_BACKENDS", "codex,claude")
    assert "(AGENT_INTERN_BACKENDS)" in server._backends_status()[2]


def test_backends_status_explains_a_hidden_claude(monkeypatch):
    monkeypatch.setattr(
        server, "ENABLED_BACKENDS", tuple(b for b in server.BACKENDS if b != "claude")
    )
    monkeypatch.setattr(server, "CLAUDE_HIDDEN", True)
    monkeypatch.delenv("AGENT_INTERN_BACKENDS", raising=False)
    label, ok, detail = server._backends_status()
    assert label == "backends" and ok is True
    assert "off: claude" in detail and detail.endswith(server.CLAUDE_HIDDEN_NOTE)
    assert "claude" not in detail.split("on: ")[1].split("; off")[0]


def test_backends_status_has_no_note_when_claude_is_not_hidden(monkeypatch):
    monkeypatch.setattr(server, "ENABLED_BACKENDS", ("codex",))
    monkeypatch.setattr(server, "CLAUDE_HIDDEN", False)
    assert server.CLAUDE_HIDDEN_NOTE not in server._backends_status()[2]


def test_bridge_rows_show_the_backends_row_when_claude_is_hidden(monkeypatch):
    monkeypatch.setattr(server, "_bridge_version_status", lambda: ("bridge version", True, "v"))
    monkeypatch.setattr(
        server, "ENABLED_BACKENDS", tuple(b for b in server.BACKENDS if b != "claude")
    )
    monkeypatch.setattr(server, "CLAUDE_HIDDEN", True)
    assert [r[0] for r in server._bridge_status_rows()] == ["bridge version", "backends"]


@pytest.mark.parametrize("depth,shown", [(0, False), (1, False), (2, True), (3, True)])
def test_bridge_rows_add_a_nesting_row_at_the_limit(monkeypatch, depth, shown):
    monkeypatch.setattr(server, "_bridge_version_status", lambda: ("bridge version", True, "v"))
    monkeypatch.setenv(proc_tree.DEPTH_ENV, str(depth))
    rows = server._bridge_status_rows()
    nesting = [r for r in rows if r[0] == "nesting"]
    assert bool(nesting) is shown
    if shown:
        assert nesting[0][1] is False
        detail = nesting[0][2]
        assert detail.startswith("delegation from here is refused")
        assert f"{depth} agents deep (AGENT_INTERN_DEPTH={depth}, limit 2)" in detail
        assert detail.endswith("the checks below still run")


@functools.lru_cache(maxsize=None)
def _tools_in_fresh_interpreter(env_items: tuple):
    """(sorted tool names, instructions) of a server imported in a fresh interpreter
    with `env_items` set. Cached: importing the server takes seconds, so each
    combination runs once."""
    script = (
        "import asyncio, json, server\n"
        "tools = sorted(t.name for t in asyncio.run(server.mcp.list_tools()))\n"
        "print(json.dumps([tools, server.mcp.instructions]))\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "AGENT_INTERN_BACKENDS")}
    env.update(dict(env_items))
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    tools, instructions = json.loads(proc.stdout.strip().splitlines()[-1])
    return tools, instructions


_CLAUDE_TOOLS = {"claude_ask", "claude_continue", "claude_status"}


def test_in_a_fresh_server_claude_tools_follow_the_host():
    # End to end, in fresh interpreters: the rule is applied at import.
    outside, instr_out = _tools_in_fresh_interpreter(())
    assert _CLAUDE_TOOLS <= set(outside) and "claude_*" in instr_out
    inside, instr_in = _tools_in_fresh_interpreter((("CLAUDECODE", "1"),))
    assert not _CLAUDE_TOOLS & set(inside) and "claude_*" not in instr_in
    # the rest of the tools are untouched
    assert set(inside) == set(outside) - _CLAUDE_TOOLS


def test_in_a_fresh_server_naming_claude_wins_inside_claude_code():
    env = (("CLAUDECODE", "1"), ("AGENT_INTERN_BACKENDS", "codex,claude"))
    tools, instructions = _tools_in_fresh_interpreter(env)
    assert _CLAUDE_TOOLS <= set(tools) and "codex_ask" in tools
    assert "claude_*" in instructions
    assert "grok_ask" not in tools  # the list still filters everything else


# --------------------------------------------------------------------------
# swarm
# --------------------------------------------------------------------------


@pytest.mark.parametrize("alias", ["claude", "claude-code", "anthropic", "CLAUDE", " Claude-Code "])
def test_swarm_normalizes_the_claude_aliases(alias, tmp_path):
    (task,) = swarm._normalize_tasks([{"backend": alias, "prompt": "p"}])
    assert task["backend"] == "claude"
    assert task["sandbox"] == "read-only" and task["model"] is None


def test_swarm_claude_task_keeps_its_sandbox_and_model():
    (task,) = swarm._normalize_tasks(
        [{"backend": "claude", "prompt": "p", "sandbox": "workspace-write", "model": "haiku"}]
    )
    assert task["sandbox"] == "workspace-write" and task["model"] == "haiku"


def test_swarm_rejects_a_bad_claude_sandbox_up_front():
    with pytest.raises(ValueError, match="invalid sandbox"):
        swarm._normalize_tasks([{"backend": "claude", "prompt": "p", "sandbox": "yolo"}])


def test_swarm_names_claude_in_its_backend_list():
    assert "claude" in swarm._BACKEND_NAMES
    with pytest.raises(ValueError) as exc:
        swarm._normalize_tasks([{"backend": "nope", "prompt": "p"}])
    assert "claude" in str(exc.value)


def test_a_claude_swarm_worker_runs_unpinned(tmp_path, monkeypatch):
    seen = {}

    def fake(prompt, workspace, sandbox, model, cont, timeout_s, pin=True):
        seen.update(pin=pin, cont=cont, sandbox=sandbox, model=model)
        return "worker answer"

    monkeypatch.setattr(claude_bridge, "run_claude", fake)
    res = swarm._run_claude_worker(3, "p", str(tmp_path), "read-only", "haiku", 30)
    assert res.ok and res.answer == "worker answer" and res.backend == "claude" and res.index == 3
    assert seen == {"pin": False, "cont": False, "sandbox": "read-only", "model": "haiku"}


def test_a_claude_swarm_worker_fails_in_place(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("claude failed (error): nope")

    monkeypatch.setattr(claude_bridge, "run_claude", boom)
    res = swarm._run_claude_worker(0, "p", str(tmp_path), "read-only", None, 30)
    assert res.ok is False and "claude failed" in res.error and res.backend == "claude"


def test_a_watched_claude_swarm_worker_streams_and_finishes(tmp_path, monkeypatch):
    swarm_watch = pytest.importorskip("swarm_watch")
    appended, finished = [], []
    monkeypatch.setattr(swarm_watch, "worker_update", lambda i, **kw: None)
    monkeypatch.setattr(swarm_watch, "worker_append", lambda i, lines: appended.extend(lines))
    monkeypatch.setattr(
        swarm_watch, "worker_finish", lambda i, s, a, e, **kw: finished.append((s, a))
    )
    stream = ndjson(INIT_EVENT, assistant_event("Thinking."), result_event("answer"))
    monkeypatch.setattr(subprocess, "Popen", fake_popen(stream))
    res = swarm._run_claude_worker_watched(0, "p", str(tmp_path / "ws"), "read-only", None, 30)
    assert res.ok and res.answer == "answer" and res.backend == "claude"
    assert [x["text"] for x in appended if x["kind"] == "narration"] == ["Thinking."]
    assert finished == [("done", "answer")]
    assert claude_bridge.get_pinned(str(tmp_path / "ws")) is None  # a worker never pins


def test_a_watched_claude_swarm_worker_reports_a_failure(tmp_path, monkeypatch):
    swarm_watch = pytest.importorskip("swarm_watch")
    finished = []
    monkeypatch.setattr(swarm_watch, "worker_update", lambda i, **kw: None)
    monkeypatch.setattr(swarm_watch, "worker_append", lambda i, lines: None)
    monkeypatch.setattr(
        swarm_watch, "worker_finish", lambda i, s, a, e, **kw: finished.append((s, a))
    )
    monkeypatch.setattr(subprocess, "Popen", fake_popen("", "segfault", 139))
    res = swarm._run_claude_worker_watched(0, "p", str(tmp_path / "ws"), "read-only", None, 30)
    assert res.ok is False and finished[0][0] == "error"


def test_swarm_dispatches_a_claude_task_to_the_claude_worker(tmp_path, monkeypatch):
    called = []

    def worker(i, prompt, workspace, sandbox, model, timeout_s):
        called.append((prompt, sandbox, model))
        return swarm.WorkerResult(i, True, answer="ok", workspace=workspace, backend="claude")

    monkeypatch.setattr(swarm, "_run_claude_worker", worker)
    out = swarm.swarm_agents(
        [{"backend": "claude", "prompt": "hello", "workspace": str(tmp_path), "model": "haiku"}],
        timeout_s=30,
    )
    assert called == [("hello", "read-only", "haiku")]
    assert out[0].ok and out[0].answer == "ok"


def test_swarm_says_claude_is_off_when_the_host_is_claude_code(tmp_path, monkeypatch):
    monkeypatch.setattr(
        server, "ENABLED_BACKENDS", tuple(b for b in server.BACKENDS if b != "claude")
    )
    monkeypatch.setattr(server, "CLAUDE_HIDDEN", True)
    monkeypatch.setattr(swarm, "_run_claude_worker", lambda *a: pytest.fail("must not run"))
    out = swarm.swarm_agents(
        [{"backend": "claude", "prompt": "p", "workspace": str(tmp_path)}], timeout_s=30
    )
    assert out[0].ok is False
    assert out[0].error == f"backend 'claude' is off: {server.CLAUDE_HIDDEN_NOTE}"


def test_swarm_keeps_the_generic_off_message_when_claude_was_left_out_by_the_list(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(server, "ENABLED_BACKENDS", ("codex",))
    monkeypatch.setattr(server, "CLAUDE_HIDDEN", False)
    out = swarm.swarm_agents(
        [{"backend": "claude", "prompt": "p", "workspace": str(tmp_path)}], timeout_s=30
    )
    assert "turned off by AGENT_INTERN_BACKENDS" in out[0].error and "on: codex" in out[0].error


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def test_doctor_knows_claude():
    cli_label, auth_label, install, login = doctor._CHECKS["claude"]
    assert (cli_label, auth_label) == ("claude CLI", "claude auth")
    assert "https://claude.com/claude-code" in install and "CLAUDE_BIN" in install
    assert "claude auth login" in login and "ANTHROPIC_API_KEY" in login


def test_doctor_checks_every_backend_the_server_has():
    assert set(doctor._CHECKS) == set(server.BACKENDS)


def test_doctor_labels_match_what_the_claude_bridge_writes():
    cli_label, auth_label, *_ = doctor._CHECKS["claude"]
    src = (REPO / "claude_bridge.py").read_text(encoding="utf-8")
    assert f'"{cli_label}"' in src and f'"{auth_label}"' in src


def test_doctor_judges_claude_from_its_real_status_rows(monkeypatch):
    rows = _rows(monkeypatch)
    assert doctor.assess("claude", rows)[0] == doctor.READY
    rows = _rows(monkeypatch, auth=(False, "not logged in"))
    state, summary, nxt, blocked = doctor.assess("claude", rows)
    assert state == doctor.UNUSABLE and blocked == "claude auth" and "claude auth login" in nxt
    rows = _rows(monkeypatch, version=None)
    state, _, nxt, blocked = doctor.assess("claude", rows)
    assert state == doctor.UNUSABLE and blocked == "claude CLI" and "install Claude Code" in nxt


def test_doctor_dispatches_claude_to_the_bridge_rows(monkeypatch):
    monkeypatch.setattr(claude_bridge, "status_rows", lambda: [("claude CLI", True, "2.1.319")])
    assert doctor._backend_rows("claude") == [("claude CLI", True, "2.1.319")]


# --------------------------------------------------------------------------
# the plugin skills and the watch window know about claude
# --------------------------------------------------------------------------


def test_the_doctor_skill_preapproves_the_claude_status_tool():
    text = (REPO / "plugin" / "skills" / "doctor" / "SKILL.md").read_text(encoding="utf-8")
    assert "claude_status" in text


def test_every_claude_tool_a_plugin_skill_names_is_registered():
    import re

    named = set()
    for md in (REPO / "plugin" / "skills").glob("*/SKILL.md"):
        named |= set(
            re.findall(r"\bclaude_(?:ask|continue|status)\b", md.read_text(encoding="utf-8"))
        )
    assert named <= _CLAUDE_TOOLS


def test_the_watch_window_maps_the_claude_backend():
    import watch_ui

    assert 'claude:"claude"' in watch_ui.BASE_JS
    assert 'claude:"Claude"' in watch_ui.BASE_JS


def test_the_fake_cli_never_lives_in_a_directory_named_claude(tmp_path):
    # conftest's tripwire reads a launcher's path components: a directory literally
    # named "claude" would make an ordinary fake look like the real thing.
    assert "claude" not in {p.lower() for p in Path(os.fsdecode(tmp_path)).parts}
