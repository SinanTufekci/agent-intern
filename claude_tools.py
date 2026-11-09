"""MCP tools for Claude Code: claude_ask, claude_continue, claude_status.

The tool layer only; claude_bridge.py drives the CLI. Registers its tools on
server.mcp when server imports it, and reaches server's shared helpers as
`server.<name>` at call time, like the other *_tools modules. server leaves these
tools out when its own host is Claude Code (see server._claude_hidden_in_claude_code).
"""

import asyncio
import time
from typing import Optional

from fastmcp import Context

import claude_bridge
import server

# What server re-exposes as server.<name> (see server.__getattr__).
__all__ = [
    "claude_ask",
    "claude_continue",
    "claude_status",
    "_claude_tool_line",
    "_claude_event_to_watch_lines",
    "_run_claude_watched",
]


@server.mcp.tool(
    annotations={
        "title": "Ask Claude Code (new session)",
        "readOnlyHint": False,  # claude may edit files per sandbox
        "idempotentHint": False,
        "openWorldHint": True,  # talks to Anthropic's service
    }
)
async def claude_ask(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = claude_bridge.DEFAULT_SANDBOX,
    model: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Best for large refactors and careful reviews: Claude Code (`claude -p`) as a
    sub-agent, for hosts other than Claude Code, in a NEW session. Pass
    workspace=<project dir>; without it the sub-agent sees no repo. Follow up with
    claude_continue. Uses your Claude plan login or ANTHROPIC_API_KEY. Its sandbox is
    agent-enforced; workspace-write edits files but runs no shell. Autonomous agent:
    trusted prompts only.

    Args:
        prompt: Question or instruction for Claude Code.
        workspace: Working directory for the session. Defaults to the server cwd.
        sandbox: Permission policy, enforced by Claude Code itself (not an OS
                 sandbox): "read-only" (default: reads and searches files, no
                 edits, no shell, no web), "workspace-write" (may edit files inside
                 the workspace, but runs NO shell commands, so it cannot run tests
                 or builds), or "danger-full-access" (every permission check
                 skipped — avoid).
        model: Optional model (`--model`): an alias such as "haiku", "sonnet",
               "opus", or a full model id. Omit for your Claude Code default.
        timeout_s: Max seconds to wait for claude to complete. Default 180.
        watch: If true, open a live "watch" view streaming Claude Code's steps.
               Same final text is returned. Best-effort. Default false.
    """
    ws = claude_bridge.normalize_workspace(workspace)
    claude_bridge.validate_sandbox(sandbox)
    model = claude_bridge.validate_model(model)
    if watch:
        return await asyncio.to_thread(
            _run_claude_watched, prompt, ws, sandbox, model, False, timeout_s
        )
    return await server._run_with_progress(
        claude_bridge.run_claude,
        (prompt, ws, sandbox, model, False, timeout_s),
        ctx,
        timeout_s,
        label="claude",
    )


@server.mcp.tool(
    annotations={
        "title": "Continue Claude Code session",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def claude_continue(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = claude_bridge.DEFAULT_SANDBOX,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the Claude Code session, for hosts other than Claude Code: resumes the
    thread rooted at this workspace, so pass the same workspace=<project dir> as the ask
    (without it the sub-agent sees no repo). After a restart it resumes the newest
    Claude Code session there. `sandbox` applies per turn. Autonomous agent: trusted
    prompts only.

    Args:
        prompt: Follow-up message.
        workspace: Working directory of the prior session. Defaults to the server cwd.
        sandbox: Permission policy for THIS turn (default "read-only"). Same values
                 and caveats as claude_ask.
        timeout_s: Max seconds to wait for claude to complete. Default 180.
        watch: If true, open the live "watch" view. Default false.
    """
    ws = claude_bridge.normalize_workspace(workspace)
    claude_bridge.validate_sandbox(sandbox)
    if watch:
        return await asyncio.to_thread(
            _run_claude_watched, prompt, ws, sandbox, None, True, timeout_s
        )
    return await server._run_with_progress(
        claude_bridge.run_claude,
        (prompt, ws, sandbox, None, True, timeout_s),
        ctx,
        timeout_s,
        label="claude",
    )


@server.mcp.tool(
    annotations={
        "title": "Claude Code bridge diagnostics",
        "readOnlyHint": True,  # only runs `claude --version` / `claude auth status`
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def claude_status() -> str:
    """Check the Claude Code setup, for hosts other than Claude Code. Spends no quota; run
    it first when a call says "not found". Its `bridge version` row says when a newer
    agent-intern release is out: if so, tell the user. Also checks claude's version
    (2.1.259+ needed), how you're signed in (`claude auth status`; account details are
    left out), and where Claude Code keeps its sessions.
    """
    rows = server._bridge_status_rows() + claude_bridge.status_rows()
    width = max(len(label) for label, _, _ in rows)
    lines = ["claude bridge status"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)


def _claude_tool_line(block: dict) -> str:
    """A short line for a stream-json tool_use block: the command, path or pattern
    it works on, else the tool's name."""
    name = str(block.get("name") or "")
    args = block.get("input") or {}
    if isinstance(args, dict):
        for f in ("command", "file_path", "path", "pattern", "query", "url"):
            v = args.get(f)
            if isinstance(v, str) and v.strip():
                return f"{name}: {v.strip().splitlines()[0]}" if name else v.strip()
    return name


def _claude_event_to_watch_lines(ev: dict) -> list[tuple[str, str]]:
    """Map one claude stream-json event to (kind, text) watch lines
    (kind is 'narration' | 'command' | 'result'). [] for events not worth showing."""
    etype = ev.get("type")
    if etype == "assistant":
        lines: list[tuple[str, str]] = []
        for block in (ev.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                lines.append(("narration", str(block["text"]).strip().splitlines()[0][:200]))
            elif block.get("type") == "tool_use":
                line = _claude_tool_line(block)
                if line:
                    lines.append(("command", line[:200]))
        return lines
    if etype == "result":
        return [("result", "error" if ev.get("is_error") else "done")]
    return []


def _run_claude_watched(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    continue_conv: bool,
    timeout_s: int,
) -> str:
    """Like claude_bridge.run_claude, but stream claude's steps to the live watch
    window. The return value is identical to claude_ask."""
    start = time.time()
    title = prompt.strip().splitlines()[0] if prompt.strip() else ""
    if len(title) > 200:
        title = title[:200].rsplit(" ", 1)[0] + "…"
    history = claude_bridge.read_history(workspace, continue_conv)
    rid = server._watch_begin(
        title, start, timeout_s, backend="claude", prompt=prompt, history=history
    )
    try:
        port = server._ensure_watch_server()
        server._open_watch_window(server._watch_url(port, rid), rid)
    except Exception:  # noqa: BLE001 — the viewer is best-effort, never fatal
        pass

    def on_event(ev: dict) -> None:
        watch_lines = _claude_event_to_watch_lines(ev)
        if watch_lines:
            t = round(time.time() - start, 1)
            server._watch_append(rid, [{"kind": k, "text": x, "t": t} for k, x in watch_lines])

    try:
        answer = claude_bridge.run_claude_streaming(
            prompt, workspace, sandbox, model, continue_conv, timeout_s, on_event
        )
    except Exception as e:  # noqa: BLE001 — show the failure in the window, then re-raise
        server._watch_finish(rid, "error", f"({e})"[:200], time.time() - start)
        raise
    server._watch_finish(rid, "done", answer, time.time() - start)
    return answer
