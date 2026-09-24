"""MCP tools for OpenAI Codex: codex_ask, codex_continue, codex_status.

The tool layer only; codex_bridge.py drives the CLI. This was a section of
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

import codex_bridge
import server

# What server re-exposes as server.<name> (see server.__getattr__).
__all__ = [
    "codex_ask",
    "codex_continue",
    "codex_status",
    "_codex_event_to_watch_lines",
    "_run_codex_watched",
]


@server.mcp.tool(
    annotations={
        "title": "Ask Codex (new session)",
        "readOnlyHint": False,  # codex may edit files when sandbox != read-only
        "idempotentHint": False,
        "openWorldHint": True,  # talks to the external OpenAI/Codex service
    }
)
async def codex_ask(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = codex_bridge.DEFAULT_SANDBOX,
    model: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Ask OpenAI Codex (`codex exec`) a question or task in a NEW session.

    Uses your existing Codex login (ChatGPT or API key — see `codex login status`).
    Returns the agent's final message as text, read from codex's
    --output-last-message file (no stdout scraping). Codex is a capable coding
    agent, so this suits heavier reasoning and real code work, not just cheap
    tool-calling. Point `workspace` at a real project dir for context-aware answers.

    Args:
        prompt: Question or instruction for Codex.
        workspace: Working root for the session (`-C`). Defaults to the server cwd.
        sandbox: Filesystem policy — "read-only" (default: reads and answers but
                 writes nothing), "workspace-write" (may edit files under the
                 workspace), or "danger-full-access" (no sandbox — avoid). `codex
                 exec` has no interactive approval gate, so this is the real safety
                 boundary; opt into write access deliberately.

                 WINDOWS CAVEAT (codex 0.149.1): sandboxed runs there currently
                 refuse EVERY command, both policies, down to `pwd` — codex's
                 policy engine cannot classify the `pwsh -Command <...>` wrapper it
                 builds. Shell commands are how codex reads files, so it sees none
                 of the workspace and ANSWERS ANYWAY, from its own knowledge or a
                 web search, with no hint that it read nothing. The bridge appends
                 a visible "[agent-intern] WARNING" to any answer whose run had
                 commands refused: if you see it, treat the answer as unsourced.
        model: Optional model override (`-m`); omit to use codex's configured default.
        timeout_s: Max seconds to wait for codex to complete. Default 180.
        watch: If true, open a live "watch" view in your browser that streams
               codex's steps (reasoning, the commands it runs, file changes) from
               its `--json` event stream. codex still runs headless; the same final
               text is returned. Best-effort — if the browser can't open, the run
               completes normally. Default false.
    """
    ws = codex_bridge.normalize_workspace(workspace)
    codex_bridge.validate_sandbox(sandbox)  # fail fast with a clear message
    if watch:
        return await asyncio.to_thread(
            _run_codex_watched, prompt, ws, sandbox, model, False, timeout_s
        )
    return await server._run_with_progress(
        codex_bridge.run_codex,
        (prompt, ws, sandbox, model, False, timeout_s),
        ctx,
        timeout_s,
        label="codex",
    )


@server.mcp.tool(
    annotations={
        "title": "Continue Codex session",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def codex_continue(
    prompt: str,
    workspace: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the Codex session rooted at this workspace (`codex exec resume`).

    Resumes the exact session id captured from the last codex_ask in this
    workspace, falling back to the newest on-disk session whose recorded cwd
    matches (so it still works after a server restart). The resumed session keeps
    its original sandbox and model — those are chosen when you start it with
    codex_ask.

    Args:
        prompt: Follow-up message for the existing session.
        workspace: Working root used by the prior session. Defaults to the server cwd.
        timeout_s: Max seconds to wait for codex to complete. Default 180.
        watch: If true, open the live "watch" view streaming codex's steps as it
               works (same viewer as codex_ask). Default false.
    """
    ws = codex_bridge.normalize_workspace(workspace)
    if watch:
        return await asyncio.to_thread(
            _run_codex_watched,
            prompt,
            ws,
            codex_bridge.DEFAULT_SANDBOX,
            None,
            True,
            timeout_s,
        )
    return await server._run_with_progress(
        codex_bridge.run_codex,
        (prompt, ws, codex_bridge.DEFAULT_SANDBOX, None, True, timeout_s),
        ctx,
        timeout_s,
        label="codex",
    )


@server.mcp.tool(
    annotations={
        "title": "Codex bridge diagnostics",
        "readOnlyHint": True,  # only runs `codex --version` / `codex login status`
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def codex_status() -> str:
    """Report diagnostics for the Codex bridge setup (spends no quota).

    Reports the bridge's own version and whether a newer release is available
    (best-effort GitHub check; honors AGY_BRIDGE_NO_UPDATE_CHECK) — the same
    update notice antigravity_status shows, so a Codex-only install still surfaces
    it — then checks whether codex is on PATH (and its version), whether you're
    logged in (`codex login status` — no model call, no quota), where codex stores
    its sessions, and how many workspace sessions are pinned this run. Use this to
    debug "codex not found" or auth errors before spending quota.
    """
    rows = [server._bridge_version_status()] + codex_bridge.status_rows()
    width = max(len(label) for label, _, _ in rows)
    lines = ["codex bridge status"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)


def _codex_event_to_watch_lines(ev: dict) -> list[tuple[str, str]]:
    """Map one codex --json event to (kind, text) watch lines (kind is
    'narration' | 'command' | 'result'), mirroring _entry_to_watch_lines for agy.
    Returns [] for events with nothing worth showing in the viewer.
    """
    etype = ev.get("type")
    if etype == "item.completed":
        item = ev.get("item") or {}
        itype = item.get("type")
        if itype in ("agent_message", "reasoning"):
            txt = (item.get("text") or item.get("summary") or "").strip()
            return [("narration", txt.splitlines()[0][:200])] if txt else []
        if itype == "command_execution":
            cmd = (item.get("command") or "").strip()
            return [("command", cmd[:200])] if cmd else []
        if itype == "file_change":
            changes = item.get("changes") or item.get("files") or []
            n = len(changes) if isinstance(changes, list) else 0
            return [("result", f"file change ({n} file(s))" if n else "file change")]
        if itype == "mcp_tool_call":
            return [("command", f"mcp: {item.get('tool') or item.get('name') or ''}"[:200])]
        if itype == "web_search":
            return [("command", f"search: {item.get('query') or ''}"[:200])]
        return []
    if etype == "turn.started":
        return [("narration", "thinking…")]
    if etype == "error":
        return [("result", f"error: {ev.get('message') or ''}"[:200])]
    return []


def _run_codex_watched(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    continue_conv: bool,
    timeout_s: int,
) -> str:
    """Like codex_bridge.run_codex, but stream codex's steps to the live watch
    window. EXPERIMENTAL. Reuses the same localhost viewer as the agy watch tools;
    the return value is identical to codex_ask.
    """
    start = time.time()
    title = prompt.strip().splitlines()[0] if prompt.strip() else ""
    if len(title) > 200:
        title = title[:200].rsplit(" ", 1)[0] + "…"
    history = codex_bridge.read_history(workspace, continue_conv)
    rid = server._watch_begin(
        title, start, timeout_s, backend="codex", prompt=prompt, history=history
    )
    try:
        port = server._ensure_watch_server()
        server._open_watch_window(server._watch_url(port, rid), rid)
    except Exception:  # noqa: BLE001 — the viewer is best-effort, never fatal
        pass

    def on_event(ev: dict) -> None:
        watch_lines = _codex_event_to_watch_lines(ev)
        if watch_lines:
            t = round(time.time() - start, 1)
            server._watch_append(rid, [{"kind": k, "text": x, "t": t} for k, x in watch_lines])

    try:
        answer = codex_bridge.run_codex_streaming(
            prompt, workspace, sandbox, model, continue_conv, timeout_s, on_event
        )
    except Exception as e:  # noqa: BLE001 — show the failure in the window, then re-raise
        server._watch_finish(rid, "error", f"({e})"[:200], time.time() - start)
        raise
    server._watch_finish(rid, "done", answer, time.time() - start)
    return answer
