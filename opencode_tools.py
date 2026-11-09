"""MCP tools for opencode: opencode_ask, opencode_continue, opencode_status.

The tool layer only; opencode_bridge.py drives the CLI. This was a section of
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

import opencode_bridge
import server

# What server re-exposes as server.<name> (see server.__getattr__).
__all__ = [
    "_opencode_tool_line",
    "_opencode_event_to_watch_lines",
    "_run_opencode_watched",
    "opencode_ask",
    "opencode_continue",
    "opencode_status",
]


# ============================================================ opencode tools
# LIVE-VERIFIED, unlike grok/kimi — opencode's free `opencode/*` models answer with
# no credentials at all, so the whole path (answer, resume, permission policy,
# event stream) was exercised end-to-end rather than inferred. See
# opencode_bridge's module docstring.
def _opencode_tool_line(part: dict) -> str:
    """A short, human-readable line for one opencode `tool_use` event part.

    The part is opencode's tool part: `{tool: "<name>", state: {status, input, …}}`,
    whose state schema (read off opencode's bundle) carries `input` on every status,
    a human `title` when completed, and `error` when it failed. Prefer a
    command/path-like input field, then that title, then the bare tool name —
    mirroring _grok_tool_line.
    """
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    tool = part.get("tool") if isinstance(part.get("tool"), str) else ""
    raw = state.get("input")
    if isinstance(raw, dict):
        for f in ("command", "filePath", "file_path", "path", "pattern", "query", "url"):
            v = raw.get(f)
            if isinstance(v, str) and v.strip():
                line = v.strip().splitlines()[0]
                return f"{tool}: {line}" if tool else line
    title = state.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip().splitlines()[0]
    return tool.strip()


def _opencode_event_to_watch_lines(ev: dict) -> list[tuple[str, str]]:
    """Map one opencode `--format json` event to (kind, text) watch lines
    (kind is 'narration' | 'command' | 'result'), mirroring
    _grok_event_to_watch_lines. Returns [] for events not worth showing.
    """
    etype = ev.get("type")
    part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
    if etype in ("text", "reasoning"):
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            return [("narration", text.strip().splitlines()[0][:200])]
        return []
    if etype == "tool_use":
        # opencode emits tool_use only once the call has finished, so a single
        # event carries both what ran and how it went.
        line = _opencode_tool_line(part)
        lines: list[tuple[str, str]] = []
        if line:
            lines.append(("command", line[:200]))
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        if state.get("status") == "error":
            lines.append(("result", f"error: {state.get('error') or ''}"[:200]))
        elif state.get("status") == "completed":
            lines.append(("result", "done"))
        return lines
    if etype == "error":
        err = ev.get("error")
        msg = ""
        if isinstance(err, dict):
            data = err.get("data")
            msg = str(data.get("message")) if isinstance(data, dict) and data.get("message") else ""
            msg = msg or str(err.get("message") or err.get("name") or "")  # 2.x: message
        return [("result", f"error: {msg}"[:200])]
    return []


def _run_opencode_watched(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    continue_conv: bool,
    timeout_s: int,
) -> str:
    """Like opencode_bridge.run_opencode, but stream its steps to the watch window.

    opencode's one `--format json` stream is already incremental, so watch mode
    reuses the very argv the plain path runs — there is no second output format to
    drift (grok has to swap in `streaming-json`). Return value is identical to
    opencode_ask.
    """
    start = time.time()
    title = prompt.strip().splitlines()[0] if prompt.strip() else ""
    if len(title) > 200:
        title = title[:200].rsplit(" ", 1)[0] + "…"
    history = opencode_bridge.read_history(workspace, continue_conv)
    rid = server._watch_begin(
        title, start, timeout_s, backend="opencode", prompt=prompt, history=history
    )
    try:
        port = server._ensure_watch_server()
        server._open_watch_window(server._watch_url(port, rid), rid)
    except Exception:  # noqa: BLE001 — the viewer is best-effort, never fatal
        pass

    def on_event(ev: dict) -> None:
        watch_lines = _opencode_event_to_watch_lines(ev)
        if watch_lines:
            t = round(time.time() - start, 1)
            server._watch_append(rid, [{"kind": k, "text": x, "t": t} for k, x in watch_lines])

    try:
        answer = opencode_bridge.run_opencode_streaming(
            prompt,
            workspace,
            sandbox,
            model,
            continue_conv,
            timeout_s,
            on_event=on_event,
        )
    except Exception as e:  # noqa: BLE001 — show the failure in the window, then re-raise
        server._watch_finish(rid, "error", f"({e})"[:200], time.time() - start)
        raise
    server._watch_finish(rid, "done", answer, time.time() - start)
    return answer


@server.mcp.tool(
    annotations={
        "title": "Ask opencode (new session)",
        "readOnlyHint": False,  # opencode may edit files / run commands per sandbox
        "idempotentHint": False,
        "openWorldHint": True,  # talks to an external model provider
    }
)
async def opencode_ask(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = opencode_bridge.DEFAULT_SANDBOX,
    model: Optional[str] = None,
    timeout_s: int = 300,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Best for users with no subscription: opencode's free `opencode/*-free` models
    answer with zero credentials, in seconds (`opencode run`, NEW session). Also for a
    model family no other backend covers: `opencode auth login` adds Claude/GPT-class
    models. Pass workspace=<project dir>; without it the sub-agent sees no repo. Follow
    up with opencode_continue. Its sandbox is agent-enforced, not an OS boundary;
    timeout_s defaults to a 300 s ceiling. Autonomous agent: trusted prompts only.

    Args:
        prompt: Question or instruction for opencode.
        workspace: Working root (the run directory). Defaults to the server cwd.
        sandbox: Permission policy opencode enforces itself, the same on every OS
                 (agent-enforced, NOT an OS boundary; codex has a real one):
                 "read-only" (default: no edit/shell/subagent/web tools, `.env` reads
                 denied), "workspace-write" (edits and shell), or "danger-full-access"
                 (`--auto`, no policy, avoid). On 1.x workspace-write refuses paths
                 outside the workspace. On 2.x file edits stay inside it, but the
                 shell is not confined: it can write anywhere and read `.env` files.
        model: Optional model id (`-m`, "provider/model" — e.g.
               "opencode/nemotron-3.5-lightning-free"); validated against
               `opencode models` and rejected on a typo. Omit for opencode's
               configured default.
        timeout_s: Max seconds to wait for opencode to complete. Default 300.
        watch: If true, open a live "watch" view streaming opencode's steps from
               the same `--format json` event stream. Same final text is returned.
               Best-effort. Default false.
    """
    ws = opencode_bridge.normalize_workspace(workspace)
    opencode_bridge.validate_sandbox(sandbox)  # fail fast — a bad mode must not run wide open
    model = opencode_bridge.validate_model(model)  # fail fast on a typo (opencode models)
    if watch:
        return await asyncio.to_thread(
            _run_opencode_watched, prompt, ws, sandbox, model, False, timeout_s
        )
    return await server._run_with_progress(
        opencode_bridge.run_opencode,
        (prompt, ws, sandbox, model, False, timeout_s),
        ctx,
        timeout_s,
        label="opencode",
    )


@server.mcp.tool(
    annotations={
        "title": "Continue opencode session",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def opencode_continue(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = opencode_bridge.DEFAULT_SANDBOX,
    timeout_s: int = 300,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the opencode session: resumes the thread rooted at this workspace (`-s
    <id>`, else opencode's own `-c` for that directory), so pass the same
    workspace=<project dir> as the ask (without it the sub-agent sees no repo).
    `sandbox` applies per turn: analyze read-only, then continue with "workspace-write"
    to apply the fix. Autonomous agent: trusted prompts only.

    Args:
        prompt: Follow-up message for the existing session.
        workspace: Working root used by the prior session. Defaults to the server cwd.
        sandbox: Permission policy for THIS turn (default "read-only"). Same values
                 and caveats as opencode_ask.
        timeout_s: Max seconds to wait for opencode to complete. Default 300.
        watch: If true, open the live "watch" view streaming opencode's steps
               (same viewer as opencode_ask). Default false.
    """
    ws = opencode_bridge.normalize_workspace(workspace)
    opencode_bridge.validate_sandbox(sandbox)
    if watch:
        return await asyncio.to_thread(
            _run_opencode_watched, prompt, ws, sandbox, None, True, timeout_s
        )
    return await server._run_with_progress(
        opencode_bridge.run_opencode,
        (prompt, ws, sandbox, None, True, timeout_s),
        ctx,
        timeout_s,
        label="opencode",
    )


@server.mcp.tool(
    annotations={
        "title": "opencode bridge diagnostics",
        "readOnlyHint": True,  # only runs `opencode --version`/`models`/`providers list`
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def opencode_status() -> str:
    """Check the opencode setup. Spends no quota; run it first when a call says "not
    found". Its `bridge version` row says when a newer agent-intern release is out: if
    so, tell the user. Also checks opencode's version (1.x or 2.x), configured
    credentials ("none" is fine: the free models still answer), the model ids available,
    and where opencode keeps its data.
    """
    rows = server._bridge_status_rows() + opencode_bridge.status_rows()
    width = max(len(label) for label, _, _ in rows)
    lines = ["opencode bridge status"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)
