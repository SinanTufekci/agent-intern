"""MCP tools for Cursor: cursor_ask, cursor_continue, cursor_status.

The tool layer only; cursor_bridge.py drives the CLI. This was a section of
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

import cursor_bridge
import server

# What server re-exposes as server.<name> (see server.__getattr__).
__all__ = [
    "cursor_ask",
    "cursor_continue",
    "cursor_status",
    "_cursor_tool_line",
    "_cursor_event_to_watch_lines",
    "_run_cursor_watched",
]


# ============================================================ Cursor tools
@server.mcp.tool(
    annotations={
        "title": "Ask Cursor (new chat)",
        "readOnlyHint": False,  # cursor may edit files / run commands per sandbox
        "idempotentHint": False,
        "openWorldHint": True,  # talks to the external Cursor service
    }
)
async def cursor_ask(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = cursor_bridge.DEFAULT_SANDBOX,
    model: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Ask the Cursor CLI (`cursor-agent -p`) a question or task in a NEW chat.

    Uses your existing Cursor login (OS credential store, or a CURSOR_API_KEY env
    var — see `cursor_status`). Returns the agent's final message, read straight
    from stdout (no scraping). Cursor is a capable agentic coder with a wide model
    menu (GPT / Claude / Grok / Composer); point `workspace` at a project dir for
    context-aware answers.

    Args:
        prompt: Question or instruction for Cursor.
        workspace: Working root for the chat (`--workspace`). Defaults to the server cwd.
        sandbox: Permission policy (maps to cursor's mode/force flags):
                 "read-only" (default — `--mode ask`: agent-enforced, no file/shell
                 edits; NOT an OS sandbox, so unlike codex it is not a hard
                 boundary), "workspace-write" (may edit files, rooted at the
                 workspace), or "danger-full-access" (OS sandbox off — avoid).
        model: Optional model override (`--model`, e.g. "auto", "gpt-5.2",
               "claude-opus-4-8-high", "composer-2.5"); validated against
               `cursor-agent models` and rejected on a typo. cursor bakes the effort
               and speed axes into the id (…-low/-high/-xhigh/-max, each with a
               -fast twin) and also accepts a bracket form on the family base, e.g.
               "claude-opus-4-8[context=1m,effort=high]". Omit to use your Cursor
               default.
        timeout_s: Max seconds to wait for cursor to complete. Default 180.
        watch: If true, open a live "watch" view streaming cursor's steps from its
               `--output-format stream-json` event stream. Same final text is
               returned. Best-effort. Default false.
    """
    ws = cursor_bridge.normalize_workspace(workspace)
    cursor_bridge.validate_sandbox(sandbox)  # fail fast with a clear message
    cursor_bridge.validate_model(model)  # fail fast on a typo (cursor models)
    if watch:
        return await asyncio.to_thread(
            _run_cursor_watched, prompt, ws, sandbox, model, False, timeout_s
        )
    return await server._run_with_progress(
        cursor_bridge.run_cursor,
        (prompt, ws, sandbox, model, False, timeout_s),
        ctx,
        timeout_s,
        label="cursor",
    )


@server.mcp.tool(
    annotations={
        "title": "Continue Cursor chat",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def cursor_continue(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = cursor_bridge.DEFAULT_SANDBOX,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the Cursor chat rooted at this workspace (resumes its chat id).

    Resumes the exact chat id the bridge minted on the last cursor_ask in this
    workspace, falling back to the newest on-disk chat whose recorded cwd matches
    (so it still works after a server restart). cursor applies permission flags per
    invocation, so `sandbox` takes effect here too — e.g. analyze read-only with
    cursor_ask, then continue with "workspace-write" to apply the fix.

    Args:
        prompt: Follow-up message for the existing chat.
        workspace: Working root used by the prior chat. Defaults to the server cwd.
        sandbox: Permission policy for THIS turn (default "read-only"). Same values
                 and caveats as cursor_ask.
        timeout_s: Max seconds to wait for cursor to complete. Default 180.
        watch: If true, open the live "watch" view streaming cursor's steps
               (same viewer as cursor_ask). Default false.
    """
    ws = cursor_bridge.normalize_workspace(workspace)
    cursor_bridge.validate_sandbox(sandbox)
    if watch:
        return await asyncio.to_thread(
            _run_cursor_watched, prompt, ws, sandbox, None, True, timeout_s
        )
    return await server._run_with_progress(
        cursor_bridge.run_cursor,
        (prompt, ws, sandbox, None, True, timeout_s),
        ctx,
        timeout_s,
        label="cursor",
    )


@server.mcp.tool(
    annotations={
        "title": "Cursor bridge diagnostics",
        "readOnlyHint": True,  # only runs `cursor-agent --version`/`status` + reads local state
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def cursor_status() -> str:
    """Report diagnostics for the Cursor bridge setup (spends no quota).

    Reports the bridge's own version and whether a newer release is available
    (best-effort GitHub check; honors AGY_BRIDGE_NO_UPDATE_CHECK) — the same
    update notice antigravity_status shows, so a Cursor-only install still surfaces
    it — then checks whether cursor-agent is found (and its version), whether
    you're logged in (`cursor-agent status`), and where cursor stores its chats.
    Use this to debug "cursor not found" or auth errors before spending quota.
    """
    rows = [server._bridge_version_status()] + cursor_bridge.status_rows()
    width = max(len(label) for label, _, _ in rows)
    lines = ["cursor bridge status"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)


def _cursor_tool_line(tc: dict) -> str:
    """A short, human-readable line for a cursor tool_call.

    cursor's `tool_call` payload has one `<name>ToolCall` key (e.g. shellToolCall,
    readToolCall) holding `args` + a `description`. Prefer a command/path-like
    arg, then the description, then the tool's name.
    """
    if not isinstance(tc, dict):
        return ""
    for key, val in tc.items():
        if not key.endswith("ToolCall") or not isinstance(val, dict):
            continue
        args = val.get("args") or {}
        if isinstance(args, dict):
            for f in ("command", "path", "file", "filePath", "query", "pattern", "url"):
                v = args.get(f)
                if isinstance(v, str) and v.strip():
                    return v.strip().splitlines()[0]
        desc = val.get("description")
        if isinstance(desc, str) and desc.strip():
            return desc.strip().splitlines()[0]
        return key[: -len("ToolCall")]  # e.g. "shell", "read"
    return ""


def _cursor_event_to_watch_lines(ev: dict) -> list[tuple[str, str]]:
    """Map one cursor stream-json event to (kind, text) watch lines
    (kind is 'narration' | 'command' | 'result'), mirroring
    _copilot_event_to_watch_lines. Returns [] for events not worth showing.
    """
    etype = ev.get("type")
    if etype == "assistant":
        content = (ev.get("message") or {}).get("content") or []
        txt = "".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
        return [("narration", txt.splitlines()[0][:200])] if txt else []
    if etype == "tool_call":
        sub = ev.get("subtype")
        if sub == "started":
            line = _cursor_tool_line(ev.get("tool_call") or {})
            return [("command", line[:200])] if line else []
        if sub == "completed":
            return [("result", "done")]
        return []
    if etype == "error":
        msg = ev.get("message") or (ev.get("error") or {}).get("message") or ""
        return [("result", f"error: {msg}"[:200])]
    if etype == "result" and ev.get("is_error"):
        return [("result", "error")]
    return []


def _run_cursor_watched(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    continue_conv: bool,
    timeout_s: int,
) -> str:
    """Like cursor_bridge.run_cursor, but stream cursor's steps to the live watch
    window. EXPERIMENTAL. Reuses the same localhost viewer as the agy/codex/copilot
    watch tools; the return value is identical to cursor_ask.
    """
    start = time.time()
    title = prompt.strip().splitlines()[0] if prompt.strip() else ""
    if len(title) > 200:
        title = title[:200].rsplit(" ", 1)[0] + "…"
    history = cursor_bridge.read_history(workspace, continue_conv)
    rid = server._watch_begin(
        title, start, timeout_s, backend="cursor", prompt=prompt, history=history
    )
    try:
        port = server._ensure_watch_server()
        server._open_watch_window(server._watch_url(port, rid), rid)
    except Exception:  # noqa: BLE001 — the viewer is best-effort, never fatal
        pass

    def on_event(ev: dict) -> None:
        watch_lines = _cursor_event_to_watch_lines(ev)
        if watch_lines:
            t = round(time.time() - start, 1)
            server._watch_append(rid, [{"kind": k, "text": x, "t": t} for k, x in watch_lines])

    try:
        answer = cursor_bridge.run_cursor_streaming(
            prompt, workspace, sandbox, model, continue_conv, timeout_s, on_event
        )
    except Exception as e:  # noqa: BLE001 — show the failure in the window, then re-raise
        server._watch_finish(rid, "error", f"({e})"[:200], time.time() - start)
        raise
    server._watch_finish(rid, "done", answer, time.time() - start)
    return answer
