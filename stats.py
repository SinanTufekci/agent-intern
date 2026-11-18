"""Local usage statistics: numbers about delegated calls, never their content.

One JSON line per delegated tool call goes to ``~/.agent-intern/stats.jsonl`` (mode 0600):
when, which tool and backend, how long, whether it worked and, if not, a *category*. No
prompt, answer, path or raw error text is ever stored; raw errors can carry paths and keys.

Recording is one FastMCP middleware (``stats_tools.StatsMiddleware``), so no backend needs
code of its own. The middleware puts a small mutable ``CallNote`` in ``CURRENT_NOTE`` before
the call runs. The shared spawn path (``proc_tree``) and a few bridges *note* what happened
into it (exit code, timeout, missing CLI, cancellation, token counts); the middleware reads
the note after the call. Contextvars follow ``asyncio.to_thread``, and the note is mutable,
so the thread's notes are visible to the middleware. A thread pool does not copy the
context: swarm workers therefore report through ``note_swarm`` with their result counts.

This module is a leaf (standard library only) so ``proc_tree`` can import it. Nothing here
may raise into a tool call: every public ``note_*`` and ``record`` swallows its own errors.

``AGENT_INTERN_NO_STATS=1`` turns recording off (reading an existing file still works).
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

RETENTION_DAYS = 90
MAX_BYTES = 5 * 1024 * 1024
SCHEMA = 1
DISABLE_ENV = "AGENT_INTERN_NO_STATS"

CATEGORIES = (
    "ok",
    "timeout",
    "quota",
    "auth",
    "not_installed",
    "cancelled",
    "nesting",
    "exit_error",
    "error",
    "refused",
    "partial",
    "other",
)

_BACKENDS = (
    "antigravity",
    "codex",
    "copilot",
    "cursor",
    "claude",
    "grok",
    "kimi",
    "opencode",
    "muse",
)
# Tools that are not delegated work: they never count, so health comes from real calls only.
_NOT_RECORDED_SUFFIXES = ("_status",)
_NOT_RECORDED = frozenset({"agent_stats", "swarm_presets"})


def enabled() -> bool:
    """False when the user turned recording off."""
    return os.environ.get(DISABLE_ENV, "").strip().lower() not in ("1", "true", "yes", "on")


def stats_path() -> Path:
    return Path.home() / ".agent-intern" / "stats.jsonl"


# ---------------------------------------------------------------- what happened in a call
class CallNote:
    """What the spawn path saw during one tool call. Thread-safe; fields only ever grow."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.spawns = 0
        self.last_rc: Optional[int] = None
        self.flags: set[str] = set()
        self.tokens: dict[str, int] = {}
        self.workers: Optional[int] = None
        self.workers_ok: Optional[int] = None
        self.run_id: Optional[str] = None


CURRENT_NOTE: contextvars.ContextVar[Optional[CallNote]] = contextvars.ContextVar(
    "agent_intern_stats_note", default=None
)


def note_exit(returncode: Optional[int]) -> None:
    """An agent process finished with `returncode` (call from the shared spawn path)."""
    try:
        n = CURRENT_NOTE.get()
        if n is not None and isinstance(returncode, int):
            with n._lock:
                n.spawns += 1
                n.last_rc = returncode
    except Exception:  # noqa: BLE001 - statistics must never touch a call
        pass


def note_flag(flag: str) -> None:
    """Something that ends a call early: timeout, not_installed, cancelled, nesting."""
    try:
        n = CURRENT_NOTE.get()
        if n is not None and flag in CATEGORIES:
            with n._lock:
                n.flags.add(flag)
    except Exception:  # noqa: BLE001
        pass


def note_tokens(
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
) -> None:
    """Token counts a backend reported for this call. Added up over its turns; ignores
    anything that is not a non-negative int."""
    try:
        n = CURRENT_NOTE.get()
        if n is None:
            return
        with n._lock:
            for key, value in (
                ("i", input_tokens),
                ("o", output_tokens),
                ("c", cache_read_tokens),
            ):
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    n.tokens[key] = n.tokens.get(key, 0) + value
    except Exception:  # noqa: BLE001
        pass


_RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


def note_usage(usage: Any) -> None:
    """Token counts from a backend's own `usage` object (agy's JSON result has
    input_tokens, output_tokens and cache_read_tokens). Anything else is ignored."""
    if isinstance(usage, dict):
        note_tokens(
            usage.get("input_tokens"), usage.get("output_tokens"), usage.get("cache_read_tokens")
        )


def note_swarm(total: int, ok: int, run_dir: Optional[str] = None) -> None:
    """A swarm or preset finished: `ok` of `total` workers answered. `run_dir` is the folder
    the long answers went to, if any; only its name (the run id) is kept."""
    try:
        n = CURRENT_NOTE.get()
        if n is None:
            return
        with n._lock:
            n.workers = int(total)
            n.workers_ok = int(ok)
            if run_dir:
                name = os.path.basename(str(run_dir).rstrip("/\\"))
                if _RUN_ID_RE.match(name):
                    n.run_id = name
    except Exception:  # noqa: BLE001
        pass


def note_run_dir(run_dir: Any) -> None:
    """Where this call's long answers were saved (only the run id is kept)."""
    try:
        n = CURRENT_NOTE.get()
        if n is None or not run_dir:
            return
        name = os.path.basename(str(run_dir).rstrip("/\\"))
        if _RUN_ID_RE.match(name):
            with n._lock:
                n.run_id = name
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- classification
_QUOTA_RE = re.compile(
    r"quota|rate.?limit|usage limit|too many requests|resource.?exhausted|\b429\b|exhausted",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"not (?:logged|signed) in|log ?in required|please (?:log|sign) ?in|unauthori[sz]ed|"
    r"authentication|invalid api key|\b401\b|\bnot authenticated\b",
    re.IGNORECASE,
)


def _text_category(text: str) -> Optional[str]:
    """Best effort only: quota and auth failures are worded by the CLIs, so this reads the
    start of the result text. It is used when the spawn path saw a non-zero exit, or saw
    nothing (a watched run), and never to turn a clean exit into a failure."""
    head = (text or "")[:2000]
    if _QUOTA_RE.search(head):
        return "quota"
    if _AUTH_RE.search(head):
        return "auth"
    return None


def classify(
    note: CallNote, text: str = "", exc: Optional[BaseException] = None
) -> tuple[bool, str, str]:
    """(ok, category, source) for one finished call. `source` is "spawn" when the exit status
    came from the process itself, "swarm" from worker counts, "text" when only the result
    text was available, and "exception" for a raised error."""
    for flag in ("cancelled", "not_installed", "timeout", "nesting"):
        if flag in note.flags:
            return False, flag, "spawn"
    if exc is not None:
        if type(exc).__name__ == "CliNotFound":
            return False, "not_installed", "exception"
        if isinstance(
            exc, ValueError
        ):  # a bad argument (e.g. a missing workspace): not the backend's fault
            return False, "refused", "exception"
        return False, "error", "exception"
    if note.workers is not None:
        total, good = note.workers, note.workers_ok or 0
        if total and good == total:
            return True, "ok", "swarm"
        if good == 0:
            return False, _text_category(text) or "error", "swarm"
        return True, "partial", "swarm"
    if note.spawns:
        if note.last_rc == 0:
            return True, "ok", "spawn"
        return False, _text_category(text) or "exit_error", "spawn"
    # nothing noted (a watched run, or a path without the hooks): read the text
    cat = _text_category(text)
    if cat:
        return False, cat, "text"
    if (text or "").lstrip().lower().startswith(("error", "[agent-intern] error")):
        return False, "other", "text"
    return True, "ok", "text"


# ----------------------------------------------------------------------- tool -> backend
def describe_tool(name: str) -> Optional[tuple[str, str]]:
    """(backend, kind) for a delegating tool, None for everything that is not recorded."""
    if name in _NOT_RECORDED or name.endswith(_NOT_RECORDED_SUFFIXES):
        return None
    if name == "agent_swarm":
        return "swarm", "swarm"
    if name == "preset_swarm":
        return "swarm", "preset"
    if name == "antigravity_image_swarm":
        return "antigravity", "image"
    for backend in _BACKENDS:
        if name.startswith(backend + "_"):
            kind = name[len(backend) + 1 :]
            if kind in ("ask", "continue", "image"):
                return backend, kind
    return None


# A model id: letters, digits and . _ : + - with at most one "provider/model" slash. Anything
# path-like (leading dot or slash, backslash, drive letter, "..", more than one slash) is dropped.
_MODEL_ID = re.compile(r"^[A-Za-z0-9][\w.:+-]{0,47}(?:/[A-Za-z0-9][\w.:+-]{0,47})?$")
_SANDBOXES = frozenset({"read-only", "workspace-write", "danger-full-access"})


def _model(value: Any) -> Optional[str]:
    if not isinstance(value, str) or ".." in value:
        return None
    return value if _MODEL_ID.match(value) else None


def _sandbox(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value in _SANDBOXES else None


def build_record(
    tool: str,
    arguments: Optional[dict],
    note: CallNote,
    elapsed_s: float,
    text: str = "",
    exc: Optional[BaseException] = None,
    now: Optional[float] = None,
) -> Optional[dict]:
    """The line to store for a finished call, or None if this tool is not recorded.
    `arguments` contributes only the model name, the sandbox word and the watch flag, each
    only if it looks like a short identifier; the prompt is never read."""
    described = describe_tool(tool)
    if described is None:
        return None
    backend, kind = described
    ok, cat, src = classify(note, text, exc)
    args = arguments if isinstance(arguments, dict) else {}
    rec: dict[str, Any] = {
        "v": SCHEMA,
        "ts": int(now if now is not None else time.time()),
        "tool": tool,
        "backend": backend,
        "kind": kind,
        "ok": ok,
        "cat": cat,
        "src": src,
        "ms": int(max(0.0, elapsed_s) * 1000),
    }
    model, sandbox = _model(args.get("model")), _sandbox(args.get("sandbox"))
    if model:
        rec["model"] = model
    if sandbox:
        rec["sandbox"] = sandbox
    if args.get("watch") is True:
        rec["watch"] = True
    if note.workers is not None:
        rec["workers"], rec["workers_ok"] = note.workers, note.workers_ok or 0
    if note.run_id:
        rec["run"] = note.run_id
    if note.tokens:
        rec["tok"] = dict(note.tokens)
    return rec


# ---------------------------------------------------------------------------- the file
_WRITE_LOCK = threading.Lock()
_PRUNED = False


def _line_ts(line: str) -> Optional[float]:
    """The `ts` of a stored line, or None for anything that is not a record object with a
    numeric timestamp (bad JSON, a bare list or string, a missing or odd `ts`)."""
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    ts = obj.get("ts") if isinstance(obj, dict) else None
    ok = isinstance(ts, (int, float)) and not isinstance(ts, bool)
    return float(ts) if ok else None


def _prune_locked(path: Path, now: float) -> None:
    """Keep the last RETENTION_DAYS (and at most MAX_BYTES) of lines. Another server process
    appending during the rewrite can lose a line; that is accepted for a statistics file."""
    cutoff = now - RETENTION_DAYS * 86400
    try:
        size = path.stat().st_size
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
        first_ts = _line_ts(first)
    except OSError:
        return
    if size == 0 or (size <= MAX_BYTES and first_ts is not None and first_ts >= cutoff):
        return
    keep: list[str] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            ts = _line_ts(line)
            if ts is not None and ts >= cutoff:
                keep.append(line if line.endswith("\n") else line + "\n")
    total = 0
    trimmed: list[str] = []
    for line in reversed(keep):
        total += len(line.encode("utf-8"))
        if total > MAX_BYTES:
            break
        trimmed.append(line)
    trimmed.reverse()
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.writelines(trimmed)
    os.replace(tmp, path)


def record(rec: Optional[dict], path: Optional[Path] = None, now: Optional[float] = None) -> bool:
    """Append one record. Returns whether it was written; never raises."""
    global _PRUNED
    if rec is None or not enabled():
        return False
    try:
        target = path or stats_path()
        line = json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n"
        with _WRITE_LOCK:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not _PRUNED:
                _PRUNED = True
                _prune_locked(target, now if now is not None else time.time())
            fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        return True
    except Exception:  # noqa: BLE001 - a full disk or a locked file must not touch a call
        return False


def read_records(
    days: int = 7, path: Optional[Path] = None, now: Optional[float] = None
) -> list[dict]:
    """Records from the last `days` days, oldest first. A bad line is skipped."""
    cutoff = (now if now is not None else time.time()) - max(1, days) * 86400
    out: list[dict] = []
    try:
        with open(path or stats_path(), "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if (
                    isinstance(rec, dict)
                    and isinstance(rec.get("ts"), (int, float))
                    and rec["ts"] >= cutoff
                ):
                    out.append(rec)
    except OSError:
        pass
    return out


# ----------------------------------------------------------------------------- summary
def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _week(ts: float) -> str:
    y, w, _ = datetime.fromtimestamp(ts, timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def summarize(records: list[dict]) -> dict:
    """Plain numbers for the table and the page. `delegation` leaves swarms out of the
    per-backend rows (their workers are not separate calls here) but counts them in the
    per-day and per-week rows."""
    by_backend: dict[str, dict] = {}
    per_day: dict[str, dict] = defaultdict(lambda: {"calls": 0, "ok": 0, "err": 0})
    per_week: dict[str, dict] = defaultdict(lambda: {"calls": 0, "ok": 0, "err": 0})
    swarms: list[dict] = []
    for r in records:
        ok = bool(r.get("ok"))
        for bucket in (per_day[_day(r["ts"])], per_week[_week(r["ts"])]):
            bucket["calls"] += 1
            bucket["ok" if ok else "err"] += 1
        b = by_backend.setdefault(
            r.get("backend", "?"),
            {"calls": 0, "ok": 0, "err": 0, "ms": 0, "last_err": None, "tokens": {}, "cats": {}},
        )
        b["calls"] += 1
        b["ok" if ok else "err"] += 1
        b["ms"] += int(r.get("ms", 0))
        if not ok and r.get("cat") == "refused":
            b["refused"] = b.get("refused", 0) + 1
        elif not ok:
            if b["last_err"] is None or r["ts"] >= b["last_err"]["ts"]:
                b["last_err"] = {"ts": r["ts"], "cat": r.get("cat", "other")}
            b["cats"][r.get("cat", "other")] = b["cats"].get(r.get("cat", "other"), 0) + 1
        for k, v in (r.get("tok") or {}).items():
            if isinstance(v, int):
                b["tokens"][k] = b["tokens"].get(k, 0) + v
        if r.get("kind") in ("swarm", "preset"):
            swarms.append(r)
    for b in by_backend.values():
        b["avg_s"] = round(b["ms"] / 1000 / b["calls"], 1) if b["calls"] else 0.0
        real = b["err"] - b.get("refused", 0)  # refused calls are the caller's, not the backend's
        b["error_rate"] = round(100 * real / b["calls"]) if b["calls"] else 0
    return {
        "total": len(records),
        "backends": dict(sorted(by_backend.items())),
        "days": dict(sorted(per_day.items())),
        "weeks": dict(sorted(per_week.items())),
        "swarms": sorted(swarms, key=lambda r: r["ts"], reverse=True)[:10],
    }


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _tok(tokens: dict) -> str:
    if not tokens:
        return "n/a"
    return "/".join(f"{tokens.get(k, 0):,}" for k in ("i", "o", "c"))


def render_table(
    summary: dict,
    days: int,
    quota: Optional[str] = None,
    recording: str = "",
) -> str:
    """The summary as chat-ready markdown. `recording` is a one-line note when recording is
    off or could not be set up; `quota` is agy's remaining-quota text, if it could be read."""
    lines = [
        f"agent-intern stats, last {days} day(s). Numbers only; no prompts or answers are stored."
    ]
    if recording:
        lines.append(f"Note: {recording}")
    if not summary["total"]:
        lines.append("\nNo delegated calls recorded in this period.")
    else:
        lines += [
            "\n**Calls per backend**",
            "| backend | calls | ok | errors | avg s | tokens in/out/cache |",
            "|---|---|---|---|---|---|",
        ]
        for name, b in summary["backends"].items():
            lines.append(
                f"| {name} | {b['calls']} | {b['ok']} | {b['err']} | {b['avg_s']} "
                f"| {_tok(b['tokens'])} |"
            )
        lines += ["\n**Per day (UTC)**", "| day | calls | ok | errors |", "|---|---|---|---|"]
        lines += [
            f"| {d} | {v['calls']} | {v['ok']} | {v['err']} |" for d, v in summary["days"].items()
        ]
        lines += ["\n**Per week**", "| week | calls | ok | errors |", "|---|---|---|---|"]
        lines += [
            f"| {d} | {v['calls']} | {v['ok']} | {v['err']} |" for d, v in summary["weeks"].items()
        ]
        lines += ["\n**Backend health**", "| backend | error rate | last error |", "|---|---|---|"]
        for name, b in summary["backends"].items():
            last = (
                f"{b['last_err']['cat']} ({_when(b['last_err']['ts'])})"
                if b["last_err"]
                else "none"
            )
            lines.append(f"| {name} | {b['error_rate']}% | {last} |")
        if summary["swarms"]:
            lines += [
                "\n**Recent swarms**",
                "| when | kind | tasks | ok | saved answers |",
                "|---|---|---|---|---|",
            ]
            for r in summary["swarms"]:
                run = (
                    f"~/.agent-intern/runs/{r['run']}"
                    if r.get("run")
                    else "none (all fit in the reply)"
                )
                tasks = r.get("workers", "?")
                lines.append(
                    f"| {_when(r['ts'])} | {r.get('kind')} | {tasks} "
                    f"| {r.get('workers_ok', '?')} | {run} |"
                )
    if quota:
        lines += ["\n**Antigravity quota left** (read from agy, spends none)", quota]
    lines.append(
        "\nTokens show only where a backend reports them (n/a otherwise). "
        "Error categories are best effort."
    )
    return "\n".join(lines)
