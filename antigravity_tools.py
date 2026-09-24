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
    """Ask Antigravity (agy CLI, Gemini by default) a question in a NEW conversation.

    Uses your existing AI Pro authentication (silent-auth via Windows Credential
    Manager). Returns the model's final response as text. Good for fast
    tool-calling and short tasks; for heavier reasoning pick a bigger `model` or
    use the host model directly.

    Args:
        prompt: Question or instruction for Antigravity.
        workspace: Working directory for the conversation. Defaults to cwd.
                   Choose an existing project dir for context-aware responses.
        model: Optional model slug to run this conversation on (agy's --model),
               e.g. "gemini-3.1-pro-high" or "claude-sonnet-4-6". Omit to use the
               model set in agy's settings.json (gemini-3.8-flash-high as of agy
               1.1.25). Must be one of `agy models` — an unknown slug is
               rejected up front (agy would otherwise silently ignore it and fall
               back to the default). agy 1.1.5 replaced the old human labels
               ("Gemini 3.1 Pro (High)") with these slugs, and the default has
               since moved to the gemini-3.8-flash family; the old form is no
               longer accepted. Note 1.1.25 also DROPPED the gemini-3.5-flash
               family with no changelog entry, so a 3.5 slug you saw in older
               docs is now rejected. See antigravity_status / `agy models` for
               the valid slugs.
        timeout_s: Max seconds to wait for agy to complete. Default 180.
        watch: If true, open a live "watch" view in your browser that streams
               agy's steps (narration + the real commands it runs) as it works.
               agy still runs headless; the same final text is returned. Best-
               effort and cross-platform — if the browser can't open, the run
               completes normally. Default false.
        plan: If true, run agy in PLAN mode (agy 1.1.12+): it investigates and
              writes an implementation plan instead of touching anything. Verified
              on 1.1.20 that a file write and a shell command are both refused and
              diverted into a plan document under agy's own directory — even when
              the prompt insists, and even though the bridge still passes
              --dangerously-skip-permissions — while file READS answer normally.
              Use it to point Antigravity at a repo you don't want it editing.
              Two caveats. It is agent-enforced, not an OS sandbox: it constrains
              agy's agent loop, so treat it as a strong default rather than a
              boundary you'd rely on against a hostile prompt (Codex has the real
              one — see codex_ask's sandbox, and its Windows caveat: as of codex
              0.149.1 a sandboxed run there refuses every command and answers
              anyway). And it is exclusive with the bridge's
              slash-command shield, because agy silently disables plan mode when
              that shield is on; a prompt whose first token is a slash command is
              therefore rejected up front rather than run. Raises on agy older than
              1.1.12, which ignores --mode in print mode, rather than silently
              running your prompt unrestricted. Default false.
        schema: Optional JSON Schema (an object, or its JSON text). When given, agy
                is asked to produce output matching it (agy 1.1.8's --json-schema)
                and this tool returns the VALIDATED OBJECT as JSON text instead of
                prose — json.loads it. What comes back is agy's own
                `structured_output`, which carries exactly the declared fields;
                agy's prose `response` on the same run also picks up its internal
                toolAction/toolSummary keys and can be prefixed with a sentence, so
                the two are NOT interchangeable. If agy produces no structured
                output the call RAISES rather than handing back prose you would
                have to parse anyway. Needs agy 1.1.8+.

                IMPORTANT — write the prompt so the ANSWER is in the turn, and let
                the schema only shape it. agy fills the schema in a finishing pass
                that does not re-reason about the content, so a field the turn never
                established gets guessed from the schema itself. Measured on 1.1.20
                with "this broke my build and wasted my whole afternoon": with
                enum ["positive","negative"] it answered "positive" 3 times out of 4,
                and simply REVERSING the enum to ["negative","positive"] flipped it
                to "negative" 2 out of 2 — it was following field order, not the
                sentence. Adding a `reason` field did not help; the reason came back
                "Completed sentiment classification task." Asking the prompt to state
                the verdict and why, and keeping the same biased enum, was correct
                3 out of 3. So: extraction of what the model has already worked out
                is reliable; a judgment delegated to the schema is not.
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
    """Continue the Antigravity conversation rooted at this workspace.

    Resumes the exact conversation id recorded for `workspace` (via agy's
    --conversation flag), not agy's global "most recent", so it stays correct
    even if agy was used elsewhere in between. On agy 1.1.8+ that id is the one
    agy itself reported for this bridge's last run in the workspace, so a
    follow-up resumes THIS thread even if you have since started a separate
    conversation in the same folder from Antigravity's own interface.

    Args:
        prompt: Follow-up message.
        workspace: Working directory used by the prior conversation. Defaults to cwd.
        model: Optional model slug for this turn (agy's --model), e.g.
               "claude-sonnet-4-6". agy's model is per-invocation, not baked into
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
    """Generate an image with Antigravity (Gemini image model via agy CLI).

    Drives agy to produce a raster image on your existing AI Pro quota, saves it,
    and returns the absolute file path plus its real format and byte size. The
    host can then read the path to view the image.

    agy picks the image format itself (JPEG for photo-like images, PNG for flat
    graphics), so the returned path's extension is corrected to match the actual
    bytes (a requested out.png may come back as out.jpg). Runs a normal,
    unsandboxed agy session — same privileges/caveats as the other tools (see the
    module SECURITY note).

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
    """Generate several images IN PARALLEL with Antigravity (one worker per prompt).

    Like antigravity_image, but runs N image generations concurrently in isolated
    workers (capped at `max_concurrency`). Returns one block listing each image's
    final path/format/size (or its error). Extensions are corrected to the real
    bytes, exactly like antigravity_image. Same unsandboxed privileges/caveats as
    antigravity_swarm.

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
    """Report diagnostics for the agy bridge setup (spends no AI Pro quota).

    Reports the bridge's own version and whether a newer release is available
    (best-effort GitHub check; honors AGY_BRIDGE_NO_UPDATE_CHECK), then checks
    whether agy is on PATH (and its version/compat), how much AI Pro quota is left
    per model family (agy 1.1.11+ answers `/usage` in print mode for free — a
    family at 0% is reported as a problem, since every call against it will fail
    until its window resets), whether agy's state directories exist, whether the
    newest conversation transcript is readable, and whether the SQLite
    conversation store is present. Use this to debug empty or failed responses —
    or to see if the bridge itself is out of date, or if you are simply out of
    quota — before spending quota.
    """
    rows = server._collect_status()
    width = max(len(label) for label, _, _ in rows)
    lines = ["agy bridge status"]
    for label, ok, detail in rows:
        mark = "ok" if ok else "!!"
        lines.append(f"  {label.ljust(width)}  [{mark}] {detail}")
    lines.append("Overall: " + ("OK" if all(ok for _, ok, _ in rows) else "PROBLEMS FOUND"))
    return "\n".join(lines)
