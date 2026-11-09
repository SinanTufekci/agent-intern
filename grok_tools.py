"""MCP tools for Grok Build: grok_ask, grok_continue, grok_status.

The tool layer only; grok_bridge.py drives the CLI. This was a section of
server.py. It registers its tools on server.mcp when server imports it, at the
end of server's own import, so the tool order clients see is unchanged. It
reaches server's shared helpers as `server.<name>` at call time, which is what
lets a test that patches `server.<name>` still reach this code.

Import server, not this module: server resolves every name in __all__ as
`server.<name>`, so callers written against server keep working.
"""

import asyncio
import time
from typing import Optional

from fastmcp import Context

import grok_bridge
import server

# What server re-exposes as server.<name> (see server.__getattr__).
__all__ = [
    "grok_ask",
    "grok_continue",
    "grok_status",
    "_grok_tool_line",
    "_grok_event_to_watch_lines",
    "_run_grok_watched",
]


# ============================================================ Grok tools
# EXPERIMENTAL — see grok_bridge's module docstring. The flag surface below is
# live-verified on grok 1.0.3; the ANSWER path behind grok's auth wall is not.
@server.mcp.tool(
    annotations={
        "title": "Ask Grok Build (new session) [experimental]",
        "readOnlyHint": False,  # grok may edit files / run commands per sandbox
        "idempotentHint": False,
        "openWorldHint": True,  # talks to the external xAI service
    }
)
async def grok_ask(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = grok_bridge.DEFAULT_SANDBOX,
    model: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Best for agentic coding on a SuperGrok / X Premium+ plan (`grok -p`), in a NEW
    session. EXPERIMENTAL: never completed an authenticated round-trip here (flags
    verified on grok 1.0.3), so if it misbehaves say so rather than working around it.
    Run grok_status first. Pass workspace=<project dir>; without it the sub-agent sees
    no repo. Follow up with grok_continue. Its OS sandbox works on Linux/macOS only; on
    Windows read-only rests on a tool allowlist. Autonomous agent: trusted prompts only.

    Args:
        prompt: Question or instruction for Grok.
        workspace: Working root (`--cwd`). Defaults to the server cwd.
        sandbox: Permission policy (maps to grok's `--sandbox` profile plus a tool
                 allowlist): "read-only" (default — the `read-only` profile, no
                 write/shell tools, no subagents), "workspace-write" (the
                 `workspace` profile: writes land in the workspace, ~/.grok and
                 temp), or "danger-full-access" (profile `off` — avoid).
                 ⚠️ grok's OS sandbox is LINUX/macOS ONLY (Landlock/Seatbelt); on
                 Windows it is silently NOT enforced, so read-only there rests on
                 the agent-enforced tool allowlist alone. For a hard boundary on
                 every platform, use codex.
        model: Optional model override (`-m`, e.g. "grok-4.5"); validated against
               `grok models` and rejected on a typo. Omit to use grok's default.
        timeout_s: Max seconds to wait for grok to complete. Default 180.
        watch: If true, open a live "watch" view streaming grok's steps from its
               `--output-format streaming-json` event stream. Same final text is
               returned. Best-effort. Default false.
    """
    ws = grok_bridge.normalize_workspace(workspace)
    grok_bridge.validate_sandbox(sandbox)  # fail fast (grok checks auth first)
    grok_bridge.validate_model(model)  # fail fast on a typo (grok models)
    if watch:
        return await asyncio.to_thread(
            _run_grok_watched, prompt, ws, sandbox, model, False, timeout_s
        )
    return await server._run_with_progress(
        grok_bridge.run_grok,
        (prompt, ws, sandbox, model, False, timeout_s),
        ctx,
        timeout_s,
        label="grok",
    )


@server.mcp.tool(
    annotations={
        "title": "Continue Grok Build session [experimental]",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def grok_continue(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = grok_bridge.DEFAULT_SANDBOX,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the Grok session (EXPERIMENTAL): resumes the thread rooted at this
    workspace (`-r <id>`, else grok's own `-c`), so pass the same workspace=<project
    dir> as the ask (without it the sub-agent sees no repo). `sandbox` applies per turn.
    Same caveats and auth as grok_ask. Autonomous agent: trusted prompts only.

    Args:
        prompt: Follow-up message for the existing session.
        workspace: Working root used by the prior session. Defaults to the server cwd.
        sandbox: Permission policy for THIS turn (default "read-only"). Same values
                 and platform caveats as grok_ask.
        timeout_s: Max seconds to wait for grok to complete. Default 180.
        watch: If true, open the live "watch" view streaming grok's steps
               (same viewer as grok_ask). Default false.
    """
    ws = grok_bridge.normalize_workspace(workspace)
    grok_bridge.validate_sandbox(sandbox)
    if watch:
        return await asyncio.to_thread(
            _run_grok_watched, prompt, ws, sandbox, None, True, timeout_s
        )
    return await server._run_with_progress(
        grok_bridge.run_grok,
        (prompt, ws, sandbox, None, True, timeout_s),
        ctx,
        timeout_s,
        label="grok",
    )


@server.mcp.tool(
    annotations={
        "title": "Grok bridge diagnostics",
        "readOnlyHint": True,  # only runs `grok --version`/`models` + reads local state
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def grok_status() -> str:
    """Check the Grok Build setup (EXPERIMENTAL). Spends no quota; run it first when a call
    says "not found". Its `bridge version` row says when a newer agent-intern release is
    out: if so, tell the user. Also checks grok's version, auth and model list (both
    from `grok models`, which answers even when logged out), and where grok keeps its
    data. Green means the setup looks right, not that a live answer was ever confirmed.
    """
    rows = server._bridge_status_rows() + grok_bridge.status_rows()
    width = max(len(label) for label, _, _ in rows)
    lines = ["grok bridge status  (EXPERIMENTAL — community-verified only)"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)


def _grok_tool_line(ev: dict) -> str:
    """A short, human-readable line for a grok `tool_call` event.

    grok's ACP-derived tool_call carries `toolName` plus a `rawInput` object and a
    human `title`. Prefer a command/path-like input field, then the title, then the
    tool name.
    """
    raw = ev.get("rawInput")
    if isinstance(raw, dict):
        for f in ("command", "path", "file_path", "pattern", "query", "url", "target_file"):
            v = raw.get(f)
            if isinstance(v, str) and v.strip():
                return v.strip().splitlines()[0]
    for f in ("title", "toolName"):
        v = ev.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip().splitlines()[0]
    return ""


def _grok_event_to_watch_lines(ev: dict) -> list[tuple[str, str]]:
    """Map one grok streaming-json event to (kind, text) watch lines
    (kind is 'narration' | 'command' | 'result'), mirroring
    _cursor_event_to_watch_lines. Returns [] for events not worth showing.
    """
    etype = ev.get("type")
    if etype in ("text", "thought"):
        data = ev.get("data")
        if isinstance(data, str) and data.strip():
            return [("narration", data.strip().splitlines()[0][:200])]
        return []
    if etype == "tool_call":
        line = _grok_tool_line(ev)
        return [("command", line[:200])] if line else []
    if etype == "tool_call_update":
        if ev.get("status") == "completed":
            return [("result", "done")]
        if ev.get("status") == "failed":
            return [("result", "failed")]
        return []
    if etype == "error":
        return [("result", f"error: {ev.get('message') or ''}"[:200])]
    return []


def _run_grok_watched(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    continue_conv: bool,
    timeout_s: int,
) -> str:
    """Like grok_bridge.run_grok, but stream grok's steps to the live watch window.

    EXPERIMENTAL twice over: the watch viewer is best-effort, and grok's
    streaming-json event shapes are docs-derived (never observed on a real answer).
    Unknown events simply render nothing. Return value is identical to grok_ask.
    """
    start = time.time()
    title = prompt.strip().splitlines()[0] if prompt.strip() else ""
    if len(title) > 200:
        title = title[:200].rsplit(" ", 1)[0] + "…"
    history = grok_bridge.read_history(workspace, continue_conv)
    rid = server._watch_begin(
        title, start, timeout_s, backend="grok", prompt=prompt, history=history
    )
    try:
        port = server._ensure_watch_server()
        server._open_watch_window(server._watch_url(port, rid), rid)
    except Exception:  # noqa: BLE001 — the viewer is best-effort, never fatal
        pass

    def on_event(ev: dict) -> None:
        watch_lines = _grok_event_to_watch_lines(ev)
        if watch_lines:
            t = round(time.time() - start, 1)
            server._watch_append(rid, [{"kind": k, "text": x, "t": t} for k, x in watch_lines])

    try:
        answer = grok_bridge.run_grok_streaming(
            prompt, workspace, sandbox, model, continue_conv, timeout_s, on_event
        )
    except Exception as e:  # noqa: BLE001 — show the failure in the window, then re-raise
        server._watch_finish(rid, "error", f"({e})"[:200], time.time() - start)
        raise
    server._watch_finish(rid, "done", answer, time.time() - start)
    return answer
