"""MCP tools for Antigravity (agy): antigravity_ask, antigravity_continue,
antigravity_image, antigravity_image_swarm and antigravity_status.

This was part of server.py. It registers its tools on server.mcp when server
imports it, at the end of server's own import, and reaches server's helpers as
`server.<name>` at call time, which is what lets a test that patches
`server.<name>` still reach this code.

Import server, not this module: server resolves every name in __all__ as
`server.<name>`, so callers written against server keep working.
"""

import asyncio
import os
import time
from typing import Optional, Union

from fastmcp import Context

import server

# What server re-exposes as server.<name> (see server.__getattr__).
__all__ = [
    "antigravity_ask",
    "antigravity_continue",
    "antigravity_image",
    "_broadcast_workspaces",
    "antigravity_image_swarm",
    "antigravity_status",
]


@server.mcp.tool(
    annotations={
        "title": "Ask Antigravity (new conversation)",
        "readOnlyHint": False,  # agy runs unsandboxed: may write files / run commands
        "idempotentHint": False,
        "openWorldHint": True,  # talks to the external Antigravity service
    }
)
async def antigravity_ask(
    prompt: str,
    workspace: Optional[str] = None,
    model: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    plan: bool = False,
    schema: Optional[Union[dict, str]] = None,
    ctx: Optional[Context] = None,
) -> str:
    """Best for fast, cheap tool-calling and short tasks on Gemini (agy CLI), in a NEW
    conversation. Pass workspace=<project dir>; without it the sub-agent sees no repo.
    Follow up with antigravity_continue. Uses your AI Pro login; pick a bigger `model`
    for heavier reasoning. No real sandbox (plan=True is agent-enforced read-only).
    Autonomous agent: trusted prompts only.

    Args:
        prompt: Question or instruction for Antigravity.
        workspace: Working directory for the conversation. Defaults to cwd.
                   Choose an existing project dir for context-aware responses.
        model: Optional model slug (agy's --model), e.g. "gemini-4-argon-high" or
               "gemini-3.8-flash-low", or one without its effort suffix
               ("gemini-4-argon"). Omit for agy's settings.json default (Gemini 4
               Argon (Medium) as of agy 1.5.1). Checked against `agy models`: an
               unknown slug is rejected up front, since agy renames and drops models
               between releases. Quota is per model family; a quota failure names a
               family that still has some.
        timeout_s: Max seconds to wait for agy to complete. Default 180.
        watch: If true, open a live "watch" view in your browser that streams
               agy's steps (narration + the real commands it runs) as it works.
               agy still runs headless; the same final text is returned. Best-
               effort and cross-platform — if the browser can't open, the run
               completes normally. Default false.
        plan: If true, run agy in PLAN mode (agy 1.1.12+): it reads and answers, and
              diverts file writes and shell commands into a plan document instead
              (verified even when the prompt insists). Use it to point Antigravity
              at a repo you don't want edited. Agent-enforced, not an OS sandbox
              (codex has the real one). A prompt whose first token is a slash
              command is rejected in plan mode, and an agy older than 1.1.12 raises
              rather than running unrestricted. Default false.
        schema: Optional JSON Schema (an object, or its JSON text). Returns agy's
                VALIDATED object (`structured_output`) as JSON text instead of prose;
                json.loads it. Raises if agy produces none. Needs agy 1.1.8+.
                IMPORTANT: write the prompt so the ANSWER is worked out in the turn,
                and let the schema only shape it. agy fills the schema in a final
                pass that does not re-reason, so a judgment left to the schema gets
                guessed (measured: flipping an enum's order flipped the verdict).
                Extraction is reliable; delegated judgment is not.
    """
    ws = server._normalize_workspace(workspace)
    server.validate_model(model)  # fail fast on a typo (agy would silently ignore it)
    if plan:
        server._check_plan_mode(prompt)  # refuses rather than downgrading; see _agy_base_args
    schema_text = None
    if schema is not None:
        server._check_schema_support()
        schema_text = server._normalize_json_schema(schema)
    if watch:
        return await asyncio.to_thread(
            server._run_agy_watched, prompt, ws, False, timeout_s, model, plan, schema_text
        )
    return await server._run_with_progress(
        server._run_agy, (prompt, ws, False, timeout_s, model, plan, schema_text), ctx, timeout_s
    )


@server.mcp.tool(
    annotations={
        "title": "Continue Antigravity conversation",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def antigravity_continue(
    prompt: str,
    workspace: Optional[str] = None,
    model: Optional[str] = None,
    timeout_s: int = 180,
    watch: bool = False,
    plan: bool = False,
    schema: Optional[Union[dict, str]] = None,
    ctx: Optional[Context] = None,
) -> str:
    """Continue the Antigravity conversation: resumes the thread rooted at this workspace,
    so pass the same workspace=<project dir> as the ask (without it the sub-agent sees
    no repo). It resumes the exact conversation id the bridge recorded there, not agy's
    global most recent, so a conversation started elsewhere in between cannot take its
    place. `model`, `plan` and `schema` apply per turn. Autonomous agent: trusted
    prompts only.

    Args:
        prompt: Follow-up message.
        workspace: Working directory used by the prior conversation. Defaults to cwd.
        model: Optional model slug for this turn (agy's --model), e.g.
               "gemini-4-argon-high". agy's model is per-invocation, not baked into
               the conversation, so a follow-up can run on a different model than
               the original ask — omit to use agy's settings.json default.
               Validated against `agy models`; an unknown slug is rejected (agy
               would silently ignore it).
        timeout_s: Max seconds to wait for agy to complete. Default 180.
        watch: If true, open a live "watch" view in your browser that streams
               agy's steps as it works (same return value, best-effort). Default false.
        plan: If true, run this turn in agy's PLAN mode (1.1.12+) — it investigates
              and writes an implementation plan instead of editing files or running
              commands, while reads still work. Per-invocation like `model`, so a
              follow-up can plan even if the original ask was unrestricted. See
              antigravity_ask's `plan` for what it does and does not guarantee.
              Default false.
        schema: Optional JSON Schema for this turn — returns the validated object as
                JSON text instead of prose. Per-invocation like `model` and `plan`.
                See antigravity_ask's `schema`. Needs agy 1.1.8+.
    """
    ws = server._normalize_workspace(workspace)
    server.validate_model(model)  # fail fast on a typo (agy would silently ignore it)
    if plan:
        server._check_plan_mode(prompt)  # refuses rather than downgrading; see _agy_base_args
    schema_text = None
    if schema is not None:
        server._check_schema_support()
        schema_text = server._normalize_json_schema(schema)
    if watch:
        return await asyncio.to_thread(
            server._run_agy_watched, prompt, ws, True, timeout_s, model, plan, schema_text
        )
    return await server._run_with_progress(
        server._run_agy, (prompt, ws, True, timeout_s, model, plan, schema_text), ctx, timeout_s
    )


@server.mcp.tool(
    annotations={
        "title": "Generate an image with Antigravity",
        "readOnlyHint": False,  # writes the generated image file to disk
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def antigravity_image(
    prompt: str,
    output_path: Optional[str] = None,
    workspace: Optional[str] = None,
    timeout_s: int = 240,
    watch: bool = False,
    ctx: Optional[Context] = None,
) -> str:
    """Your only image generator: makes an image with Gemini (agy CLI) on the user's AI Pro
    quota, saves it, and returns its absolute path, real format and byte size (read the
    path to view it). agy picks the format, so the extension is corrected to the bytes
    (out.png may come back as out.jpg). Relative paths land in `workspace`. For several
    images, use antigravity_image_swarm. Runs an unsandboxed agy session: trusted
    prompts only.

    Args:
        prompt: Description of the image to generate.
        output_path: Where to save. Absolute, or relative to `workspace`. If
                     omitted, a timestamped name under `workspace` is used.
        workspace: Working directory for the conversation. Defaults to cwd.
        timeout_s: Max seconds to wait for agy to complete. Default 240
                   (image generation is slower than text).
        watch: If true, open the live "watch" window that streams agy's steps and
               shows the finished image inline (same return value, best-effort).
               Default false.
    """
    ws = server._normalize_workspace(workspace)
    target = server._resolve_output_path(output_path, ws)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    wrapped = server._wrap_image_prompt(prompt, target)
    if watch:
        return await asyncio.to_thread(
            server._run_agy_image_watched, wrapped, target, ws, timeout_s, prompt
        )

    start = time.time()
    agy_text: Optional[str] = None
    agy_error: Optional[Exception] = None
    try:
        agy_text = await server._run_with_progress(
            server._run_agy, (wrapped, ws, False, timeout_s), ctx, timeout_s
        )
    except RuntimeError as e:
        # The transcript read may fail even though agy wrote the image. Don't
        # lose a successfully generated file to a transcript hiccup — try to
        # locate it anyway, and only surface this error if nothing was produced.
        agy_error = e

    try:
        final_path, fmt, size = server._finalize_image(target, agy_text, start)
    except RuntimeError as fin_err:
        if agy_error is not None:
            raise RuntimeError(f"{fin_err} (agy also failed: {agy_error})") from agy_error
        raise
    return f"{final_path}\nformat={fmt}  size={size} bytes"


def _broadcast_workspaces(workspaces: Optional[list], n: int):
    """Map the MCP `workspaces` arg to swarm's None|str|list contract.

    None -> server cwd for all; a 1-item list -> that dir for all N; an N-item
    list -> one workspace per prompt. (MCP can't pass a bare str for a list field,
    so a 1-item list is the "same dir for everyone" shorthand.)
    """
    if not workspaces:
        return None
    if len(workspaces) == 1:
        return workspaces[0]
    return workspaces


@server.mcp.tool(
    annotations={
        "title": "Generate several images in parallel",
        "readOnlyHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
def antigravity_image_swarm(
    prompts: list[str],
    output_paths: Optional[list[str]] = None,
    workspaces: Optional[list[str]] = None,
    max_concurrency: int = 4,
    timeout_s: int = 240,
    watch: bool = False,
) -> str:
    """Best for several images at once: like antigravity_image, but one isolated worker per
    prompt, run in parallel (capped at `max_concurrency`). Returns each image's path,
    format and size, or its error; extensions are corrected to the real bytes.
    Unsandboxed agy workers: trusted prompts only.

    Args:
        prompts: One image description per parallel worker.
        output_paths: Where to save each image (aligned to prompts). Omit to write
                      timestamped files in the first workspace (or server cwd).
        workspaces: Working directory per worker (same shorthand as antigravity_swarm).
        max_concurrency: Max workers running at once (default 4).
        timeout_s: Per-worker timeout in seconds. Default 240 (images are slower).
        watch: If true, open the live dashboard; each finished image shows in its
               pane, and clicking a row opens that agent's window beside the dashboard.
    """
    import swarm

    n = len(prompts)
    if output_paths is None:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        base = workspaces[0] if workspaces else os.getcwd()
        output_paths = [os.path.join(base, f"agy-swarm-image-{stamp}-{i}.png") for i in range(n)]
    results = swarm.swarm_image(
        prompts,
        output_paths,
        workspaces=_broadcast_workspaces(workspaces, n),
        max_concurrency=max_concurrency,
        timeout_s=timeout_s,
        watch=watch,
    )
    return swarm.format_image_results(results)


@server.mcp.tool(
    annotations={
        "title": "agy bridge diagnostics",
        "readOnlyHint": True,  # only reads local state + runs `agy --version`
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def antigravity_status() -> str:
    """Check the agy (Antigravity) setup. Spends no quota; run it first when a call says
    "not found". Its `bridge version` row says when a newer agent-intern release is out:
    if so, tell the user. Also checks agy's version, the AI Pro quota left per model
    family (a family at 0% fails every call until it resets), agy's state directories,
    and whether the bridge can still read agy's answers. Use it to debug empty or failed
    answers before spending quota.
    """
    rows = server._collect_status()
    width = max(len(label) for label, _, _ in rows)
    lines = ["agy bridge status"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)
