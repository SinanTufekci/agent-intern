"""MCP tool agent_stats and the middleware that records delegated calls (see stats.py).

Like the other tool modules this registers on server.mcp and reaches server's helpers as
`server.<name>` at call time. Nothing here depends on the host: Claude Code and Antigravity
get the same tool, the same table and the same page.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import server
import stats

# What server re-exposes as server.<name> (see server.__getattr__).
__all__ = ["agent_stats"]

try:  # FastMCP middleware needs fastmcp >= 2.9; without it recording stays off, visibly
    from fastmcp.server.middleware import Middleware as _Middleware
except Exception:  # noqa: BLE001
    _Middleware = None

_STATE = {"note": "recording has not been set up in this server"}


def recording_note() -> str:
    """One line saying why nothing is being recorded, or "" when recording is on."""
    return _STATE["note"]


def _result_text(result: Any) -> str:
    """The text of a tool result, for the best-effort quota/auth fallback only."""
    try:
        parts = [c.text for c in getattr(result, "content", []) if hasattr(c, "text")]
        return "\n".join(parts)[:4000]
    except Exception:  # noqa: BLE001
        return ""


if _Middleware is not None:

    class StatsMiddleware(_Middleware):
        """Records one line per delegated tool call. Never changes the call's result or
        error, and never lets a recording problem reach it."""

        async def on_call_tool(self, context, call_next):
            try:
                name = context.message.name
                arguments = context.message.arguments
                tracked = stats.describe_tool(name) is not None
            except Exception:  # noqa: BLE001
                return await call_next(context)
            if not tracked:
                return await call_next(context)
            note = stats.CallNote()
            token = stats.CURRENT_NOTE.set(note)
            start = time.monotonic()
            try:
                result = await call_next(context)
            except BaseException as e:  # incl. CancelledError: record, then re-raise unchanged
                self._record(name, arguments, note, start, "", e)
                raise
            finally:
                stats.CURRENT_NOTE.reset(token)
            self._record(name, arguments, note, start, _result_text(result), None)
            return result

        @staticmethod
        def _record(name, arguments, note, start, text, exc) -> None:
            try:
                if exc is not None and type(exc).__name__ == "CancelledError":
                    note.flags.add("cancelled")
                stats.record(
                    stats.build_record(name, arguments, note, time.monotonic() - start, text, exc)
                )
            except Exception:  # noqa: BLE001
                server.log.debug("stats: could not record %s", name, exc_info=True)

else:
    StatsMiddleware = None


def install(mcp) -> str:
    """Add the recording middleware to `mcp`. Never raises; returns the one-line note that
    agent_stats shows when recording is off ("" when it is on)."""
    if not stats.enabled():
        note = f"recording is off ({stats.DISABLE_ENV} is set); only existing records are shown"
    elif StatsMiddleware is None:
        note = "recording is off: this fastmcp has no middleware support (needs 2.9 or newer)"
    else:
        try:
            mcp.add_middleware(StatsMiddleware())
            note = ""
        except Exception as e:  # noqa: BLE001
            note = f"recording is off: the middleware could not be added ({type(e).__name__})"
    _STATE["note"] = note
    return note


def _limit_name(family: str, limit: str) -> str:
    """A limit's name without its family in front: agy prints a family-specific limit as
    "Gemini 4 Argon Weekly" under the family "Gemini 4 Argon"; shown under the family it
    is just "Weekly"."""
    if limit.lower().startswith(family.lower() + " "):
        return limit[len(family) :].strip() or limit
    return limit


def _agy_quota() -> Optional[list[dict]]:
    """agy's remaining quota, read through the free `/usage` path (antigravity_status uses
    it too): [{"family", "limits": [{"name", "left" (percent or None), "text", "resets"}]}].
    None where it cannot be read or agy is not enabled."""
    try:
        if "antigravity" not in server.ENABLED_BACKENDS or not server.supports_print_usage():
            return None
        out = server._recent_agy_usage()
        if not out:
            return None
        rows = [
            {
                "family": family,
                "limits": [
                    {
                        "name": _limit_name(family, limit),
                        "left": server._percent_remaining(left),
                        "text": left,
                        "resets": resets,
                    }
                    for limit, left, resets in limits
                ],
            }
            for family, limits in server._parse_usage_table(out).items()
        ]
        return rows or None
    except Exception:  # noqa: BLE001 - a stats view must not fail on a quota probe
        return None


def _agy_quota_text() -> Optional[str]:
    """agy's remaining quota as plain lines for the chat table, or None."""
    rows = _agy_quota()
    if not rows:
        return None
    return "\n".join(
        f"- {row['family']}: "
        + ", ".join(
            f"{lim['name']} {lim['text']}" + (f" (resets {lim['resets']})" if lim["resets"] else "")
            for lim in row["limits"]
        )
        for row in rows
    )


def clamp_days(days: Any) -> int:
    try:
        return max(1, min(stats.RETENTION_DAYS, int(days)))
    except (TypeError, ValueError):
        return 7


def stats_payload(days: Any = 7, with_quota: bool = True) -> dict:
    """What /stats.json serves and the table is drawn from. The page asks without the
    quota (switching ranges must not wait on agy) and reads it once from /stats/quota.json."""
    n = clamp_days(days)
    now = time.time()
    records = stats.read_records(n, since=stats.range_start(n, now))
    return {
        "days": n,
        "summary": stats.summarize(records, days=n, now=now),
        "quota": _agy_quota_text() if with_quota else None,
        "recording": recording_note(),
    }


def quota_payload() -> dict:
    """What /stats/quota.json serves: agy's quota as rows for the page's meters."""
    return {"quota": _agy_quota()}


# The stats page has a table and a chart side by side; the run viewer's narrow window
# would squeeze both.
STATS_WINDOW_SIZE = "1100,820"


@server.mcp.tool(
    annotations={
        "title": "agent-intern usage statistics (local)",
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def agent_stats(days: int = 7, watch: bool = False) -> str:
    """Show how the delegation tools have been used, from a local file. Calls per backend
    (ok, errors, average seconds), per day and per week, backend health (error rate and
    the last error's category), recent swarms with where their saved answers are, token
    counts where a backend reports them, and Antigravity's remaining quota. Numbers only:
    no prompt, answer or raw error text is ever stored (turn recording off with
    AGENT_INTERN_NO_STATS=1). Spends no quota.

    Args:
        days: How many days back to summarise, 1 to 90. Default 7.
        watch: Also open a local page with charts (127.0.0.1, same token check as the
               other watch windows). The table is returned either way.
    """
    n = clamp_days(days)
    payload = await asyncio.to_thread(stats_payload, n)
    table = stats.render_table(payload["summary"], n, payload["quota"], payload["recording"])
    if watch:
        try:
            port = server._ensure_watch_server()
            url = f"http://127.0.0.1:{port}/stats?days={n}&k={server._WATCH_TOKEN}"
            await asyncio.to_thread(server._open_watch_window, url, "stats", size=STATS_WINDOW_SIZE)
            table += "\n\n(Opened the stats page in a local window.)"
        except Exception as e:  # noqa: BLE001
            table += f"\n\n(The stats page could not be opened: {type(e).__name__}.)"
    return table
