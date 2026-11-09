"""MCP tools for the cross-backend swarms: agent_swarm, preset_swarm, swarm_presets.

This was part of server.py. It registers its tools on server.mcp when server
imports it, at the end of server's own import, and reaches server's helpers as
`server.<name>` at call time, which is what lets a test that patches
`server.<name>` still reach this code.

Import server, not this module: server resolves every name in __all__ as
`server.<name>`, so callers written against server keep working.
"""

from typing import Optional

import server

# What server re-exposes as server.<name> (see server.__getattr__).
__all__ = [
    "agent_swarm",
    "preset_swarm",
    "swarm_presets",
]


@server.mcp.tool(
    annotations={
        "title": "Agent swarm (mixed Antigravity + Codex + Copilot + Cursor + …, parallel)",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
def agent_swarm(
    tasks: list[dict],
    max_concurrency: int = 4,
    timeout_s: int = 180,
    watch: bool = False,
) -> str:
    """Best for many independent sub-tasks: runs them IN PARALLEL across backends in one
    call. Each task names its backend (Antigravity, Codex, Copilot, Cursor, Claude Code,
    Grok, opencode, Muse), so one swarm can mix model families; every answer comes back
    in one labelled block, and a failed worker is reported in place. Give each task
    workspace=<project dir>; without it that worker sees no repo. Workers are one-shot
    (no continue). N autonomous agents at once is N times the prompt-injection surface:
    trusted prompts only.

    Args:
        tasks: One object per worker. backend (required): "antigravity" ("agy"),
               "codex", "copilot", "cursor", "claude", "opencode", "grok" or
               "muse". prompt (required). workspace: the project dir (default: the
               server cwd, so no repo). sandbox: "read-only" (default),
               "workspace-write" or "danger-full-access", each with that backend's
               own meaning (see its *_ask). ANTIGRAVITY differs: "read-only" is plan
               mode, "workspace-write" is refused, and an omitted sandbox leaves the
               worker UNRESTRICTED, so set one. model: optional, in the backend's
               own format (opencode: "provider/model").
        max_concurrency: Max workers running at once (default 4). Higher = faster
                         but more quota/rate-limit pressure and more agents at once.
        timeout_s: Per-worker timeout in seconds. Default 180. An opencode
                   worker is given at least 300s regardless, as a ceiling: its free
                   models answer in seconds today but have been queue-scheduled to
                   minutes before. The budget is only ever raised, never lowered.
        watch: If true, open the live "Agent Swarm" dashboard window (one card per
               worker, with its backend's logo; click a card for its full step log).
    """
    import swarm

    results = swarm.swarm_agents(tasks, max_concurrency, timeout_s, watch)
    return swarm.format_agent_results(results)


@server.mcp.tool(
    annotations={
        "title": "Preset swarm (jury / research / red team / council)",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
def preset_swarm(
    preset: str,
    material: str,
    workspace: Optional[str] = None,
    timeout_s: Optional[int] = None,
    max_concurrency: int = 4,
    watch: bool = False,
) -> str:
    """Best for getting something JUDGED, RESEARCHED or ATTACKED by several model families:
    runs a predefined panel (jury, research, red-team, council, or the user's own;
    swarm_presets lists them), each member in its own role on the same material, in
    parallel. The same panel every time, so two runs can be compared. Pass
    workspace=<project dir>; without it the sub-agent sees no repo. Autonomous agents:
    trusted prompts only.

    Args:
        preset: The preset's name, e.g. "jury", "research", "red-team", "council",
                or one of the user's own.
        material: The text the panel works on, in full.
        workspace: Directory the members run in, and whose
                   .agent-intern/swarms/ presets are included (default: server cwd).
        timeout_s: Per-member timeout in seconds (default: the preset's own,
                   240-360s for the built-ins). opencode members get at least 300s.
        max_concurrency: Members running at once (default 4).
        watch: If true, open the live "Agent Swarm" dashboard, one card per
               member, captioned with its role.
    """
    import swarm_presets

    return swarm_presets.run(preset, material, workspace, timeout_s, max_concurrency, watch)


@server.mcp.tool(
    annotations={
        "title": "Swarm presets (list, or show one to customise)",
        "readOnlyHint": True,  # reads preset files; runs nothing
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def swarm_presets(name: Optional[str] = None, workspace: Optional[str] = None) -> str:
    """List the predefined swarms preset_swarm can run, or show one in full. Spends no
    quota. Shows each preset's kind, members (role and backend), a jury's rubric, and
    where it comes from. Built-ins can be replaced or extended with JSON files in
    ~/.agent-intern/swarms/ (every project) or <workspace>/.agent-intern/swarms/ (one
    project: its members run read-only and it cannot replace a built-in or user preset).
    Broken files are listed with the reason.

    Args:
        name: Show this preset in full, as the JSON to save and edit. Omit to list.
        workspace: Project directory whose presets to include (default: server cwd).
    """
    import swarm_presets as presets

    return presets.describe(name, workspace)
