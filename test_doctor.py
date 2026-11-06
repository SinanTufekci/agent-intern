"""Offline tests for `agent-intern --version` / `agent-intern doctor` (doctor.py)
and for how server.main() dispatches between the terminal commands and the stdio
server.

Nothing here starts a real CLI: doctor's probe seam (`doctor._backend_rows`) and
the bridge-row seam (`server._bridge_status_rows`) are patched.

    pytest test_doctor.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import doctor
import server
from conftest import fake_os_name

REPO = Path(__file__).parent

BRIDGE_ROWS = [("bridge version", True, "v0.0.0 (up to date)")]


def _rows(cli_label, *, cli=True, extra=()):
    return [(cli_label, cli, "1.2.3" if cli else "not found"), *extra]


# --------------------------------------------------------------------------- #
# server.main() dispatch
# --------------------------------------------------------------------------- #


@pytest.fixture
def served(monkeypatch):
    """Replace the three calls the server path makes; record that they ran."""
    calls = []
    monkeypatch.setattr(server, "_configure_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(server, "_startup_checks", lambda: calls.append("checks"))
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: calls.append("run"))
    return calls


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_main_version_prints_and_returns(flag, served, capsys):
    server.main([flag])  # returns normally, no SystemExit
    out = capsys.readouterr()
    assert out.out == f"agent-intern {server.__version__}\n"
    assert served == []  # the server never started


@pytest.mark.parametrize("argv", [[], ["--bogus"], ["something", "else"]])
def test_main_without_a_terminal_command_starts_the_server_silently(
    argv, served, capsys, monkeypatch
):
    # The server path must not even import doctor, and must not write to stdout:
    # stdout is the JSON-RPC channel.
    monkeypatch.delitem(sys.modules, "doctor", raising=False)
    server.main(argv)
    assert served == ["logging", "checks", "run"]
    assert capsys.readouterr().out == ""
    assert "doctor" not in sys.modules


def test_main_none_reads_sys_argv(served, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["agent-intern", "--version"])
    server.main()
    assert capsys.readouterr().out.strip() == f"agent-intern {server.__version__}"
    assert served == []


def test_main_only_the_first_argument_counts(served, capsys):
    # "--version" later on the line is not the version flag.
    server.main(["--bogus", "--version"])
    assert served == ["logging", "checks", "run"]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("cmd", ["doctor", "help", "--help", "-h"])
def test_main_terminal_commands_exit_through_doctor(cmd, served, monkeypatch):
    seen = []
    monkeypatch.setattr(doctor, "cli", lambda argv: seen.append(argv) or 7)
    with pytest.raises(SystemExit) as exc:
        server.main([cmd, "--json"])
    assert exc.value.code == 7
    assert seen == [[cmd, "--json"]]
    assert served == []


def test_version_flag_in_a_real_process():
    # The seam the unit tests cannot show: the installed entry point's own
    # stdout, with a fresh interpreter.
    r = subprocess.run(
        [sys.executable, str(REPO / "server.py"), "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO,
    )
    assert r.returncode == 0
    assert r.stdout == f"agent-intern {server.__version__}\n"


# --------------------------------------------------------------------------- #
# assess()
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,cli_label",
    [
        ("antigravity", "agy CLI"),
        ("codex", "codex CLI"),
        ("copilot", "copilot CLI"),
        ("cursor", "cursor CLI"),
        ("grok", "grok CLI"),
        ("opencode", "opencode CLI"),
        ("kimi", "kimi CLI"),
        ("muse", "muse CLI"),
    ],
)
def test_assess_missing_cli_is_unusable_with_install_hint(name, cli_label):
    state, summary, nxt, blocked = doctor.assess(name, _rows(cli_label, cli=False))
    assert state == doctor.UNUSABLE
    assert blocked == cli_label
    assert summary == f"{cli_label}: not found"
    assert "install" in nxt


def test_assess_no_cli_row_at_all_is_unusable():
    state, summary, _, blocked = doctor.assess("codex", [("codex auth", True, "ok")])
    assert state == doctor.UNUSABLE
    assert blocked == "codex CLI"
    assert "no CLI check reported" in summary


@pytest.mark.parametrize(
    "name,auth_label",
    [
        ("codex", "codex auth"),
        ("copilot", "copilot auth"),
        ("cursor", "cursor auth"),
        ("grok", "grok auth"),
        ("kimi", "kimi auth"),
        ("muse", "muse auth"),
        ("opencode", "credentials"),
    ],
)
def test_assess_failed_auth_is_unusable_with_login_hint(name, auth_label):
    cli_label = doctor._CHECKS[name][0]
    rows = _rows(cli_label, extra=[(auth_label, False, "not signed in")])
    state, summary, nxt, blocked = doctor.assess(name, rows)
    assert state == doctor.UNUSABLE
    assert blocked == auth_label
    assert summary == f"{auth_label}: not signed in"
    assert nxt  # a login step


def test_assess_all_ok_is_ready_and_summarises_the_cli_row():
    rows = _rows("codex CLI", extra=[("codex auth", True, "signed in")])
    assert doctor.assess("codex", rows) == (doctor.READY, "1.2.3", "", None)


def test_assess_other_failed_row_is_only_a_warning():
    rows = _rows(
        "codex CLI",
        extra=[("codex auth", True, "signed in"), ("sessions dir", False, "missing")],
    )
    assert doctor.assess("codex", rows) == (doctor.WARNING, "1.2.3", "", None)


def test_assess_agy_has_no_auth_row_requirement():
    state, *_ = doctor.assess("antigravity", _rows("agy CLI"))
    assert state == doctor.READY


def test_assess_agy_all_quota_families_empty_is_unusable():
    rows = _rows(
        "agy CLI",
        extra=[("quota: Gemini 4 Argon", False, "0%"), ("quota: Flash", False, "0%")],
    )
    state, _, nxt, blocked = doctor.assess("antigravity", rows)
    assert state == doctor.UNUSABLE
    assert blocked == "quota"
    assert nxt


def test_assess_agy_one_family_with_quota_left_is_usable():
    rows = _rows(
        "agy CLI",
        extra=[("quota: Gemini 4 Argon", False, "0%"), ("quota: Flash", True, "80%")],
    )
    state, _, _, blocked = doctor.assess("antigravity", rows)
    assert state == doctor.WARNING  # a failed row, but not a blocking one
    assert blocked is None


def test_assess_agy_failed_transcript_is_unusable():
    rows = _rows("agy CLI", extra=[("newest transcript", False, "unreadable")])
    state, _, nxt, blocked = doctor.assess("antigravity", rows)
    assert state == doctor.UNUSABLE
    assert blocked == "newest transcript"
    assert "agent-intern" in nxt


def test_assess_quota_rows_do_not_block_other_backends():
    rows = _rows("codex CLI", extra=[("quota: x", False, "0%")])
    state, *_ = doctor.assess("codex", rows)
    assert state == doctor.WARNING


# --------------------------------------------------------------------------- #
# report() / render()
# --------------------------------------------------------------------------- #


def _data(**per_backend):
    return doctor.report(BRIDGE_ROWS, per_backend)


def test_report_shape_and_usable_count():
    data = _data(
        codex=_rows("codex CLI", extra=[("codex auth", True, "ok")]),
        grok=_rows("grok CLI", cli=False),
    )
    assert set(data) == {"version", "python", "platform", "bridge", "backends", "usable"}
    assert data["version"] == server.__version__
    assert data["bridge"] == [
        {"label": "bridge version", "ok": True, "detail": "v0.0.0 (up to date)"}
    ]
    assert [b["name"] for b in data["backends"]] == ["codex", "grok"]  # order kept
    assert data["usable"] == 1
    codex, grok = data["backends"]
    assert set(codex) == {"name", "state", "usable", "summary", "next", "blocked_by", "rows"}
    assert codex["usable"] is True and grok["usable"] is False
    assert grok["blocked_by"] == "grok CLI"
    assert codex["rows"][0] == {"label": "codex CLI", "ok": True, "detail": "1.2.3"}
    json.dumps(data)  # plain data


def test_report_crash_row_is_unusable_and_says_to_report_it():
    data = _data(codex=[("codex status", False, "status check crashed: boom")])
    b = data["backends"][0]
    assert b["state"] == doctor.UNUSABLE
    assert "report" in b["next"]
    assert data["usable"] == 0


def test_render_layout():
    data = _data(
        codex=_rows("codex CLI", extra=[("codex auth", True, "ok")]),
        grok=_rows("grok CLI", cli=False),
    )
    text = doctor.render(data)
    lines = text.splitlines()
    assert lines[0].startswith(f"agent-intern {server.__version__} (Python ")
    assert any(line.startswith("bridge version") for line in lines[1:3])
    assert any(line.split() == ["backend", "state", "detail"] for line in lines)
    assert any(line.startswith("codex") and "ready" in line for line in lines)
    assert any(line.startswith("grok") and "unusable" in line for line in lines)
    assert "next: install grok" in text
    assert "1 of 2 backend(s) usable." in text


def test_render_hides_echo_rows_when_cli_is_missing():
    rows = _rows("codex CLI", cli=False, extra=[("codex auth", False, "cannot check")])
    text = doctor.render(_data(codex=rows))
    assert "cannot check" not in text
    assert "[!!]" not in text.split("backend", 1)[1]


def test_render_shows_non_blocking_failures_and_verbose_shows_all():
    rows = _rows(
        "codex CLI",
        extra=[("codex auth", True, "signed in"), ("sessions dir", False, "missing")],
    )
    data = _data(codex=rows)
    quiet = doctor.render(data)
    assert "[!!] sessions dir: missing" in quiet
    assert "[ok]" not in quiet
    loud = doctor.render(data, verbose=True)
    assert "[ok] codex CLI: 1.2.3" in loud
    assert "[ok] codex auth: signed in" in loud
    assert "[!!] sessions dir: missing" in loud


def test_render_agy_quota_blocking_rows_are_not_repeated():
    rows = _rows("agy CLI", extra=[("quota: A", False, "0%"), ("quota: B", False, "0%")])
    text = doctor.render(_data(antigravity=rows))
    assert "every agy quota family is at 0%" in text
    assert "quota: A" not in text  # only the summary, until --verbose
    assert "quota: A" in doctor.render(_data(antigravity=rows), verbose=True)


# --------------------------------------------------------------------------- #
# cli(): exit codes, output channel, probe crashes
# --------------------------------------------------------------------------- #


@pytest.fixture
def probes(monkeypatch):
    """Patch doctor's two seams. Set `table` to {backend: rows or Exception}."""
    table = {}

    def backend_rows(name):
        v = table[name]
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(doctor, "_backend_rows", backend_rows)
    monkeypatch.setattr(server, "_bridge_status_rows", lambda: BRIDGE_ROWS)
    monkeypatch.setattr(server, "_configure_logging", lambda: None)
    return table


def _enable(monkeypatch, *names):
    monkeypatch.setattr(server, "ENABLED_BACKENDS", tuple(names))


def test_cli_exit_0_when_one_backend_usable(probes, monkeypatch, capsys):
    _enable(monkeypatch, "codex", "grok")
    probes["codex"] = _rows("codex CLI", extra=[("codex auth", True, "ok")])
    probes["grok"] = _rows("grok CLI", cli=False)
    assert doctor.cli(["doctor"]) == 0
    assert "1 of 2 backend(s) usable." in capsys.readouterr().out


def test_cli_exit_1_when_none_usable(probes, monkeypatch, capsys):
    _enable(monkeypatch, "codex", "grok")
    probes["codex"] = _rows("codex CLI", cli=False)
    probes["grok"] = _rows("grok CLI", cli=False)
    assert doctor.cli(["doctor"]) == 1
    assert "0 of 2 backend(s) usable." in capsys.readouterr().out


def test_cli_checks_only_enabled_backends_in_order(probes, monkeypatch, capsys):
    _enable(monkeypatch, "opencode", "codex")
    probes["opencode"] = _rows("opencode CLI", extra=[("credentials", True, "ok")])
    probes["codex"] = _rows("codex CLI", extra=[("codex auth", True, "ok")])
    assert doctor.cli(["doctor", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [b["name"] for b in data["backends"]] == ["opencode", "codex"]


def test_cli_json_is_exactly_the_report_dict(probes, monkeypatch, capsys):
    _enable(monkeypatch, "codex")
    probes["codex"] = _rows("codex CLI", extra=[("codex auth", True, "ok")])
    assert doctor.cli(["doctor", "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == doctor.report(BRIDGE_ROWS, {"codex": probes["codex"]}) | {
        "python": json.loads(out)["python"],
        "platform": json.loads(out)["platform"],
    }
    assert out == json.dumps(json.loads(out), indent=2, ensure_ascii=False) + "\n"


def test_cli_json_is_ascii_escaped_and_round_trips_non_ascii(probes, monkeypatch, capsys):
    # ensure_ascii=True: the raw output is pure ASCII, so it survives any console
    # codepage or pipe, and json.loads gives the original character back.
    _enable(monkeypatch, "codex")
    probes["codex"] = [("codex CLI", True, "1.2.3 — fine")]
    doctor.cli(["doctor", "--json"])
    out = capsys.readouterr().out
    assert out.isascii()
    assert "\\u2014" in out and "—" not in out
    assert json.loads(out)["backends"][0]["rows"][0]["detail"] == "1.2.3 — fine"


def test_cli_probe_crash_is_a_failed_backend_not_a_traceback(probes, monkeypatch, capsys):
    _enable(monkeypatch, "codex", "grok")
    probes["codex"] = RuntimeError("boom")
    probes["grok"] = _rows("grok CLI", extra=[("grok auth", True, "ok")])
    assert doctor.cli(["doctor", "--json"]) == 0  # grok is still usable
    data = json.loads(capsys.readouterr().out)
    codex = data["backends"][0]
    assert codex["state"] == doctor.UNUSABLE
    assert "status check crashed" in codex["summary"]
    assert "boom" in codex["summary"]
    assert "report" in codex["next"]


def test_cli_all_probes_crash_exits_1(probes, monkeypatch, capsys):
    _enable(monkeypatch, "codex")
    probes["codex"] = ValueError("x")
    assert doctor.cli(["doctor"]) == 1


def test_cli_bridge_row_crash_does_not_hide_the_backends(probes, monkeypatch, capsys):
    _enable(monkeypatch, "codex")
    probes["codex"] = _rows("codex CLI", extra=[("codex auth", True, "ok")])

    def boom():
        raise OSError("no network")

    monkeypatch.setattr(server, "_bridge_status_rows", boom)
    assert doctor.cli(["doctor"]) == 0
    assert "1 of 1 backend(s) usable." in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["help"], ["--help"], ["-h"]])
def test_cli_help_variants(argv, probes, capsys):
    try:
        code = doctor.cli(argv)
    except SystemExit as e:  # argparse's own -h/--help exits 0
        code = e.code
    assert code == 0
    out = capsys.readouterr().out
    assert "doctor" in out and "usage" in out.lower()


def test_cli_usage_errors_exit_2(probes, capsys):
    for argv in (["doctor", "--bogus"], ["bogus"]):
        with pytest.raises(SystemExit) as exc:
            doctor.cli(argv)
        assert exc.value.code == 2
    assert capsys.readouterr().out == ""  # argparse writes usage errors to stderr


def test_cli_version_flag_through_argparse(capsys):
    with pytest.raises(SystemExit) as exc:
        doctor.cli(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == f"agent-intern {server.__version__}\n"


def test_cli_survives_a_legacy_codepage_stdout(probes, monkeypatch):
    # A Windows pipe in cp1252 cannot encode some characters; doctor reconfigures
    # stdout with errors="replace" instead of crashing on a check's detail.
    import io

    _enable(monkeypatch, "codex")
    probes["codex"] = [("codex CLI", True, "ok → — 中")]
    raw = io.BytesIO()
    fake = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stdout", fake)
    assert doctor.cli(["doctor", "--verbose"]) == 0
    fake.flush()
    assert b"codex CLI" in raw.getvalue()


def test_json_output_is_valid_utf8_even_on_a_legacy_codepage_pipe(probes, monkeypatch):
    # `agent-intern doctor --json > out.json` on Windows writes stdout in the console's
    # codepage (cp1252/cp1254): a check's "—" became byte 0x97, and a strict UTF-8
    # JSON reader failed on the file. The JSON must decode as UTF-8 (escaped ASCII
    # or real UTF-8, either is fine) and round-trip the character.
    import io

    _enable(monkeypatch, "codex")
    probes["codex"] = [("codex CLI", True, "1.2.3 — café")]
    raw = io.BytesIO()
    fake = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stdout", fake)
    assert doctor.cli(["doctor", "--json"]) == 0
    fake.flush()
    data = json.loads(raw.getvalue().decode("utf-8"))
    assert data["backends"][0]["rows"][0]["detail"] == "1.2.3 — café"


def test_doctor_in_a_real_process_rejects_a_bad_flag():
    # Exit code 2 and silence on stdout, with a fresh interpreter. Usage errors
    # happen before any backend is probed, so no CLI is started.
    env = {**os.environ, "AGENT_INTERN_BACKENDS": "codex"}
    r = subprocess.run(
        [sys.executable, str(REPO / "server.py"), "doctor", "--bogus"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO,
        env=env,
    )
    assert r.returncode == 2
    assert r.stdout == ""
    assert "usage" in r.stderr.lower()


# --------------------------------------------------------------------------- #
# the real status rows feed assess() with the labels it expects
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", [n for n in doctor._CHECKS if n != "antigravity"])
def test_check_labels_match_what_each_bridge_writes(name):
    # doctor judges rows by label; a bridge that renames "codex auth" would
    # silently turn "logged out" into "ready". Pin the labels against the source.
    cli_label, auth_label, *_ = doctor._CHECKS[name]
    src = (REPO / f"{name}_bridge.py").read_text(encoding="utf-8")
    assert f'"{cli_label}"' in src or f"'{cli_label}'" in src, cli_label
    assert f'"{auth_label}"' in src or f"'{auth_label}'" in src, auth_label


def test_agy_check_labels_match_server():
    src = (REPO / "server.py").read_text(encoding="utf-8")
    for label in ("agy CLI", doctor._AGY_TRANSCRIPT_ROW):
        assert f'"{label}"' in src, label
    assert doctor._AGY_QUOTA_PREFIX in src


def test_every_backend_has_a_doctor_check():
    assert set(doctor._CHECKS) >= set(server.ENABLED_BACKENDS)
    assert set(doctor._CHECKS) == set(server.BACKENDS)


# --------------------------------------------------------------------------- #
# agy validate_model: base names
# --------------------------------------------------------------------------- #

ARGON = [
    "gemini-4-argon-low",
    "gemini-4-argon-high",
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-low",
]


@pytest.fixture
def agy_models(monkeypatch):
    monkeypatch.setattr(server, "list_agy_models", lambda: list(ARGON))


def test_agy_base_names_ordered_and_deduplicated():
    assert server._agy_base_names(ARGON) == ["gemini-4-argon", "gemini-3.8-flash"]


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "xhigh", "max"])
def test_agy_base_names_every_effort_suffix(effort):
    assert server._agy_base_names([f"m-x-{effort}"]) == ["m-x"]


def test_agy_base_names_ignores_unsuffixed_slugs_and_partial_matches():
    assert server._agy_base_names(["gpt-oss-120b", "model-highest", "flash"]) == []


def test_agy_base_names_does_not_strip_a_suffix_word_inside_the_name():
    # "high" in the middle of a slug is not an effort suffix
    assert server._agy_base_names(["foo-2-high-1", "foo-2-high-1-low"]) == ["foo-2-high-1"]
    assert server._agy_base_names(["foo-2-high-1"]) == []


def test_validate_model_accepts_a_base_name(agy_models):
    assert server.validate_model("gemini-4-argon") == "gemini-4-argon"
    assert server.validate_model("gemini-3.8-flash") == "gemini-3.8-flash"


def test_validate_model_still_accepts_full_slugs(agy_models):
    assert server.validate_model("gemini-4-argon-high") == "gemini-4-argon-high"


def test_validate_model_rejects_a_prefix_of_the_base_name(agy_models):
    with pytest.raises(ValueError, match="unknown agy model 'gemini-4'"):
        server.validate_model("gemini-4")


def test_validate_model_rejects_unknown_with_slugs_and_bases_in_message(agy_models):
    with pytest.raises(ValueError) as exc:
        server.validate_model("nope")
    msg = str(exc.value)
    assert msg.startswith("unknown agy model 'nope'; expected one of: ")
    assert "gemini-4-argon-high" in msg
    assert "(or a base name without the effort: gemini-4-argon, gemini-3.8-flash)" in msg


def test_validate_model_rejects_an_unlisted_suffixed_slug(agy_models):
    with pytest.raises(ValueError):
        server.validate_model("gemini-4-argon-max")


def test_validate_model_base_name_via_the_models_cache(monkeypatch):
    monkeypatch.setattr(server, "_AGY_MODELS_CACHE", list(ARGON))
    assert server.validate_model("gemini-4-argon") == "gemini-4-argon"
    with pytest.raises(ValueError):
        server.validate_model("gemini-4")


# --------------------------------------------------------------------------- #
# home redaction: doctor output is pasted into public issues
# --------------------------------------------------------------------------- #


def _redactor(monkeypatch, home, *, nt):
    """home_redactor as it behaves on Windows (nt=True) or POSIX."""
    fake_os_name(monkeypatch, doctor, "nt" if nt else "posix")
    return doctor.home_redactor(home)


WIN_HOME = "C:\\Users\\ann"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("C:\\Users\\ann\\.codex", "~\\.codex"),
        ("c:/users/ann/.kimi", "~/.kimi"),  # other separator and other case
        ("C:/Users/ann/.kimi", "~/.kimi"),
        ("C:\\Users\\ANN\\bin", "~\\bin"),
        ("C:\\Users\\anna\\x", "C:\\Users\\anna\\x"),  # a longer user name stays
        ("C:\\Users\\ann.smith\\x", "C:\\Users\\ann.smith\\x"),
        ("C:\\Users\\ann-b\\x", "C:\\Users\\ann-b\\x"),
        ("tried 'C:\\Users\\ann\\bin\\kimi.exe'", "tried '~\\bin\\kimi.exe'"),
        ("at C:\\Users\\ann", "at ~"),  # the home itself, end of string
        ("A: C:\\Users\\ann\\a B: C:/Users/ann/b", "A: ~\\a B: ~/b"),  # every occurrence
        ("nothing personal here", "nothing personal here"),
    ],
)
def test_home_redactor_windows(monkeypatch, text, expected):
    assert _redactor(monkeypatch, WIN_HOME, nt=True)(text) == expected


def test_home_redactor_windows_ignores_a_trailing_separator_on_home(monkeypatch):
    assert _redactor(monkeypatch, WIN_HOME + "\\", nt=True)("C:\\Users\\ann\\x") == "~\\x"
    assert _redactor(monkeypatch, "C:/Users/ann/", nt=True)("C:\\Users\\ann\\x") == "~\\x"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/home/ann/.local", "~/.local"),
        ("/home/anna", "/home/anna"),
        ("/home/ann", "~"),
        ("/home/ann/.local and /home/ann/bin", "~/.local and ~/bin"),
        ("/HOME/ann/x", "/HOME/ann/x"),  # POSIX paths are case-sensitive
    ],
)
def test_home_redactor_posix(monkeypatch, text, expected):
    assert _redactor(monkeypatch, "/home/ann", nt=False)(text) == expected


@pytest.mark.parametrize("home", ["/", "", "x", "\\"])
def test_home_redactor_too_short_a_home_redacts_nothing(monkeypatch, home):
    assert _redactor(monkeypatch, home, nt=False)("/usr/bin /x/y") == "/usr/bin /x/y"


def test_home_redactor_defaults_to_the_users_home(monkeypatch):
    monkeypatch.setattr(doctor.os.path, "expanduser", lambda p: "/home/ann")
    redact = _redactor(monkeypatch, None, nt=False)
    assert redact("/home/ann/.codex") == "~/.codex"


def test_home_redactor_matches_literally_not_as_a_pattern(monkeypatch):
    # "." and "+" in a home path are not regex syntax
    assert (
        _redactor(monkeypatch, "/home/a.n+", nt=False)("/home/abnn/x /home/a.n+/x")
        == "/home/abnn/x ~/x"
    )


def test_report_redacts_every_string_in_the_result(monkeypatch):
    home = "/home/ann"
    rows = [("codex CLI", True, "/home/ann/.npm/bin/codex 1.2.3")]
    bridge = [("bridge", True, "installed at /home/ann/.cache/uv")]
    fake_os_name(monkeypatch, doctor, "posix")
    data = doctor.report(bridge, {"codex": rows}, home=home)
    flat = json.dumps(data)
    assert "/home/ann" not in flat
    assert data["bridge"][0]["detail"] == "installed at ~/.cache/uv"
    assert data["backends"][0]["rows"][0]["detail"] == "~/.npm/bin/codex 1.2.3"
    assert data["backends"][0]["summary"] == "~/.npm/bin/codex 1.2.3"


def test_report_redacts_the_home_in_the_summary_and_the_next_step(monkeypatch):
    fake_os_name(monkeypatch, doctor, "posix")
    data = doctor.report(
        [], {"codex": [("codex CLI", False, "not at /home/ann/bin/codex")]}, home="/home/ann"
    )
    b = data["backends"][0]
    assert b["summary"] == "codex CLI: not at ~/bin/codex"
    assert "/home/ann" not in json.dumps(data)


def test_report_keeps_non_string_fields_intact():
    data = doctor.report(BRIDGE_ROWS, {"codex": _rows("codex CLI")}, home="/home/ann")
    assert data["usable"] == 1
    assert data["backends"][0]["usable"] is True
    assert data["backends"][0]["blocked_by"] is None
    assert data["backends"][0]["rows"][0]["ok"] is True


def test_table_verbose_and_json_output_are_all_redacted(probes, monkeypatch, capsys, tmp_path):
    # Runs on the real platform: a fake home (HOME and USERPROFILE, which is what
    # expanduser reads on POSIX and Windows) appears in the patched probe rows.
    home = str(tmp_path / "fake-home")
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    assert os.path.expanduser("~") == home
    codex_dir = home + os.sep + ".codex"
    _enable(monkeypatch, "codex")
    probes["codex"] = [
        ("codex CLI", True, "1.2.3"),
        ("codex auth", True, codex_dir + os.sep + "auth.json"),
        ("sessions dir", False, "missing: " + codex_dir + os.sep + "sessions"),
    ]
    redacted = "~" + os.sep + ".codex"

    for argv in (["doctor"], ["doctor", "--verbose"]):
        doctor.cli(argv)
        out = capsys.readouterr().out
        assert home not in out, argv
        assert redacted + os.sep + "sessions" in out, argv  # the failed row is always shown
    doctor.cli(["doctor", "--verbose"])
    assert redacted + os.sep + "auth.json" in capsys.readouterr().out

    doctor.cli(["doctor", "--json"])
    data = json.loads(capsys.readouterr().out)
    b = data["backends"][0]
    # Compare decoded strings: json.dumps would double the backslashes of a Windows home.
    seen = [b["summary"], b["next"]] + [r["detail"] for r in b["rows"]]
    assert all(home not in s for s in seen)
    assert redacted + os.sep + "auth.json" in seen


# --------------------------------------------------------------------------- #
# identity redaction: e-mail addresses and API keys, on top of the home path
# --------------------------------------------------------------------------- #


def _identity(text, home="/home/ann"):
    return doctor.identity_redactor(home)(text)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Logged in as ann.smith+x@example.co.uk", "Logged in as <email>"),
        ("ann@example.com", "<email>"),
        ("a@b.io and c@d.org", "<email> and <email>"),
        ("(ann_x-1%2@sub.example.com)", "(<email>)"),
        ("Logged in using ChatGPT (ann@example.com)", "Logged in using ChatGPT (<email>)"),
        ("user@localhost", "user@localhost"),  # no dotted domain with a real TLD
        ("1.2.3 @ host", "1.2.3 @ host"),
        ("git@github", "git@github"),
        ("a@b.c", "a@b.c"),  # a one-letter TLD is not one
        ("no address here", "no address here"),
        ("", ""),
    ],
)
def test_identity_redactor_emails(text, expected):
    assert _identity(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("key sk-ant-api03-abcdEFGH1234 ok", "key <key> ok"),
        ("sk-abcdefgh", "<key>"),
        ("token ghp_abcdefgh12345678", "token <key>"),
        ("gho_abcdefgh", "<key>"),
        ("ghu_abcdefgh", "<key>"),
        ("ghs_abcdefgh", "<key>"),
        ("ghr_abcdefgh", "<key>"),
        ("github_pat_11ABCDEFG0abcdefgh_xyz", "<key>"),
        ("xai-abcdefgh1234", "<key>"),
        ("AIzaSyAbcdefgh-12345", "<key>"),
        ("AQ.Ab8RN6abcdefgh", "<key>"),
        ("XAI_API_KEY=xai-abcdefgh1234;", "XAI_API_KEY=<key>;"),
    ],
)
def test_identity_redactor_keys(text, expected):
    assert _identity(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "sk-short",  # fewer than 8 token characters after the prefix
        "task-1234567890",  # "sk-" inside a word is not a key prefix
        "ask-abcdefghijk",
        "ghp_short",
        "github_pat_x",
        "xai-abc",
        "AIza123",
        "AQ.short",
        "risk-averse-model",
    ],
)
def test_identity_redactor_leaves_lookalikes_alone(text):
    assert _identity(text) == text


def test_identity_redactor_still_redacts_the_home_path_first():
    text = "config at /home/ann/.codex/auth.json for ann@example.com key sk-abcdefgh1234"
    assert _identity(text) == "config at ~/.codex/auth.json for <email> key <key>"


def test_identity_redactor_email_inside_the_home_path_is_still_redacted():
    # a home like /home/ann.smith@example.com/x: the home goes first, nothing is left
    assert _identity("/home/ann.smith@example.com/x", home="/home/ann.smith@example.com") == "~/x"


def test_report_redacts_identities_in_every_string(monkeypatch):
    rows = [
        ("codex CLI", True, "1.2.3"),
        ("codex auth", True, "Logged in as ann@example.com"),
        ("sessions dir", False, "key sk-abcdefgh1234 in /home/ann/.codex"),
    ]
    bridge = [("bridge", True, "owner ann@example.com")]
    data = doctor.report(bridge, {"codex": rows}, home="/home/ann")
    flat = json.dumps(data)
    assert "ann@example.com" not in flat and "sk-abcdefgh1234" not in flat
    assert "/home/ann" not in flat
    assert data["bridge"][0]["detail"] == "owner <email>"
    details = [r["detail"] for r in data["backends"][0]["rows"]]
    assert "Logged in as <email>" in details
    assert "key <key> in ~/.codex" in details


def test_identities_are_redacted_in_the_table_verbose_and_json(probes, monkeypatch, capsys):
    _enable(monkeypatch, "codex")
    probes["codex"] = [
        ("codex CLI", True, "1.2.3"),
        ("codex auth", True, "Logged in as ann.smith@example.co.uk"),
        ("sessions dir", False, "token ghp_abcdefgh12345678 rejected"),
    ]
    for argv in (["doctor"], ["doctor", "--verbose"], ["doctor", "--json"]):
        doctor.cli(argv)
        out = capsys.readouterr().out
        assert "ann.smith@example.co.uk" not in out, argv
        assert "ghp_abcdefgh12345678" not in out, argv
        assert "<key>" in out, argv  # the failed row is always shown
    doctor.cli(["doctor", "--verbose"])
    assert "Logged in as <email>" in capsys.readouterr().out
