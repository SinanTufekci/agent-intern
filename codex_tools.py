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
    """Best for the hardest reasoning and real code work, behind a REAL OS sandbox (OpenAI
    Codex, `codex exec`), in a NEW session. Pass workspace=<project dir>; without it the
    sub-agent sees no repo. Follow up with codex_continue. Uses your Codex login
    (ChatGPT or API key). Read-only by default; sandbox="workspace-write" lets it edit.
    On Windows the bridge flags refused commands and a silent read-only downgrade with
    an [agent-intern] WARNING in the answer: treat such an answer as unsourced.
    Autonomous agent: trusted prompts only.

    Args:
        prompt: Question or instruction for Codex.
        workspace: Working root for the session (`-C`). Defaults to the server cwd.
        sandbox: Filesystem policy, enforced by an OS sandbox: "read-only" (default:
                 reads and answers, writes nothing), "workspace-write" (may edit
                 files in the workspace), or "danger-full-access" (no sandbox, avoid).
                 There is no approval gate, so this is the safety boundary. On Windows,
                 workspace-write runs read-only unless `[windows] sandbox =
                 "unelevated"` is set in ~/.codex/config.toml (see codex_status), and
                 refused commands leave codex answering unsourced; the bridge appends
                 an [agent-intern] WARNING in both cases.
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
    """Continue the Codex session: resumes the thread rooted at this workspace (`codex exec
    resume`), so pass the same workspace=<project dir> as the ask (without it the
    sub-agent sees no repo). It resumes the session the last codex_ask there started,
    else the newest on-disk session for that directory. The session keeps its original
    sandbox and model. Autonomous agent: trusted prompts only.

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
    """Check the Codex setup. Spends no quota; run it first when a call says "not found".
    Its `bridge version` row says when a newer agent-intern release is out: if so, tell
    the user. Also checks codex's version, whether you're logged in (`codex login
    status`, no model call), whether a Windows sandbox is configured (without one,
    workspace-write runs read-only), and where codex keeps its sessions.
    """
    rows = server._bridge_status_rows() + codex_bridge.status_rows()
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
