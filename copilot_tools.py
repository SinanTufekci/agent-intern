"""MCP tools for GitHub Copilot: copilot_ask, copilot_continue, copilot_status.

The tool layer only; copilot_bridge.py drives the CLI. This was a section of
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

import copilot_bridge
import server

# What server re-exposes as server.<name> (see server.__getattr__).
__all__ = [
    "copilot_ask",
    "copilot_continue",
    "copilot_status",
    "_copilot_tool_arg",
    "_copilot_event_to_watch_lines",
    "_run_copilot_watched",
]


@server.mcp.tool(
    annotations={
        "title": "Ask GitHub Copilot (new session)",
        "readOnlyHint": False,  # copilot may edit files / run commands per sandbox
        "idempotentHint": False,
        "openWorldHint": True,  # talks to the external GitHub Copilot service
    }
)
async def copilot_ask(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = copilot_bridge.DEFAULT_SANDBOX,
    model: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Ask the GitHub Copilot CLI (`copilot -p`) a question or task in a NEW session.

    Uses your existing Copilot login (OS credential store, or a
    COPILOT_GITHUB_TOKEN/GH_TOKEN/GITHUB_TOKEN env var — see `copilot_status`).
    Returns the agent's final message, read straight from stdout (the CLI's `-s`
    silent mode; no scraping). Copilot is a capable agentic coder — good for real
    code/repo work; point `workspace` at a project dir for context-aware answers.

    Args:
        prompt: Question or instruction for Copilot.
        workspace: Working root for the session (`-C`). Defaults to the server cwd.
        sandbox: Permission policy (maps to copilot's tool/path flags):
                 "read-only" (default — best-effort: denies the local write/shell
                 tools; NOT an OS sandbox, so unlike codex it is not a hard
                 boundary), "workspace-write" (may edit files, confined to the
                 workspace), or "danger-full-access" (--allow-all — avoid).
        model: Optional model override (`--model`). Use "auto" to let Copilot pick.
               Which ids work is ACCOUNT-DEPENDENT and copilot exposes no
               non-interactive list, so the bridge cannot validate this the way the
               agy/cursor tools do — an unavailable id errors immediately with
               copilot's own message, costing a call. Prefer omitting it (your
               account default) or "auto" unless you know your plan's ids.
        timeout_s: Max seconds to wait for copilot to complete. Default 180.
                   (Copilot's reasoning models can be slow; raise this if needed.)
        watch: If true, open a live "watch" view streaming copilot's steps from its
               `--output-format json` event stream. Same final text is returned.
               Best-effort. Default false.
    """
    ws = copilot_bridge.normalize_workspace(workspace)
    copilot_bridge.validate_sandbox(sandbox)  # fail fast with a clear message
    if watch:
        return await asyncio.to_thread(
            _run_copilot_watched, prompt, ws, sandbox, model, False, timeout_s
        )
    return await server._run_with_progress(
        copilot_bridge.run_copilot,
        (prompt, ws, sandbox, model, False, timeout_s),
        ctx,
        timeout_s,
        label="copilot",
    )


@server.mcp.tool(
    annotations={
        "title": "Continue GitHub Copilot session",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def copilot_continue(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = copilot_bridge.DEFAULT_SANDBOX,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the Copilot session rooted at this workspace (resumes its `--session-id`).

    Resumes the exact session id the bridge set on the last copilot_ask in this
    workspace, falling back to the newest on-disk session whose recorded cwd
    matches (so it still works after a server restart). Unlike codex_continue,
    copilot re-applies permission flags on every call, so `sandbox` takes effect
    here too — e.g. analyze read-only with copilot_ask, then continue with
    "workspace-write" to apply the fix.

    Args:
        prompt: Follow-up message for the existing session.
        workspace: Working root used by the prior session. Defaults to the server cwd.
        sandbox: Permission policy for THIS turn (default "read-only"). Same values
                 and caveats as copilot_ask.
        timeout_s: Max seconds to wait for copilot to complete. Default 180.
        watch: If true, open the live "watch" view streaming copilot's steps
               (same viewer as copilot_ask). Default false.
    """
    ws = copilot_bridge.normalize_workspace(workspace)
    copilot_bridge.validate_sandbox(sandbox)
    if watch:
        return await asyncio.to_thread(
            _run_copilot_watched, prompt, ws, sandbox, None, True, timeout_s
        )
    return await server._run_with_progress(
        copilot_bridge.run_copilot,
        (prompt, ws, sandbox, None, True, timeout_s),
        ctx,
        timeout_s,
        label="copilot",
    )


@server.mcp.tool(
    annotations={
        "title": "Copilot bridge diagnostics",
        "readOnlyHint": True,  # only runs `copilot --version` + reads local state
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def copilot_status() -> str:
    """Report diagnostics for the Copilot bridge setup (spends no quota).

    Reports the bridge's own version and whether a newer release is available
    (best-effort GitHub check; honors AGY_BRIDGE_NO_UPDATE_CHECK) — the same
    update notice antigravity_status shows, so a Copilot-only install still
    surfaces it — then checks whether copilot is on PATH (and its version), an auth
    hint (copilot has no `login status` command, so this is best-effort — an env
    token is reported when set, otherwise login via the credential store is
    assumed and unverified), where copilot stores session state, and how many
    workspace sessions are pinned this run. Use this to debug "copilot not found"
    or auth errors before a call.
    """
    rows = [server._bridge_version_status()] + copilot_bridge.status_rows()
    width = max(len(label) for label, _, _ in rows)
    lines = ["copilot bridge status"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)


def _copilot_tool_arg(arguments) -> str:
    """A short, human-readable representative argument for a copilot tool call.

    copilot's tool `arguments` is a dict (e.g. {"path": ...} for view,
    {"command": ...} for shell). Return the first informative field's first line.
    """
    if not isinstance(arguments, dict):
        return ""
    for key in ("command", "path", "file", "filePath", "query", "pattern", "url"):
        v = arguments.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().splitlines()[0]
    return ""


def _copilot_event_to_watch_lines(ev: dict) -> list[tuple[str, str]]:
    """Map one copilot --output-format json event to (kind, text) watch lines
    (kind is 'narration' | 'command' | 'result'), mirroring
    _codex_event_to_watch_lines. Returns [] for events not worth showing.
    """
    etype = ev.get("type")
    data = ev.get("data") or {}
    if etype == "assistant.message":
        txt = (data.get("content") or "").strip()
        return [("narration", txt.splitlines()[0][:200])] if txt else []
    if etype == "assistant.turn_start":
        return [("narration", "thinking…")]
    if etype == "tool.execution_start":
        name = data.get("toolName") or data.get("name") or "tool"
        arg = _copilot_tool_arg(data.get("arguments"))
        return [("command", f"{name} {arg}".strip()[:200])]
    if etype == "tool.execution_complete":
        return [("result", "done" if data.get("success") else "tool failed")]
    if etype == "error":
        msg = data.get("message") or ev.get("message") or ""
        return [("result", f"error: {msg}"[:200])]
    return []


def _run_copilot_watched(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    continue_conv: bool,
    timeout_s: int,
) -> str:
    """Like copilot_bridge.run_copilot, but stream copilot's steps to the live watch
    window. EXPERIMENTAL. Reuses the same localhost viewer as the agy/codex watch
    tools; the return value is identical to copilot_ask.
    """
    start = time.time()
    title = prompt.strip().splitlines()[0] if prompt.strip() else ""
    if len(title) > 200:
        title = title[:200].rsplit(" ", 1)[0] + "…"
    history = copilot_bridge.read_history(workspace, continue_conv)
    rid = server._watch_begin(
        title, start, timeout_s, backend="copilot", prompt=prompt, history=history
    )
    try:
        port = server._ensure_watch_server()
        server._open_watch_window(server._watch_url(port, rid), rid)
    except Exception:  # noqa: BLE001 — the viewer is best-effort, never fatal
        pass

    def on_event(ev: dict) -> None:
        watch_lines = _copilot_event_to_watch_lines(ev)
        if watch_lines:
            t = round(time.time() - start, 1)
            server._watch_append(rid, [{"kind": k, "text": x, "t": t} for k, x in watch_lines])

    try:
        answer = copilot_bridge.run_copilot_streaming(
            prompt, workspace, sandbox, model, continue_conv, timeout_s, on_event
        )
    except Exception as e:  # noqa: BLE001 — show the failure in the window, then re-raise
        server._watch_finish(rid, "error", f"({e})"[:200], time.time() - start)
        raise
    server._watch_finish(rid, "done", answer, time.time() - start)
    return answer
