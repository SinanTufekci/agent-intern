"""`agent-intern doctor` and `agent-intern --help`: the terminal side of the bridge.

`server.main()` imports this module only when the first argument asks for it, so
the stdio server never loads it and nothing here can write into its JSON-RPC
stream. doctor is for bug reports and for hosts without the Claude Code plugin's
/agent-intern:doctor skill (Antigravity): it runs every enabled backend's status
checks at once, the same checks the *_status tools run, and spends no quota.

Exit codes: 0 when at least one backend is usable, 1 when none is, 2 for a usage
error (argparse's own).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib
import json
import os
import platform
import re
import sys
from typing import Optional

import server

Row = tuple[str, bool, str]

READY = "ready"  # every check passed
WARNING = "warning"  # usable, but a check failed that is worth a look
UNUSABLE = "unusable"  # a call would fail: CLI missing, logged out, out of quota


# label of the row that says the CLI runs, label of the row that says it is
# signed in (None: the backend has none), the step that fixes each. The labels
# are the ones each bridge's status_rows() writes.
_CHECKS: dict[str, tuple[str, Optional[str], str, str]] = {
    "antigravity": (
        "agy CLI",
        None,
        "install Antigravity (https://antigravity.google/) so `agy` is on PATH, or set "
        "AGY_BIN; then sign in once through the IDE or `agy -i`",
        "",
    ),
    "codex": (
        "codex CLI",
        "codex auth",
        "install codex (https://developers.openai.com/codex/) so `codex` is on PATH, "
        "or set CODEX_BIN",
        "run `codex login` once (ChatGPT account or API key)",
    ),
    "copilot": (
        "copilot CLI",
        "copilot auth",
        "install copilot (`npm i -g @github/copilot` or `winget install GitHub.Copilot`), "
        "or set COPILOT_BIN",
        "run `copilot`, then /login once, or set COPILOT_GITHUB_TOKEN / GH_TOKEN",
    ),
    "cursor": (
        "cursor CLI",
        "cursor auth",
        "install cursor-agent (https://cursor.com/cli), or set CURSOR_BIN",
        "run `cursor-agent login` once, or set CURSOR_API_KEY",
    ),
    "claude": (
        "claude CLI",
        "claude auth",
        "install Claude Code (https://claude.com/claude-code), or set CLAUDE_BIN",
        "run `claude auth login`, or set ANTHROPIC_API_KEY",
    ),
    "grok": (
        "grok CLI",
        "grok auth",
        "install grok (https://docs.x.ai/build/overview), or set GROK_BIN",
        "run `grok login` once, or set XAI_API_KEY",
    ),
    "opencode": (
        "opencode CLI",
        "credentials",
        "install opencode (`npm i -g @opencode/cli`; 1.x's `opencode-ai` works too), "
        "or set OPENCODE_BIN",
        "check that `opencode models` lists the free opencode/* models; "
        "`opencode auth login` adds your own provider key",
    ),
    "kimi": (
        "kimi CLI",
        "kimi auth",
        "install kimi (https://github.com/MoonshotAI/kimi-code), or set KIMI_BIN",
        "run `kimi login` once, or put an API key in ~/.kimi-code/config.toml",
    ),
    "muse": (
        "muse CLI",
        "muse auth",
        "install muse (https://dev.meta.ai/docs/muse-code), or set MUSE_BIN",
        "run `muse login` once, or set META_API_KEY",
    ),
}

_AGY_QUOTA_PREFIX = "quota: "
_AGY_TRANSCRIPT_ROW = "newest transcript"
_ISSUES_URL = "https://github.com/SinanTufekci/agent-intern/issues"


def _backend_rows(name: str) -> list[Row]:
    """`name`'s own status rows, without the bridge rows every backend shares.

    Looked up at call time, so tests patch server._agy_status_rows or
    <name>_bridge.status_rows."""
    if name == "antigravity":
        return server._agy_status_rows()
    return importlib.import_module(f"{name}_bridge").status_rows()


def assess(name: str, rows: list[Row]) -> tuple[str, str, str, Optional[str]]:
    """(state, summary, next step, label of the row that blocks it) for one
    backend's status rows.

    Only the CLI row, the sign-in row and, for agy, the quota and transcript rows
    can make a backend unusable. Any other failed row is a warning: a missing
    sessions dir, say, is created by the first call.
    """
    cli_label, auth_label, install, login = _CHECKS[name]
    by_label = {label: (ok, detail) for label, ok, detail in rows}

    cli = by_label.get(cli_label)
    if cli is None or not cli[0]:
        detail = cli[1] if cli else "no CLI check reported"
        return UNUSABLE, f"{cli_label}: {detail}", install, cli_label
    if auth_label is not None and not by_label.get(auth_label, (True, ""))[0]:
        return UNUSABLE, f"{auth_label}: {by_label[auth_label][1]}", login, auth_label
    if name == "antigravity":
        quota = [(label, ok) for label, ok, _ in rows if label.startswith(_AGY_QUOTA_PREFIX)]
        if quota and not any(ok for _, ok in quota):
            return (
                UNUSABLE,
                "every agy quota family is at 0%",
                "wait for the reset `agy` reports, or use another backend meanwhile",
                "quota",  # blocks on every "quota: <family>" row, not one of them
            )
        transcript = by_label.get(_AGY_TRANSCRIPT_ROW)
        if transcript is not None and not transcript[0]:
            return (
                UNUSABLE,
                f"{_AGY_TRANSCRIPT_ROW}: {transcript[1]}",
                "agy may have changed its transcript format: upgrade with "
                f"`uvx agent-intern@latest`, or report it at {_ISSUES_URL}",
                _AGY_TRANSCRIPT_ROW,
            )
    summary = cli[1]
    if all(ok for _, ok, _ in rows):
        return READY, summary, "", None
    return WARNING, summary, "", None


def _probe(name: str) -> list[Row]:
    """Like _backend_rows, but a crash becomes a failed row instead of an exception."""
    try:
        return _backend_rows(name)
    except Exception as e:  # noqa: BLE001 - one broken check must not hide the others
        return [(f"{name} status", False, f"status check crashed: {e!r}"[:200])]


def collect(backends: tuple[str, ...]) -> tuple[list[Row], dict[str, list[Row]]]:
    """(bridge rows, {backend: rows}), every backend's checks run concurrently.

    Each check waits on its own subprocesses (`<cli> --version`, a login probe,
    the GitHub release lookup), so threads overlap them and the wall time is the
    slowest check rather than the sum."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(backends) + 1) as pool:
        bridge = pool.submit(server._bridge_status_rows)
        futures = {name: pool.submit(_probe, name) for name in backends}
        per_backend = {name: f.result() for name, f in futures.items()}
        try:
            bridge_rows = bridge.result()
        except Exception as e:  # noqa: BLE001
            bridge_rows = [("bridge version", True, f"v{server.__version__} ({e!r})")]
    return bridge_rows, per_backend


def home_redactor(home: Optional[str] = None):
    """A function that writes `home` (default: the user's home directory) as "~"
    in a string.

    doctor's output is meant to be pasted into public issues, and a home path names
    the user. Both separator styles are matched, case-insensitively on Windows, and
    only where the path segment ends: C:/Users/ann is redacted inside
    C:/Users/ann/.codex but not inside C:/Users/anna.
    """
    home = (home if home is not None else os.path.expanduser("~")).rstrip("\\/")
    if len(home) < 2:  # "/" or unset: nothing personal to hide, and too broad to match
        return lambda text: text
    variants = {home, home.replace("\\", "/"), home.replace("/", "\\")}
    alternation = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
    flags = re.IGNORECASE if os.name == "nt" else 0
    pattern = re.compile("(?:" + alternation + r")(?![\w.-])", flags)
    return lambda text: pattern.sub("~", text)


# What else in a status row names the account: an auth row can carry the login
# ("Logged in as ann@example.com" from cursor-agent) or, from a CLI that echoes its
# config, a key. Keys are matched by the prefixes the backends' providers use.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_KEY_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}"
    r"|xai-[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{8,}|AQ\.[A-Za-z0-9_-]{8,})"
)


def identity_redactor(home: Optional[str] = None):
    """home_redactor plus e-mail addresses (-> "<email>") and API keys (-> "<key>")."""
    home_sub = home_redactor(home)
    return lambda text: _KEY_RE.sub("<key>", _EMAIL_RE.sub("<email>", home_sub(text)))


def _redact(value, redact):
    """`value` with `redact` applied to every string in it, recursively."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact(v, redact) for v in value]
    if isinstance(value, dict):
        return {k: _redact(v, redact) for k, v in value.items()}
    return value


def report(
    bridge_rows: list[Row], per_backend: dict[str, list[Row]], home: Optional[str] = None
) -> dict:
    """The doctor result as plain data (what --json prints), with the user's home
    directory written as "~", e-mail addresses as "<email>" and API keys as "<key>"
    everywhere (see identity_redactor)."""
    backends = []
    for name, rows in per_backend.items():
        if rows and rows[0][0] == f"{name} status" and not rows[0][1]:
            # _probe's crash row: there is no CLI row to judge.
            state, summary, blocked_by = UNUSABLE, rows[0][2], rows[0][0]
            next_step = f"the status check itself failed; please report it at {_ISSUES_URL}"
        else:
            state, summary, next_step, blocked_by = assess(name, rows)
        backends.append(
            {
                "name": name,
                "state": state,
                "usable": state != UNUSABLE,
                "summary": summary,
                "next": next_step,
                "blocked_by": blocked_by,
                "rows": [{"label": lb, "ok": ok, "detail": d} for lb, ok, d in rows],
            }
        )
    data = {
        "version": server.__version__,
        "python": platform.python_version(),
        "platform": platform.platform(terse=True),
        "bridge": [{"label": lb, "ok": ok, "detail": d} for lb, ok, d in bridge_rows],
        "backends": backends,
        "usable": sum(1 for b in backends if b["usable"]),
    }
    return _redact(data, identity_redactor(home))


def render(data: dict, verbose: bool = False) -> str:
    """The compact table `agent-intern doctor` prints."""
    # ASCII only here: a legacy-codepage pipe on Windows turns "·" into "?".
    lines = [f"agent-intern {data['version']} (Python {data['python']}, {data['platform']})"]
    width = max((len(r["label"]) for r in data["bridge"]), default=0)
    for r in data["bridge"]:
        mark = "" if r["ok"] else "  [!!]"
        lines.append(f"{r['label'].ljust(width)}  {r['detail']}{mark}")
    lines.append("")

    name_w = max([len("backend")] + [len(b["name"]) for b in data["backends"]])
    state_w = max(len(READY), len(WARNING), len(UNUSABLE))
    pad = " " * (name_w + 2 + state_w + 2)
    lines.append(f"{'backend'.ljust(name_w)}  {'state'.ljust(state_w)}  detail")
    for b in data["backends"]:
        lines.append(f"{b['name'].ljust(name_w)}  {b['state'].ljust(state_w)}  {b['summary']}")
        # With the CLI itself missing, every other failed check is its echo.
        cli_missing = b["blocked_by"] in (_CHECKS[b["name"]][0], f"{b['name']} status")
        for r in b["rows"]:
            blocking = r["label"] == b["blocked_by"] or (
                b["blocked_by"] == "quota" and r["label"].startswith(_AGY_QUOTA_PREFIX)
            )
            shown = not r["ok"] and not blocking and not cli_missing
            if verbose or shown:
                mark = "ok" if r["ok"] else "!!"
                lines.append(f"{pad}[{mark}] {r['label']}: {r['detail']}")
        if b["next"]:
            lines.append(f"{pad}next: {b['next']}")

    total = len(data["backends"])
    lines.append("")
    lines.append(f"{data['usable']} of {total} backend(s) usable.")
    if not verbose:
        lines.append("`agent-intern doctor --verbose` shows every check.")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-intern",
        description=(
            "MCP server that runs coding CLIs (agy, codex, copilot, cursor-agent, opencode, "
            "...) as sub-agents. With no arguments it serves MCP on stdio; that is how an "
            "MCP host launches it."
        ),
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"agent-intern {server.__version__}"
    )
    sub = parser.add_subparsers(dest="command", metavar="command")
    doc = sub.add_parser(
        "doctor",
        help="check every enabled backend (installed, signed in, quota) and exit; spends no quota",
        description=(
            "Run the status checks of every backend AGENT_INTERN_BACKENDS enables, the "
            "same ones the *_status tools run, and print what to fix. Spends no quota. "
            "Exits 0 when at least one backend is usable, 1 when none is."
        ),
    )
    doc.add_argument("-v", "--verbose", action="store_true", help="show every check")
    doc.add_argument("--json", action="store_true", help="print the result as JSON")
    sub.add_parser(
        "rules",
        help="print the routing guide to paste into a host's rules file",
        description=(
            "Print the routing guide: the same text this server sends as MCP "
            "instructions, for the backends AGENT_INTERN_BACKENDS enables. Paste it "
            "into a host's rules file for hosts that don't show MCP instructions to "
            "their model (the agy CLI, Copilot without its flag)."
        ),
    )
    sub.add_parser("help", help="show this help and exit")
    return parser


def print_rules() -> None:
    """Write SERVER_INSTRUCTIONS to stdout as UTF-8 bytes, so a redirect on Windows
    (`agent-intern rules > AGENTS.md`) is not written in the legacy codepage."""
    data = (server.SERVER_INSTRUCTIONS + "\n").encode("utf-8")
    raw = getattr(sys.stdout, "buffer", None)
    if raw is None:  # a replaced stdout (tests) takes text
        sys.stdout.write(data.decode("utf-8"))
        return
    sys.stdout.flush()
    raw.write(data)
    raw.flush()


def _utf8_safe_stdout() -> None:
    """Don't crash on a check's "—" when stdout is a legacy-codepage pipe on Windows."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


def cli(argv: list[str]) -> int:
    """`agent-intern <argv>` for the terminal commands; returns the exit code."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "rules":
        print_rules()
        return 0
    if args.command != "doctor":
        parser.print_help()
        return 0
    server._configure_logging()  # a backend's warnings go to stderr, as in the server
    _utf8_safe_stdout()
    data = report(*collect(server.ENABLED_BACKENDS))
    if args.json:
        # ASCII-escaped: on Windows a redirected stdout is in the legacy codepage, so
        # a raw "—" would land as byte 0x97 and the file would not be valid UTF-8 JSON.
        print(json.dumps(data, indent=2, ensure_ascii=True))
    else:
        print(render(data, verbose=args.verbose))
    return 0 if data["usable"] else 1
