"""Offline unit tests for the pure logic in kimi_bridge.py.

Like test_cursor.py these monkeypatch subprocess and never invoke `kimi`, so they
cost no Kimi quota and pass whether or not kimi is installed (CI has none). The
live round-trip is DEFERRED — this backend is experimental and was built without
an authenticated Kimi, so there is no smoke coverage of the happy path yet.

    pytest test_kimi.py
"""

import os

import pytest

import kimi_bridge
import server


class _Proc:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run(record=None, *, returncode=0, stdout="", stderr=""):
    """A subprocess.run replacement that records argv/kwargs and returns a _Proc."""

    def run(*a, **k):
        if record is not None:
            record["argv"] = a[0] if a else k.get("args")
            record["kwargs"] = k
        return _Proc(returncode=returncode, stdout=stdout, stderr=stderr)

    return run


# --------------------------------------------------------------------------
# normalize_workspace
# --------------------------------------------------------------------------


def test_normalize_workspace_none_is_cwd():
    assert kimi_bridge.normalize_workspace(None) == os.getcwd()


def test_normalize_workspace_abspath(tmp_path):
    assert kimi_bridge.normalize_workspace(str(tmp_path)) == os.path.abspath(str(tmp_path))


# --------------------------------------------------------------------------
# validate_model — lenient pass-through (Kimi has no enumerable model list)
# --------------------------------------------------------------------------


def test_validate_model_strips():
    assert kimi_bridge.validate_model("  k2  ") == "k2"


def test_validate_model_blank_is_none():
    assert kimi_bridge.validate_model("") is None
    assert kimi_bridge.validate_model("   ") is None
    assert kimi_bridge.validate_model(None) is None


# --------------------------------------------------------------------------
# build_args — the argv shape, and the load-bearing "no --auto/--yolo" invariant
# --------------------------------------------------------------------------


def test_build_args_fresh_basic():
    args = kimi_bridge.build_args("hello", "C:\\ws", None, False)
    assert args[0] == kimi_bridge.KIMI_BIN
    assert "-c" not in args  # fresh, not continue
    assert "-m" not in args  # no model
    assert args[args.index("-p") + 1] == "hello"  # prompt is the -p VALUE
    assert args[args.index("--output-format") + 1] == "text"


def test_build_args_never_passes_auto_or_yolo():
    # VERIFIED on kimi 0.29.1: `-p` rejects --auto AND --yolo ("Cannot combine
    # --prompt with ...") because print mode already auto-approves. This invariant
    # is the whole reason the bridge passes neither — guard it.
    for cont in (False, True):
        for model in (None, "k2"):
            args = kimi_bridge.build_args("p", "ws", model, cont)
            assert "--auto" not in args
            assert "--yolo" not in args


def test_build_args_continue_adds_dash_c():
    args = kimi_bridge.build_args("p", "ws", None, True)
    assert "-c" in args


def test_build_args_with_model():
    args = kimi_bridge.build_args("p", "ws", "k2", False)
    assert args[args.index("-m") + 1] == "k2"


def test_build_args_continue_and_model_together():
    args = kimi_bridge.build_args("p", "ws", "kimi-for-coding", True)
    assert "-c" in args
    assert args[args.index("-m") + 1] == "kimi-for-coding"
    assert args[args.index("-p") + 1] == "p"


# --------------------------------------------------------------------------
# run_kimi — stdout answer, cwd, error paths (subprocess mocked)
# --------------------------------------------------------------------------


def test_run_kimi_returns_stdout_answer(tmp_path, monkeypatch):
    rec = {}
    monkeypatch.setattr(kimi_bridge.proc_tree, "run_captured", _fake_run(rec, stdout="PONG\n"))
    out = kimi_bridge.run_kimi("hi", str(tmp_path))
    assert out == "PONG"
    assert rec["kwargs"].get("cwd") == str(tmp_path)  # runs rooted at the workspace
    argv = rec["argv"]
    assert "--auto" not in argv and "--yolo" not in argv
    assert argv[argv.index("-p") + 1] == "hi"


def test_run_kimi_continue_passes_dash_c(tmp_path, monkeypatch):
    rec = {}
    monkeypatch.setattr(kimi_bridge.proc_tree, "run_captured", _fake_run(rec, stdout="ok"))
    kimi_bridge.run_kimi("hi", str(tmp_path), continue_conv=True)
    assert "-c" in rec["argv"]


def test_run_kimi_raises_on_nonzero_with_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(
        kimi_bridge.proc_tree,
        "run_captured",
        _fake_run(returncode=1, stderr="failed to run prompt: No model configured"),
    )
    with pytest.raises(RuntimeError, match="No model configured"):
        kimi_bridge.run_kimi("hi", str(tmp_path))


def test_run_kimi_raises_on_empty_stdout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        kimi_bridge.proc_tree, "run_captured", _fake_run(returncode=0, stdout="   ")
    )
    with pytest.raises(RuntimeError, match="no output"):
        kimi_bridge.run_kimi("hi", str(tmp_path))


# --------------------------------------------------------------------------
# diagnostics — version, auth_status, status_rows
# --------------------------------------------------------------------------


def test_kimi_version_first_line(monkeypatch):
    monkeypatch.setattr(kimi_bridge.proc_tree, "run_captured", _fake_run(stdout="0.29.1\n"))
    assert kimi_bridge.kimi_version() == "0.29.1"


def test_kimi_version_none_on_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("not found")

    monkeypatch.setattr(kimi_bridge.proc_tree, "run_captured", boom)
    assert kimi_bridge.kimi_version() is None


def test_auth_status_no_providers_is_not_ok(monkeypatch):
    # Verified unauthed on 0.29.1: `kimi provider list` -> "No providers configured."
    monkeypatch.setattr(
        kimi_bridge.proc_tree, "run_captured", _fake_run(stdout="No providers configured.")
    )
    ok, detail = kimi_bridge.auth_status()
    assert ok is False
    assert "no providers" in detail.lower()


def test_auth_status_configured_is_ok(monkeypatch):
    monkeypatch.setattr(
        kimi_bridge.proc_tree, "run_captured", _fake_run(stdout="kimi (managed) — 3 models")
    )
    ok, detail = kimi_bridge.auth_status()
    assert ok is True
    assert "kimi" in detail.lower()


def test_auth_status_error_is_not_ok(monkeypatch):
    def boom(*a, **k):
        raise OSError()

    monkeypatch.setattr(kimi_bridge.proc_tree, "run_captured", boom)
    ok, _ = kimi_bridge.auth_status()
    assert ok is False


def test_status_rows_shape(monkeypatch):
    monkeypatch.setattr(kimi_bridge, "kimi_version", lambda: "0.29.1")
    monkeypatch.setattr(kimi_bridge, "auth_status", lambda: (False, "no providers configured"))
    rows = kimi_bridge.status_rows()
    assert [r[0] for r in rows] == ["kimi CLI", "kimi auth", "data dir"]
    assert rows[0][1] is True  # version present
    assert rows[1][1] is False  # not authed


def test_status_rows_version_missing(monkeypatch):
    monkeypatch.setattr(kimi_bridge, "kimi_version", lambda: None)
    monkeypatch.setattr(kimi_bridge, "auth_status", lambda: (False, "x"))
    rows = kimi_bridge.status_rows()
    assert rows[0][1] is False
    assert "not found" in rows[0][2]


# --------------------------------------------------------------------------
# server wiring — the three MCP tools exist and status renders
# --------------------------------------------------------------------------


def test_server_exposes_kimi_tools():
    for name in ("kimi_ask", "kimi_continue", "kimi_status"):
        assert hasattr(server, name)


def test_kimi_status_renders(monkeypatch):
    monkeypatch.setattr(
        kimi_bridge,
        "status_rows",
        lambda: [("kimi CLI", True, "0.29.1"), ("kimi auth", False, "no providers")],
    )
    monkeypatch.setattr(
        server, "_bridge_version_status", lambda: ("bridge version", True, "v0.0.0")
    )
    out = server.kimi_status()
    assert "kimi bridge status" in out
    assert "Overall: PROBLEMS FOUND" in out  # auth row False -> not all ok
