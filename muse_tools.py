"""MCP tools for Muse Code: muse_ask, muse_continue, muse_status.

The tool layer only; muse_bridge.py drives the CLI. This was a section of
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

import muse_bridge
import server

# What server re-exposes as server.<name> (see server.__getattr__).
__all__ = [
    "muse_ask",
    "muse_continue",
    "muse_status",
    "_run_muse_watched",
]


# ============================================================ Muse tools
# EXPERIMENTAL — see muse_bridge's module docstring. The whole exec pipeline is
# live-verified on Muse Code 1.3.0 through muse's own offline `echo` provider; a
# real Muse Spark answer (and the event shapes of real tool calls) is not.
@server.mcp.tool(
    annotations={
        "title": "Ask Muse Code (new session) [experimental]",
        "readOnlyHint": False,  # muse may edit files / run commands per sandbox
        "idempotentHint": False,
        "openWorldHint": True,  # talks to Meta's model API
    }
)
async def muse_ask(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = muse_bridge.DEFAULT_SANDBOX,
    model: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Ask Meta's Muse Code (`muse exec`) a question or task in a NEW session. EXPERIMENTAL.

    ⚠️ The real model has never answered through this bridge — the author has no
    Muse plan. Muse's built-in offline `echo` provider verified everything else
    end to end (argv, event stream, answer, session resume), so a failure here is
    most likely auth or the model itself. If it misbehaves, say so plainly and
    please report it.

    Needs a Muse Code plan (`muse login`) or a META_API_KEY; run `muse_status`
    first. Returns the agent's final message (the `run_terminal` event of
    `muse exec --json`). Point `workspace` at a project dir for repo context.

    Args:
        prompt: Question or instruction for Muse. Passed in a file, never argv.
        workspace: Working root (`--workspace`). Defaults to the server cwd.
        sandbox: "read-only" (default — muse's write, shell and web tools switched
                 off, so it can only read and answer; holds on every OS),
                 "workspace-write" (shell runs inside muse's OS sandbox, network
                 proxy-only; on Windows that sandbox needs a one-time elevated
                 setup — see muse_status), or "danger-full-access" (`--yolo`: no
                 approval, no sandbox — avoid).
        model: Optional model id (`--model`, e.g. "muse-spark-1.3"). Muse accepts
               any id, so this is not validated. Omit for muse's default.
        timeout_s: Max seconds to wait for muse to complete. Default 180.
        watch: If true, open a live "watch" view of muse's task stream. Same final
               text is returned. Best-effort. Default false.
    """
    ws = muse_bridge.normalize_workspace(workspace)
    muse_bridge.validate_sandbox(sandbox)
    model = muse_bridge.validate_model(model)
    if watch:
        return await asyncio.to_thread(
            _run_muse_watched, prompt, ws, sandbox, model, False, timeout_s
        )
    return await server._run_with_progress(
        muse_bridge.run_muse,
        (prompt, ws, sandbox, model, False, timeout_s),
        ctx,
        timeout_s,
        label="muse",
    )


@server.mcp.tool(
    annotations={
        "title": "Continue Muse Code session [experimental]",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def muse_continue(
    prompt: str,
    workspace: Optional[str] = None,
    sandbox: str = muse_bridge.DEFAULT_SANDBOX,
    timeout_s: int = 180,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the Muse session rooted at this workspace. EXPERIMENTAL.

    Resumes the exact session the last muse_ask in this workspace created (the
    bridge names each session itself with `--session-id`). After a server restart
    it falls back to muse's own record of the workspace's most recent session
    (`muse export --last`); with no session there at all it errors rather than
    silently starting a fresh one. Muse applies safety flags per run, so `sandbox`
    takes effect here too. Same experimental caveat and auth needs as muse_ask.

    Args:
        prompt: Follow-up message for the existing session.
        workspace: Working root used by the prior session. Defaults to the server cwd.
        sandbox: Policy for THIS turn (default "read-only"); same values as muse_ask.
        timeout_s: Max seconds to wait for muse to complete. Default 180.
        watch: If true, open the live "watch" view (same viewer as muse_ask).
    """
    ws = muse_bridge.normalize_workspace(workspace)
    muse_bridge.validate_sandbox(sandbox)
    if watch:
        return await asyncio.to_thread(
            _run_muse_watched, prompt, ws, sandbox, None, True, timeout_s
        )
    return await server._run_with_progress(
        muse_bridge.run_muse,
        (prompt, ws, sandbox, None, True, timeout_s),
        ctx,
        timeout_s,
        label="muse",
    )


@server.mcp.tool(
    annotations={
        "title": "Muse bridge diagnostics",
        "readOnlyHint": True,  # runs `muse --version` / `sandbox windows check` + reads state
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def muse_status() -> str:
    """Report diagnostics for the Muse Code bridge setup (spends no quota).

    Reports the bridge's own version and any newer release, then whether `muse` is
    found (and which binary the bridge runs), whether credentials are present
    (META_API_KEY or a `muse login`), any cached model catalog, the Windows OS
    sandbox state, and where muse keeps its data. Muse has no free auth probe, so a
    green auth row means credentials exist, not that they are still valid. This
    backend is EXPERIMENTAL: green here means the setup looks right, not that a real
    answer has ever been confirmed.
    """
    rows = [server._bridge_version_status()] + muse_bridge.status_rows()
    width = max(len(label) for label, _, _ in rows)
    lines = ["muse bridge status  (EXPERIMENTAL — community-verified only)"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)


def _run_muse_watched(
    prompt: str,
    workspace: str,
    sandbox: str,
    model: Optional[str],
    continue_conv: bool,
    timeout_s: int,
) -> str:
    """Like muse_bridge.run_muse, but stream muse's task events to the watch window.

    Return value is identical to muse_ask. Unknown events render nothing, and tool
    calls show by task kind only (their payloads were never observed).
    """
    start = time.time()
    title = prompt.strip().splitlines()[0] if prompt.strip() else ""
    if len(title) > 200:
        title = title[:200].rsplit(" ", 1)[0] + "…"
    history = muse_bridge.read_history(workspace, continue_conv)
    rid = server._watch_begin(
        title, start, timeout_s, backend="muse", prompt=prompt, history=history
    )
    try:
        port = server._ensure_watch_server()
        server._open_watch_window(server._watch_url(port, rid), rid)
    except Exception:  # noqa: BLE001 — the viewer is best-effort, never fatal
        pass
    to_lines = muse_bridge.watch_mapper()

    def on_event(ev: dict) -> None:
        watch_lines = to_lines(ev)
        if watch_lines:
            t = round(time.time() - start, 1)
            server._watch_append(rid, [{"kind": k, "text": x, "t": t} for k, x in watch_lines])

    try:
        answer = muse_bridge.run_muse_streaming(
            prompt, workspace, sandbox, model, continue_conv, timeout_s, on_event
        )
    except Exception as e:  # noqa: BLE001 — show the failure in the window, then re-raise
        server._watch_finish(rid, "error", f"({e})"[:200], time.time() - start)
        raise
    server._watch_finish(rid, "done", answer, time.time() - start)
    return answer
