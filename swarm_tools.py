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
    """Run SEVERAL tasks IN PARALLEL across ALL backends in a single swarm.

    Each task is its own worker and names the backend to run on, so one swarm can
    mix Antigravity (Gemini), Codex, Copilot, Cursor, Grok, opencode and Muse workers —
    they run truly concurrently (capped at `max_concurrency`) and every answer comes
    back in one labelled block. A worker that fails is reported in place; the others
    still return.

    SECURITY: this launches N unsandboxed agents at once — N times the
    prompt-injection surface of a single call (see the module SECURITY note). Only
    use it with trusted prompts on trusted content.

    Args:
        tasks: One object per parallel worker:
               - backend: "antigravity" (alias "agy"/"gemini"), "codex",
                          "copilot" (alias "gh"/"github"), "cursor", "opencode"
                          (alias "oc" — the one backend that needs no
                          subscription; see opencode_ask), "grok" (alias
                          "xai"; EXPERIMENTAL — see grok_ask), or "muse" (alias
                          "meta"; EXPERIMENTAL — see muse_ask) (required)
               - prompt:  the question or instruction (required)
               - workspace: working dir for that worker (default: server cwd)
               - sandbox: "read-only" (default), "workspace-write", or
                          "danger-full-access". Codex's is an enforced OS sandbox
                          everywhere; Grok's is enforced on Linux/macOS only;
                          Copilot's, Cursor's and opencode's are agent/tool-level,
                          not OS boundaries; Muse's read-only switches its write,
                          shell and web tools off — see copilot_ask / cursor_ask /
                          grok_ask / opencode_ask / muse_ask.
                          ANTIGRAVITY is the odd one: "read-only" maps to agy's
                          plan mode (it investigates and writes a plan instead of
                          editing files or running commands — see antigravity_ask's
                          `plan`, and note it is agent-enforced, and needs agy
                          1.1.12+), "danger-full-access" states plainly that the
                          worker is unrestricted, and "workspace-write" is REFUSED
                          because agy has no write scoping to offer. Omitting it
                          leaves an Antigravity worker unrestricted — that is the
                          long-standing default, unlike every other backend here,
                          so fence it explicitly if you want it fenced.
               - model:   optional model override for ANY backend — Codex's `-m`,
                          Copilot's/Cursor's/Muse's `--model`, Grok's/opencode's `-m`
                          (opencode wants "provider/model"), or Antigravity's
                          `--model` (an agy slug like "claude-sonnet-4-6";
                          validated against each backend's model list). Omit for
                          each backend's default.
        max_concurrency: Max workers running at once (default 4). Higher = faster
                         but more quota/rate-limit pressure and more agents at once.
        timeout_s: Per-worker timeout in seconds. Default 180. An opencode
                   worker is given at least 300s regardless — its free models are
                   queue-scheduled and were measured at 152-428s, so the shared
                   default would kill about half of them mid-answer and report a
                   slow worker as a broken one. The budget is only ever raised,
                   never lowered.
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
    """Run a PREDEFINED swarm: a named panel of agents from different model
    families, each in its own role, all working on the same material in parallel.
    One call instead of building agent_swarm tasks by hand, and the same panel
    every time, so two runs (two applications, two drafts) can be compared.

    Built-in presets:
      - jury: independent jurors score the material against a rubric. You get a
        score table (each criterion's mean and spread, a weighted total per
        juror, disagreements flagged) plus each juror's reasons. For judging an
        application, proposal, pitch or submission.
      - research: one topic researched from four angles at once (landscape, prior
        work and competitors, data and evidence, the case against), with sources.
      - red-team: attackers try to break a plan, proposal or design from the
        technical, assumption and execution sides; findings worst first.
      - council: independent code review of a change. Put the diff and a
        paragraph of context in `material`.
    The user can add their own; call swarm_presets to list every preset with its
    members, or to get one as a JSON file to customise.

    Put EVERYTHING the panel needs in `material`: the full application text, the
    plan, the diff plus context. Members get it inline and may not be able to read
    files (Codex's read-only sandbox refuses every command on Windows). Every
    member runs read-only unless the preset says otherwise; on Antigravity that is
    plan mode. A member that fails is reported in place; the rest still count.

    The result ends with guidance addressed to you on how to combine the answers.
    Follow it: check the members' claims against the material instead of pasting
    their answers back.

    SECURITY: as for agent_swarm, this runs several autonomous agents at once, so
    use it on trusted content. Each member is told to treat instructions inside the
    material as data, but for an agent that is a request, not a guarantee.

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
    """List the predefined swarms preset_swarm can run, or show one in full.

    Lists each preset's kind, members (role and backend), rubric for a jury, and
    where it comes from. Built-ins can be replaced or extended with JSON files in
    ~/.agent-intern/swarms/ (every project) or <workspace>/.agent-intern/swarms/
    (one project; its members run read-only and it cannot replace a built-in or
    user preset, because it arrives with whatever repo was cloned). Broken files
    are listed with the reason they were skipped.

    Args:
        name: Show this preset in full, as the JSON to save and edit. Omit to list.
        workspace: Project directory whose presets to include (default: server cwd).
    """
    import swarm_presets as presets

    return presets.describe(name, workspace)
