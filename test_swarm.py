"""Offline unit tests for the pure logic in swarm.py and swarm_watch.py.

Like test_server.py these use temp fixtures and never invoke agy, so they cost no
AI Pro quota. The live parallel round-trip is in test_smoke.py.

    pytest test_swarm.py
"""

import os

import pytest

import server
import swarm
import swarm_watch


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
    yield
    swarm._ISOLATION_OK = None


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
    out = swarm._normalize_workspaces(2, [str(a), str(b)])
    assert out == [os.path.abspath(str(a)), os.path.abspath(str(b))]


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

    def fake_codex(index, prompt, workspace, sandbox, model, timeout_s):
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


def test_run_codex_worker_never_pins(monkeypatch):
    # Swarm codex workers are one-shot — they must call run_codex with pin=False.
    import codex_bridge

    seen = {}

    def fake_run(prompt, ws, sandbox, model, cont, t, pin=True):
        seen["pin"] = pin
        return "ans:" + prompt

    monkeypatch.setattr(codex_bridge, "run_codex", fake_run)
    r = swarm._run_codex_worker(0, "hello", os.getcwd(), "read-only", None, 5)
    assert r.ok and r.answer == "ans:hello" and r.backend == "codex"
    assert seen["pin"] is False


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
