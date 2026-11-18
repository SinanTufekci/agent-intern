"""Offline unit tests for the pure logic in swarm.py and swarm_watch.py.

Like test_server.py these use temp fixtures and never invoke agy, so they cost no
AI Pro quota. The live parallel round-trip is in test_smoke.py.

    pytest test_swarm.py
"""

import asyncio
import inspect
import io
import os
import subprocess
import sys
import threading
import time

import pytest

import proc_tree
import server
import swarm
import swarm_watch

_REAL_RUNS_ROOT = swarm.runs_root  # before the fixture below redirects it


@pytest.fixture(autouse=True)
def _runs_in_tmp(tmp_path, monkeypatch):
    """Spilled swarm output goes under ~/.agent-intern/runs/; never write there from a test."""
    runs = tmp_path / "home" / ".agent-intern" / "runs"
    monkeypatch.setattr(swarm, "runs_root", lambda: runs)
    return runs


@pytest.fixture(autouse=True)
def _pin_isolation_ok():
    """Pin HOME isolation ON for every test, and clear the latch afterwards.

    swarm._ISOLATION_OK is process-global and resolved lazily by a subprocess
    probe, so leaving it unset would (a) let one test's latched failure change the
    next test's code path and (b) spawn a real `agy -p "/usage"` from a unit test
    on any non-Windows machine that has agy installed. Tests for the fallback set
    it explicitly.
    """
    swarm._ISOLATION_OK = True
    # A worker that fails with a quota error reads agy's `/usage` (server's quota
    # note). Pin that probe OFF and the shared report empty, so no test spawns it.
    server._AGY_USAGE_GATE = False
    server._QUOTA_REPORT_CACHE = None
    yield
    swarm._ISOLATION_OK = None
    server._AGY_USAGE_GATE = None
    server._QUOTA_REPORT_CACHE = None


# --------------------------------------------------------------------------
# _normalize_workspaces
# --------------------------------------------------------------------------


def test_normalize_workspaces_none_is_cwd_for_all():
    assert swarm._normalize_workspaces(3, None) == [os.getcwd()] * 3


def test_normalize_workspaces_str_broadcasts(tmp_path):
    out = swarm._normalize_workspaces(2, str(tmp_path))
    assert out == [os.path.abspath(str(tmp_path))] * 2


def test_normalize_workspaces_list_per_worker(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    out = swarm._normalize_workspaces(2, [str(a), str(b)])
    assert out == [os.path.abspath(str(a)), os.path.abspath(str(b))]


def test_normalize_workspaces_missing_dir_is_an_error_and_is_not_created(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        swarm._normalize_workspaces(2, str(tmp_path / "nope"))
    with pytest.raises(ValueError, match="does not exist"):
        swarm._normalize_workspaces(1, [str(tmp_path / "nope")])
    assert not (tmp_path / "nope").exists()


def test_normalize_tasks_missing_workspace_names_the_task(tmp_path):
    tasks = [{"backend": "codex", "prompt": "hi", "workspace": str(tmp_path / "nope")}]
    with pytest.raises(ValueError, match=r"task 0: workspace .* does not exist"):
        swarm._normalize_tasks(tasks)
    assert not (tmp_path / "nope").exists()


def test_normalize_tasks_expands_the_home_marker(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    out = swarm._normalize_tasks([{"backend": "codex", "prompt": "hi", "workspace": "~/proj"}])
    assert os.path.samefile(out[0]["workspace"], tmp_path / "proj")


def test_normalize_workspaces_length_mismatch_raises():
    with pytest.raises(ValueError):
        swarm._normalize_workspaces(3, ["only", "two"])


# --------------------------------------------------------------------------
# _labels / _repos
# --------------------------------------------------------------------------


def test_labels_takes_first_nonempty_line():
    assert swarm._labels(["first line\nsecond"])[0] == "first line"


def test_labels_truncates_long_prompts():
    label = swarm._labels(["x" * 200])[0]
    assert label.endswith("…") and len(label) == 121  # 120 chars + ellipsis


def test_labels_empty_prompt():
    assert swarm._labels(["   "])[0] == "(empty)"


def test_repos_uses_basename():
    assert swarm._repos(["C:\\a\\b\\my-repo", "/x/y/other"]) == ["my-repo", "other"]


# --------------------------------------------------------------------------
# isolated HOME helpers
# --------------------------------------------------------------------------


def test_make_isolated_home_creates_state_dir():
    home = swarm._make_isolated_home()
    try:
        assert (home / ".gemini" / "antigravity-cli").is_dir()
    finally:
        import shutil

        shutil.rmtree(home, ignore_errors=True)


def test_env_for_home_redirects_home(tmp_path):
    env = swarm._env_for_home(tmp_path)
    assert env["HOME"] == str(tmp_path)
    assert env["USERPROFILE"] == str(tmp_path)


def _make_brain(home, conv_id, entries):
    """Write a fake isolated transcript for conv_id with the given JSONL entries."""
    import json

    d = home / ".gemini" / "antigravity-cli" / "brain" / conv_id / ".system_generated" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "transcript.jsonl").write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")


def test_only_conv_none_when_empty(tmp_path):
    assert swarm._only_conv(tmp_path) is None


def test_only_conv_returns_single(tmp_path):
    _make_brain(tmp_path, "conv-xyz", [])
    assert swarm._only_conv(tmp_path) == "conv-xyz"


def test_read_isolated_response_returns_last_planner(tmp_path):
    entries = [
        {"source": "MODEL", "status": "DONE", "type": "PLANNER_RESPONSE", "content": "first"},
        {"source": "MODEL", "status": "DONE", "type": "PLANNER_RESPONSE", "content": "final"},
    ]
    _make_brain(tmp_path, "c1", entries)
    assert swarm._read_isolated_response(tmp_path, "c1") == "final"


def test_read_isolated_response_raises_without_done(tmp_path):
    _make_brain(tmp_path, "c2", [{"source": "MODEL", "status": "RUNNING", "type": "X"}])
    with pytest.raises(RuntimeError):
        swarm._read_isolated_response(tmp_path, "c2")


# --------------------------------------------------------------------------
# _finalize_image_isolated (extension correction to the real bytes)
# --------------------------------------------------------------------------

_JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 8
_PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def test_finalize_image_corrects_extension(tmp_path):
    # Requested .png, but the bytes are JPEG -> final path must be .jpg.
    target = str(tmp_path / "out.png")
    with open(target, "wb") as f:
        f.write(_JPEG_HEAD)
    final, fmt, size = swarm._finalize_image_isolated(tmp_path, target, None, 0.0)
    assert fmt == "JPEG"
    assert final.endswith(".jpg")
    assert os.path.isfile(final) and size > 0


def test_finalize_image_keeps_matching_extension(tmp_path):
    target = str(tmp_path / "pic.png")
    with open(target, "wb") as f:
        f.write(_PNG_HEAD)
    final, fmt, _ = swarm._finalize_image_isolated(tmp_path, target, None, 0.0)
    assert fmt == "PNG" and final.endswith(".png")


def test_finalize_image_missing_raises(tmp_path):
    with pytest.raises(RuntimeError):
        swarm._finalize_image_isolated(tmp_path, str(tmp_path / "nope.png"), None, 0.0)


def test_finalize_image_isolated_copies_a_reply_path_outside_its_scratch(tmp_path):
    home = tmp_path / "home"
    out = tmp_path / "out"
    out.mkdir()
    named = out / "agy-named.png"  # in the target's folder: accepted, but copied
    named.write_bytes(_PNG_HEAD)
    final, fmt, _ = swarm._finalize_image_isolated(home, str(out / "pic.png"), str(named), 0.0)
    assert final == str(out / "pic.png") and fmt == "PNG"
    assert named.is_file()


def test_finalize_image_isolated_ignores_a_reply_path_elsewhere(tmp_path):
    home = tmp_path / "home"
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.png").write_bytes(_PNG_HEAD)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(RuntimeError, match="no image file produced"):
        swarm._finalize_image_isolated(home, str(out / "pic.png"), str(other / "x.png"), 0.0)


def test_finalize_image_isolated_moves_from_its_own_scratch(tmp_path):
    home = tmp_path / "home"
    scratch = home / ".gemini" / "antigravity-cli" / "scratch"
    scratch.mkdir(parents=True)
    (scratch / "gen.png").write_bytes(_PNG_HEAD)
    final, _, _ = swarm._finalize_image_isolated(home, str(tmp_path / "pic.png"), None, 0.0)
    assert final == str(tmp_path / "pic.png")
    assert not (scratch / "gen.png").exists()


# --------------------------------------------------------------------------
# result formatting
# --------------------------------------------------------------------------


def test_format_image_results():
    results = [
        swarm.WorkerResult(
            0, True, elapsed=2.0, image_path="C:\\a.png", image_format="PNG", image_size=123
        ),
        swarm.WorkerResult(1, False, error="nope", elapsed=0.0),
    ]
    out = swarm.format_image_results(results)
    assert "1/2 succeeded" in out
    assert "C:\\a.png" in out and "PNG" in out and "nope" in out


# --------------------------------------------------------------------------
# server._broadcast_workspaces (MCP arg -> swarm contract)
# --------------------------------------------------------------------------


def test_broadcast_none():
    assert server._broadcast_workspaces(None, 3) is None
    assert server._broadcast_workspaces([], 3) is None


def test_broadcast_single_to_str():
    assert server._broadcast_workspaces(["C:\\p"], 3) == "C:\\p"


def test_broadcast_list_passthrough():
    assert server._broadcast_workspaces(["a", "b"], 2) == ["a", "b"]


# --------------------------------------------------------------------------
# swarm_watch in-memory dashboard state
# --------------------------------------------------------------------------


def test_watch_state_lifecycle():
    swarm_watch.init(["promptA", "promptB"], ["repo1", "repo2"], 100.0)
    snap = swarm_watch._snapshot()
    assert len(snap["workers"]) == 2
    assert snap["workers"][0]["label"] == "promptA"
    assert snap["workers"][1]["repo"] == "repo2"
    assert all(w["status"] == "queued" for w in snap["workers"])

    swarm_watch.worker_update(0, status="working", elapsed=1.0)
    swarm_watch.worker_append(0, [{"kind": "command", "text": "ls", "t": 0.5}])
    swarm_watch.worker_finish(1, "done", "the answer", 3.0, image="C:\\img.png")

    snap = swarm_watch._snapshot()
    assert snap["workers"][0]["status"] == "working"
    assert snap["workers"][0]["events"][0]["text"] == "ls"
    assert snap["workers"][1]["answer"] == "the answer"
    assert swarm_watch._allowed_images() == {"C:\\img.png"}


def test_watch_full_prompt_falls_back_to_label():
    # Without explicit prompts, the detail-window prompt mirrors the row label.
    swarm_watch.init(["short label"], ["repo"], 1.0)
    assert swarm_watch._snapshot()["workers"][0]["prompt"] == "short label"


def test_watch_full_prompt_kept_untruncated():
    # The full prompt is stored verbatim for the detail window even though the
    # row label is the clipped, single-line caption.
    full = "Kısaca açıkla: " + "x" * 500
    swarm_watch.init(["clipped…"], ["repo"], 1.0, [full])
    w = swarm_watch._snapshot()["workers"][0]
    assert w["label"] == "clipped…"
    assert w["prompt"] == full


def test_watch_init_stores_timeout():
    # The per-worker time progress bar needs the timeout in dashboard state.
    swarm_watch.init(["p"], ["repo"], 1.0, ["p"], 240)
    assert swarm_watch._snapshot()["timeout"] == 240


def test_dashboard_is_live_reflects_recent_poll(monkeypatch):
    # No recent /events poll -> not live, so a new swarm run opens a window.
    monkeypatch.setattr(swarm_watch, "_LAST_POLL", 0.0)
    assert swarm_watch._dashboard_is_live() is False
    # A poll within the alive window -> live, so a new run reuses the open dashboard.
    monkeypatch.setattr(swarm_watch, "_LAST_POLL", swarm_watch.time.time())
    assert swarm_watch._dashboard_is_live() is True


@pytest.mark.parametrize("live", [False, True])
def test_open_window_keeps_stdout_clean(monkeypatch, capsys, live):
    # stdout is the MCP JSON-RPC stream, so a stray line there lands in the protocol.
    # Both paths (new window, reused dashboard) must stay off it.
    monkeypatch.setattr(swarm_watch, "_port", lambda: 8765)
    monkeypatch.setattr(swarm_watch, "_launch", lambda *a, **k: None)
    monkeypatch.setattr(swarm_watch, "_dashboard_is_live", lambda: live)
    monkeypatch.delenv("AGY_WATCH_ALWAYS_NEW", raising=False)
    swarm_watch.open_window(3)
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------
# unified agent swarm: _normalize_tasks / swarm_agents / format_agent_results
# --------------------------------------------------------------------------


def test_normalize_tasks_mixed_and_aliases(tmp_path):
    out = swarm._normalize_tasks(
        [
            {"backend": "agy", "prompt": "a", "workspace": str(tmp_path)},
            {"backend": "Codex", "prompt": "b", "sandbox": "workspace-write", "model": "m"},
            {"backend": "gemini", "prompt": "c"},
        ]
    )
    assert [t["backend"] for t in out] == ["antigravity", "codex", "antigravity"]
    assert out[0]["workspace"] == os.path.abspath(str(tmp_path))
    assert out[1]["sandbox"] == "workspace-write" and out[1]["model"] == "m"
    # antigravity has no sandbox; with no model given, model stays None; workspace -> cwd
    assert out[2]["sandbox"] is None and out[2]["model"] is None
    assert out[2]["workspace"] == os.getcwd()


def test_normalize_tasks_codex_default_sandbox():
    import codex_bridge

    out = swarm._normalize_tasks([{"backend": "codex", "prompt": "x"}])
    assert out[0]["sandbox"] == codex_bridge.DEFAULT_SANDBOX


def test_normalize_tasks_antigravity_keeps_model_and_full_access(monkeypatch):
    """danger-full-access is agy's own posture said out loud: no plan mode."""
    import server

    monkeypatch.setattr(server, "list_agy_models", lambda: ["gemini-3.1-pro-high"])
    out = swarm._normalize_tasks(
        [
            {
                "backend": "antigravity",
                "prompt": "x",
                "sandbox": "danger-full-access",
                "model": "gemini-3.1-pro-high",
            }
        ]
    )
    assert out[0]["sandbox"] == "danger-full-access"
    assert out[0]["plan"] is False
    assert out[0]["model"] == "gemini-3.1-pro-high"


def test_normalize_tasks_antigravity_omitted_sandbox_stays_unrestricted(monkeypatch):
    """The long-standing default is deliberately left alone: flipping it would turn
    every existing file-writing swarm task into a plan document.
    """
    import server

    monkeypatch.setattr(server, "list_agy_models", lambda: [])
    out = swarm._normalize_tasks([{"backend": "antigravity", "prompt": "x"}])
    assert out[0]["sandbox"] is None and out[0]["plan"] is False


def test_normalize_tasks_antigravity_read_only_becomes_plan(monkeypatch):
    """The footgun this closes: `sandbox: "read-only"` on an agy task used to be
    silently ignored, so it ran completely unrestricted while reading as fenced.
    """
    import server

    monkeypatch.setattr(server, "list_agy_models", lambda: [])
    monkeypatch.setattr(server, "_AGY_PLAN_GATE", True)
    out = swarm._normalize_tasks(
        [{"backend": "antigravity", "prompt": "review this", "sandbox": "read-only"}]
    )
    assert out[0]["plan"] is True


def test_normalize_tasks_antigravity_default_env_plans_tasks_without_a_sandbox(monkeypatch):
    import server

    monkeypatch.setattr(server, "list_agy_models", lambda: [])
    monkeypatch.setattr(server, "_AGY_PLAN_GATE", True)
    monkeypatch.setenv("AGENT_INTERN_AGY_DEFAULT", "plan")
    out = swarm._normalize_tasks(
        [
            {"backend": "antigravity", "prompt": "a"},
            {"backend": "antigravity", "prompt": "b", "sandbox": "danger-full-access"},
        ]
    )
    assert out[0]["plan"] is True  # omitted -> the user's default
    assert out[1]["plan"] is False  # an explicit sandbox still wins


def test_normalize_tasks_antigravity_unknown_default_env_fails_the_swarm(monkeypatch):
    import server

    monkeypatch.setattr(server, "list_agy_models", lambda: [])
    monkeypatch.setenv("AGENT_INTERN_AGY_DEFAULT", "plann")
    with pytest.raises(ValueError, match="task 0: AGENT_INTERN_AGY_DEFAULT='plann'"):
        swarm._normalize_tasks([{"backend": "antigravity", "prompt": "a"}])


def test_normalize_tasks_antigravity_rejects_workspace_write(monkeypatch):
    """agy has no write scoping to offer — under its own --sandbox it still wrote
    outside the declared workspace — so accepting this would promise a fence that
    does not exist.
    """
    import server

    monkeypatch.setattr(server, "list_agy_models", lambda: [])
    with pytest.raises(ValueError, match="task 0: antigravity cannot scope writes"):
        swarm._normalize_tasks(
            [{"backend": "antigravity", "prompt": "x", "sandbox": "workspace-write"}]
        )


def test_normalize_tasks_antigravity_plan_checked_before_any_worker_spawns(monkeypatch):
    """Fail the whole swarm at normalization rather than N calls in: plan mode needs
    the agy version gate AND the slash guard that replaces --disable-slash-commands.
    """
    import server

    monkeypatch.setattr(server, "list_agy_models", lambda: [])
    monkeypatch.setattr(server, "_AGY_PLAN_GATE", True)
    with pytest.raises(ValueError, match="task 0: plan mode cannot run a prompt"):
        swarm._normalize_tasks(
            [{"backend": "antigravity", "prompt": "/schedule nightly", "sandbox": "read-only"}]
        )


def test_normalize_tasks_antigravity_plan_refused_on_old_agy(monkeypatch):
    import server

    monkeypatch.setattr(server, "list_agy_models", lambda: [])
    monkeypatch.setattr(server, "_AGY_PLAN_GATE", False)
    monkeypatch.setattr(server, "_get_agy_version", lambda: "1.1.11")
    with pytest.raises(ValueError, match="task 0: plan=True needs agy 1.1.12"):
        swarm._normalize_tasks([{"backend": "antigravity", "prompt": "x", "sandbox": "read-only"}])


def test_normalize_tasks_antigravity_plan_with_no_agy_names_the_install(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "AGY_BIN", str(tmp_path / "gone" / "agy.exe"))
    monkeypatch.setattr(server, "list_agy_models", lambda: [])
    monkeypatch.setattr(server, "_AGY_PLAN_GATE", False)
    monkeypatch.setattr(server, "_get_agy_version", lambda: None)
    with pytest.raises(ValueError, match="task 0: agy was not found") as info:
        swarm._normalize_tasks([{"backend": "antigravity", "prompt": "x", "sandbox": "read-only"}])
    assert "AGY_BIN" in str(info.value) and "needs agy" not in str(info.value)


def test_schema_gate_with_no_agy_names_the_install(monkeypatch, tmp_path):
    import proc_tree
    import server

    monkeypatch.setattr(server, "AGY_BIN", str(tmp_path / "gone" / "agy.exe"))
    monkeypatch.setattr(server, "_AGY_JSON_SUPPORT", False)
    monkeypatch.setattr(server, "_get_agy_version", lambda: None)
    with pytest.raises(proc_tree.CliNotFound, match="^agy was not found"):
        server._check_schema_support()


def test_version_gates_keep_their_message_when_agy_is_installed(monkeypatch):
    import server

    monkeypatch.setattr(server, "AGY_BIN", sys.executable)  # there, version unreadable
    monkeypatch.setattr(server, "_AGY_JSON_SUPPORT", False)
    monkeypatch.setattr(server, "_get_agy_version", lambda: None)
    with pytest.raises(ValueError, match=r"schema needs agy .*\(found unknown\)"):
        server._check_schema_support()


def test_a_swarm_worker_whose_cli_is_missing_reports_the_install_hint(monkeypatch, tmp_path):
    import codex_bridge

    def no_such_file(args, *a, **k):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(codex_bridge, "CODEX_BIN", str(tmp_path / "gone" / "codex.exe"))
    monkeypatch.setattr(subprocess, "Popen", no_such_file)
    results = swarm.swarm_agents(
        [{"backend": "codex", "prompt": "hi", "workspace": str(tmp_path)}], 1, 30, False
    )
    out = swarm.format_agent_results(results)
    assert "codex was not found" in out and "CODEX_BIN" in out and "WinError" not in out


def test_normalize_tasks_antigravity_rejects_unknown_model(monkeypatch):
    import server

    monkeypatch.setattr(server, "list_agy_models", lambda: ["gemini-3.5-flash-high"])
    with pytest.raises(ValueError, match="unknown agy model"):
        swarm._normalize_tasks([{"backend": "antigravity", "prompt": "x", "model": "Bogus 9000"}])


def test_normalize_tasks_bad_backend_raises():
    with pytest.raises(ValueError):
        swarm._normalize_tasks([{"backend": "llama", "prompt": "x"}])


def test_normalize_tasks_missing_prompt_raises():
    with pytest.raises(ValueError):
        swarm._normalize_tasks([{"backend": "codex", "prompt": "  "}])


def test_normalize_tasks_invalid_sandbox_raises():
    with pytest.raises(ValueError):
        swarm._normalize_tasks([{"backend": "codex", "prompt": "x", "sandbox": "yolo"}])


def test_normalize_tasks_non_list_and_non_dict_raise():
    with pytest.raises(ValueError):
        swarm._normalize_tasks("nope")
    with pytest.raises(ValueError):
        swarm._normalize_tasks([["not", "a", "dict"]])


def test_swarm_agents_dispatches_by_backend(monkeypatch):
    import server

    monkeypatch.setattr(server, "list_agy_models", lambda: ["M1"])
    calls = []

    def fake_text(index, prompt, workspace, model, timeout_s, plan=False):
        calls.append(("antigravity", index, prompt, model))
        return swarm.WorkerResult(index, True, answer="agy:" + prompt, workspace=workspace)

    def fake_codex(index, prompt, workspace, sandbox, model, timeout_s, effort=None):
        calls.append(("codex", index, prompt, sandbox, model))
        return swarm.WorkerResult(index, True, answer="cdx:" + prompt, workspace=workspace)

    monkeypatch.setattr(swarm, "_run_text_worker", fake_text)
    monkeypatch.setattr(swarm, "_run_codex_worker", fake_codex)

    results = swarm.swarm_agents(
        [
            {"backend": "antigravity", "prompt": "p0", "model": "M1"},
            {"backend": "codex", "prompt": "p1", "sandbox": "workspace-write", "model": "m"},
        ],
        max_concurrency=2,
        timeout_s=5,
        watch=False,
    )
    results.sort(key=lambda r: r.index)
    assert results[0].backend == "antigravity" and results[0].answer == "agy:p0"
    assert results[1].backend == "codex" and results[1].answer == "cdx:p1"
    agy = next(c for c in calls if c[0] == "antigravity")
    assert agy[3] == "M1"  # model threaded through to the text worker
    cdx = next(c for c in calls if c[0] == "codex")
    assert cdx[3] == "workspace-write" and cdx[4] == "m"


def test_swarm_agents_empty_returns_empty():
    assert swarm.swarm_agents([]) == []


def test_swarm_agents_fails_a_turned_off_backend_in_place(monkeypatch):
    # AGENT_INTERN_BACKENDS left copilot off: its worker fails without launching
    # anything, and the codex worker still answers, the way a preset with one
    # member not installed still runs the rest.
    monkeypatch.setattr(server, "ENABLED_BACKENDS", ("codex",))

    def fake_codex(index, prompt, workspace, sandbox, model, timeout_s, effort=None):
        return swarm.WorkerResult(index, True, answer="cdx:" + prompt, workspace=workspace)

    def no_copilot(*a, **k):
        raise AssertionError("a turned-off backend must not be launched")

    monkeypatch.setattr(swarm, "_run_codex_worker", fake_codex)
    monkeypatch.setattr(swarm, "_run_copilot_worker", no_copilot)

    results = swarm.swarm_agents(
        [{"backend": "codex", "prompt": "p0"}, {"backend": "copilot", "prompt": "p1"}],
        timeout_s=5,
    )
    results.sort(key=lambda r: r.index)
    assert results[0].ok and results[0].answer == "cdx:p0"
    assert not results[1].ok and results[1].backend == "copilot"
    assert "'copilot' is turned off by AGENT_INTERN_BACKENDS (on: codex)" in results[1].error


def test_swarm_agents_marks_a_turned_off_backend_failed_on_the_dashboard(monkeypatch):
    # Without the worker_finish, its card would sit at "queued" forever.
    monkeypatch.setattr(server, "ENABLED_BACKENDS", ("codex",))
    monkeypatch.setattr(swarm_watch, "open_window", lambda n: None)

    swarm.swarm_agents([{"backend": "copilot", "prompt": "p0"}], timeout_s=5, watch=True)
    (worker,) = swarm_watch._snapshot()["workers"]
    assert worker["status"] == "error"
    assert "turned off by AGENT_INTERN_BACKENDS" in worker["answer"]


def test_format_agent_results_tags_backend():
    results = [
        swarm.WorkerResult(
            0, True, answer="ok", elapsed=1.0, workspace="C:\\x\\repo", backend="codex"
        ),
        swarm.WorkerResult(1, False, error="boom", elapsed=0.0, backend="antigravity"),
    ]
    out = swarm.format_agent_results(results)
    assert "1/2 succeeded" in out
    assert "[worker 0 · codex]" in out and "[worker 1 · antigravity]" in out
    assert "ok" in out and "boom" in out


def test_watch_init_stores_backends():
    swarm_watch.init(
        ["p0", "p1"], ["r0", "r1"], 1.0, ["p0", "p1"], 180, backends=["antigravity", "codex"]
    )
    snap = swarm_watch._snapshot()
    assert snap["workers"][0]["backend"] == "antigravity"
    assert snap["workers"][1]["backend"] == "codex"


def test_watch_init_stores_models():
    swarm_watch.init(
        ["p0", "p1", "p2"], ["r"] * 3, 1.0, models=["gemini-4-hydrogen-high", "", None]
    )
    ws = swarm_watch._snapshot()["workers"]
    assert [w["model"] for w in ws] == ["gemini-4-hydrogen-high", "", ""]
    swarm_watch.init(["p0"], ["r"], 1.0)  # no models given: empty, not a guess
    assert swarm_watch._snapshot()["workers"][0]["model"] == ""


def test_watch_models_use_the_task_model_then_the_agy_default_for_antigravity_only(monkeypatch):
    monkeypatch.setattr(server, "_agy_settings_model", lambda: "Gemini 4 Argon (High)")
    tasks = [
        {"backend": "antigravity", "model": "gemini-4-hydrogen-high"},
        {"backend": "antigravity", "model": None},
        {"backend": "codex", "model": None},
        {"backend": "codex", "model": "gpt-5.5"},
    ]
    assert swarm._watch_models(tasks) == [
        "gemini-4-hydrogen-high",
        "Gemini 4 Argon (High)",
        "",
        "gpt-5.5",
    ]


def test_swarm_agents_puts_each_workers_model_on_the_dashboard(monkeypatch):
    monkeypatch.setattr(server, "ENABLED_BACKENDS", ("codex",))
    monkeypatch.setattr(swarm_watch, "open_window", lambda n: None)
    monkeypatch.setattr(
        swarm, "_run_codex_worker_watched", lambda *a, **k: swarm.WorkerResult(0, True)
    )
    swarm.swarm_agents(
        [{"backend": "codex", "prompt": "p0", "model": "gpt-5.5"}], timeout_s=5, watch=True
    )
    (worker,) = swarm_watch._snapshot()["workers"]
    assert worker["backend"] == "codex" and worker["model"] == "gpt-5.5"


def test_the_swarm_window_draws_the_model_chip_on_a_row_and_in_the_detail_header():
    assert 'el("span","mdl",w.model)' in swarm_watch._HTML
    assert "setAgent(w.backend,w.repo,w.model)" in swarm_watch._WORKER_HTML


def test_run_codex_worker_never_pins(monkeypatch):
    # Swarm codex workers are one-shot — they must call run_codex with pin=False.
    import codex_bridge

    seen = {}

    def fake_run(prompt, ws, sandbox, model, cont, t, pin=True, effort=None):
        seen["pin"] = pin
        return "ans:" + prompt

    monkeypatch.setattr(codex_bridge, "run_codex", fake_run)
    r = swarm._run_codex_worker(0, "hello", os.getcwd(), "read-only", None, 5)
    assert r.ok and r.answer == "ans:hello" and r.backend == "codex"
    assert seen["pin"] is False


def test_normalize_tasks_carries_effort_for_codex_and_claude(tmp_path):
    out = swarm._normalize_tasks(
        [
            {"backend": "codex", "prompt": "a", "workspace": str(tmp_path), "effort": " Low "},
            {"backend": "claude", "prompt": "b", "workspace": str(tmp_path), "effort": "MAX"},
            {"backend": "codex", "prompt": "c", "workspace": str(tmp_path)},
        ]
    )
    # codex's level is passed through as typed (stripped); claude's list is closed, so
    # it is lower-cased and checked.
    assert [t["effort"] for t in out] == ["Low", "max", None]


def test_normalize_tasks_rejects_a_bad_effort_naming_the_task(tmp_path):
    with pytest.raises(ValueError):
        swarm._normalize_tasks([{"backend": "claude", "prompt": "b", "effort": "turbo"}])
    with pytest.raises(ValueError):
        swarm._normalize_tasks([{"backend": "codex", "prompt": "b", "effort": 'x"y'}])


@pytest.mark.parametrize("backend", ["copilot", "cursor", "grok", "opencode", "muse"])
def test_normalize_tasks_refuses_effort_on_other_backends(backend):
    with pytest.raises(ValueError, match=rf"task 1: 'effort' is only supported.*{backend}"):
        swarm._normalize_tasks(
            [
                {"backend": "codex", "prompt": "ok"},
                {"backend": backend, "prompt": "p", "effort": "low"},
            ]
        )


def test_normalize_tasks_points_agy_at_the_model_name(monkeypatch):
    import server

    monkeypatch.setattr(server, "list_agy_models", lambda: ["gemini-4-argon-low"])
    with pytest.raises(ValueError, match="gemini-4-argon-low"):
        swarm._normalize_tasks([{"backend": "agy", "prompt": "p", "effort": "low"}])
    # An empty effort is no effort, on any backend.
    swarm._normalize_tasks([{"backend": "copilot", "prompt": "p", "effort": ""}])


def test_run_codex_worker_passes_effort_to_the_runner(monkeypatch):
    import codex_bridge

    seen = {}

    def fake_run(prompt, ws, sandbox, model, cont, t, pin=True, effort=None):
        seen["effort"] = effort
        return "ok"

    monkeypatch.setattr(codex_bridge, "run_codex", fake_run)
    assert swarm._run_codex_worker(0, "p", os.getcwd(), "read-only", None, 5, "high").ok
    assert seen["effort"] == "high"


def test_swarm_dispatch_hands_each_worker_its_own_effort(monkeypatch):
    seen = {}

    def fake_codex(index, prompt, workspace, sandbox, model, timeout_s, effort=None):
        seen[("codex", index)] = effort
        return swarm.WorkerResult(index, True, answer="c", workspace=workspace)

    def fake_claude(index, prompt, workspace, sandbox, model, timeout_s, effort=None):
        seen[("claude", index)] = effort
        return swarm.WorkerResult(index, True, answer="k", workspace=workspace)

    monkeypatch.setattr(swarm, "_run_codex_worker", fake_codex)
    monkeypatch.setattr(swarm, "_run_claude_worker", fake_claude)
    monkeypatch.setattr(server, "ENABLED_BACKENDS", ("codex", "claude"))
    swarm.swarm_agents(
        [
            {"backend": "codex", "prompt": "a", "effort": "low"},
            {"backend": "claude", "prompt": "b", "effort": "xhigh"},
        ],
        timeout_s=5,
    )
    assert seen == {("codex", 0): "low", ("claude", 1): "xhigh"}


def test_run_text_worker_exit0_without_transcript_surfaces_stderr(monkeypatch, tmp_path):
    """A swarm agy worker whose agy exits 0 but writes no readable transcript must
    surface agy's stderr (e.g. a 1.1.3 permission soft-deny) in the WorkerResult
    error, not a bare scrape failure that reads as a bridge bug.
    """
    denial = 'a tool required the "command" permission, so it was auto-denied'

    class _Done:
        returncode = 0
        stdout = ""
        stderr = denial

    monkeypatch.setattr(swarm.proc_tree, "run_captured", lambda *a, **k: _Done())
    # Fresh isolated HOME has no conversation, so _only_conv stays None and the
    # read loop runs to its deadline. Jump time.time() past the 5s deadline at once.
    times = iter([0.0] + [1e9 * i for i in range(1, 50)])
    monkeypatch.setattr(swarm.time, "time", lambda: next(times))
    monkeypatch.setattr(swarm.time, "sleep", lambda *a, **k: None)

    res = swarm._run_text_worker(0, "hi", str(tmp_path), None, 10)
    assert res.ok is False
    assert "no readable transcript" in res.error and "auto-denied" in res.error


_PRINT_TIMEOUT_STDERR = (
    "[agy] print timeout after 10s with turn in progress; returning partial output"
)
# A partial ANSWER that happens to contain a sign-in marker: the timeout message
# carries it, so it must never reach the auth-failure matcher.
_PARTIAL_ANSWER = "To reset it, please sign in to the admin pan"


def test_run_text_worker_print_timeout_fails_with_the_partial(monkeypatch, tmp_path):
    """agy 1.2.0+ exits 0 when --print-timeout cuts a turn; the worker must fail."""

    class _Cut:
        returncode = 0
        stdout = _PARTIAL_ANSWER
        stderr = _PRINT_TIMEOUT_STDERR

    monkeypatch.setattr(swarm.proc_tree, "run_captured", lambda *a, **k: _Cut())
    monkeypatch.setattr(server, "_run_agy", lambda *a, **k: pytest.fail("must not retry"))
    res = swarm._run_text_worker(0, "hi", str(tmp_path), None, 10)
    assert res.ok is False
    assert "10s timeout" in res.error and _PARTIAL_ANSWER in res.error
    assert swarm._ISOLATION_OK is True  # the answer's "please sign in" did not latch


_PRINT_TIMEOUT_STDERR_EXIT3 = (
    "[agy] print timeout after 5s with turn in progress; returning partial output\n"
    'AGY_ERROR: {"status":"DEADLINE_EXCEEDED","reason":"print_timeout"}\n'
)


def test_run_text_worker_print_timeout_exit_3_fails_with_the_partial(monkeypatch, tmp_path):
    """agy 1.5.0+ exits 3 on an expired --print-timeout: a failed result, not a raise."""

    class _Cut:
        returncode = 3
        stdout = _PARTIAL_ANSWER
        stderr = _PRINT_TIMEOUT_STDERR_EXIT3

    monkeypatch.setattr(swarm.proc_tree, "run_captured", lambda *a, **k: _Cut())
    monkeypatch.setattr(server, "_run_agy", lambda *a, **k: pytest.fail("must not retry"))
    res = swarm._run_text_worker(0, "hi", str(tmp_path), None, 5)
    assert res.ok is False
    assert "5s timeout" in res.error and _PARTIAL_ANSWER in res.error
    assert "agy exited" not in res.error
    assert swarm._ISOLATION_OK is True  # the answer's "please sign in" did not latch


def test_run_text_worker_exit_3_without_a_timeout_mark_stays_the_generic_error(
    monkeypatch, tmp_path
):
    class _Fail:
        returncode = 3
        stdout = '{"status":"ERROR"}'
        stderr = 'AGY_ERROR: {"status":"INTERNAL"}'

    monkeypatch.setattr(swarm.proc_tree, "run_captured", lambda *a, **k: _Fail())
    res = swarm._run_text_worker(0, "hi", str(tmp_path), None, 5)
    assert res.ok is False
    assert "agy exited 3" in res.error and "INTERNAL" in res.error
    assert "hit its" not in res.error


def test_run_text_worker_watched_print_timeout_exit_3_fails_with_the_partial(monkeypatch, tmp_path):
    class _Popen:
        returncode = 3

        def __init__(self, *a, **k):
            self.stdout = io.StringIO(_PARTIAL_ANSWER)
            self.stderr = io.StringIO(_PRINT_TIMEOUT_STDERR_EXIT3)

        def poll(self):
            return 3

    finished = []
    monkeypatch.setattr(swarm.proc_tree, "popen", lambda *a, **k: _Popen())
    monkeypatch.setattr(server, "_run_agy", lambda *a, **k: pytest.fail("must not retry"))
    monkeypatch.setattr(swarm_watch, "worker_update", lambda i, **kw: None)
    monkeypatch.setattr(
        swarm_watch, "worker_finish", lambda i, s, a, e, **kw: finished.append((s, a))
    )
    res = swarm._run_text_worker_watched(0, "hi", str(tmp_path), None, 5)
    assert res.ok is False
    assert "5s timeout" in res.error and _PARTIAL_ANSWER in res.error
    assert "agy exited" not in res.error
    assert finished == [("error", res.error)]
    assert swarm._ISOLATION_OK is True


def test_run_text_worker_watched_exit_3_without_a_timeout_mark_stays_generic(monkeypatch, tmp_path):
    class _Popen:
        returncode = 3

        def __init__(self, *a, **k):
            self.stdout = io.StringIO('{"status":"ERROR"}')
            self.stderr = io.StringIO('AGY_ERROR: {"status":"INTERNAL"}')

        def poll(self):
            return 3

    monkeypatch.setattr(swarm.proc_tree, "popen", lambda *a, **k: _Popen())
    monkeypatch.setattr(swarm_watch, "worker_update", lambda i, **kw: None)
    monkeypatch.setattr(swarm_watch, "worker_finish", lambda *a, **kw: None)
    res = swarm._run_text_worker_watched(0, "hi", str(tmp_path), None, 5)
    assert res.ok is False
    assert "agy exited 3" in res.error and "timeout" not in res.error


def test_run_text_worker_watched_print_timeout_fails_with_the_partial(monkeypatch, tmp_path):
    class _Popen:
        returncode = 0

        def __init__(self, *a, **k):
            self.stdout = io.StringIO(_PARTIAL_ANSWER)
            self.stderr = io.StringIO(_PRINT_TIMEOUT_STDERR)

        def poll(self):
            return 0

    finished = []
    monkeypatch.setattr(swarm.proc_tree, "popen", lambda *a, **k: _Popen())
    monkeypatch.setattr(server, "_run_agy", lambda *a, **k: pytest.fail("must not retry"))
    monkeypatch.setattr(swarm_watch, "worker_update", lambda i, **kw: None)
    monkeypatch.setattr(
        swarm_watch, "worker_finish", lambda i, s, a, e, **kw: finished.append((s, a))
    )
    res = swarm._run_text_worker_watched(0, "hi", str(tmp_path), None, 10)
    assert res.ok is False and _PARTIAL_ANSWER in res.error
    assert finished == [("error", res.error)]
    assert swarm._ISOLATION_OK is True


# --------------------------------------------------------------------------
# HOME-isolation auth fallback (issue #2: macOS hides agy's credentials from a
# worker whose HOME is redirected, so it starts a fresh OAuth flow and times out)
# --------------------------------------------------------------------------

# The failure exactly as reported, stderr and all.
_AUTH_FAILURE_TEXT = (
    "agy exited 1: ...&state=...\nWaiting for authentication (timeout 60s)...\n"
    "Or, paste the authorization code here and press Enter:\n"
    "Error: authentication timed out."
)
_USAGE_TSV = "Gemini Models\tWeekly Limit Remaining\t100%\t2026-08-11T18:50:23Z\n"


def test_looks_like_auth_failure_matches_the_reported_text():
    assert swarm._looks_like_auth_failure(_AUTH_FAILURE_TEXT)
    assert swarm._looks_like_auth_failure("Error: NOT AUTHENTICATED")


def test_looks_like_auth_failure_ignores_unrelated_errors():
    assert not swarm._looks_like_auth_failure("agy exited 1: rate limited, try again")
    assert not swarm._looks_like_auth_failure(None)


def test_isolation_ok_env_override_skips_the_probe(monkeypatch):
    monkeypatch.setattr(swarm, "_ISOLATION_OK", None)
    monkeypatch.setenv("AGY_BRIDGE_NO_HOME_ISOLATION", "1")
    monkeypatch.setattr(swarm, "_probe_isolated_auth", lambda: pytest.fail("must not probe"))
    assert swarm.isolation_ok() is False


@pytest.mark.parametrize("probed,expected", [(True, True), (False, False), (None, True)])
def test_isolation_ok_from_probe(monkeypatch, probed, expected):
    # An ambiguous probe (None) must keep the parallel path — the reactive
    # fallback still covers a real auth failure.
    monkeypatch.setattr(swarm, "_ISOLATION_OK", None)
    monkeypatch.delenv("AGY_BRIDGE_NO_HOME_ISOLATION", raising=False)
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        return probed

    monkeypatch.setattr(swarm, "_probe_isolated_auth", probe)
    assert swarm.isolation_ok() is expected
    assert swarm.isolation_ok() is expected
    assert calls["n"] == 1  # resolved once per process


@pytest.mark.parametrize("osname,follows", [("nt", False), ("posix", True)])
def test_credential_store_follows_home_by_platform(monkeypatch, osname, follows):
    # Safe to patch os.name here only because this function builds no Path — doing
    # so around pathlib re-points it at the wrong flavour and raises.
    monkeypatch.setattr(swarm.os, "name", osname)
    assert swarm._credential_store_follows_home() is follows


def test_probe_skipped_where_home_cannot_hide_auth(monkeypatch):
    # Windows' credential store is HOME-independent, so the probe would burn
    # seconds per process on a question that cannot fail there.
    monkeypatch.setattr(swarm, "_credential_store_follows_home", lambda: False)
    monkeypatch.setattr(
        swarm.proc_tree, "run_captured", lambda *a, **k: pytest.fail("must not spawn")
    )
    assert swarm._probe_isolated_auth() is None


def test_probe_none_when_agy_cannot_answer_usage(monkeypatch):
    monkeypatch.setattr(swarm, "_credential_store_follows_home", lambda: True)
    monkeypatch.setattr(server, "supports_print_usage", lambda: False)
    monkeypatch.setattr(
        swarm.proc_tree, "run_captured", lambda *a, **k: pytest.fail("must not spawn")
    )
    assert swarm._probe_isolated_auth() is None


def _probe_env(monkeypatch, result):
    monkeypatch.setattr(swarm, "_credential_store_follows_home", lambda: True)
    monkeypatch.setattr(server, "supports_print_usage", lambda: True)
    monkeypatch.setattr(swarm.proc_tree, "run_captured", result)


def test_probe_true_on_a_real_quota_table(monkeypatch):
    import subprocess as sp

    _probe_env(
        monkeypatch,
        lambda *a, **k: sp.CompletedProcess(a, 0, stdout=_USAGE_TSV, stderr=""),
    )
    assert swarm._probe_isolated_auth() is True


def test_probe_false_on_timeout(monkeypatch):
    import subprocess as sp

    def boom(*a, **k):
        raise sp.TimeoutExpired(cmd="agy", timeout=25)

    _probe_env(monkeypatch, boom)
    assert swarm._probe_isolated_auth() is False  # sat in agy's sign-in wait


def test_probe_false_on_auth_signature(monkeypatch):
    import subprocess as sp

    _probe_env(
        monkeypatch,
        lambda *a, **k: sp.CompletedProcess(a, 1, stdout="", stderr=_AUTH_FAILURE_TEXT),
    )
    assert swarm._probe_isolated_auth() is False


def test_probe_none_on_an_unrelated_failure(monkeypatch):
    import subprocess as sp

    _probe_env(
        monkeypatch,
        lambda *a, **k: sp.CompletedProcess(a, 1, stdout="", stderr="network unreachable"),
    )
    assert swarm._probe_isolated_auth() is None


def test_text_worker_uses_serialized_path_when_isolation_is_off(monkeypatch, tmp_path):
    monkeypatch.setattr(swarm, "_ISOLATION_OK", False)
    monkeypatch.setattr(swarm, "_make_isolated_home", lambda: pytest.fail("must not isolate"))
    seen = {}

    def fake_run_agy(prompt, ws, cont, timeout_s, model=None, plan=False, pin=True):
        seen.update(prompt=prompt, ws=ws, model=model, plan=plan, pin=pin)
        return "serial answer"

    monkeypatch.setattr(server, "_run_agy", fake_run_agy)
    res = swarm._run_text_worker(0, "hi", str(tmp_path), "gemini-3.1-pro-high", 10)
    assert res.ok and res.answer == "serial answer"
    # pin=False: a swarm worker must not claim the workspace's continue slot.
    assert seen["pin"] is False and seen["model"] == "gemini-3.1-pro-high"


def test_text_worker_retries_serialized_after_an_auth_failure(monkeypatch, tmp_path):
    """The reactive half of the fix: the worker recovers instead of just failing."""

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = _AUTH_FAILURE_TEXT

    monkeypatch.setattr(swarm.proc_tree, "run_captured", lambda *a, **k: _Failed())
    monkeypatch.setattr(server, "_run_agy", lambda *a, **k: "recovered")
    res = swarm._run_text_worker(0, "hi", str(tmp_path), None, 10)
    assert res.ok and res.answer == "recovered"
    # ...and the process latches serialized mode, so worker 2..N skip the failure.
    assert swarm._ISOLATION_OK is False


def test_text_worker_plan_reaches_the_isolated_argv(monkeypatch, tmp_path):
    """The parallel path builds its OWN argv rather than going through _run_agy, so
    plan mode has to be threaded to it explicitly or a fenced task runs unfenced.
    """
    seen = {}

    class _Failed:  # fail after argv capture; the read path is not what's under test
        returncode = 1
        stdout = ""
        stderr = "boom"

    def fake_run(args, **kwargs):
        seen["args"] = args
        return _Failed()

    monkeypatch.setattr(swarm.proc_tree, "run_captured", fake_run)
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", True)  # the shield plan mode replaces
    swarm._run_text_worker(0, "review this", str(tmp_path), None, 10, True)
    argv = seen["args"]
    assert argv[argv.index("--mode") + 1] == "plan"
    assert "--disable-slash-commands" not in argv


def test_text_worker_without_plan_keeps_the_slash_shield(monkeypatch, tmp_path):
    seen = {}

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(
        swarm.proc_tree, "run_captured", lambda args, **k: (seen.update(args=args), _Failed())[1]
    )
    monkeypatch.setattr(server, "_AGY_SLASH_GATE", True)
    swarm._run_text_worker(0, "hi", str(tmp_path), None, 10)
    assert "--mode" not in seen["args"]
    assert "--disable-slash-commands" in seen["args"]


def test_text_worker_serialized_forwards_plan(monkeypatch, tmp_path):
    """The macOS fallback path must carry the fence too, not just the isolated one."""
    seen = {}

    def fake_run_agy(prompt, ws, cont, timeout_s, model=None, plan=False, pin=True):
        seen.update(plan=plan, pin=pin)
        return "answer"

    monkeypatch.setattr(server, "_run_agy", fake_run_agy)
    res = swarm._run_text_worker_serialized(0, "hi", str(tmp_path), None, 10, 0.0, True)
    assert res.ok and seen["plan"] is True and seen["pin"] is False


def test_text_worker_does_not_retry_other_failures(monkeypatch, tmp_path):
    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "agy exited 1: quota exhausted"

    monkeypatch.setattr(swarm.proc_tree, "run_captured", lambda *a, **k: _Failed())
    monkeypatch.setattr(server, "_run_agy", lambda *a, **k: pytest.fail("must not retry"))
    res = swarm._run_text_worker(0, "hi", str(tmp_path), None, 10)
    assert res.ok is False and "quota exhausted" in res.error
    assert swarm._ISOLATION_OK is True  # unrelated failures don't latch


# Issue #7, on the swarm's paths: agy's `/usage` with the Gemini 4 Argon family empty.
_USAGE_ARGON_EMPTY = (
    "Gemini Models\tWeekly Limit Remaining\t91%\t2026-11-09T13:12:12Z\n"
    "Gemini 4 Argon\tGemini 4 Argon Weekly Limit\t0%\t2026-11-04T13:34:06Z\n"
)


@pytest.fixture
def argon_empty(monkeypatch):
    monkeypatch.setattr(server, "supports_print_usage", lambda: True)
    monkeypatch.setattr(server, "_read_agy_usage", lambda: _USAGE_ARGON_EMPTY)
    monkeypatch.setattr(
        server, "list_agy_models", lambda: ["gemini-4-argon-high", "gemini-3.8-flash-high"]
    )
    monkeypatch.setattr(server, "_agy_settings_model", lambda: "Gemini 4 Argon (High)")


class _QuotaFailed:
    returncode = 1
    stdout = ""
    stderr = "Error: Individual quota reached, resets in ~75h\n"


def test_text_worker_quota_failure_says_which_family_is_out(monkeypatch, tmp_path, argon_empty):
    monkeypatch.setattr(swarm.proc_tree, "run_captured", lambda *a, **k: _QuotaFailed())
    monkeypatch.setattr(server, "_run_agy", lambda *a, **k: pytest.fail("must not retry"))
    res = swarm._run_text_worker(0, "hi", str(tmp_path), "gemini-4-argon-high", 10)
    assert res.ok is False and "Individual quota reached" in res.error
    assert "that family is out of quota" in res.error
    assert 'model="gemini-3.8-flash-high"' in res.error
    assert swarm._ISOLATION_OK is True  # a quota note must not read as an auth failure


def test_image_worker_quota_failure_is_not_no_image_produced(monkeypatch, tmp_path, argon_empty):
    # This path used to swallow every non-auth exit and fail with "no image file
    # produced", which hides that the account is simply out of quota.
    target = str(tmp_path / "out.png")
    monkeypatch.setattr(swarm.proc_tree, "run_captured", lambda *a, **k: _QuotaFailed())
    res = swarm._run_image_worker(0, "draw", target, str(tmp_path), 10)
    assert res.ok is False and "Individual quota reached" in res.error
    assert "agy's default model (Gemini 4 Argon (High))" in res.error
    assert swarm._ISOLATION_OK is True


def test_image_worker_auth_failure_retries_serialized(monkeypatch, tmp_path):
    class _Failed:
        returncode = 1
        stdout = ""
        stderr = _AUTH_FAILURE_TEXT

    target = str(tmp_path / "out.png")
    monkeypatch.setattr(swarm.proc_tree, "run_captured", lambda *a, **k: _Failed())
    monkeypatch.setattr(server, "_run_agy", lambda *a, **k: "")
    monkeypatch.setattr(server, "_finalize_image", lambda t, txt, start: (target, "png", 123))
    res = swarm._run_image_worker(0, "draw", target, str(tmp_path), 10)
    assert res.ok and res.image_path == target and res.image_format == "png"
    assert swarm._ISOLATION_OK is False


def test_watched_serialized_worker_reports_progress_and_a_note(monkeypatch, tmp_path):
    # No isolated transcript to pump, so the viewer must be told why there are no
    # live steps instead of showing an empty panel that reads as a hung worker.
    monkeypatch.setattr(swarm, "_ISOLATION_OK", False)
    monkeypatch.setattr(server, "_run_agy", lambda *a, **k: "serial answer")
    appended, finished = [], []
    monkeypatch.setattr(swarm_watch, "worker_append", lambda i, lines: appended.extend(lines))
    monkeypatch.setattr(swarm_watch, "worker_update", lambda i, **kw: None)
    monkeypatch.setattr(
        swarm_watch, "worker_finish", lambda i, s, a, e, **kw: finished.append((s, a))
    )
    res = swarm._run_text_worker_watched(0, "hi", str(tmp_path), None, 10)
    assert res.ok and res.answer == "serial answer"
    assert finished == [("done", "serial answer")]
    assert any("serialized" in ln["text"] for ln in appended)


def test_normalize_tasks_copilot_default_sandbox_and_aliases():
    import copilot_bridge

    out = swarm._normalize_tasks(
        [
            {"backend": "copilot", "prompt": "a"},
            {"backend": "gh", "prompt": "b", "sandbox": "workspace-write", "model": "gpt-5"},
        ]
    )
    assert [t["backend"] for t in out] == ["copilot", "copilot"]
    assert out[0]["sandbox"] == copilot_bridge.DEFAULT_SANDBOX
    assert out[1]["sandbox"] == "workspace-write" and out[1]["model"] == "gpt-5"


def test_normalize_tasks_copilot_invalid_sandbox_raises():
    with pytest.raises(ValueError):
        swarm._normalize_tasks([{"backend": "copilot", "prompt": "x", "sandbox": "yolo"}])


def test_swarm_agents_dispatches_copilot(monkeypatch):
    calls = []

    def fake_copilot(index, prompt, workspace, sandbox, model, timeout_s):
        calls.append(("copilot", index, prompt, sandbox, model))
        return swarm.WorkerResult(index, True, answer="cop:" + prompt, workspace=workspace)

    monkeypatch.setattr(swarm, "_run_copilot_worker", fake_copilot)

    results = swarm.swarm_agents(
        [{"backend": "copilot", "prompt": "p0", "sandbox": "workspace-write", "model": "m"}],
        max_concurrency=2,
        timeout_s=5,
        watch=False,
    )
    assert results[0].backend == "copilot" and results[0].answer == "cop:p0"
    cop = next(c for c in calls if c[0] == "copilot")
    assert cop[3] == "workspace-write" and cop[4] == "m"


def test_run_copilot_worker_never_pins(monkeypatch):
    # Swarm copilot workers are one-shot — they must call run_copilot with pin=False.
    import copilot_bridge

    seen = {}

    def fake_run(prompt, ws, sandbox, model, cont, t, pin=True):
        seen["pin"] = pin
        return "ans:" + prompt

    monkeypatch.setattr(copilot_bridge, "run_copilot", fake_run)
    r = swarm._run_copilot_worker(0, "hello", os.getcwd(), "read-only", None, 5)
    assert r.ok and r.answer == "ans:hello" and r.backend == "copilot"
    assert seen["pin"] is False


# --------------------------------------------------------------------------
# Image workers when agy cuts the turn at --print-timeout (exit 3 on 1.5.0+, exit 0
# before): no image + the timeout mark = a timeout error, not "no image file found"
# --------------------------------------------------------------------------

_IMG_TIMEOUT_STDERR = (
    "[agy] print timeout after 5s with turn in progress; returning partial output\n"
    'AGY_ERROR: {"status":"DEADLINE_EXCEEDED","reason":"print_timeout"}\n'
)


def _png_at(path):
    with open(path, "wb") as f:
        f.write(_PNG_HEAD)
    return str(path)


def test_finalize_or_timeout_reports_the_timeout_when_no_image_was_produced(tmp_path):
    with pytest.raises(RuntimeError) as exc:
        swarm._finalize_or_timeout(
            tmp_path, str(tmp_path / "nope.png"), None, 0.0, _IMG_TIMEOUT_STDERR, 5
        )
    msg = str(exc.value)
    assert "agy hit its 5s timeout" in msg and "Raise timeout_s" in msg
    assert msg.endswith(")") and "(" in msg  # the underlying "no image" error rides along


def test_finalize_or_timeout_re_raises_the_original_without_the_mark(tmp_path):
    target = str(tmp_path / "nope.png")
    with pytest.raises(RuntimeError) as plain:
        swarm._finalize_image_isolated(tmp_path, target, None, 0.0)
    with pytest.raises(RuntimeError) as wrapped:
        swarm._finalize_or_timeout(tmp_path, target, None, 0.0, "some other stderr", 5)
    assert str(wrapped.value) == str(plain.value)
    assert "hit its" not in str(wrapped.value)


def test_finalize_or_timeout_returns_an_image_written_before_the_cut(tmp_path):
    target = _png_at(tmp_path / "out.png")
    final, fmt, size = swarm._finalize_or_timeout(
        tmp_path, target, None, 0.0, _IMG_TIMEOUT_STDERR, 5
    )
    assert fmt == "PNG" and final.endswith(".png") and size > 0


class _ImgRun:
    def __init__(self, returncode, stderr, stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


@pytest.mark.parametrize("returncode", [3, 0])
def test_image_worker_no_image_after_a_print_timeout_says_timeout(
    monkeypatch, tmp_path, returncode
):
    target = str(tmp_path / "out.png")
    run = _ImgRun(returncode, _IMG_TIMEOUT_STDERR)
    monkeypatch.setattr(swarm.proc_tree, "run_captured", lambda *a, **k: run)
    res = swarm._run_image_worker(0, "draw", target, str(tmp_path), 5)
    assert res.ok is False
    assert "agy hit its 5s timeout" in res.error and "Raise timeout_s" in res.error
    assert swarm._ISOLATION_OK is True  # a timeout is not a sign-in failure


@pytest.mark.parametrize("returncode", [3, 0])
def test_image_worker_keeps_an_image_written_before_the_cut(monkeypatch, tmp_path, returncode):
    target = _png_at(tmp_path / "out.png")
    run = _ImgRun(returncode, _IMG_TIMEOUT_STDERR)
    monkeypatch.setattr(swarm.proc_tree, "run_captured", lambda *a, **k: run)
    res = swarm._run_image_worker(0, "draw", target, str(tmp_path), 5)
    assert res.ok is True and res.image_path == target and res.image_format == "PNG"


def test_image_worker_exit_3_without_the_mark_is_still_a_missing_image(monkeypatch, tmp_path):
    target = str(tmp_path / "out.png")
    run = _ImgRun(3, 'AGY_ERROR: {"status":"INTERNAL"}')
    monkeypatch.setattr(swarm.proc_tree, "run_captured", lambda *a, **k: run)
    res = swarm._run_image_worker(0, "draw", target, str(tmp_path), 5)
    assert res.ok is False
    assert "hit its" not in res.error


def _watched_popen(returncode, stderr):
    class _Popen:
        def __init__(self, *a, **k):
            self.returncode = returncode
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO(stderr)

        def poll(self):
            return self.returncode

    return _Popen


@pytest.fixture
def quiet_swarm_watch(monkeypatch):
    finished = []
    monkeypatch.setattr(swarm_watch, "worker_update", lambda i, **kw: None)
    monkeypatch.setattr(swarm_watch, "worker_append", lambda i, lines: None)
    monkeypatch.setattr(
        swarm_watch, "worker_finish", lambda i, s, a, e, **kw: finished.append((s, a))
    )
    return finished


@pytest.mark.parametrize("returncode", [3, 0])
def test_watched_image_worker_no_image_after_a_print_timeout_says_timeout(
    monkeypatch, tmp_path, quiet_swarm_watch, returncode
):
    target = str(tmp_path / "out.png")
    monkeypatch.setattr(
        swarm.proc_tree, "popen", lambda *a, **k: _watched_popen(returncode, _IMG_TIMEOUT_STDERR)()
    )
    res = swarm._run_image_worker_watched(0, "draw", target, str(tmp_path), 5)
    assert res.ok is False
    assert "agy hit its 5s timeout" in res.error
    assert quiet_swarm_watch == [("error", res.error)]
    assert swarm._ISOLATION_OK is True


def test_watched_image_worker_keeps_an_image_written_before_the_cut(
    monkeypatch, tmp_path, quiet_swarm_watch
):
    target = _png_at(tmp_path / "out.png")
    monkeypatch.setattr(
        swarm.proc_tree, "popen", lambda *a, **k: _watched_popen(3, _IMG_TIMEOUT_STDERR)()
    )
    res = swarm._run_image_worker_watched(0, "draw", target, str(tmp_path), 5)
    assert res.ok is True and res.image_path == target
    assert quiet_swarm_watch[0][0] == "done"


def test_serialized_image_worker_keeps_the_agy_error_next_to_the_finalize_error(
    monkeypatch, tmp_path
):
    target = str(tmp_path / "out.png")

    def agy_fails(*a, **k):
        raise RuntimeError("agy hit its 5s timeout before finishing")

    def no_image(t, txt, start):
        raise RuntimeError("no image file produced")

    monkeypatch.setattr(server, "_run_agy", agy_fails)
    monkeypatch.setattr(server, "_finalize_image", no_image)
    res = swarm._run_image_worker_serialized(0, "draw", target, str(tmp_path), 5, 0.0)
    assert res.ok is False
    assert res.error == (
        "no image file produced (agy also failed: agy hit its 5s timeout before finishing)"
    )


def test_serialized_image_worker_without_an_agy_error_reports_only_the_finalize_error(
    monkeypatch, tmp_path
):
    target = str(tmp_path / "out.png")

    def no_image(t, txt, start):
        raise RuntimeError("no image file produced")

    monkeypatch.setattr(server, "_run_agy", lambda *a, **k: "")
    monkeypatch.setattr(server, "_finalize_image", no_image)
    res = swarm._run_image_worker_serialized(0, "draw", target, str(tmp_path), 5, 0.0)
    assert res.error == "no image file produced"


def test_serialized_image_worker_still_returns_an_image_when_agy_failed(monkeypatch, tmp_path):
    target = str(tmp_path / "out.png")

    def agy_fails(*a, **k):
        raise RuntimeError("agy hit its 5s timeout")

    monkeypatch.setattr(server, "_run_agy", agy_fails)
    monkeypatch.setattr(server, "_finalize_image", lambda t, txt, start: (target, "png", 9))
    res = swarm._run_image_worker_serialized(0, "draw", target, str(tmp_path), 5, 0.0)
    assert res.ok is True and res.image_path == target


# --------------------------------------------------------------------------
# progress: how many workers have finished
# --------------------------------------------------------------------------


def _fake_codex_answering(monkeypatch, delays=None):
    def fake_codex(index, prompt, workspace, sandbox, model, timeout_s, effort=None):
        if delays:
            time.sleep(delays[index])
        return swarm.WorkerResult(index, True, answer="cdx:" + prompt, workspace=workspace)

    monkeypatch.setattr(swarm, "_run_codex_worker", fake_codex)


def test_swarm_agents_reports_each_finished_worker(monkeypatch):
    # The slowest worker is index 0, so completion order differs from index order: the
    # count still climbs by one per worker.
    _fake_codex_answering(monkeypatch, delays=[0.15, 0.0, 0.05])
    seen = []
    results = swarm.swarm_agents(
        [{"backend": "codex", "prompt": f"p{i}"} for i in range(3)],
        max_concurrency=3,
        on_progress=lambda done, total: seen.append((done, total)),
    )
    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]
    assert [r.answer for r in results] == ["cdx:p0", "cdx:p1", "cdx:p2"]  # still in index order


def test_swarm_agents_counts_a_failed_worker_as_finished(monkeypatch):
    monkeypatch.setattr(server, "ENABLED_BACKENDS", ("codex",))
    _fake_codex_answering(monkeypatch)
    seen = []
    swarm.swarm_agents(
        [{"backend": "codex", "prompt": "p0"}, {"backend": "copilot", "prompt": "p1"}],
        on_progress=lambda done, total: seen.append(done),
    )
    assert seen[-1] == 2


def test_a_progress_listener_that_raises_does_not_fail_the_swarm(monkeypatch):
    _fake_codex_answering(monkeypatch)

    def boom(done, total):
        raise RuntimeError("listener bug")

    results = swarm.swarm_agents([{"backend": "codex", "prompt": "p0"}], on_progress=boom)
    assert results[0].ok and results[0].answer == "cdx:p0"


def test_swarm_image_reports_each_finished_worker(monkeypatch, tmp_path):
    def fake_image(index, prompt, target, workspace, timeout_s):
        return swarm.WorkerResult(index, True, image_path=target, image_format="png", image_size=1)

    monkeypatch.setattr(swarm, "_run_image_worker", fake_image)
    seen = []
    swarm.swarm_image(
        ["a", "b"],
        [str(tmp_path / "a.png"), str(tmp_path / "b.png")],
        workspaces=str(tmp_path),
        on_progress=lambda done, total: seen.append((done, total)),
    )
    assert seen == [(0, 2), (1, 2), (2, 2)]


def test_swarm_image_gives_every_worker_its_own_free_target(monkeypatch, tmp_path):
    (tmp_path / "a.png").write_bytes(b"already here")
    got = {}

    def fake_image(index, prompt, target, workspace, timeout_s):
        got[index] = target
        return swarm.WorkerResult(index, True, image_path=target, image_format="png", image_size=1)

    monkeypatch.setattr(swarm, "_run_image_worker", fake_image)
    same = str(tmp_path / "a.png")
    swarm.swarm_image(["x", "y", "z"], [same, same, str(tmp_path / "b.png")], str(tmp_path))
    assert got == {
        0: str(tmp_path / "a-1.png"),
        1: str(tmp_path / "a-2.png"),
        2: str(tmp_path / "b.png"),
    }


def test_swarm_image_targets_differing_only_in_extension_get_distinct_stems(monkeypatch, tmp_path):
    # agy may write JPEG bytes to a.png, which is then renamed a.jpg: the same name
    # worker 1 was promised. Names are compared by stem across image extensions.
    got = {}

    def fake_image(index, prompt, target, workspace, timeout_s):
        got[index] = target
        return swarm.WorkerResult(index, True, image_path=target, image_format="png", image_size=1)

    monkeypatch.setattr(swarm, "_run_image_worker", fake_image)
    paths = [str(tmp_path / n) for n in ("a.png", "a.jpg", "a.JPEG", "a.webp", "a.txt")]
    swarm.swarm_image(["p"] * len(paths), paths, str(tmp_path))
    assert got == {
        0: str(tmp_path / "a.png"),
        1: str(tmp_path / "a-1.jpg"),
        2: str(tmp_path / "a-2.JPEG"),
        3: str(tmp_path / "a-3.webp"),
        4: str(tmp_path / "a.txt"),  # not an image name: compared in full
    }


class _Ctx:
    """The part of fastmcp's Context the progress helper uses."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def report_progress(self, progress, total=None, message=None):
        if self.fail:
            raise RuntimeError("client went away")
        self.calls.append((progress, total, message))


def test_run_with_worker_progress_sends_finished_over_total():
    def run(on_progress):
        for done in range(0, 4):
            on_progress(done, 3)
            time.sleep(0.05)
        return "answer"

    ctx = _Ctx()
    assert asyncio.run(swarm.run_with_worker_progress(ctx, run)) == "answer"
    msgs = [m for _, _, m in ctx.calls]
    assert any(m.startswith("3/3 workers done") for m in msgs)
    assert all(total == 3.0 for _, total, _ in ctx.calls)
    assert ctx.calls[-1][0] == 3.0  # the last report is exactly "all done", never past it


def test_run_with_worker_progress_only_ever_increases(monkeypatch):
    # A quiet swarm sends heartbeats; each must be larger than the last, and none may
    # reach the next whole worker before it has finished.
    monkeypatch.setattr(swarm, "_PROGRESS_HEARTBEAT_S", 0.03)

    def run(on_progress):
        on_progress(0, 2)
        time.sleep(0.3)
        on_progress(1, 2)
        time.sleep(0.2)
        on_progress(2, 2)
        return "x"

    ctx = _Ctx()
    asyncio.run(swarm.run_with_worker_progress(ctx, run))
    values = [p for p, _, _ in ctx.calls]
    assert len(values) >= 4
    assert values == sorted(values) and len(set(values)) == len(values)
    first_done = next(i for i, (_, _, m) in enumerate(ctx.calls) if m.startswith("1/2"))
    assert all(v < 1.0 for v in values[:first_done])


def test_run_with_worker_progress_without_a_ctx_is_a_plain_threaded_call():
    got = []

    def run(on_progress):
        got.append(on_progress)
        return "plain"

    assert asyncio.run(swarm.run_with_worker_progress(None, run)) == "plain"
    assert got == [None]


def test_run_with_worker_progress_survives_a_client_that_rejects_progress():
    def run(on_progress):
        on_progress(0, 1)
        time.sleep(0.05)
        on_progress(1, 1)
        return "still done"

    assert asyncio.run(swarm.run_with_worker_progress(_Ctx(fail=True), run)) == "still done"


def test_run_with_worker_progress_propagates_the_swarms_error():
    def run(on_progress):
        raise ValueError("tasks must be a list")

    with pytest.raises(ValueError, match="tasks must be a list"):
        asyncio.run(swarm.run_with_worker_progress(_Ctx(), run))


def test_the_swarm_tools_are_async_and_keep_ctx_out_of_their_schema():
    import antigravity_tools  # noqa: F401 - registers antigravity_image_swarm
    import swarm_tools  # noqa: F401

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("agent_swarm", "preset_swarm", "antigravity_image_swarm"):
        assert "ctx" not in tools[name].parameters["properties"], name
        assert inspect.iscoroutinefunction(
            getattr(tools[name], "fn", None) or getattr(server, name)
        )


def test_agent_swarm_reports_progress_through_the_mcp_client(monkeypatch):
    # The whole seam: a real MCP client with a progress handler, the registered tool,
    # the swarm underneath it. Only the workers are faked.
    import fastmcp

    _fake_codex_answering(monkeypatch, delays=[0.2, 0.0])
    seen = []

    async def handler(progress, total, message):
        seen.append((progress, total, message))

    async def go():
        async with fastmcp.Client(server.mcp, progress_handler=handler) as client:
            res = await client.call_tool(
                "agent_swarm",
                {
                    "tasks": [
                        {"backend": "codex", "prompt": "a"},
                        {"backend": "codex", "prompt": "b"},
                    ]
                },
            )
        return res.content[0].text

    text = asyncio.run(go())
    assert "2/2 succeeded" in text and "cdx:a" in text
    assert seen, "the swarm sent no progress"
    assert seen[-1][1] == 2.0 and seen[-1][2].startswith("2/2 workers done")


# --------------------------------------------------------------------------
# cancel: cancelling the tool call stops the agent processes the swarm started
# --------------------------------------------------------------------------


@pytest.mark.parametrize("with_ctx", [False, True], ids=["no-ctx", "with-ctx"])
def test_cancelling_a_swarm_kills_every_worker_process(monkeypatch, with_ctx):
    """End to end with real (python) children: the tool's asyncio task is cancelled
    while pool threads wait on their processes, and the processes die instead of
    running to their timeout. This is also the proof that pool threads see the call:
    without it nothing would be registered under it."""
    started = threading.Barrier(3, timeout=60)  # two workers + the test
    procs = []

    def fake_codex(index, prompt, workspace, sandbox, model, timeout_s, effort=None):
        proc = proc_tree.popen(
            [sys.executable, "-c", "import time; time.sleep(180)"],
            start_new_session=os.name != "nt",  # kill_tree's POSIX branch needs its group
        )
        procs.append(proc)
        started.wait()
        proc.wait()
        return swarm.WorkerResult(index, False, error="killed", workspace=workspace)

    monkeypatch.setattr(swarm, "_run_codex_worker", fake_codex)
    ctx = _Ctx() if with_ctx else None

    def run(on_progress):
        results = swarm.swarm_agents(
            [{"backend": "codex", "prompt": f"p{i}"} for i in range(2)],
            max_concurrency=2,
            on_progress=on_progress,
        )
        return swarm.format_agent_results(results)

    async def scenario():
        task = asyncio.ensure_future(swarm.run_with_worker_progress(ctx, run))
        await asyncio.to_thread(started.wait)
        assert len(proc_tree.live_processes()) == 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    t0 = time.monotonic()
    try:
        asyncio.run(scenario())  # returns only once the worker thread is free
        for proc in procs:
            assert proc.wait(timeout=60) is not None
        assert time.monotonic() - t0 < 120  # well short of the children's 180 s
        assert proc_tree.live_processes() == []
    finally:
        for proc in procs:  # never leave a sleeper behind if an assert failed
            if proc.poll() is None:
                proc_tree.kill_tree(proc)


def test_a_swarm_that_is_not_cancelled_leaves_nothing_registered_as_cancelled(monkeypatch):
    # a finished call must not poison the next one: each run gets its own Call
    _fake_codex_answering(monkeypatch)

    def run(on_progress):
        return swarm.format_agent_results(
            swarm.swarm_agents([{"backend": "codex", "prompt": "a"}], on_progress=on_progress)
        )

    for _ in range(2):
        assert "1/1 succeeded" in asyncio.run(swarm.run_with_worker_progress(None, run))
    assert proc_tree.CURRENT_CALL.get() is None  # the caller's context is left as it was


# --------------------------------------------------------------------------
# output budget: a long answer goes to a file, the reply keeps an excerpt and the path
# --------------------------------------------------------------------------


def _result(i, size, ok=True, backend="codex", char="x"):
    text = f"start-{i} " + char * size + f" end-{i}"
    if ok:
        return swarm.WorkerResult(i, True, answer=text, backend=backend, elapsed=1.5)
    return swarm.WorkerResult(i, False, error=text, backend=backend, elapsed=1.5)


def test_budget_leaves_a_swarm_that_fits_alone_and_writes_nothing(_runs_in_tmp):
    results = [_result(0, 5000), _result(1, 100)]
    assert swarm.budget_results(results) is results
    assert not _runs_in_tmp.exists()


def test_a_long_answer_becomes_an_excerpt_and_a_path(_runs_in_tmp):
    long_ = _result(1, swarm.WORKER_OUTPUT_CAP + 5000)
    results = [_result(0, 300), long_]
    out = swarm.format_agent_results(results)
    assert len(out) < swarm.WORKER_OUTPUT_CAP
    assert "start-1" in out and "end-1" in out  # the head and the tail survive
    assert "characters omitted" in out
    (run_dir,) = list(_runs_in_tmp.iterdir())
    saved = run_dir / "worker-1.md"
    assert str(saved) in out
    assert saved.read_text(encoding="utf-8").endswith(long_.answer + "\n")
    assert "# worker 1 · codex · OK" in saved.read_text(encoding="utf-8")
    assert not (run_dir / "worker-0.md").exists()  # the short one stays inline
    assert results[1].answer == long_.answer  # the caller's results are not changed


def test_the_runs_directory_is_under_the_users_home_never_the_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(swarm.Path, "home", classmethod(lambda cls: tmp_path))
    assert _REAL_RUNS_ROOT() == tmp_path / ".agent-intern" / "runs"


def test_many_answers_under_the_worker_cap_still_respect_the_total_cap(_runs_in_tmp):
    # Five answers of 12,000 characters each are fine one by one and 60,000 together.
    results = [_result(i, 12_000) for i in range(5)]
    out = swarm.format_agent_results(results)
    assert len(out) <= swarm.TOTAL_OUTPUT_CAP
    spilled = {p.name for p in next(_runs_in_tmp.iterdir()).iterdir()}
    assert 0 < len(spilled) < 5  # only as many as it takes, the rest stay whole
    for i in range(5):
        assert f"end-{i}" in out  # every worker's conclusion is still in the reply


def test_a_huge_swarm_shrinks_the_excerpts_to_fit(_runs_in_tmp):
    results = [_result(i, 20_000) for i in range(30)]
    out = swarm.format_agent_results(results)
    assert len(out) <= swarm.TOTAL_OUTPUT_CAP
    assert len(list(next(_runs_in_tmp.iterdir()).iterdir())) == 30


def test_a_long_error_is_budgeted_too(_runs_in_tmp):
    out = swarm.format_agent_results([_result(0, swarm.WORKER_OUTPUT_CAP + 100, ok=False)])
    assert "(failed) start-0" in out and "worker-0.md" in out


def test_an_unwritable_runs_directory_degrades_to_an_excerpt(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is wanted")
    monkeypatch.setattr(swarm, "runs_root", lambda: blocker / "runs")
    out = swarm.format_agent_results([_result(0, swarm.WORKER_OUTPUT_CAP + 100)])
    assert "the full text could not be saved" in out
    assert len(out) < swarm.WORKER_OUTPUT_CAP


def test_old_run_directories_are_pruned_and_other_entries_left_alone(_runs_in_tmp):
    _runs_in_tmp.mkdir(parents=True)
    old = _runs_in_tmp / "20200101-000000-abcdef"
    fresh = _runs_in_tmp / "20991231-235959-abcdef"
    other = _runs_in_tmp / "my-notes"
    for d in (old, fresh, other):
        d.mkdir()
        (d / "keep.txt").write_text("x")
    ancient = time.time() - (swarm.RUNS_KEEP_DAYS + 1) * 86400
    for d in (old, other):
        os.utime(d, (ancient, ancient))
    swarm.format_agent_results([_result(0, swarm.WORKER_OUTPUT_CAP + 1)])
    assert not old.exists()
    assert fresh.exists() and other.exists()


# ----------------------------------------------- viewer security headers and profile cleanup
def _viewer_get(port, path, host=None):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
        r = conn.getresponse()
        r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}
    finally:
        conn.close()


def _assert_security_headers(headers):
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    csp = headers["content-security-policy"]
    for part in (
        "default-src 'none'",
        "script-src 'unsafe-inline'",
        "connect-src 'self'",
        "img-src 'self' data:",
        "frame-ancestors 'none'",
        "base-uri 'none'",
    ):
        assert part in csp


def test_swarm_viewer_responses_carry_security_headers():
    port = swarm_watch.ensure_server()
    k = server._WATCH_TOKEN
    for path in (f"/?k={k}", f"/events?k={k}", f"/worker?k={k}", f"/image?p=nope&k={k}"):
        status, headers = _viewer_get(port, path)
        assert status in (200, 404)
        _assert_security_headers(headers)
    # a refused request carries them too
    status, headers = _viewer_get(port, "/events?k=wrong")
    assert status == 403
    _assert_security_headers(headers)


def test_single_run_viewer_responses_carry_security_headers():
    import watch_server

    port = watch_server._ensure_watch_server()
    k = server._WATCH_TOKEN
    for path in (f"/?k={k}", f"/events?id=main&k={k}", f"/image?p=nope&k={k}"):
        status, headers = _viewer_get(port, path)
        assert status in (200, 404)
        _assert_security_headers(headers)
    status, headers = _viewer_get(port, "/events?id=main&k=wrong")
    assert status == 403
    _assert_security_headers(headers)


def test_the_policy_still_allows_what_the_pages_do():
    """The pages use an inline script, fetch their own origin and show data:/same-origin
    images; the policy must not forbid any of that, and the pages must not load anything else."""
    import re

    import watch_ui

    csp = dict(
        (p.split(" ", 1)[0], p.split(" ", 1)[1])
        for p in dict(watch_ui.SECURITY_HEADERS)["Content-Security-Policy"].split("; ")
    )
    assert "'unsafe-inline'" in csp["script-src"] and "'unsafe-inline'" in csp["style-src"]
    for html in (swarm_watch._HTML, swarm_watch._WORKER_HTML, server._watch_html()):
        assert not re.search(r"<script[^>]+src=|<link[^>]+href=|@import|<iframe", html)
        assert not re.search(r"""fetch\(["']https?:""", html)


def _make_profile(root, name, age_s):
    d = root / name
    d.mkdir()
    (d / "Local State").write_text("x")
    old = time.time() - age_s
    os.utime(d, (old, old))
    return d


def test_old_viewer_profiles_are_pruned_at_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(swarm_watch.tempfile, "gettempdir", lambda: str(tmp_path))
    old = _make_profile(tmp_path, "agy_chrome_old", 2 * 86400)
    fresh = _make_profile(tmp_path, "agy_chrome_fresh", 60)
    other = _make_profile(tmp_path, "someone_elses_dir", 5 * 86400)
    swarm_watch._prune_old_profiles()
    assert not old.exists()
    assert fresh.exists() and other.exists()


def test_a_locked_viewer_profile_is_skipped_quietly(monkeypatch, tmp_path):
    monkeypatch.setattr(swarm_watch.tempfile, "gettempdir", lambda: str(tmp_path))
    held = _make_profile(tmp_path, "agy_chrome_held", 3 * 86400)
    gone = _make_profile(tmp_path, "agy_chrome_gone", 3 * 86400)
    monkeypatch.setattr(swarm_watch, "_profile_in_use", lambda p: p.endswith("held"))
    swarm_watch._prune_old_profiles()
    assert held.exists() and not gone.exists()


def test_profile_in_use_sees_an_unremovable_lockfile(monkeypatch, tmp_path):
    (tmp_path / "lockfile").write_text("")

    def deny(path):
        raise PermissionError(path)

    monkeypatch.setattr(swarm_watch.os, "remove", deny)
    assert swarm_watch._profile_in_use(str(tmp_path)) is True
    monkeypatch.undo()
    assert (
        swarm_watch._profile_in_use(str(tmp_path)) is False
    )  # removable lockfile: nobody holds it


def test_new_profile_is_tracked_and_removed_at_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(swarm_watch.tempfile, "gettempdir", lambda: str(tmp_path))
    real_mkdtemp = swarm_watch.tempfile.mkdtemp
    monkeypatch.setattr(
        swarm_watch.tempfile, "mkdtemp", lambda prefix: real_mkdtemp(prefix=prefix, dir=tmp_path)
    )
    monkeypatch.setattr(swarm_watch, "_OWN_PROFILES", [])
    registered = []
    monkeypatch.setattr(swarm_watch.atexit, "register", registered.append)
    monkeypatch.setattr(swarm_watch, "_CLEANUP_REGISTERED", False)
    prof = swarm_watch._new_profile()
    swarm_watch._new_profile()
    assert os.path.isdir(prof) and os.path.basename(prof).startswith("agy_chrome_")
    assert registered == [swarm_watch._cleanup_own_profiles]  # once, not per window
    swarm_watch._cleanup_own_profiles()
    assert not os.path.exists(prof) and swarm_watch._OWN_PROFILES == []
