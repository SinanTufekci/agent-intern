"""Offline tests for the local statistics (stats.py, stats_tools.py, the /stats route).

No agent CLI is started. The middleware is exercised through a real in-memory FastMCP
server with fake tools, so the seam between FastMCP and the recorder is covered, not just
the pieces.
"""

import asyncio
import concurrent.futures
import http.client
import json
import os
import sys
import time

import pytest

import server
import stats
import stats_page
import stats_tools

# Importing stats_tools registers agent_stats on the shared server.mcp. Until server wires the
# module in (server._TOOL_MODULES), take it back off so the tool-surface tests in test_server.py
# keep counting what the server really ships; once it is wired this is a no-op.
if stats_tools not in server._TOOL_MODULES:
    getattr(server.mcp, "local_provider", server.mcp).remove_tool("agent_stats")


@pytest.fixture(autouse=True)
def _stats_file(monkeypatch, tmp_path):
    path = tmp_path / ".agent-intern" / "stats.jsonl"
    monkeypatch.setattr(stats, "stats_path", lambda: path)
    monkeypatch.setattr(stats, "_PRUNED", False)
    monkeypatch.delenv(stats.DISABLE_ENV, raising=False)
    return path


def _lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- which tools are recorded
@pytest.mark.parametrize(
    "tool, expected",
    [
        ("codex_ask", ("codex", "ask")),
        ("opencode_continue", ("opencode", "continue")),
        ("antigravity_image", ("antigravity", "image")),
        ("antigravity_image_swarm", ("antigravity", "image")),
        ("cursor_ask", ("cursor", "ask")),
        ("agent_swarm", ("swarm", "swarm")),
        ("preset_swarm", ("swarm", "preset")),
    ],
)
def test_delegating_tools_are_described(tool, expected):
    assert stats.describe_tool(tool) == expected


@pytest.mark.parametrize(
    "tool",
    ["codex_status", "antigravity_status", "claude_status", "agent_stats", "swarm_presets", "x"],
)
def test_status_stats_and_unknown_tools_are_not_recorded(tool):
    assert stats.describe_tool(tool) is None


# ------------------------------------------------------------------------- the outcome
def _note(**kw):
    n = stats.CallNote()
    for k, v in kw.items():
        setattr(n, k, v)
    return n


def test_a_clean_exit_is_ok_whatever_the_text_says():
    result = stats.classify(_note(spawns=1, last_rc=0), "the quota docs say 429")
    assert result == (True, "ok", "spawn")


def test_a_non_zero_exit_is_a_failure_even_with_unremarkable_text():
    result = stats.classify(_note(spawns=1, last_rc=3), "something went wrong")
    assert result == (False, "exit_error", "spawn")


@pytest.mark.parametrize(
    "text, cat",
    [
        ("Error: you have hit your usage limit", "quota"),
        ("429 Too Many Requests", "quota"),
        ("RESOURCE_EXHAUSTED: quota", "quota"),
        ("Not logged in. Run codex login", "auth"),
        ("401 Unauthorized", "auth"),
    ],
)
def test_quota_and_auth_are_told_apart_from_the_text_after_a_failed_exit(text, cat):
    assert stats.classify(_note(spawns=1, last_rc=1), text)[1] == cat


@pytest.mark.parametrize("flag", ["timeout", "not_installed", "cancelled", "nesting"])
def test_a_spawn_flag_wins(flag):
    result = stats.classify(_note(flags={flag}, spawns=1, last_rc=0), "fine")
    assert result == (False, flag, "spawn")


def test_an_exception_is_an_error():
    assert stats.classify(_note(), "", RuntimeError("boom")) == (False, "error", "exception")


def test_with_nothing_noted_the_text_decides_but_plain_text_is_ok():
    assert stats.classify(_note(), "here is the answer")[:2] == (True, "ok")
    assert stats.classify(_note(), "Error: could not start")[:2] == (False, "other")
    assert stats.classify(_note(), "quota exceeded")[:2] == (False, "quota")


def test_swarm_outcome_comes_from_the_worker_counts():
    assert stats.classify(_note(workers=3, workers_ok=3), "")[:2] == (True, "ok")
    assert stats.classify(_note(workers=3, workers_ok=1), "")[:2] == (True, "partial")
    assert stats.classify(_note(workers=3, workers_ok=0), "")[:2] == (False, "error")


# ---------------------------------------------------------------------- the record itself
def test_a_record_holds_numbers_and_categories_only():
    n = _note(spawns=1, last_rc=0)
    rec = stats.build_record(
        "codex_ask",
        {"prompt": "SECRET prompt with /home/me/key=abc", "model": "gpt-5", "sandbox": "read-only"},
        n,
        1.234,
        text="SECRET answer",
        now=1_000_000,
    )
    assert rec["ms"] == 1234 and rec["model"] == "gpt-5" and rec["sandbox"] == "read-only"
    blob = json.dumps(rec)
    assert "SECRET" not in blob and "/home" not in blob and "prompt" not in rec


def test_an_odd_model_value_is_dropped_rather_than_stored():
    rec = stats.build_record("codex_ask", {"model": "x; rm -rf / \n"}, _note(), 0.1)
    assert "model" not in rec


def _with_note(note, fn):
    token = stats.CURRENT_NOTE.set(note)
    try:
        fn()
    finally:
        stats.CURRENT_NOTE.reset(token)


def test_swarm_records_keep_counts_and_only_the_run_id():
    n = _note()
    folder = os.path.join("home", "u", ".agent-intern", "runs", "20261118-120000-abc123")
    _with_note(n, lambda: stats.note_swarm(4, 3, folder))
    rec = stats.build_record("agent_swarm", {"tasks": [{"prompt": "SECRET"}]}, n, 5.0)
    assert (rec["workers"], rec["workers_ok"], rec["run"]) == (4, 3, "20261118-120000-abc123")
    assert "SECRET" not in json.dumps(rec) and "home" not in json.dumps(rec)


def test_a_run_dir_that_is_not_a_run_id_is_not_kept():
    n = _note()
    _with_note(n, lambda: stats.note_swarm(1, 1, "/tmp/some-other-folder"))
    assert n.run_id is None


def test_tokens_add_up_and_ignore_bad_values():
    n = _note()

    def feed():
        stats.note_tokens(10, 5, 2)
        stats.note_tokens(1, None, True)  # bool and None are not counts
        stats.note_tokens(-4, "x", 3.5)

    _with_note(n, feed)
    assert n.tokens == {"i": 11, "o": 5, "c": 2}
    assert stats.build_record("claude_ask", {}, n, 1)["tok"] == {"i": 11, "o": 5, "c": 2}


def test_note_helpers_do_nothing_without_a_call():
    stats.note_exit(1)
    stats.note_flag("timeout")
    stats.note_tokens(1, 1, 1)
    stats.note_swarm(1, 1)  # no note set: no error


# --------------------------------------------------------------------------- the file
def test_records_are_appended_one_line_each(_stats_file):
    for i in range(3):
        assert stats.record({"v": 1, "ts": int(time.time()), "i": i})
    assert [r["i"] for r in _lines(_stats_file)] == [0, 1, 2]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_file_and_folder_are_private(_stats_file):
    stats.record({"v": 1, "ts": int(time.time())})
    assert _stats_file.stat().st_mode & 0o777 == 0o600
    assert _stats_file.parent.stat().st_mode & 0o777 == 0o700


def test_no_stats_env_writes_nothing(monkeypatch, _stats_file):
    monkeypatch.setenv(stats.DISABLE_ENV, "1")
    assert stats.record({"v": 1, "ts": 1}) is False
    assert not _stats_file.exists()


def test_a_write_problem_never_raises(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the folder should be")
    assert stats.record({"v": 1, "ts": 1}, path=blocker / "sub" / "stats.jsonl") is False


def test_lines_older_than_the_retention_are_dropped_on_first_write(_stats_file):
    now = time.time()
    _stats_file.parent.mkdir(parents=True)
    old = {"v": 1, "ts": int(now - 100 * 86400), "tag": "old"}
    fresh = {"v": 1, "ts": int(now - 86400), "tag": "fresh"}
    _stats_file.write_text(json.dumps(old) + "\n" + json.dumps(fresh) + "\nnot json\n")
    stats.record({"v": 1, "ts": int(now), "tag": "new"}, now=now)
    assert [r["tag"] for r in _lines(_stats_file)] == ["fresh", "new"]


def test_non_object_json_lines_do_not_stop_pruning(_stats_file):
    now = time.time()
    _stats_file.parent.mkdir(parents=True)
    old = {"v": 1, "ts": int(now - 100 * 86400), "tag": "old"}
    fresh = {"v": 1, "ts": int(now - 86400), "tag": "fresh"}
    _stats_file.write_text(
        json.dumps(old) + '\n[1, 2]\n"text"\n42\nnull\n{"ts": "x"}\n' + json.dumps(fresh) + "\n"
    )
    stats.record({"v": 1, "ts": int(now), "tag": "new"}, now=now)
    assert [r["tag"] for r in _lines(_stats_file)] == ["fresh", "new"]
    assert stats._PRUNED is True


def test_a_non_object_first_line_is_pruned_away_too(_stats_file):
    now = time.time()
    _stats_file.parent.mkdir(parents=True)
    fresh = {"v": 1, "ts": int(now - 86400), "tag": "fresh"}
    _stats_file.write_text("[1]\n" + json.dumps(fresh) + "\n")
    stats.record({"v": 1, "ts": int(now), "tag": "new"}, now=now)
    assert [r["tag"] for r in _lines(_stats_file)] == ["fresh", "new"]


@pytest.mark.parametrize(
    "value",
    [
        "/home/me/x",
        "C:\\Users\\me\\m",
        "../etc/passwd",
        "a/b/c",
        ".hidden",
        "a..b/c",
        "x y",
        "m" * 60,
    ],
)
def test_path_like_model_values_are_not_stored(value):
    rec = stats.build_record("codex_ask", {"model": value}, _note(), 0.1)
    assert "model" not in rec


@pytest.mark.parametrize(
    "value", ["gpt-5", "gemini-4-argon-high", "opencode/nemotron-3.5-lightning-free", "o3:latest"]
)
def test_model_id_shapes_are_stored(value):
    rec = stats.build_record("opencode_ask", {"model": value}, _note(), 0.1)
    assert rec["model"] == value


def test_sandbox_is_stored_only_from_the_known_words():
    ok = stats.build_record("codex_ask", {"sandbox": "workspace-write"}, _note(), 0.1)
    assert ok["sandbox"] == "workspace-write"
    for odd in ("/etc", "plan2", "read-only; x", 5):
        assert "sandbox" not in stats.build_record("codex_ask", {"sandbox": odd}, _note(), 0.1)


def test_an_oversized_file_is_trimmed_to_its_newest_lines(monkeypatch, _stats_file):
    monkeypatch.setattr(stats, "MAX_BYTES", 2000)
    now = time.time()
    _stats_file.parent.mkdir(parents=True)
    rows = [{"v": 1, "ts": int(now - 60 + i), "i": i, "pad": "x" * 100} for i in range(50)]
    _stats_file.write_text("".join(json.dumps(r) + "\n" for r in rows))
    stats.record({"v": 1, "ts": int(now), "i": 99}, now=now)
    kept = [r["i"] for r in _lines(_stats_file)]
    assert kept[-2:] == [49, 99] and len(kept) < 50
    assert _stats_file.stat().st_size < 3000


def test_read_records_skips_bad_lines_and_old_ones(_stats_file):
    now = time.time()
    _stats_file.parent.mkdir(parents=True)
    _stats_file.write_text(
        f'{{"ts": {now - 10}, "t": 1}}\nnot json\n[1]\n{{"ts": {now - 9 * 86400}, "t": 2}}\n'
    )
    assert [r["t"] for r in stats.read_records(7, now=now)] == [1]
    assert stats.read_records(7, path=_stats_file.parent / "missing.jsonl") == []


# ------------------------------------------------------------------------ summary + table
def _rec(ts, backend, ok, ms=2000, cat=None, **kw):
    r = {
        "ts": ts,
        "backend": backend,
        "kind": "ask",
        "ok": ok,
        "cat": cat or ("ok" if ok else "timeout"),
        "ms": ms,
    }
    r.update(kw)
    return r


def test_summary_counts_rates_and_last_error():
    now = 1_800_000_000
    recs = [
        _rec(now, "codex", True, 2000, tok={"i": 10, "o": 4}),
        _rec(now + 5, "codex", False, 4000),
        _rec(now + 9, "opencode", True, 1000),
        _rec(
            now,
            "swarm",
            True,
            9000,
            kind="swarm",
            workers=3,
            workers_ok=3,
            run="20261118-120000-abc123",
        ),
    ]
    s = stats.summarize(recs)
    codex = s["backends"]["codex"]
    got = (codex["calls"], codex["ok"], codex["err"], codex["avg_s"], codex["error_rate"])
    assert got == (2, 1, 1, 3.0, 50)
    assert codex["last_err"]["cat"] == "timeout" and codex["tokens"] == {"i": 10, "o": 4}
    assert s["backends"]["opencode"]["error_rate"] == 0
    assert sum(d["calls"] for d in s["days"].values()) == 4
    assert s["swarms"][0]["run"] == "20261118-120000-abc123"


def test_the_table_says_what_it_shows_and_what_it_does_not():
    now = 1_800_000_000
    s = stats.summarize([_rec(now, "codex", True), _rec(now, "codex", False)])
    out = stats.render_table(
        s, 7, quota="- Gemini Models: Weekly 80%", recording="recording is off"
    )
    for part in (
        "Calls per backend",
        "Per day",
        "Per week",
        "Backend health",
        "codex",
        "Note: recording is off",
    ):
        assert part in out
    assert "Antigravity quota left" in out and "Weekly 80%" in out
    assert "n/a" in out and "best effort" in out
    assert "No delegated calls" in stats.render_table(stats.summarize([]), 7)


# --------------------------------------------------------- the middleware, through FastMCP
def _client_call(mcp, name, args=None):
    from fastmcp import Client

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(name, args or {}, raise_on_error=False)

    return asyncio.run(go())


def _fake_server():
    from fastmcp import FastMCP

    mcp = FastMCP("t")
    assert stats_tools.install(mcp) == ""

    @mcp.tool
    async def codex_ask(prompt: str, model: str = "") -> str:
        def worker():  # a CLI run in a thread, as the bridges do
            stats.note_exit(0)
            stats.note_tokens(7, 3, 1)
            return "an answer with SECRET inside"

        return await asyncio.to_thread(worker)

    @mcp.tool
    async def opencode_ask(prompt: str) -> str:
        await asyncio.to_thread(stats.note_exit, 2)
        return "Error: opencode died"

    @mcp.tool
    async def grok_ask(prompt: str) -> str:
        await asyncio.to_thread(stats.note_flag, "timeout")
        return "timed out"

    @mcp.tool
    async def copilot_ask(prompt: str) -> str:
        raise RuntimeError("boom with /home/me/secret path")

    @mcp.tool
    async def codex_status() -> str:
        return "ok"

    @mcp.tool
    async def agent_swarm(tasks: list) -> str:
        stats.note_swarm(2, 1, "x/runs/20261118-120000-abc123")
        return "swarm result"

    return mcp


def test_middleware_records_the_outcome_the_spawn_path_noted(_stats_file):
    mcp = _fake_server()
    r = _client_call(mcp, "codex_ask", {"prompt": "SECRET prompt", "model": "gpt-5"})
    assert not r.is_error
    (rec,) = _lines(_stats_file)
    assert rec["backend"] == "codex" and rec["ok"] is True and rec["src"] == "spawn"
    assert rec["tok"] == {"i": 7, "o": 3, "c": 1} and rec["model"] == "gpt-5"
    assert "SECRET" not in _stats_file.read_text(encoding="utf-8")


def test_a_failed_exit_and_a_timeout_are_counted_as_failures(_stats_file):
    mcp = _fake_server()
    _client_call(mcp, "opencode_ask", {"prompt": "p"})
    _client_call(mcp, "grok_ask", {"prompt": "p"})
    a, b = _lines(_stats_file)
    assert (a["ok"], a["cat"]) == (False, "exit_error")
    assert (b["ok"], b["cat"]) == (False, "timeout")


def test_a_raised_error_is_recorded_and_still_reaches_the_client(_stats_file):
    mcp = _fake_server()
    r = _client_call(mcp, "copilot_ask", {"prompt": "p"})
    assert r.is_error
    (rec,) = _lines(_stats_file)
    assert (rec["ok"], rec["cat"], rec["src"]) == (False, "error", "exception")
    assert "secret" not in _stats_file.read_text(encoding="utf-8")


def test_status_calls_are_not_recorded(_stats_file):
    mcp = _fake_server()
    _client_call(mcp, "codex_status")
    assert not _stats_file.exists()


def test_swarm_outcome_comes_from_note_swarm(_stats_file):
    mcp = _fake_server()
    _client_call(mcp, "agent_swarm", {"tasks": [{"prompt": "SECRET"}]})
    (rec,) = _lines(_stats_file)
    got = (rec["backend"], rec["workers"], rec["workers_ok"], rec["cat"])
    assert got == ("swarm", 2, 1, "partial")
    assert rec["run"] == "20261118-120000-abc123"


def test_a_recording_failure_never_changes_the_result(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(stats, "record", boom)
    mcp = _fake_server()
    r = _client_call(mcp, "codex_ask", {"prompt": "p"})
    assert not r.is_error and "an answer" in r.content[0].text


def test_a_cancelled_call_is_recorded_and_re_raised(_stats_file):
    from types import SimpleNamespace

    mw = stats_tools.StatsMiddleware()
    ctx = SimpleNamespace(message=SimpleNamespace(name="codex_ask", arguments={"prompt": "p"}))

    async def cancelled(_ctx):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mw.on_call_tool(ctx, cancelled))
    (rec,) = _lines(_stats_file)
    assert (rec["ok"], rec["cat"]) == (False, "cancelled")


def test_the_note_follows_to_thread_but_not_a_plain_thread_pool():
    """Why swarm workers report through note_swarm: a ThreadPoolExecutor does not copy the
    context, asyncio.to_thread does."""
    note = stats.CallNote()
    seen = {}

    async def go():
        token = stats.CURRENT_NOTE.set(note)
        try:
            seen["to_thread"] = await asyncio.to_thread(stats.CURRENT_NOTE.get)
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                seen["pool"] = pool.submit(stats.CURRENT_NOTE.get).result()
        finally:
            stats.CURRENT_NOTE.reset(token)

    asyncio.run(go())
    assert seen["to_thread"] is note and seen["pool"] is None


# ------------------------------------------------------------------------------ install
def test_install_off_by_env_says_so(monkeypatch):
    from fastmcp import FastMCP

    monkeypatch.setenv(stats.DISABLE_ENV, "1")
    note = stats_tools.install(FastMCP("t"))
    assert stats.DISABLE_ENV in note and stats_tools.recording_note() == note


def test_install_without_middleware_support_says_so(monkeypatch):
    from fastmcp import FastMCP

    monkeypatch.setattr(stats_tools, "StatsMiddleware", None)
    assert "2.9" in stats_tools.install(FastMCP("t"))


def test_install_survives_add_middleware_failing():
    from fastmcp import FastMCP

    class Broken:
        def add_middleware(self, mw):
            raise TypeError("nope")

    assert "could not be added" in stats_tools.install(Broken())
    stats_tools.install(FastMCP("t"))  # leave the shared state as "on"


# ------------------------------------------------------------------- tool and web page
def _tool_fn(tool):
    return getattr(tool, "fn", tool)


def test_agent_stats_returns_a_table_and_clamps_days(monkeypatch):
    monkeypatch.setattr(stats_tools, "_agy_quota_text", lambda: "- Gemini Models: Weekly 50%")
    stats.record(_rec(int(time.time()), "codex", True))
    out = asyncio.run(_tool_fn(stats_tools.agent_stats)(days=9999))
    assert "last 90 day(s)" in out and "codex" in out and "Weekly 50%" in out
    assert stats_tools.clamp_days("x") == 7 and stats_tools.clamp_days(0) == 1


def test_stats_page_has_no_outside_loads_and_writes_data_as_text():
    html = stats_page.PAGE
    assert "<script src" not in html and "<link" not in html and "@import" not in html
    assert "innerHTML" not in html and "document.write" not in html
    assert 'fetch("/stats.json' in html


def _get(port, path, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
        r = conn.getresponse()
        body = r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, body
    finally:
        conn.close()


def test_stats_routes_need_the_token_and_carry_the_security_headers(monkeypatch):
    import watch_server

    monkeypatch.setattr(stats_tools, "_agy_quota_text", lambda: None)
    stats.record(_rec(int(time.time()), "codex", True))
    port = watch_server._ensure_watch_server()
    k = server._WATCH_TOKEN
    status, headers, body = _get(port, f"/stats?days=7&k={k}")
    assert status == 200 and b"agent-intern stats" in body
    assert headers["cache-control"] == "no-store"
    assert "default-src 'none'" in headers["content-security-policy"]
    status, headers, body = _get(port, f"/stats.json?days=7&k={k}")
    data = json.loads(body)
    assert status == 200 and data["days"] == 7
    assert data["summary"]["backends"]["codex"]["calls"] == 1
    assert headers["content-type"].startswith("application/json")
    for path in ("/stats?k=wrong", "/stats.json?days=7", f"/stats.json?k={k}x"):
        assert _get(port, path)[0] == 403
    assert _get(port, f"/stats.json?k={k}", host="evil.example")[0] == 403


# ------------------------------------------- the hooks in proc_tree, swarm and the agy parser
class _FakeProc:
    def __init__(self, returncode=0, exc=None):
        self.returncode = returncode
        self._exc = exc
        self.pid = 1

    def communicate(self, timeout=None):
        if self._exc:
            raise self._exc
        return "out", "err"


def _noted(fn):
    note = stats.CallNote()
    token = stats.CURRENT_NOTE.set(note)
    try:
        try:
            fn()
        except Exception:  # the hook is under test, not the error
            pass
    finally:
        stats.CURRENT_NOTE.reset(token)
    return note


def test_run_captured_notes_the_exit_of_an_agent_run(monkeypatch):
    import subprocess

    import proc_tree

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc(returncode=3))
    note = _noted(lambda: proc_tree.run_captured(["cli"], timeout=5))
    assert (note.spawns, note.last_rc) == (1, 3)


def test_run_captured_does_not_note_a_probe(monkeypatch):
    import subprocess

    import proc_tree

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc(returncode=1))
    note = _noted(
        lambda: proc_tree.run_captured(["cli", "--version"], timeout=5, starts_agent=False)
    )
    assert note.spawns == 0 and not note.flags


def test_run_captured_notes_a_timeout(monkeypatch):
    import subprocess

    import proc_tree

    expired = subprocess.TimeoutExpired(["cli"], 1)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc(exc=expired))
    monkeypatch.setattr(proc_tree, "kill_tree", lambda p: None)
    monkeypatch.setattr(proc_tree, "_drain", lambda p: (None, None))
    note = _noted(lambda: proc_tree.run_captured(["cli"], timeout=1))
    assert note.flags == {"timeout"}


def test_popen_notes_a_missing_cli_nesting_and_cancellation(monkeypatch):
    import subprocess

    import proc_tree

    def missing(*a, **k):
        raise FileNotFoundError(2, "no such file", "codex")

    monkeypatch.setattr(subprocess, "Popen", missing)
    note = _noted(lambda: proc_tree.popen(["codex", "exec"]))
    assert note.flags == {"not_installed"}

    monkeypatch.setenv(proc_tree.DEPTH_ENV, str(proc_tree.MAX_DEPTH))
    note = _noted(lambda: proc_tree.popen(["codex", "exec"]))
    assert note.flags == {"nesting"}
    monkeypatch.delenv(proc_tree.DEPTH_ENV)

    call = proc_tree.Call()
    call.cancelled = True
    token = proc_tree.CURRENT_CALL.set(call)
    try:
        note = _noted(lambda: proc_tree.popen(["codex", "exec"]))
    finally:
        proc_tree.CURRENT_CALL.reset(token)
    assert note.flags == {"cancelled"}


def test_a_cli_not_found_error_is_classified_as_not_installed():
    import proc_tree

    exc = proc_tree.not_found_error("codex", "codex")
    assert stats.classify(stats.CallNote(), "", exc)[:2] == (False, "not_installed")


def test_swarm_results_note_counts_and_the_run_folder(tmp_path, monkeypatch):
    import swarm

    runs = tmp_path / "runs"
    monkeypatch.setattr(swarm, "runs_root", lambda: runs)
    big = "x" * (swarm.WORKER_OUTPUT_CAP + 10)
    results = [
        swarm.WorkerResult(0, True, answer=big, backend="codex", elapsed=1.0),
        swarm.WorkerResult(1, False, error="nope", backend="opencode", elapsed=1.0),
    ]
    note = _noted(lambda: swarm.format_agent_results(results))
    assert (note.workers, note.workers_ok) == (2, 1)
    assert note.run_id and (runs / note.run_id).is_dir()
    rec = stats.build_record("agent_swarm", {}, note, 3.0)
    assert rec["cat"] == "partial" and rec["run"] == note.run_id


def test_a_preset_notes_its_worker_counts(monkeypatch, tmp_path):
    import swarm
    import swarm_presets

    members = swarm_presets.get("council", str(tmp_path)).members
    results = [
        swarm.WorkerResult(i, i > 0, answer="a" if i > 0 else None, error=None if i > 0 else "e")
        for i in range(len(members))
    ]
    monkeypatch.setattr(swarm, "swarm_agents", lambda *a, **k: results)
    note = stats.CallNote()
    token = stats.CURRENT_NOTE.set(note)
    try:
        swarm_presets.run("council", "material", str(tmp_path))
    finally:
        stats.CURRENT_NOTE.reset(token)
    assert (note.workers, note.workers_ok) == (len(members), len(members) - 1)


def test_agy_usage_is_noted_as_tokens():
    note = stats.CallNote()
    token = stats.CURRENT_NOTE.set(note)
    try:
        stats.note_usage({"input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 0})
        stats.note_usage("not a dict")
        stats.note_usage(None)
    finally:
        stats.CURRENT_NOTE.reset(token)
    assert note.tokens == {"i": 10, "o": 2, "c": 0}


def test_a_refused_argument_is_not_held_against_the_backend():
    assert stats.classify(stats.CallNote(), "", ValueError("workspace '/x' does not exist"))[
        :2
    ] == (
        False,
        "refused",
    )
    now = 1_800_000_000
    s = stats.summarize(
        [
            _rec(now, "codex", True),
            _rec(now + 1, "codex", False, cat="refused"),
            _rec(now + 2, "codex", False, cat="timeout"),
        ]
    )
    codex = s["backends"]["codex"]
    assert (codex["calls"], codex["err"]) == (3, 2)
    assert codex["error_rate"] == 33 and codex["last_err"]["cat"] == "timeout"
