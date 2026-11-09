"""MCP tools for Kimi Code: kimi_ask, kimi_continue, kimi_status.

The tool layer only; kimi_bridge.py drives the CLI. This was a section of
server.py. It registers its tools on server.mcp when server imports it, at the
end of server's own import, so the tool order clients see is unchanged. It
reaches server's shared helpers as `server.<name>` at call time, which is what
lets a test that patches `server.<name>` still reach this code.

Import server, not this module: server resolves every name in __all__ as
`server.<name>`, so callers written against server keep working.
"""

from typing import Optional

from fastmcp import Context

import kimi_bridge
import server

# What server re-exposes as server.<name> (see server.__getattr__).
__all__ = [
    "kimi_ask",
    "kimi_continue",
    "kimi_status",
]


# ============================================================ Kimi tools
# EXPERIMENTAL — see kimi_bridge's module docstring. Verified only as far as an
# unauthenticated kimi 0.29.1 allows; deliberately no swarm/watch support until a
# live round-trip confirms kimi's stream-json envelope.
@server.mcp.tool(
    annotations={
        "title": "Ask Kimi (new session) [experimental]",
        "readOnlyHint": False,  # kimi print mode auto-executes tools (no sandbox)
        "idempotentHint": False,
        "openWorldHint": True,  # talks to the external Moonshot/Kimi service
    }
)
async def kimi_ask(
    prompt: str,
    workspace: Optional[str] = None,
    model: Optional[str] = None,
    timeout_s: int = 180,
    ctx: Optional[Context] = None,
) -> str:
    """Best for the Kimi K2 family (Moonshot's Kimi Code, `kimi -p`), in a NEW session.
    EXPERIMENTAL: built without a Kimi account, so no authenticated round-trip has run;
    it needs `kimi login` or an API key in ~/.kimi-code/config.toml (check kimi_status).
    Pass workspace=<project dir>; without it the sub-agent sees no repo. Follow up with
    kimi_continue. NO sandbox: it auto-executes every tool call, so trusted prompts
    only.

    Args:
        prompt: Question or instruction for Kimi.
        workspace: Working root (kimi's cwd). Defaults to the server cwd.
        model: Optional model alias (`-m`, from ~/.kimi-code/config.toml); omit to
               use config's default_model. Not validated up front (Kimi has no
               `models` list), so a bad alias surfaces as Kimi's own run-time error.
        timeout_s: Max seconds to wait for kimi to complete. Default 180.
    """
    ws = kimi_bridge.normalize_workspace(workspace)
    model = kimi_bridge.validate_model(model)
    return await server._run_with_progress(
        kimi_bridge.run_kimi,
        (prompt, ws, model, False, timeout_s),
        ctx,
        timeout_s,
        label="kimi",
    )


@server.mcp.tool(
    annotations={
        "title": "Continue Kimi session [experimental]",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def kimi_continue(
    prompt: str,
    workspace: Optional[str] = None,
    timeout_s: int = 180,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the Kimi session (EXPERIMENTAL): resumes the thread rooted at this
    workspace (`kimi -c`; Kimi scopes sessions per directory), so pass the same
    workspace=<project dir> as the ask (without it the sub-agent sees no repo). Errors
    if no kimi_ask ran there. NO sandbox: trusted prompts only.

    Args:
        prompt: Follow-up message for the existing session.
        workspace: Working root used by the prior session. Defaults to the server cwd.
        timeout_s: Max seconds to wait for kimi to complete. Default 180.
    """
    ws = kimi_bridge.normalize_workspace(workspace)
    return await server._run_with_progress(
        kimi_bridge.run_kimi,
        (prompt, ws, None, True, timeout_s),
        ctx,
        timeout_s,
        label="kimi",
    )


@server.mcp.tool(
    annotations={
        "title": "Kimi bridge diagnostics",
        "readOnlyHint": True,  # only runs `kimi --version`/`provider list` + reads local state
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def kimi_status() -> str:
    """Check the Kimi Code setup (EXPERIMENTAL). Spends no quota; run it first when a call
    says "not found". Its `bridge version` row says when a newer agent-intern release is
    out: if so, tell the user. Also checks kimi's version, whether a provider is
    configured (`kimi provider list`; expect "no providers configured" until you log
    in), and where Kimi keeps its data.
    """
    rows = server._bridge_status_rows() + kimi_bridge.status_rows()
    width = max(len(label) for label, _, _ in rows)
    lines = ["kimi bridge status  (EXPERIMENTAL — community-verified only)"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)
