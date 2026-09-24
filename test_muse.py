"""Tests for muse_bridge.py — the Muse Code (Meta) backend.

Two layers:

1. Offline unit tests (run everywhere, including CI): the argv we build, the
   JSONL shapes we parse, session pinning and its restart fallback, binary
   resolution behind the Windows launcher, the watch mapper and swarm wiring.
   The event fixtures are copied from real `muse exec --json` output (Muse Code
   1.3.0, echo provider), not invented.
2. Live integration tests against the REAL binary using muse's offline `echo`
   provider — the whole pipeline, no account, no quota. They skip when muse isn't
   installed, so CI (which has no muse) runs layer 1 only.

    pytest test_muse.py
"""

import json
import os
import shutil
import tempfile

import pytest

import muse_bridge
import proc_tree
import swarm

SID = "364168ce-7588-40b8-b295-9b9ecaff5c42"


def _ev(payload, sid=SID, kind="session"):
    return json.dumps(
        {"schema_version": 1, "stream": {"kind": kind, "id": sid}, "payload": payload}
    )


# Observed live: a completed echo run ends with exactly this terminal record.
TERMINAL_OK = {
    "kind": "run_terminal",
    "command_id": "d5139393-c353-49e6-a4a2-d65a0bcb41ca",
    "run_stream": {"kind": "run", "id": "d5139393-c353-49e6-a4a2-d65a0bcb41ca"},
    "terminal": "completed",
    "text": "echo: say hi",
    "reason": None,
}
STDOUT_OK = "\n".join(
    [
        _ev({"kind": "command_accepted", "command_kind": "turn.submit"}),
        _ev({"kind": "run_started", "prompt": "say hi"}),
        _ev({"kind": "run_output_delta", "text": "echo: say hi"}),
        _ev(TERMINAL_OK),
    ]
)
# Observed live: the real provider, logged out — exit 1, NO events, one stderr line.
STDERR_LOGGED_OUT = (
    "missing meta credentials: run `muse login` or set META_API_KEY, or save credentials "
    "at C:\\Users\\USER\\.config\\muse\\auth.json\n"
)


class _P:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(muse_bridge, "_PINNED", {})
    monkeypatch.setattr(muse_bridge, "MUSE_PROVIDER", None)
    monkeypatch.setattr(muse_bridge, "MUSE_BIN", "muse")


# -------------------------------------------------------------------- argv
@pytest.mark.parametrize(
    "mode,flags",
    [
        (
            "read-only",
            ["--disable-approval", "--disable-write", "--disable-shell", "--disable-web-tools"],
        ),
        ("workspace-write", ["--disable-approval"]),
        ("danger-full-access", ["--yolo"]),
    ],
)
def test_sandbox_flags(mode, flags):
    assert muse_bridge._sandbox_flags(mode) == flags


def test_read_only_leaves_nothing_that_could_write_or_run():
    # The whole point: no OS sandbox is needed because nothing can act.
    flags = muse_bridge._sandbox_flags("read-only")
    assert {"--disable-write", "--disable-shell"} <= set(flags)
    assert "--yolo" not in flags and "--disable-sandbox" not in flags


def test_validate_sandbox_rejects_unknown():
    with pytest.raises(ValueError, match="invalid sandbox"):
        muse_bridge.validate_sandbox("read-write")


def test_build_args_never_carries_the_prompt():
    args = muse_bridge.build_args("/tmp/p.txt", "/work", "read-only", "muse-spark-1.3", SID)
    assert args[:3] == ["muse", "exec", "--json"]
    assert args[args.index("--prompt-file") + 1] == "/tmp/p.txt"
    assert args[args.index("--workspace") + 1] == "/work"
    assert args[args.index("--session-id") + 1] == SID
    assert args[args.index("--model") + 1] == "muse-spark-1.3"
    for must in ("--user-input-auto-resolve", "--no-foreign-personal-context", "--trust-workspace"):
        assert must in args
    assert args[args.index("--worktree") + 1] == "off"


def test_build_args_omits_optional_parts():
    args = muse_bridge.build_args("p.txt", "/w", "danger-full-access", None, None)
    assert "--session-id" not in args and "--model" not in args and "--provider" not in args


def test_build_args_echo_provider_drops_model(monkeypatch):
    # Verified live: `--model requires --provider meta` (exit 2) with echo.
    monkeypatch.setattr(muse_bridge, "MUSE_PROVIDER", "echo")
    args = muse_bridge.build_args("p.txt", "/w", "read-only", "muse-spark-1.3", None)
    assert args[args.index("--provider") + 1] == "echo"
    assert "--model" not in args


def test_validate_model_is_lenient():
    assert muse_bridge.validate_model(None) is None
    assert muse_bridge.validate_model("  ") is None
    assert muse_bridge.validate_model(" anything-goes ") == "anything-goes"


# -------------------------------------------------------------------- parsing
def _fold(stdout):
    state = {}
    for ev in muse_bridge._events(stdout):
        muse_bridge._answer_from_event(ev, state)
    return state


def test_finish_returns_the_terminal_text():
    assert muse_bridge._finish(_fold(STDOUT_OK), 0, "") == "echo: say hi"


def test_finish_falls_back_to_deltas_when_terminal_text_is_empty():
    stdout = "\n".join(
        [
            _ev({"kind": "run_output_delta", "text": "part "}),
            _ev({"kind": "run_output_delta", "text": "two"}),
        ]
        + [_ev({**TERMINAL_OK, "text": ""})]
    )
    assert muse_bridge._finish(_fold(stdout), 0, "") == "part two"


def test_finish_reports_a_failed_run_with_its_reason():
    stdout = _ev({**TERMINAL_OK, "terminal": "failed", "text": "", "reason": "authRequired"})
    with pytest.raises(RuntimeError, match="muse run failed: authRequired"):
        muse_bridge._finish(_fold(stdout), 1, "")


def test_finish_surfaces_the_logged_out_message():
    with pytest.raises(RuntimeError, match="missing meta credentials"):
        muse_bridge._finish(_fold(""), 1, STDERR_LOGGED_OUT)


def test_finish_filters_muse_informational_stderr():
    noise = "muse: workspace root: C:\\w (explicit)\nmuse: Agent delegation: auto unavailable\n"
    with pytest.raises(RuntimeError) as e:
        muse_bridge._finish(_fold(""), 0, noise)
    assert "workspace root" not in str(e.value)


def test_events_skips_non_json_lines():
    assert muse_bridge._events("hello\n\n" + _ev(TERMINAL_OK) + "\n{broken") == [
        json.loads(_ev(TERMINAL_OK))
    ]


# -------------------------------------------------------------------- run + pinning
def test_run_muse_pins_the_session_it_minted_and_deletes_the_prompt_file(tmp_path, monkeypatch):
    seen = {}

    def fake_run(args, **kw):
        seen["args"] = args
        pf = args[args.index("--prompt-file") + 1]
        seen["prompt_file"] = pf
        with open(pf, encoding="utf-8") as f:
            seen["prompt"] = f.read()
        return _P(stdout=STDOUT_OK)

    monkeypatch.setattr(muse_bridge.proc_tree, "run_captured", fake_run)
    hostile = '-leading dash with "quotes" & %PATH% !x!'
    assert (
        muse_bridge.run_muse(hostile, str(tmp_path), "read-only", None, False, 30) == "echo: say hi"
    )
    assert seen["prompt"] == hostile  # verbatim, via the file
    assert hostile not in seen["args"]  # and never in argv
    assert not os.path.exists(seen["prompt_file"])
    minted = seen["args"][seen["args"].index("--session-id") + 1]
    assert muse_bridge.get_pinned(str(tmp_path)) == minted


def test_swarm_workers_let_muse_mint_and_never_pin(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        muse_bridge.proc_tree,
        "run_captured",
        lambda args, **kw: seen.setdefault("a", args) and _P(stdout=STDOUT_OK),
    )
    muse_bridge.run_muse("p", str(tmp_path), "read-only", None, False, 30, pin=False)
    assert "--session-id" not in seen["a"]
    assert muse_bridge.get_pinned(str(tmp_path)) is None


def test_continue_resumes_the_pin(tmp_path, monkeypatch):
    muse_bridge._pin(str(tmp_path), SID)
    seen = {}
    monkeypatch.setattr(
        muse_bridge.proc_tree,
        "run_captured",
        lambda args, **kw: seen.setdefault("a", args) and _P(stdout=STDOUT_OK),
    )
    muse_bridge.run_muse("again", str(tmp_path), "read-only", None, True, 30)
    assert seen["a"][seen["a"].index("--session-id") + 1] == SID


def test_continue_after_restart_uses_muse_export(tmp_path, monkeypatch):
    monkeypatch.setattr(muse_bridge, "last_session", lambda ws: SID)
    assert muse_bridge._session_for(str(tmp_path), True, True) == SID


def test_continue_with_nothing_to_resume_errors_instead_of_starting_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(muse_bridge, "last_session", lambda ws: None)
    with pytest.raises(RuntimeError, match="no Muse session to continue"):
        muse_bridge._session_for(str(tmp_path), True, True)


def test_last_session_reads_the_export_document(tmp_path, monkeypatch):
    def fake_run(args, **kw):
        out = args[args.index("--out") + 1]
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"export_schema_version": 1, "sessions": [{"session_id": SID}]}, f)
        return _P()

    monkeypatch.setattr(muse_bridge.proc_tree, "run_captured", fake_run)
    assert muse_bridge.last_session(str(tmp_path)) == SID


def test_last_session_is_none_when_muse_has_none(tmp_path, monkeypatch):
    # Verified live: exit 1, "no retained sessions found for this workspace".
    monkeypatch.setattr(muse_bridge.proc_tree, "run_captured", lambda *a, **k: _P(returncode=1))
    assert muse_bridge.last_session(str(tmp_path)) is None


# -------------------------------------------------------------------- resolution
def _win_install(tmp_path, version="1.3.0-R3401.1"):
    (tmp_path / "muse.cmd").write_text("@echo off\r\n", encoding="ascii")
    (tmp_path / ".muse-version").write_text(version + "\n", encoding="ascii")
    binary = tmp_path / f"muse-bin-{version}.exe"
    binary.write_bytes(b"MZ")
    return binary


def test_resolve_runs_the_binary_behind_the_windows_shim(tmp_path, monkeypatch):
    binary = _win_install(tmp_path)
    monkeypatch.setattr(muse_bridge, "MUSE_BIN_ENV", str(tmp_path / "muse.cmd"))
    assert muse_bridge._resolve_bin(windows=True) == str(binary)


def test_resolve_falls_back_to_the_install_dir_when_path_is_stale(tmp_path, monkeypatch):
    binary = _win_install(tmp_path)
    monkeypatch.setattr(muse_bridge, "MUSE_BIN_ENV", "muse-not-on-path")
    monkeypatch.setattr(muse_bridge, "_WIN_INSTALL_DIR", tmp_path)
    assert muse_bridge._resolve_bin(windows=True) == str(binary)


def test_resolve_ignores_a_tampered_version_file(tmp_path, monkeypatch):
    _win_install(tmp_path, version="1.3.0-R3401.1")
    (tmp_path / ".muse-version").write_text("..\\..\\evil", encoding="ascii")
    shim = str(tmp_path / "muse.cmd")
    monkeypatch.setattr(muse_bridge, "MUSE_BIN_ENV", shim)
    # Unresolvable -> the shim itself, which proc_tree.check_args then guards.
    assert muse_bridge._resolve_bin(windows=True) == shim


def test_resolve_uses_the_launcher_as_is_off_windows(tmp_path, monkeypatch):
    launcher = tmp_path / "muse"
    launcher.write_text("#!/usr/bin/env bash\n", encoding="ascii")
    monkeypatch.setattr(muse_bridge, "MUSE_BIN_ENV", str(launcher))
    assert muse_bridge._resolve_bin(windows=False) == str(launcher)


# -------------------------------------------------------------------- watch mapper
def test_watch_mapper_shows_tool_tasks_and_hides_bookkeeping():
    to_lines = muse_bridge.watch_mapper()

    def lc(inner):
        return json.loads(_ev({"kind": "task_lifecycle", "event": inner}))

    assert (
        to_lines(
            lc({"kind": "proposed", "task_id": "r", "task_kind": "reminder.agent.skill-reminder"})
        )
        == []
    )
    assert (
        to_lines(lc({"kind": "proposed", "task_id": "m", "task_kind": "model.unknown.response"}))
        == []
    )
    assert (
        to_lines(lc({"kind": "failed", "task_id": "r", "reason": "echo has no base instructions"}))
        == []
    )
    assert to_lines(lc({"kind": "proposed", "task_id": "t", "task_kind": "tool.read_file"})) == [
        ("command", "tool.read_file")
    ]
    assert to_lines(lc({"kind": "completed", "task_id": "t"})) == [("result", "done")]
    assert to_lines(
        json.loads(_ev({"kind": "run_model_configured", "model_id": "muse-spark-1.3"}))
    ) == [("narration", "model: muse-spark-1.3")]
    assert to_lines(json.loads(_ev({**TERMINAL_OK, "terminal": "failed", "reason": "boom"}))) == [
        ("result", "failed: boom")
    ]
    assert to_lines({"no": "payload"}) == []


# -------------------------------------------------------------------- status + swarm
def test_auth_status_prefers_the_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("META_API_KEY", "x")
    assert muse_bridge.auth_status()[0] is True
    monkeypatch.delenv("META_API_KEY")
    monkeypatch.setattr(muse_bridge, "MUSE_AUTH_PATH", tmp_path / "auth.json")
    assert muse_bridge.auth_status() == (
        False,
        "not logged in (run `muse login`, or set META_API_KEY)",
    )
    (tmp_path / "auth.json").write_text("{}", encoding="ascii")
    assert muse_bridge.auth_status()[0] is True


def test_swarm_accepts_muse_and_its_aliases(tmp_path):
    tasks = [
        {"backend": b, "prompt": "p", "workspace": str(tmp_path)}
        for b in ("muse", "meta", "muse-code")
    ]
    norm = swarm._normalize_tasks(tasks)
    assert [t["backend"] for t in norm] == ["muse"] * 3
    assert all(t["sandbox"] == "read-only" for t in norm)
    with pytest.raises(ValueError, match="invalid sandbox"):
        swarm._normalize_tasks([{"backend": "muse", "prompt": "p", "sandbox": "nope"}])


# -------------------------------------------------------------------- live (echo provider)
_REAL = muse_bridge._resolve_bin()
_HAVE_MUSE = bool(shutil.which(_REAL) or os.path.isfile(_REAL))
_skip_without_muse = pytest.mark.skipif(not _HAVE_MUSE, reason="muse is not installed")


def live(test):
    """Runs the real muse (echo provider, no quota) when it is installed. Marked
    real_cli so conftest's tripwire lets it start muse."""
    return pytest.mark.real_cli(_skip_without_muse(test))


@pytest.fixture
def real_muse(monkeypatch):
    monkeypatch.setattr(muse_bridge, "MUSE_BIN", _REAL)
    monkeypatch.setattr(muse_bridge, "MUSE_PROVIDER", "echo")
    return muse_bridge


@live
def test_live_ask_continue_and_restart_recovery(real_muse):
    ws = tempfile.mkdtemp(prefix="muse-live-")
    assert real_muse.run_muse("first", ws, "read-only", None, False, 90) == "echo: first"
    sid = real_muse.get_pinned(ws)
    assert real_muse.run_muse("second", ws, "workspace-write", None, True, 90) == "echo: second"
    real_muse._PINNED.clear()  # what a server restart looks like
    assert real_muse.last_session(ws) == sid
    assert real_muse.run_muse("third", ws, "read-only", None, True, 90) == "echo: third"


@live
def test_live_hostile_prompt_arrives_verbatim(real_muse):
    ws = tempfile.mkdtemp(prefix="muse-live-")
    hostile = '-dash "quotes" & %PATH% !x! | <>'
    events = []
    out = real_muse.run_muse_streaming(
        hostile, ws, "danger-full-access", None, False, 90, events.append
    )
    assert out == f"echo: {hostile}"
    assert any(e.get("payload", {}).get("kind") == "run_terminal" for e in events)


@live
def test_live_every_sandbox_mode_parses(real_muse):
    ws = tempfile.mkdtemp(prefix="muse-live-")
    for mode in real_muse.SANDBOX_MODES:
        assert real_muse.run_muse(mode, ws, mode, None, False, 90, pin=False) == f"echo: {mode}"


@live
def test_live_version_is_reported(real_muse):
    assert (real_muse.muse_version() or "").startswith("Muse Code ")


def test_every_spawn_goes_through_the_shim_guard():
    # muse is launched directly on Windows, but keep the invariant visible here too.
    src = open(muse_bridge.__file__, encoding="utf-8").read()
    assert "subprocess.Popen(" not in src
    assert "proc_tree.popen(" in src and "proc_tree.run_captured(" in src
    assert proc_tree.check_args  # the guard those two call
